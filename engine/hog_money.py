#!/usr/bin/env python3
"""生猪(LH)机构资金引擎:合计流向跟随。

**与金银引擎(smart_money.py)刻意分开的两个文件**,不是重复造轮子——两套信号
形态根本不同,研究阶段用数据判过(research/REPORT_LH_PHASE1_v1.md):

  - 金银:逐家席位算权重、多席位共振。生猪照搬这套会失败——单家席位的加减仓
    事件整体胜率只有 50%(2026 年 44.7%),只有东证一家 t≈2.8 显著。
  - 生猪:**八家合计的流向**,控制动量后偏相关 t=5.4~7.5,与金银核心因子同量级。
  - 生猪还有两条金银没有的坑:各合约相对主力偏离最大 49%(金银不到 1%),
    以及 84.6% 的交易日里同日不同合约的持仓变化方向相反(移仓换月)。

所以口径上有两条铁律,改动时不要想当然:

  1. **信号用品种合计**。拆到合约层面会被移仓撕成相反的两半(实测 IC_t 仅 0.58,
     品种合计 t=5.22)。
  2. **收益一律逐合约算,换月日用新合约自己的前一日结算价**。跨合约相除得到的
     不是收益,是价差。

席位组**滚动重选**(每年按截至当时的历史 alpha 取前 5),不硬编码名单:
生猪只有三年样本、且只有一种市况,焊死名单等于把这一段行情的偏好写死。
金银敢硬编码七家是有 17 年样本兜底。

**只做空**——做多支路默认关闭,理由见 RULES["long_enabled"]。

回测证据(2023-08~2026-08,一年选人 + 只做空 + **次日开盘成交**,DEC-090):
  恒定满仓做空基准 +99.2%/夏普 1.65/回撤 −14.8%
  本引擎            +87.4%/夏普 2.34/回撤  −4.2%,18 笔胜率 66.7%
  (2026-08-21 修掉回榜前视后重算;生猪反推行少,几乎不受影响 —— 玻璃纯碱掉得很惨,
   见各自的 backtest 与 research/REPORT_PIT_LOOKAHEAD_v1.md)

**必须正视的一件事:绝对收益没跑赢基准**(+87.4% vs +99.2%)。这三年是单边熊市,
躺着满仓做空本身就有 +99% 复利。策略赢的是夏普(2.25 vs 1.65)与回撤(−5.0% vs
−14.8%),以及**趋势反转时会退出而不是硬扛**——而后者在样本内无法验证(没有牛市)。
界面上必须摆出这个对比(payload 的 `compare`),不然看的人会把熊市 beta 当成本事。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CN_TZ = timezone(timedelta(hours=8))

# 当前正在跑的品种(由 run_one 设置)。build_payload 要用它填品种名与单位。
CURRENT: dict = {}

# 各品种算好的信号表,供 FG-SA 配对信号复用(别再重算一遍)。
SIG_CACHE: dict = {}

COST = 0.0005            # 单边手续费+滑点。逐日净值在成交那两天各扣一次。
SPLIT: dict = {}         # 本轮品种的「跳空占比」,build_payload 算好给 _caveats 用

RULES = {
    # —— 席位组 ——
    "group_k": 5,            # Phase 1:3/5/8 里 5 在三个训练截点上都最好
    # 2026-08-19 运营者拍板由 3 改 12:「3 个月太短,会有很多噪音」。数据支持这个
    # 判断的一半——换组次数 9→2、胜率 58.3%→63.9%、最大回撤 −10.2%→−8.6%;
    # **代价要说清**:离散版累计从 +104.5% 掉到 +86.5%,低于「恒定满仓做空」的 +99.2%。
    # 另外 3/6/12 月三档是 94.2/67.7/77.1,**不单调**,所以这三个数之间的差异
    # 多半是噪音,不能反过来论证 3 个月更好。
    "reselect_months": 12,
    "warmup_days": 250,      # 首次选组前的最少历史
    "member_min_days": 120,  # 一家至少在榜这么多天才参与排名
    # —— 信号 ——
    "sig_win": 5,            # 合计净持仓的变化窗口。20 日窗会混入动量(相关 +0.317)
    "z_win": 120,            # 无量纲化的滚动窗。2026 年机构净空是 2024 年的四倍,
                             # 绝对手数不能直接当阈值
    # —— 进出场 ——
    "enter": 1.0,            # 0.8/1.0/1.2 平稳(94/94/110%),取中不取峰值
    # exit_z=0 意味着「消退出场」要求 z 恰好为 0,**实测三年 36 笔里一次都没触发过**
    # (出场全部是 反向 32 / 持满 2 / 止损 2)。留着是当安全网,不要误以为它在起作用;
    # 真想让信号衰减就出场,得把它调成 0.3 这类值,而那是个**新参数,必须先回测**。
    "exit_z": 0.0,
    "stop": 0.06,            # 4/6/8/10% 相邻档同向,不敏感
    "max_hold": 40,          # 20/30/40/60 相邻档同向
    # **散户交割纪律**(2026-08-19 运营者要求):主力合约进入「窗口止点前 10 个
    # 交易日」就强制平仓,并且**这段时间也不许进场**——不然平了立刻又开,天天空转。
    # 窗口止点 = 交割月前月最后一个非周末日,与套利监控 `days_to_window_end` 同口径。
    # 运营者的原话与算例:「我是散户,玻璃 2609 合约 8.31 之前需要离场,
    # 要提前 10 个交易日,8.18 之前要离场」——FG2609 止点 2026-08-31,
    # 倒数第 10 个交易日(含当日)正是 08-18。
    # 这不是调出来的参数,是纪律,别拿回测去优化它。
    "exit_before_delivery": 10,
    # **做多支路默认关闭**(2026-08-19 运营者拍板)。三条依据:
    #   ① 一年选人口径下多头 15 笔逐笔累计 −1.5%、均值 −0.02%(抛硬币),
    #      还贡献了全表最差的 −7.4%;关掉后夏普 1.96 → 2.39。
    #   ② 运营者本来想跟的是「机构真转多」。样本里它出现过 14 天(集中在
    #      2025-07,最高 +4,046 手),但之后 20 日主力仍平均跌 1.18%,
    #      14 次里最好一次只有 +0.61%——转多意味着跌得慢,不意味着会涨。
    #   ③ 关掉不等于牛市被埋:机构减空时策略平仓观望,不是继续扛空单。
    # **页面仍然显示机构转多状态**(见 payload 的 institution),只是不进场。
    # 那 14 天全挤在一个窗口里,实质是 1 个事件,只能说「没有证据支持」,
    # 不能说「证明必亏」——生猪真走出熊市、样本攒够了再议。
    # long_enabled / multiplier / replay_start 由 use() 按品种注入,见 VARIETIES。
    # 做多开启时才用得上:Phase 0 双分档里「跌 × 机构减空」是全表唯一
    # 正格子(+0.30%),「涨 × 减空」是 −1.97%。
    "long_needs_dip": True,
    "dip_win": 20,
    # 与 Rust MEMBER_ALIASES / smart_money RULES["alias"] 保持同集。
    # 不归一会把一家算成两家。「大华期货」不许加(与格林期货同日同合约并存 266 次,
    # 是 2013 年被吸收合并的另一家公司)。
    "alias": {"浙江永安": "永安期货", "乾坤期货": "高盛期货",
              "上海东证": "东证期货", "国投安信": "国投期货",
              "国投安信期货": "国投期货", "申银万国": "申万期货",
              "格林大华期货": "格林大华", "格林期货": "格林大华"},
    # —— 散户反向维度(2026-08-19 加,DEC-085)——
    # 这三家是运营者定的:在多个品种上长期站多头、长期亏钱的席位。
    # 判据是**散户天然站多头**——一致净空的是套保席位(为交割锁价、不在乎盈亏),
    # 运营者据此点名剔除了格林大华(生猪上净空 2,499 手)。
    # 名单**跨品种固定、不逐品种重选**:这正是它相对「找聪明钱」的优势所在——
    # 没有挑人的过拟合,新品种可直接套用。加人反而变差(实测四家不如三家)。
    "retail_seed": ["东方财富", "平安期货", "徽商期货"],
    # 共振 = 聪明钱流向与散户反向流向同号。
    #
    # **现行策略(方案 C)就是用它进出场** —— 见下面 `signal_source = "resonance"`。
    # 这里原先写的是「不参与进出场」,那是 2026-08-19 拍板切方案 C **之前**的状态,
    # 切完之后忘了改,于是同一段注释里前面说「不参与」、13 行后说「共振进场」。
    # **前端照抄了前半句**(`HogPayload.retail` 的类型注释),「进场条件」那一行
    # 就长期显示机构的数而不是真正被比的散户数 —— 2026-08-20 玻璃机构 2.09、
    # 散户 0.92,页面写着「需达 1(现 2.09)」却又显示无持仓,运营者当场发现;
    # 同一天纯碱反过来漏报(页面 0.52、实际 1.19 已达标)。详见 DEC-104。
    #
    # 当年那句「不参与」的**理由**仍然成立、也仍然重要,原样保留在这里:
    # 三个方案**表现相当**(不是「新信号不够好」)——这两个说法差很多,别再搞混
    # (2026-08-19 我先用错口径算出「共振碾压现有信号」,又据此反过来主张切换,
    # 是运营者从界面数字对不上把错误揪出来的)。
    #
    # 三方对比的实数见下面「方案 C」那一段 —— **同一张表这里原先也抄了一份**,
    # 2026-08-21 删掉:同一事实两处维护正是 DEC-106 要消灭的东西,而它们已经
    # 一起过期了(两处都是修前视之前的数)。
    # 要点只留一句:**单笔均值差的 t 只有 0.22 / 0.49,21 笔样本分不出高下。**
    #
    # 所以它的定位是**独立的第二意见**(与主信号相关 0.59),不是「更好的信号」:
    # 看两者一致还是背离,比看它自己的方向更有用。共振时回撤明显小一半
    # (−4.1% vs −9.4%),方向一致但尚不显著,值得继续观察。
    # 2026-08-19 运营者拍板改用**方案 C:共振进场 / 散户出场**。
    #   进场:聪明钱流向与散户反向流向同号(共振),且**共振后的散户 z** 过门槛——
    #         z ≤ −enter 做空;z ≥ +enter 做多,**但只在该品种 long_enabled 时**。
    #   出场:散户反向信号翻到反向 / 硬止损 / 持满 / 临近交割。
    #
    # 下面这组对比是在**生猪**上做的,当时生猪做多是关的,所以原注释写着「只做空」。
    # **那三个字对 FG/SA/JD 不成立**(它们 long_enabled=True),已删 —— 留着会让人
    # 以为玻璃根本不会做多,而玻璃恰好是双向的。逐品种的开关见 VARIETIES。
    # 同一时间轴(2023-08 起)三个方案 —— **⚠️ 这三行是 2026-08-19 拍板当时的数,
    # 算在含回榜前视的口径上(DEC-108),不是现行值**:
    #   现有主信号 21 笔 净 +66.7%/胜率 61.9%/回撤 −12.0%/夏普 1.79
    #   散户反向   21 笔 净 +73.8%/胜率 57.1%/回撤  −8.0%/夏普 1.74
    #   **方案 C** 18 笔 净 +79.8%/胜率 61.1%/回撤 **−6.8%**/夏普 **2.23**
    # 方案 C 在**现行**口径下是 18 笔 净 +87.4%/胜率 66.7%/回撤 −4.2%/夏普 2.34
    # (见 VARIETIES["LH"]["backtest"],那里是唯一现行来源)。另两个方案没有重算 ——
    # **所以这三行只能用来看「当时为什么这么选」,不能拿来做今天的横向比较。**
    # **必须如实记住:三者单笔均值差的 t 只有 0.22~0.49,统计上分不出高下。**
    # 选 C 是运营者的判断(回撤最小、夏普最高),不是数据证明它更优——
    # 别在后续文档里把它写成「实测最优」。
    "signal_source": "resonance",   # "flow"=原聪明钱单信号;"resonance"=方案 C;
                                    # "cost"=机构成本状态信号(DEC-112,由 use() 按品种注入)
    # 成本进场的卸仓阈值:机构本轮已卸掉超过这个比例就不再进(0~1 小数!)。
    # **预注册值,不是调参旋钮**:0.3→0.5 五个品种全部单调变差
    # (REPORT_COST_ENTRY_v1),别回头调它。
    "cost_unload_max": 0.30,
    # 玻璃专用的两个附加条件(DEC-114,由 use() 按品种注入;默认关 = 鸡蛋纯碱不变):
    #   cost_need_adding —— 机构近 sig_win 日仍在**同向**加仓(「补仓我们也补」);
    #   cost_min_age     —— 机构本轮已持仓 ≥ N 个可见交易日(翻向当天成本=现价,
    #                       进场是构造性的;要求它先拿两天再跟)。
    # 玻璃六关全过(REPORT_FG_AGE_v1),参数面轮龄 2/3/5 = 0.65/0.56/0.38 单调衰减,
    # 2 是边界最优 —— **不是旋钮**,旁边就是下坡。
    "cost_need_adding": False,
    "cost_min_age": 0,
}

# 品种参数。**每加一个品种,规则要重新验一遍,不许照抄**——
#
# 做多开关三档,**2026-08-21 修完 past 跨合约污染(DEC-111)后重扫**。
# 格式:夏普 / 逐日累计 / 最大回撤 / 笔数。**★ = 现行配置**。
#
#   生猪 LH  ★关 2.34/+87.4%/−4.2%/18 · 开+dip 1.69/+85.7%/−13.5%/32
#            · 开−dip 1.58/+82.4%/−13.5%/37                        → 关(仍是最优)
#   焦煤 JM  ★关 0.91/+64.2%/−14.7%/21 · 开−dip 0.73/+69.8%/−32.9%/46
#            · 开+dip 0.45/+29.1%/−31.0%/39                        → 关(仍是最优)
#   玻璃 FG  ★开−dip 0.21/+34.9%/−67.9%/228 · 关 0.14/+12.7%/−52.8%/112
#            · 开+dip 0.14/+11.7%/−62.5%/168                       → 开、不要 dip(仍是最优)
#   纯碱 SA  ★开−dip 0.36/+47.0%/−61.8%/111 · 开+dip 0.30/+31.5%/−52.4%/92
#            · 关 0.25/+20.6%/−32.2%/56                            → 开、不要 dip(DEC-111 回退)
#   鸡蛋 JD  关 0.69/+20.3%/−15.3%/16 · ★开+dip 0.59/+21.3%/−18.2%/26
#            · 开−dip 0.39/+14.2%/−20.4%/32                        → **现行不是最优**
#     → 鸡蛋这一条 2026-08-21 **翻了回去**:修 past 之前是「开+dip 每项都略好」,
#       修完变成关掉夏普更高、回撤更浅。**有意没跟** —— 26 笔 / 三年样本,
#       0.69 与 0.59 在噪音量级内,跟着每次重扫翻开关就是拟合噪音。
#       留着等运营者拿主意,**不许写成「实测最优」**。
#
# **t 值这一轮没有重算**,上一代那组 t(2.59/2.53/2.37/2.05/1.78 …)是在含前视、
# 含 past 污染的口径上算的,**已作废,不许再引用**。
# 三个大商所品种一致指向「关做多」。**别把这读成三个独立证据**:它们共用
# 2023-08~2026-08 这同一段行情(对这几个品种基本是熊/震荡),只能算一次观察。
# 而且这是**在同一份要报告成绩的样本上做的选择**,属于样本内选择(见 DEC-090)。
#
# 生猪只做空(它样本里只有单边熊市,做多支路逐笔累计 −1.5%,DEC-084 那一代的数,
# 未在 DEC-111 口径下重算;结论方向未变,见上表 2.34 vs 1.69/1.58),而玻璃纯碱双向
# 更好(见上表:FG 0.21 vs 0.14、SA 0.36 vs 0.25),因为它们跨了完整周期、
# 做多支路有真实机会。这条差异是实测出来的,不是设计出来的。
# **但差距很薄** —— FG 0.21 与 0.14、SA 0.36 与 0.25,都不是能拍胸脯的量级。
VARIETIES = {
    "LH": {
        "name": "生猪 LH", "unit": "元/吨", "multiplier": 16.0,
        "replay_start": "2023-08-11",   # 大商所席位数据起点
        "long_enabled": False,          # DEC-084:多头 15 笔逐笔累计 −1.5%,关掉
        "long_needs_dip": True,   # 做多已关,这条用不上;留 True 是生猪原口径
        "out": "hog_signals.json",
        "backtest": "18 笔 净 +87.4%/胜率 66.7%/回撤 −4.2%/夏普 2.34"
                    "(2023-08 起,**低于基准 +99.2%**)(2026-08-21 修掉回榜前视后重算,见 REPORT_PIT_LOOKAHEAD_v1)",
    },
    "FG": {
        "name": "玻璃 FG", "unit": "元/吨", "multiplier": 20.0,
        "replay_start": "2013-01-01",   # 郑商所席位 2012-12 起,留一个月预热
        "long_enabled": True,
        # 实测带 dip 反而差:228 笔 夏普 0.21 → 168 笔 0.14
        # (2026-08-21 DEC-111 口径;旧的「207 笔 0.58 → 158 笔 0.40」是 DEC-090
        #  那一代、含前视含 past 污染,已作废)
        "long_needs_dip": False,
        # **进场信号换成机构成本 + 两条附加(DEC-114,2026-08-22 运营者拍板)**。
        # 规格:成本 + 卸仓≤30% + 机构近 5 日仍同向加仓 + 本轮已持仓 ≥2 日。
        # 六关全过(REPORT_FG_AGE_v1):逐年赢 10/14、选臂 walk-forward +109.8% vs
        # +34.9%(事前就会选中,连选 8 年)、收盘价源 0.52、轮龄≥4/避开换组仍赢、
        # t +1.92、参数面 2/3/5 单调不翻脸。
        # **来历是跑闸门时顺带看见的,记账标「事后假设复验通过」**,证据等级低鸡蛋一档。
        # 流量信号时代 228 笔 +34.9%/0.21/−67.9% 是五品种最弱的,赢它门槛不高;
        # 但 −67.9% → −31.6% 的回撤与事前选中是实打实的。
        "signal_source": "cost",
        "cost_need_adding": True,
        "cost_min_age": 2,
        "out": "fg_signals.json",
        "backtest": "120 笔 净 +218.1%/胜率 54.2%/回撤 −31.6%/夏普 0.65"
                    "(2013-01 起,基准 −17.7%)(2026-08-22 换成本进场+还在加仓+轮龄≥2,"
                    "六关全过但属事后假设复验,见 REPORT_FG_AGE_v1 与 DEC-114;"
                    "流量信号时代 228 笔 +34.9%/0.21/−67.9% 见 DEC-111)",
    },
    "SA": {
        "name": "纯碱 SA", "unit": "元/吨", "multiplier": 20.0,
        "replay_start": "2020-06-01",   # 席位 2019-12 起,留半年预热
        "long_enabled": True,
        # **不要 dip。2026-08-21 当天开过又回退了**(DEC-111)。
        # 开的理由是「两档夏普完全相同(0.36 vs 0.36),按回撤取更浅的那个」——
        # 那次比较用的 past 混了换月跳空(见 main_series 里 past 的注释),
        # 修掉之后平局不存在了:
        #   开−dip 夏普 0.36 / 累计 +47.0% / 回撤 −61.8% / 111 笔
        #   开+dip 夏普 0.30 / 累计 +31.5% / 回撤 −52.4% /  92 笔
        # 夏普与收益都是不要 dip 更好,只有回撤更深。**改它的那条理由被证伪了,
        # 就退回原状**,而不是换个理由把改动留着 —— 后者是结论找依据。
        # 回撤 −52% 与 −62% 之间的取舍如果哪天要重议,连同 dip_win 一起重扫。
        "long_needs_dip": False,
        # **进场信号换成机构成本(DEC-113,2026-08-22 运营者知情破例)**。
        # 闸门 4/5(REPORT_COST_GATES_v1):逐年 4/7、收盘价源 0.56、排除翻向日/
        # 换组余波仍赢、t +1.49;**挂在选臂 walk-forward**(−23.3% vs +47.0%)——
        # 2023/2024 成本臂让出 +90.0%/+20.8%,另一面是躲掉 2025 的 −34.8%。
        # 组合实验(REPORT_SA_COMBO_v1)试过,不比成本单臂好。
        # 运营者知情后拍板上线:回撤 −61.8% → −36.7% 换「可能再遇 2023/24 那种年份」。
        # **这不是「验证通过」,是知情破例** —— 页面 risk_flags 照常挂。
        "signal_source": "cost",
        "out": "sa_signals.json",
        "backtest": "72 笔 净 +93.1%/胜率 51.4%/回撤 −36.7%/夏普 0.59"
                    "(2020-06 起,基准 −17.5%)(2026-08-22 换成本进场信号,闸门 4/5,"
                    "运营者知情破例上线,见 REPORT_COST_GATES_v1 与 DEC-113;"
                    "流量信号时代 111 笔 +47.0%/0.36/−61.8% 见 DEC-111)",
    },
    # 鸡蛋、焦煤(2026-08-19 加)。两者的席位数据与生猪同一个起点 2023-08-11
    # (大商所),所以样本同样只有三年——**开关一律按各自的数据实测,不许照抄生猪**。
    "JD": {
        "name": "鸡蛋 JD", "unit": "元/500千克", "multiplier": 10.0,
        "replay_start": "2023-08-11",   # 大商所席位数据起点
        # **进场信号换成机构成本(DEC-112,2026-08-22 运营者拍板)** ——
        # 五个品种里唯一把五道闸门全过的(REPORT_COST_GATES_v1):
        # 逐年 3/4、选臂 walk-forward +42.8% vs +21.3%、收盘价源 1.34、
        # 排除翻向日/换组余波仍 1.43/1.27、单笔 t +1.91。
        # 机制指标:进场时机构建仓轮龄中位 4 日(流量信号 26 日 —— 运营者
        # 「信号慢了」的诊断),进场成本优势中位 +0.32%(流量 −0.16%)。
        # **首次在夏普与回撤上同时超过「恒定满仓做空」**(1.23/−8.9% vs
        # 1.14/−20.1%),绝对收益仍略低(+48.9% vs +61.6%),页面照实摆。
        # 丑话也钉死:34 笔、三年样本,t 仍未过 2。
        # (流量信号时代的三档扫描 0.69/0.59/0.39 见 DEC-111,已成历史。)
        "long_enabled": True,
        "long_needs_dip": False,  # 价不劣于成本本身就是「不追高」,再叠 dip 是双重计数
        "signal_source": "cost",
        "out": "jd_signals.json",
        "backtest": "34 笔 净 +48.9%/胜率 64.7%/回撤 −8.9%/夏普 1.23"
                    "(2023-08 起;恒定做空基准 +61.6%/夏普 1.14/回撤 −20.1% —— "
                    "夏普与回撤首次胜过躺空,绝对收益仍略低)"
                    "(2026-08-22 换成本进场信号,五道闸门 5/5,"
                    "见 REPORT_COST_GATES_v1 与 DEC-112)",
    },
    "JM": {
        "name": "焦煤 JM", "unit": "元/吨", "multiplier": 60.0,
        "replay_start": "2023-08-11",
        # **做多开,不要 dip(DEC-116,2026-08-23 运营者知情破例)**。
        # 回测上开做多更差:只做空 0.91 / 开−dip 0.73 / 开+dip 0.45(DEC-111 口径),
        # 但那是 2023-08 起三年基本熊市的样本 —— 做多那条腿没有历史可以验证自己,
        # 「没有证据」不是「证明不该做多」。2026-06 起机构组合计转多、8 月 +23% 主升,
        # 引擎 side=net_long 却 long_enabled=False,对机构做多装瞎。运营者拍板打开。
        # **这是知情破例,不是验证通过**:回撤从 −14.7% 放大到 −32.9%,页面 risk_flags 照挂。
        "long_enabled": True,
        "long_needs_dip": False,
        "out": "jm_signals.json",
        "backtest": "46 笔 净 +69.8%/胜率 54.3%/回撤 −32.9%/夏普 0.73"
                    "(2023-08 起,基准 +18.2%)(2026-08-23 开做多,知情破例,见 DEC-116;"
                    "只做空时代 21 笔 +64.2%/0.91/−14.7% 见 DEC-111)",
    },
}


def use(code: str) -> dict:
    """把某个品种的参数并进 RULES 供本轮使用。返回该品种的配置。

    引擎按品种逐个跑,每次跑之前调一次——RULES 里那些与品种相关的键
    (点值、回放起点、做多开关)由它覆盖,其余规则三个品种共用。
    """
    v = VARIETIES[code]
    RULES["multiplier"] = v["multiplier"]
    RULES["replay_start"] = v["replay_start"]
    RULES["long_enabled"] = v["long_enabled"]
    RULES["long_needs_dip"] = v["long_needs_dip"]
    # 进场信号按品种选(DEC-112:鸡蛋走成本信号,其余仍是方案 C)。
    RULES["signal_source"] = v.get("signal_source", "resonance")
    RULES["cost_need_adding"] = v.get("cost_need_adding", False)
    RULES["cost_min_age"] = v.get("cost_min_age", 0)
    return v

SEAT_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sanhe": 3}
PRICE_RANK = {"akshare_v1": 1, "eastmoney_seats_v1": 2, "sina_v1": 3}


# ---------------------------------------------------------------- 数据

def _rank(src: pd.Series, table: dict) -> pd.Series:
    out = src.map(table)
    return out.where(~src.str.contains("_official", na=False), 0).fillna(4)


def load_from_pg(code: str, container: str, pg_user: str,
                 pg_db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    def q(sql: str) -> pd.DataFrame:
        cmd = ["docker", "exec", "-i", container, "psql", "-U", pg_user, "-d", pg_db,
               "-A", "-F", "\t", "--no-align", "-c", sql]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True,
                             encoding="utf-8").stdout
        lines = [ln for ln in out.splitlines() if ln and not ln.startswith("(")]
        from io import StringIO
        return pd.read_csv(StringIO("\n".join(lines)), sep="\t")

    price = q("select exchange,instrument,contract,trade_date,open_price,high_price,"
              "low_price,close_price,settlement_price,volume,open_interest,source "
              f"from price_history where instrument='{code}'")
    seat = q("select instrument,contract,is_variety_total,trade_date,rank_type,member,"
             f"quantity,change,source from seat_history where instrument='{code}'")
    return price, seat


def load_from_csv(code: str, csv_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    def one(stem: str) -> pd.DataFrame:
        # 生产链路落的是 .csv(run-smart-money.sh 导出);研究目录存的是 .csv.gz。
        for name in (f"{stem}.csv", f"{stem}.csv.gz"):
            p = csv_dir / name
            if p.exists():
                return pd.read_csv(p)
        raise FileNotFoundError(f"{csv_dir}/{stem}.csv[.gz] 都不存在")
    low = code.lower()
    return one(f"{low}_price"), one(f"{low}_seat")


def clean_price(price: pd.DataFrame) -> pd.DataFrame:
    df = price.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["_r"] = _rank(df["source"].astype(str), PRICE_RANK)
    df = (df.sort_values(["contract", "trade_date", "_r", "source"])
            .drop_duplicates(["contract", "trade_date"], keep="first"))
    # 收盘价 0 是「当天无成交」不是价格(DEC-073),用结算价兜底
    df["px"] = df["close_price"].replace(0, np.nan).fillna(df["settlement_price"])
    df["settle"] = df["settlement_price"].replace(0, np.nan)
    return df[df["settle"].notna()].reset_index(drop=True)


def clean_seat(seat: pd.DataFrame) -> pd.DataFrame:
    df = seat.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # PG boolean 经 CSV 是 't'/'f';astype(bool) 会把 'f' 判成 True
    df["is_variety_total"] = df["is_variety_total"].astype(str).isin(["t", "true", "True", "1"])
    df = df[(~df["is_variety_total"]) & df["rank_type"].isin(["long", "short"])
            & df["contract"].notna()].copy()
    key = df["member"].astype(str).str.replace(r"[（(][^）)]*[）)]$", "", regex=True)
    df["member_key"] = key.map(lambda m: RULES["alias"].get(m, m))
    df["_r"] = _rank(df["source"].astype(str), SEAT_RANK)
    df = (df.sort_values(["trade_date", "contract", "rank_type", "member_key", "_r", "source"])
            .drop_duplicates(["trade_date", "contract", "rank_type", "member_key"], keep="first"))
    # 除了 `net`(事后完整),再算一条 `net_off`:**只用官方行**的净持仓。
    # 两条并排存着,`_pit_pair` 据此把「当天可见」与「事后完整」分开 —— 那条
    # 回榜反推的前视就是这么摘掉的(见 `_pit_pair`)。
    #
    # **逐腿判,不是整行判。** 一家可能多头榜在(官方行)、空头榜掉了(反推行);
    # 整行丢掉会连那条**实盘看得见的**多头腿一起扔,实测在玻璃上 3,131 天里有
    # 1,352 天因此算错。两腿都没有官方行才是「不知道」,给 NaN —— 掉榜≠零持仓
    # (`research/PITFALLS.md` 第 4 条),给 0 会凭空造出一次清仓。
    df["_off"] = ~df["source"].astype(str).eq("reboard_inferred")
    idx = ["member_key", "contract", "trade_date"]
    wide = df.pivot_table(index=idx, columns="rank_type", values="quantity", aggfunc="sum")
    offw = (df[df["_off"]]
            .pivot_table(index=idx, columns="rank_type", values="quantity", aggfunc="sum")
            .reindex(wide.index))
    out = pd.DataFrame(index=wide.index)
    out["long_q"] = wide["long"] if "long" in wide.columns else np.nan
    out["short_q"] = wide["short"] if "short" in wide.columns else np.nan
    out["net"] = out["long_q"].fillna(0) - out["short_q"].fillna(0)
    lo = offw["long"] if "long" in offw.columns else pd.Series(np.nan, index=wide.index)
    sh = offw["short"] if "short" in offw.columns else pd.Series(np.nan, index=wide.index)
    out["net_off"] = np.where(lo.isna() & sh.isna(), np.nan,
                              lo.fillna(0) - sh.fillna(0))
    return out.reset_index()


def window_end(contract: str) -> pd.Timestamp:
    """散户可交易窗口的止点 = **交割月前月最后一个非周末日**。

    与套利监控 `last_weekday_before_delivery` / `days_to_window_end` 同口径,
    两个模块对「散户还能拿多久」必须给同一个答案。
    节假日不查表:止点只用来卡纪律,±1~2 天的误差不影响「提前 10 个交易日走」。
    """
    raw = "".join(ch for ch in str(contract) if ch.isdigit())
    yy, mm = 2000 + int(raw[:2]), int(raw[2:])
    d = pd.Timestamp(year=yy, month=mm, day=1) - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def days_to_window_end(contract: str, today: pd.Timestamp) -> int:
    """从**次日**起到窗口止点(含)的工作日数。已过止点给 0。

    从次日起算是因为信号是盘后出的:今天判「剩 10 天」,平仓动作发生在明天。
    """
    end = window_end(contract)
    if end <= today:
        return 0
    return int(np.busday_count((today + pd.Timedelta(days=1)).date(),
                               (end + pd.Timedelta(days=1)).date()))


def _self_fingerprint() -> str:
    """本文件内容的 sha256 前 12 位。读不到就给空串,不能让它拖垮出信号。"""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def contract_prices(price: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐合约的开盘价与结算价(行=交易日,列=合约)。

    持仓要**留在自己的合约里**(DEC-096),所以计价不能只有主力那一条序列。
    郑商所对无成交合约写开盘价 0(DEC-073),按缺失处理。
    """
    px = price.assign(_o=price["open_price"].replace(0, np.nan))
    op = px.pivot_table(index="trade_date", columns="contract", values="_o", aggfunc="first")
    st = px.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first")
    return op.sort_index(), st.sort_index()


def main_series(price: pd.DataFrame) -> pd.DataFrame:
    """主力合约与它的逐日收益。**换月日用新主力自己的前一日结算价。**

    这是计价的地基:一旦跨合约相除,换月那天会凭空多出几个百分点的假收益,
    而且不报错。
    """
    p = price.dropna(subset=["open_interest"])
    idx = p.groupby("trade_date")["open_interest"].idxmax()
    cand = p.loc[idx, ["trade_date", "contract"]].sort_values("trade_date")
    dates, cands = cand["trade_date"].tolist(), cand["contract"].tolist()
    def ym(c):
        return str(c)[2:]
    main, cur = [], cands[0]
    for i in range(len(dates)):
        if i > 0 and ym(cands[i - 1]) > ym(cur):
            cur = cands[i - 1]
        main.append(cur)

    px = price.set_index(["contract", "trade_date"])["settle"].sort_index()
    # **只给页面展示用的收盘价**,不参与任何计价。
    # clean_price 的 px 列已经按 DEC-073 处理过:收盘价 0 是「当天无成交」
    # 不是价格,用结算价兜底。全套盈亏/成本仍旧一律走 settle —— 混用两种价
    # 会让页面上的数与回测里的数对不上,那比标签写错更难查。
    pc = price.set_index(["contract", "trade_date"])["px"].sort_index()
    # 开盘价:成交口径改成「次日开盘」之后它才是真正的成交价(DEC-090)。
    # 郑商所对无成交合约会写 0(DEC-073),按缺失处理,别当成真价格。
    po = (price.assign(_o=price["open_price"].replace(0, np.nan))
               .set_index(["contract", "trade_date"])["_o"].sort_index())
    rows = []
    for d, c in zip(dates, main):
        s = px.get((c, d), np.nan)
        hist = px.loc[c] if c in px.index.get_level_values(0) else pd.Series(dtype=float)
        earlier = hist[hist.index < d]
        prev = earlier.iloc[-1] if len(earlier) else np.nan
        ret = s / prev - 1.0 if (np.isfinite(s) and np.isfinite(prev) and prev > 0) else np.nan
        o = po.get((c, d), np.nan)
        oh = po.loc[c] if c in po.index.get_level_values(0) else pd.Series(dtype=float)
        oe = oh[oh.index < d]
        oprev = oe.iloc[-1] if len(oe) else np.nan
        # 开→开:换月日照样用**新合约自己的**前一日开盘价,与结算价那条同一纪律。
        ret_o = o / oprev - 1.0 if (np.isfinite(o) and np.isfinite(oprev) and oprev > 0) else np.nan
        # 开→结算:同日同合约,天然安全。用来算「持仓到今天收盘的浮盈」。
        o2c = s / o - 1.0 if (np.isfinite(s) and np.isfinite(o) and o > 0) else np.nan
        rows.append((d, c, s, ret, o, ret_o, o2c, pc.get((c, d), np.nan)))
    out = pd.DataFrame(rows, columns=["trade_date", "main", "settle", "ret",
                                      "open", "ret_open", "o2c",
                                      "close"]).set_index("trade_date")
    # 回撤判据:**不能用 settle.pct_change** —— settle 是混合主连,换月那天
    # 会凭空跳几个百分点(纯碱 2026-08-13 由 SA2609 换 SA2701,990 → 1031,+4.1%
    # 全是合约价差)。旧写法把这个跳空当成真涨,于是「近 20 日在涨、没有回撤」,
    # 把本该成立的做多挡在门外。实测符号被改写的天数:纯碱 8.6%、鸡蛋 12.2%、
    # 生猪 10.4%、玻璃 7.5%、焦煤 5.6%。
    #
    # 正解是复用上面那条**已经处理过换月**的逐日 ret 连乘 —— 与本函数开头那句
    # 「一旦跨合约相除,换月那天会凭空多出几个百分点,而且不报错」是同一条纪律,
    # 只是 past 当初漏掉了。窗口内任一天 ret 缺失则整段为 NaN(判据从严)。
    out["past"] = (1.0 + out["ret"]).rolling(RULES["dip_win"]).apply(
        np.prod, raw=True) - 1.0
    # 每天的主力离自己的窗口止点还有几个交易日 —— 散户交割纪律靠它卡。
    out["dleft"] = [days_to_window_end(c, d) for c, d in zip(out["main"], out.index)]
    return out


# ---------------------------------------------------------------- 席位组

def alpha_upto(seat: pd.DataFrame, price: pd.DataFrame, hi: pd.Timestamp) -> pd.Series:
    """截至 hi(不含)每家的择时收益 alpha = 实际盈亏 − 恒定仓位能赚到的钱。

    **绝不许看 hi 之后的数据**——滚动重选的全部意义就在这里。
    """
    d = seat[seat["trade_date"] < hi].merge(
        price[["contract", "trade_date", "settle"]], on=["contract", "trade_date"], how="inner")
    if d.empty:
        return pd.Series(dtype=float)
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)]
    if d.empty:
        return pd.Series(dtype=float)
    d = d.assign(dpx=(d["settle"] - d["prev_settle"]) * RULES["multiplier"])
    grp = d.groupby("member_key")
    pnl = grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False)
    beta = grp.apply(lambda s: (s["dpx"] * s["prev_net"].mean()).sum(), include_groups=False)
    days = grp["trade_date"].nunique()
    return (pnl - beta)[days >= RULES["member_min_days"]].sort_values(ascending=False)


def rolling_groups(seat: pd.DataFrame, price: pd.DataFrame,
                   dates: pd.DatetimeIndex) -> tuple[pd.Series, list, list]:
    """逐日生效的席位组、**换人**历史、以及全部重选切点。

    第三个返回值是 2026-08-19 加的:`log` 只在**阵容变了**的时候写一条,于是玻璃
    自 2023-10 起三次重选都选中同一批人,界面上就只剩 2023 那条,运营者据此以为
    「席位三年没更新」。切点单独给出来,界面才说得清「重选跑过、只是没换人」。
    """
    start = dates.min() + pd.Timedelta(days=RULES["warmup_days"])
    cuts = pd.date_range(start, dates.max(), freq=f"{RULES['reselect_months']}MS")
    picks, log, cur = {}, [], None
    for cut in cuts:
        a = alpha_upto(seat, price, cut)
        if len(a) >= RULES["group_k"]:
            new = tuple(a.head(RULES["group_k"]).index)
            if new != cur:
                log.append({"date": cut.strftime("%Y-%m-%d"), "members": list(new),
                            "alpha": {m: round(float(a[m]) / 1e8, 2)
                                      for m in new}})
            cur = new
        picks[cut] = cur
    ser = pd.Series(index=dates, dtype=object)
    for d in dates:
        valid = [c for c in cuts if c <= d]
        ser[d] = picks[valid[-1]] if valid else None
    # 末尾补一个**未来**切点:界面要写「下次 X」。date_range 只走到数据末尾,
    # 不补的话 next 永远是空的。
    nxt = cuts[-1] + pd.DateOffset(months=RULES["reselect_months"]) if len(cuts) else None
    out = [c.strftime("%Y-%m-%d") for c in cuts]
    if nxt is not None:
        out.append(nxt.strftime("%Y-%m-%d"))
    return ser, log, out


def _pit_pair(rows: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """按交易日汇总出两条线:**当日可见的**(只算官方行)与**事后完整的**(含反推)。

    **这是那条前视的修法**(2026-08-21 实测):`reboard_inferred` 行是用**回榜日的
    增减倒推**出来的,实测可见滞后**恒为 1 个交易日,100% 如此**(玻璃 5.0 万条、
    纯碱 3.1 万条,最长都是 1)。时点因此很清楚 ——

        第 D 日收盘,引擎算信号  → D 日的反推值**不存在**
        第 D+1 日开盘,引擎成交  → 仍**不存在**(要 D+1 收盘后才推得出)
        第 D+1 日收盘之后        → 才算得出来

    而 D−1 及更早的反推值,在 D 日**确实已经可见**,用它们不是前视。
    所以只需把「当天那一格」换成官方口径,历史照旧用全量。

    修之前的实测代价(`research/REPORT_PIT_LOOKAHEAD_v1.md`):
    玻璃 +573% → +48.5%(回撤 −45.9% → −64.7%)、纯碱 +295% → +63.7%、
    鸡蛋 −37%、焦煤 −24%;生猪反而略好(它只有 2,520 条反推行)。
    **那些差额全是实盘拿不到的钱。**
    """
    full = rows.groupby("trade_date")["net"].sum().sort_index()
    # 那天这一组人**一条官方行都没有** → 整天不可知,给 NaN 而不是 0。
    known = rows.dropna(subset=["net_off"])
    off = known.groupby("trade_date")["net_off"].sum().reindex(full.index)
    return off, full


def signal_series(seat: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """品种合计净持仓与它的变化、无量纲化 z。

    换组当天不能直接 diff:新旧两组的持仓水平不同,那会把"换了一批人"当成
    "机构大幅调仓"。所以每个组各自算一条,再按生效期取值。
    """
    net = pd.Series(index=groups.index, dtype=float)
    chg = pd.Series(index=groups.index, dtype=float)
    for grp in {g for g in groups.dropna().unique()}:
        days = groups.index[groups == grp]
        off, full = _pit_pair(seat[seat["member_key"].isin(list(grp))])
        # **当天用可见口径,被减数用全量** —— 滞后恒为 1 天,所以 sig_win 天前
        # 那一格在今天必定已经可见,用全量不是前视。理由见 `_pit_pair`。
        cur = off.reindex(days)
        lag = full.shift(RULES["sig_win"]).reindex(days)
        net.loc[days] = cur.values
        chg.loc[days] = (cur - lag).values
    z = chg / chg.rolling(RULES["z_win"], min_periods=60).std()
    return pd.DataFrame({"net": net, "chg": chg, "z": z})


# ---------------------------------------------------------------- 回放

def unload_series(sig: pd.DataFrame, seat: pd.DataFrame,
                  groups: pd.Series) -> pd.DataFrame:
    """**逐日**的「机构相对本轮峰值卸掉了多少」。列:pct / peak_net / legs_now /
    legs_at_peak,以及峰值那天的日期。

    页面只要最后一天(`unload_state`),研究要整条序列(拿它当出场判据做对照实验)。
    **两者共用这一个实现** —— 今天已经栽过好几次「同一件事两处实现,一处过期」。

    三处必须重置,否则这个数会说谎:

    1. **换组当天** —— 新旧两组持仓水平不同,不重置会把「换了一批人」读成
       「机构大幅出货」。`signal_series` 算 `chg` 时为同一个理由分组算过一遍。
    2. **方向翻转 / 归零** —— 那一轮结束了,上一轮的峰值与新一轮无关。
    3. **掉榜那天冻结,不给值** —— 掉榜是「不知道」不是「卸完了」
       (`research/PITFALLS.md` 第 4 条)。研究脚本第一版在这里把整轮重置掉,
       于是掉榜一天峰值就重新起算,出货程度掉回 0。

    还要带出**在榜家数**:五家掉两家会让合计净持仓下降,而人家可能一手没动。
    实测这个混淆专门吃掉长窗口(纯碱 20 日的表观效应几乎全由它贡献)。
    分不清的时候页面必须说出来,所以 `legs_at_peak` 一起返回。
    """
    net = sig["net"]
    # 当日在榜的**组内**家数。一次 groupby 建索引 —— 逐日在 seat 上过滤是
    # O(天数 × 行数),玻璃 140 万行会跑到没法看。
    by_day = seat.groupby("trade_date")["member_key"].agg(set)
    legs = pd.Series(
        [len(set(g) & by_day.get(d, set())) if (g := groups.get(d)) else np.nan
         for d in net.index], index=net.index)

    rows = []
    peak = np.nan
    peak_at = None
    peak_legs = np.nan
    cur_side = 0
    cur_grp = None
    for d in net.index:
        n = net.get(d, np.nan)
        grp = groups.get(d)
        if grp != cur_grp:                       # 换组:重来一轮
            cur_grp, cur_side, peak, peak_at, peak_legs = grp, 0, np.nan, None, np.nan
        if not np.isfinite(n):
            rows.append((d, np.nan, None, None, np.nan, np.nan))
            continue                             # 掉榜=不知道:冻结,不给值
        if n == 0:
            cur_side, peak, peak_at, peak_legs = 0, np.nan, None, np.nan
            rows.append((d, np.nan, None, None, np.nan, np.nan))
            continue
        side = int(np.sign(n))
        if side != cur_side:
            cur_side, peak, peak_at, peak_legs = side, abs(n), d, legs.get(d, np.nan)
        elif abs(n) > peak:
            peak, peak_at, peak_legs = abs(n), d, legs.get(d, np.nan)
        rows.append((
            d,
            round(1.0 - abs(n) / peak, 3) if peak > 0 else np.nan,
            int(np.sign(n) * peak),
            peak_at.strftime("%Y-%m-%d") if peak_at is not None else None,
            legs.get(d, np.nan),
            peak_legs,
        ))
    out = pd.DataFrame(
        rows, columns=["date", "pct", "peak_net", "peak_date", "legs_now", "legs_at_peak"]
    ).set_index("date")
    return out


def unload_state(sig: pd.DataFrame, seat: pd.DataFrame, groups: pd.Series) -> dict:
    """页面要的那一格:**最后一个算得出来的**那天的出货程度。

    **只作展示,不进任何判据**(`replay` 一个字都不读它)。作为进场判据它只在
    纯碱 5 日窗口上通过检验(`research/REPORT_SA_UNLOAD_DEEP_v1.md`),
    玻璃样本外符号翻转、焦煤明确否 —— 横截面上没有支持。
    """
    ser = unload_series(sig, seat, groups)
    valid = ser[ser["pct"].notna()]
    if valid.empty:
        return {"pct": None, "peak_net": None, "peak_date": None,
                "legs_now": None, "legs_at_peak": None}
    r = valid.iloc[-1]
    def _i(v):
        return int(v) if v is not None and np.isfinite(v) else None
    return {"pct": float(r["pct"]), "peak_net": _i(r["peak_net"]),
            "peak_date": r["peak_date"],
            "legs_now": _i(r["legs_now"]), "legs_at_peak": _i(r["legs_at_peak"])}


def inst_cost_series(sig: pd.DataFrame, mkt: pd.DataFrame,
                     groups: pd.Series) -> pd.DataFrame:
    """机构均价/方向/轮龄的逐日重建 —— 成本进场信号的地基(DEC-112)。

    会计规则(与 research/run_cost_entry.py 同一套,闸门就是按它过的):
    加仓那天按当日**主力结算价**加权进 VWAP;减仓成本不动;换组/方向翻转重置;
    掉榜或缺价那天**冻结**(不知道 ≠ 没动),当天不产出任何值。

    这是前 20 截断席位数据上的**研究代理量**,不是交易所真值 ——
    页面引用时不许写成「机构的真实成本」。
    """
    net = sig["net"]
    px = mkt["settle"]
    out = pd.DataFrame(index=net.index,
                       columns=["side", "cost", "age"], dtype=float)
    cur_grp, side, qty, cost, age = None, 0, 0.0, np.nan, 0
    for d in net.index:
        grp = groups.get(d)
        if grp != cur_grp:
            cur_grp, side, qty, cost, age = grp, 0, 0.0, np.nan, 0
        n = net.get(d, np.nan)
        p = px.get(d, np.nan)
        if not np.isfinite(n) or not np.isfinite(p):
            continue
        s = int(np.sign(n)) if n != 0 else 0
        if s == 0:
            side, qty, cost, age = 0, 0.0, np.nan, 0
            continue
        if s != side:
            side, qty, cost, age = s, abs(n), p, 0
        else:
            dn = abs(n) - qty
            if dn > 0:
                cost = (cost * qty + dn * p) / (qty + dn)
            qty = abs(n)
            age += 1
        out.loc[d] = (side, cost, age)
    return out


def cost_entry_frame(cc: pd.DataFrame, net: pd.Series, settle: pd.Series,
                     unload: pd.Series, chg: pd.Series | None = None) -> pd.DataFrame:
    """把成本状态翻成进出场能用的两列:cost_z(±(enter+0.5) / 0)与
    cost_reason(挡单原因,给页面「进场条件」那一行说人话用)。

    三个条件缺一不可:机构在场(有净方向)、价格不劣于机构成本
    (多:价 ≤ 成本;空:价 ≥ 成本,容差 0 —— 预注册,不设旋钮)、
    机构本轮已卸掉 ≤ cost_unload_max。
    玻璃(DEC-114)再加两条:cost_min_age(机构本轮已持仓 ≥ N 日)与
    cost_need_adding(机构近 sig_win 日仍同向加仓,看 `chg`)。
    纯函数,好测:所有输入都是现成序列,不碰全局状态(RULES 只读)。
    """
    amp = RULES["enter"] + 0.5
    umax = RULES["cost_unload_max"]
    min_age = RULES.get("cost_min_age", 0)
    need_adding = RULES.get("cost_need_adding", False)
    z = pd.Series(0.0, index=settle.index)
    reason = pd.Series(None, index=settle.index, dtype=object)
    for d in settle.index:
        n = net.get(d, np.nan)
        p = settle.get(d, np.nan)
        if not np.isfinite(n) or not np.isfinite(p):
            reason[d] = "机构席位掉榜,今日仓位看不清"
            continue
        side = cc["side"].get(d, np.nan)
        cost = cc["cost"].get(d, np.nan)
        if not np.isfinite(side) or side == 0 or not np.isfinite(cost):
            reason[d] = "机构未建立净方向"
            continue
        u = unload.get(d, np.nan)
        if not np.isfinite(u):
            reason[d] = "机构席位掉榜,今日仓位看不清"
            continue
        if u > umax:
            reason[d] = f"机构本轮已卸掉 {u:.0%}(超过 {umax:.0%} 不追)"
            continue
        age = cc["age"].get(d, np.nan)
        if min_age and (not np.isfinite(age) or age < min_age):
            reason[d] = f"机构本轮刚翻向(持仓 {0 if not np.isfinite(age) else int(age)} 日),等它先建仓 {min_age} 日"
            continue
        if need_adding:
            g = chg.get(d, np.nan) if chg is not None else np.nan
            if not (np.isfinite(g) and np.sign(g) == side):
                reason[d] = f"机构近 {RULES['sig_win']} 日没在同向加仓,不追"
                continue
        if side > 0:
            if p <= cost:
                z[d] = amp
            else:
                reason[d] = f"价 {p:.0f} 高于机构成本 {cost:.0f},等回到成本再进"
        else:
            if p >= cost:
                z[d] = -amp
            else:
                reason[d] = f"价 {p:.0f} 低于机构空头成本 {cost:.0f},等弹回成本再进"
    return pd.DataFrame({"cost_z": z, "cost_reason": reason})


def attach_cost_signal(sig: pd.DataFrame, seat: pd.DataFrame, mkt: pd.DataFrame,
                       groups: pd.Series) -> pd.DataFrame:
    """把成本进场信号(cost_z / cost_reason)挂到 sig 上。cost 模式的品种
    必须在 build_payload / replay 之前走这一步 —— entry_exit_signals 只认列。"""
    unload = unload_series(sig, seat, groups)["pct"]
    cc = inst_cost_series(sig, mkt, groups)
    ext = cost_entry_frame(cc, sig["net"], mkt["settle"],
                           unload.reindex(mkt.index), sig["chg"].reindex(mkt.index))
    return sig.assign(cost_z=ext["cost_z"].reindex(sig.index),
                      cost_reason=ext["cost_reason"].reindex(sig.index))


def entry_exit_signals(sig: pd.DataFrame, retail: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """按 RULES["signal_source"] 决定进场与出场各用哪一路信号。

    两路都以「正=看涨」为约定:聪明钱用净持仓变化本身(增加=减空/加多),
    散户那路在 retail_series 里已经取过负号。所以共振 = 两者同号。

    方案 C 的进场用共振后的散户信号、出场只用散户信号——出场不要求共振,
    否则聪明钱一转向就把仓位锁死在里面。
    """
    if RULES["signal_source"] == "cost":
        # 成本进场(DEC-112,目前只有鸡蛋):进场用 attach_cost_signal 预先挂在
        # sig 上的状态信号(机构在场 + 价不劣于其成本 + 本轮卸仓 ≤30%);
        # **出场仍走散户反向,四件套一字不动**(REPORT_COST_GATES_v1 的闸门
        # 就是这么过的,出场换了闸门作废)。
        if retail is None or retail.empty:
            # 散户三家全缺席在现有品种上不会发生。真发生时宁可让反向/消退
            # 失效(只剩止损/持满/交割),也不能安静换一路出场口径。
            return sig["cost_z"], sig["cost_z"] * np.nan
        return sig["cost_z"], retail["rz"]
    if RULES["signal_source"] != "resonance" or retail is None or retail.empty:
        return sig["z"], sig["z"]
    # 用**标准化后的 z** 判共振,不用原始 chg。
    # chg 不需要预热,拿它判等于「聪明钱信号还没预热完成就先拿来用」——
    # 2026-08-19 对拍时抓到:席位组 2024-05-01 首次生成,z 要 60 个交易日才有值,
    # 而 chg 当天就有,于是 2024-05-17 凭一个尚不可用的信号开了一仓。
    # np.sign(NaN) 是 NaN、NaN==NaN 为 False,所以改用 z 之后预热期自动不进场。
    resonate = np.sign(sig["z"]) == np.sign(retail["rz"])
    return retail["rz"].where(resonate), retail["rz"]


def entry_side(ze: float, past: float) -> tuple[int, str | None]:
    """今天这个信号会往**哪个方向**进场,进不了的话卡在哪一条。

    返回 `(方向, 挡住的理由)`;方向 −1 做空 / 0 不进 / +1 做多。

    **`replay` 与 `build_payload` 共用这一个。** 进场判据在两处各写一份,页面
    迟早与实际不一致 —— DEC-104 就是这么来的:前端照着一句过期注释显示了机构
    那个数,而引擎比的是散户那个数,页面上写着「需达 1(现 2.09)」却又显示无持仓。
    这次运营者要求「一触发就显示做多还是做空」,更不能让前端自己推一遍。

    这里**不判**交割窗口与次日有没有开盘价 —— 那两条要知道持仓合约、要能取到
    次日开盘,是执行细节,由调用方各自补。
    """
    if not np.isfinite(ze):
        return 0, "信号未就绪"
    if ze <= -RULES["enter"]:
        return -1, None
    if ze < RULES["enter"]:
        return 0, "强度未到门槛"
    if not RULES["long_enabled"]:
        # 关着做多时 z 上穿门槛只代表「机构在减空」,不产生进场 —— 界面要说清,
        # 否则看的人会以为信号漏了。
        return 0, "本品种做多已关"
    if RULES["long_needs_dip"] and not (np.isfinite(past) and past < 0):
        return 0, f"做多要求先有回撤(近 {RULES['dip_win']} 日未回落)"
    return 1, None


def replay(sig: pd.DataFrame, mkt: pd.DataFrame,
           retail: pd.DataFrame | None = None,
           op: pd.DataFrame | None = None,
           st: pd.DataFrame | None = None,
           extra_exit: pd.Series | None = None,
           disable_reverse: bool = False) -> tuple[list[dict], pd.Series, pd.Series]:
    """全量回放历史信号。

    `extra_exit` / `disable_reverse` 是**给研究做对照实验用的**,两个都默认关闭,
    生产路径一个字节都不变(`test_两个研究参数默认关闭时与不传完全一致` 钉住)。
    加在这里而不是另写一份 replay:今天已经栽过好几次「同一件事两处实现」,
    出场口径一分叉,研究结论就与线上跑的不是同一套东西了。

      · `extra_exit` —— 逐日布尔序列,为真那天挂出场(理由记「外部」)。
        优先级排在交割纪律与止损**之后**:那两条是「不能再拿了」,不容替换。
      · `disable_reverse` —— 关掉「反向」这条出场,用来做「换掉它」的对照。

    **成交口径:信号日收盘出信号,次日开盘成交**(DEC-090)。席位持仓排名是收盘之后
    才公布的(大商所约 15:30-16:00、郑商所约 16:26),按信号日结算价成交做不到。
    实测这一条把玻璃从 +5247% 打到 +436%——收益几乎全在信号后第一天,而那天拿不到。

    **持仓留在自己的合约里**(DEC-096,2026-08-20 运营者指出)。原来跟着主力走,
    主力一换,仓位就无声地从近月变成远月:FG2609 07-06 进场,08-17 主力换到 FG2701,
    页面上就出现「进场 966·FG2609 / 现价 912·FG2701」这种读不懂的东西,而那笔仓
    **本该在 08-17 因为 FG2609 只剩 10 个交易日被强制平掉**。
    交割纪律查的必须是**持仓所在合约**还剩几天,不是今天的主力还剩几天——
    原来查后者,主力一换检查对象就变成远月,老仓免费续命。实测被免费滚过去的那些笔
    收益普遍高好几倍(鸡蛋 +7.38% vs −1.83%),它们拿到了一次不用付钱也不用重新
    判断的续命。改完之后玻璃 +385%→+470%、纯碱 +244%→+278%、生猪 +79.8%→+85.0%,
    **修掉反而更好**:该在交割前平掉的仓不再硬扛到远月。
    换月后还要拿着老合约 1~6 天(最长玻璃 21 天),实测那些天**全都有成交价**。

    进场仍然按**当天的主力合约**开:换月之后要不要在新合约上开,是新的一次判断。
    """
    idx = mkt.index
    z_in, z_out = entry_exit_signals(sig, retail)
    main = mkt["main"]
    op = (op if op is not None else pd.DataFrame()).reindex(idx)
    st = (st if st is not None else pd.DataFrame()).reindex(idx)

    def px(c: str, i: int, which: str) -> float:
        """第 i 个交易日、合约 c 的开盘价或结算价。取不到给 nan。"""
        tab = op if which == "open" else st
        if not isinstance(c, str) or c not in tab.columns or i < 0 or i >= len(idx):
            return np.nan
        v = tab[c].iloc[i]
        return float(v) if np.isfinite(v) else np.nan

    def hold_days_of(c: str, a: int, b: int) -> list[int]:
        """持仓期间该合约**真正有开盘价**的交易日。空档跳过,链条才接得上。"""
        return [k for k in range(a, b + 1) if np.isfinite(px(c, k, "open"))]

    def compound(c: str, days: list[int], sd: float) -> float:
        """沿着这些日子逐日连乘。

        **必须连乘,不能用 −(p_out/p_in−1) 这种简单收益**:做空时两者不相等,
        而引擎其余部分(基准、夏普、回撤)全是逐日连乘口径。2026-08-20 第一版
        记账用简单、日净值用连乘,断言当场抓住(生猪 +79.6% vs +88.3%)。"""
        v = 1.0
        for a_idx, k in zip(days, days[1:]):
            a, b = px(c, a_idx, "open"), px(c, k, "open")
            if a > 0:
                v *= 1 + sd * (b / a - 1)
        return v

    trades, side, entry_i, entry_c, pending = [], 0, None, None, None
    pos = pd.Series(0.0, index=idx)
    for i, d in enumerate(idx):
        z = z_out.get(d, np.nan)
        reason = None
        if side != 0 and i > entry_i:
            hd = hold_days_of(entry_c, entry_i + 1, i)
            last_open = px(entry_c, hd[-1], "open") if hd else np.nan
            p_now = px(entry_c, i, "settle")
            # 止损判的是**今天收盘时已知的**浮亏:连乘到最后一个有价日的开盘,
            # 再乘当天的开→结算。口径必须与记账一致,否则止损线名不副实。
            cum = (compound(entry_c, hd, side) * (1 + side * (p_now / last_open - 1)) - 1
                   if np.isfinite(last_open) and np.isfinite(p_now) and last_open > 0 else 0.0)
            # 交割纪律排在最前:它不是择时判断,是「不能再拿了」。查的是**持仓所在
            # 合约**,不是今天的主力——后者会在换月时把检查对象换掉。
            if days_to_window_end(entry_c, d) <= RULES["exit_before_delivery"]:
                reason = "临近交割"
            elif cum <= -RULES["stop"]:
                reason = "止损"
            elif i - entry_i >= RULES["max_hold"]:
                reason = "持满"
            elif (not disable_reverse) and np.isfinite(z) and side * z <= -RULES["enter"]:
                reason = "反向"
            elif extra_exit is not None and bool(extra_exit.get(d, False)):
                reason = "外部"
            elif np.isfinite(z) and abs(z) <= RULES["exit_z"] and side * z <= 0:
                reason = "消退"
        # 出场只能在**次日开盘**成交。那天没成交价就继续持有,理由挂着等到能成交
        # 那天再平 —— 不能一边说「平了」一边让下一笔在老仓真正平掉之前就开进去,
        # 两笔的持仓日会重叠(2026-08-20 第一版就是这么错的,断言当场抓住)。
        pending = pending or reason
        j = i + 1 if (pending and np.isfinite(px(entry_c, i + 1, "open"))) else None
        if pending and j is not None:
            reason, pending = pending, None
            p_in, p_out = px(entry_c, entry_i + 1, "open"), px(entry_c, j, "open")
            booked = compound(entry_c, hold_days_of(entry_c, entry_i + 1, j), side) - 1
            trades.append({
                "side": "short" if side < 0 else "long",
                "entry_date": idx[entry_i].strftime("%Y-%m-%d"),
                "exit_date": d.strftime("%Y-%m-%d"),
                "entry_px": _f(p_in), "exit_px": _f(p_out),
                "contract": entry_c,
                "ret_pct": round(booked * 100, 2),
                "hold_days": i - entry_i,
                "exit_reason": reason,
                "_i": entry_i, "_j": i, "_c": entry_c, "_fill": j,
            })
            side, pending = 0, None
        ze = z_in.get(d, np.nan)
        c_now = main.get(d)
        # 交割窗口内不进场:只挡不进。换月之后主力是新合约,剩余天数回到 90 多天,
        # 信号还在的话照常能进 —— 这正是运营者说的「2701 位置好就到 2701 开仓」。
        if side == 0 and (not isinstance(c_now, str)
                          or days_to_window_end(c_now, d) <= RULES["exit_before_delivery"]):
            pos.iloc[i] = 0
            continue
        if side == 0 and np.isfinite(ze) and np.isfinite(px(c_now, i + 1, "open")):
            want, _ = entry_side(ze, mkt["past"].get(d, np.nan))
            if want != 0:
                side, entry_i, entry_c = want, i, c_now
        pos.iloc[i] = side
    # 尚未平仓的那笔单独带出来(界面要显示"持有中"),按最新结算价估值。
    if side != 0:
        p_in = px(entry_c, entry_i + 1, "open")
        days = hold_days_of(entry_c, entry_i + 1, len(idx) - 1)
        last_open = px(entry_c, days[-1], "open") if days else np.nan
        p_now = px(entry_c, days[-1], "settle") if days else np.nan
        # 连乘到最后一个有价日的开盘,再补上那天的开→结算(浮盈按最新结算价估)。
        cum = (compound(entry_c, days, side)
               * (1 + side * (p_now / last_open - 1)) - 1
               if np.isfinite(last_open) and np.isfinite(p_now) and last_open > 0 else 0.0)
        trades.append({
            "side": "short" if side < 0 else "long",
            "entry_date": idx[entry_i].strftime("%Y-%m-%d"), "exit_date": None,
            "entry_px": _f(p_in), "exit_px": None, "contract": entry_c,
            "ret_pct": round(cum * 100, 2), "hold_days": len(idx) - 1 - entry_i,
            "exit_reason": None, "_i": entry_i, "_j": None, "_c": entry_c, "_fill": None,
        })

    # ---- 逐日净值 ----
    # **只走这个合约真正有价的那些天**,并用「上一个有价日」作分母。中间有没成交的
    # 空档时按自然日连乘链条会断,而记账是跨过空档的,两条就对不上——2026-08-20
    # 第一版就是这么错的(玻璃逐笔 +442% 而逐日 +2856%)。函数末尾有断言钉住。
    daily = pd.Series(0.0, index=idx)
    for t in trades:
        c, i0 = t["_c"], t["_i"]
        last = t["_fill"] if t["_fill"] is not None else len(idx) - 1
        sd = 1.0 if t["side"] == "long" else -1.0
        days = [k for k in range(i0 + 1, last + 1) if np.isfinite(px(c, k, "open"))]
        for a_idx, k in zip(days, days[1:]):
            a, b = px(c, a_idx, "open"), px(c, k, "open")
            if a > 0:
                daily.iloc[k] = sd * (b / a - 1)
        if days:
            daily.iloc[days[0]] -= COST
            if t["_j"] is not None:
                daily.iloc[days[-1]] -= COST

    closed = [t for t in trades if t["_j"] is not None]
    if closed:
        by_trade = float(np.prod([1 + t["ret_pct"] / 100 for t in closed]))
        gross = pd.Series(0.0, index=idx)
        for t in closed:
            c, sd = t["_c"], (1.0 if t["side"] == "long" else -1.0)
            days = [k for k in range(t["_i"] + 1, t["_fill"] + 1)
                    if np.isfinite(px(c, k, "open"))]
            for a_idx, k in zip(days, days[1:]):
                a, b = px(c, a_idx, "open"), px(c, k, "open")
                if a > 0:
                    gross.iloc[k] = sd * (b / a - 1)
        by_day = float((1 + gross).prod())
        if abs(by_trade - by_day) > max(0.01, 0.01 * abs(by_trade)):
            raise AssertionError(
                f"逐日净值与逐笔记账对不上:逐笔 {(by_trade-1)*100:+.1f}% / "
                f"逐日 {(by_day-1)*100:+.1f}%")
    for t in trades:
        for k in ("_i", "_j", "_c", "_fill"):
            t.pop(k, None)
    return trades, pos, daily


def _f(v):
    return None if v is None or not np.isfinite(v) else round(float(v), 1)


def edge_split(sig: pd.DataFrame, mkt: pd.DataFrame,
               retail: pd.DataFrame | None) -> dict | None:
    """信号后第一天的超额,有多少落在**拿不到的隔夜跳空**里。

    2026-08-19 运营者问「散户反向明明是好的反向指标,为什么回撤这么大」,查出来的
    根因就是这个:席位持仓排名 16:26 才公布,所有人同一时刻看到,价格在夜盘/次日
    开盘一步跳过去。**指标是准的,准的那一段却结构性地拿不到。**

    实测(2026-08-19):生猪 71%、玻璃 86%、纯碱 83% 的第一天超额都在跳空里。
    剩下能吃到的日内部分只有 +0.07%~+0.14%,对玻纯 1.5% 量级的日波动就是噪音,
    净值曲线因此又毛又深。这也解释了为什么生猪受影响最小——它跳空占比最低,
    而且信号在 D+5~D+20 还有余温(+1.05%),不靠那一跳。

    **别拿它当可优化的参数**:延迟 1/2/3/5 天进场全部更差(实测),躲不开。
    """
    z_in, _ = entry_exit_signals(sig, retail)
    idx = mkt.index
    ret = mkt["ret"].fillna(0).to_numpy()
    o2c = mkt["o2c"].fillna(0).to_numpy()
    settle, openp = mkt["settle"].to_numpy(), mkt["open"].to_numpy()
    main_c = mkt["main"].to_numpy()
    d1, gp, it = [], [], []
    for i, d in enumerate(idx[:-1]):
        z = z_in.get(d, np.nan)
        if not np.isfinite(z) or abs(z) < RULES["enter"]:
            continue
        sd = float(np.sign(z))
        d1.append(sd * ret[i + 1] * 100)
        it.append(sd * o2c[i + 1] * 100)
        # 换月日 settle 与 open 不是同一个合约,跳空无意义,跳过
        if (main_c[i + 1] == main_c[i] and np.isfinite(openp[i + 1])
                and np.isfinite(settle[i]) and settle[i] > 0):
            gp.append(sd * (openp[i + 1] / settle[i] - 1) * 100)
    if len(d1) < 30 or not gp:
        return None
    m1, mg, mi = float(np.mean(d1)), float(np.mean(gp)), float(np.mean(it))
    return {"n": len(d1), "day1_pct": round(m1, 3), "gap_pct": round(mg, 3),
            "intraday_pct": round(mi, 3),
            "gap_share_pct": round(100 * mg / m1, 0) if m1 else None}


def risk_flags(strat: dict, closed: list, daily: pd.Series,
               bench: dict | None = None) -> list[dict]:
    """页面顶部那条醒目风险标识的素材(运营者 2026-08-19 要求)。

    **门槛写死、数字实算,不针对某个品种定制。**生猪现在一条都不触发,玻璃纯碱
    各触发几条——这是它们自己的数字说的,不是我挑出来贴上去的。哪天玻璃真的变好了,
    条目会自己消失;哪天生猪变差了,它自己会挂上来。

    六个门槛,每一个都有它自己的意思:
      夏普 < 1.0   —— 一年赚的抵不上一年的波动;
      夏普 ≤ 基准  —— **风险调整后还不如躺着满仓做空**,这个信号等于白做;
      回撤 ≥ 25%   —— 单次回撤超过四分之一,多数人拿不住;
      胜率 < 50%   —— 不到一半的交易赚钱,全靠少数几笔大赢撑着;
      t 值 < 2.0   —— 单笔均值在统计上分不出与 0 的差别,**等于还没验证**;
      亏损年 ≥ 1/4 —— 四年里有一年是亏的,不是偶发。
    """
    if not closed:
        return []
    r = np.array([t["ret_pct"] for t in closed])
    out = []
    sh = strat.get("sharpe")
    if sh is not None and sh < 1.0:
        out.append({"key": "sharpe",
                    "text": f"**夏普只有 {sh:.2f}** —— 一年赚到的抵不上一年的波动。"})
    bsh = (bench or {}).get("sharpe")
    if sh is not None and bsh is not None and sh <= bsh:
        out.append({"key": "vs_benchmark",
                    "text": f"**风险调整后还不如躺着满仓做空**(夏普 {sh:.2f} vs 基准 "
                            f"{bsh:.2f})—— 这个信号本身没有带来好处。"})
    dd = strat.get("max_dd_pct")
    if dd is not None and dd <= -25:
        out.append({"key": "drawdown",
                    "text": f"**最大回撤 {dd:.1f}%** —— 中途要扛得住净值腰斩级别的下跌。"})
    win = 100 * float((r > 0).mean())
    if win < 50:
        out.append({"key": "winrate",
                    "text": f"**胜率 {win:.1f}%,不到一半** —— 收益靠少数几笔大赢撑着,"
                            "连亏很多笔是常态。"})
    if len(r) >= 10 and r.std(ddof=1) > 0:
        t = float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r))))
        if t < 2.0:
            out.append({"key": "significance",
                        "text": f"**单笔均值的 t 值只有 {t:.2f}(<2)** —— 统计上分不出它"
                                f"与 0 的差别,{len(r)} 笔样本还**没有证明这条策略成立**。"})
    if len(daily):
        eq = (1 + daily).cumprod()
        yr = eq.resample("YE").last() / eq.resample("YE").last().shift(1) - 1
        yr = yr.dropna()
        neg = int((yr < 0).sum())
        if len(yr) >= 4 and neg * 4 >= len(yr):
            out.append({"key": "negative_years",
                        "text": f"**{len(yr)} 年里有 {neg} 年是亏的** —— 亏损年不是偶发。"})
    return out


def _caveats(strat: dict, bench: dict, closed: list) -> list[str]:
    """边界说明。**凡是数字都从实参算**,不许写死——参数一改文案就会对不上。"""
    shorts = [t for t in closed if t["side"] == "short"]
    out = ["样本只有三年(2023-08 起,大商所席位数据起点),且**只有一种市况**——全程熊市。"]
    if not RULES["long_enabled"]:
        out.append(
            "**做多支路已关闭**:回测里多头 15 笔逐笔累计 −1.5%、均值 −0.02%(抛硬币),"
            "关掉后夏普 1.96 → 2.39。机构减空时策略平仓观望,不是继续扛空单。"
            "(**这三个数是按结算价成交那一版算的**,DEC-090 改口径后没有重算;"
            "留作当时的决策依据,不要拿它和现在页面上的数字比。)")
    out.append(
        "**「机构真转多」也不是买入信号**:样本里它出现过 14 天(集中在 2025-07),"
        "之后 20 日主力仍平均跌 1.18%,最好一次只有 +0.61%。但那 14 天全挤在一个"
        "窗口里,实质是 1 个事件——只能说没有证据支持,不能说证明必亏。")
    if shorts:
        wins = sum(1 for t in shorts if t["ret_pct"] > 0)
        cum = (np.prod([1 + t["ret_pct"] / 100 for t in shorts]) - 1) * 100
        worst = min(t["ret_pct"] for t in shorts)
        out.append(f"空头信号有回测支撑:{len(shorts)} 笔 {cum:+.1f}%(毛),"
                   f"胜率 {100 * wins / len(shorts):.1f}%,最差 {worst:+.1f}%。")
    gap = "没跑赢" if strat["cum_pct"] < bench["cum_pct"] else "跑赢了"
    out.append(
        f"**绝对收益{gap}「躺着满仓做空」**({strat['cum_pct']:+.1f}% vs "
        f"{bench['cum_pct']:+.1f}%)。策略赢的是回撤({strat['max_dd_pct']:+.1f}% vs "
        f"{bench['max_dd_pct']:+.1f}%)与夏普({strat['sharpe']} vs {bench['sharpe']}),"
        "以及趋势反转时会跟着退出——后者样本内无法验证。")
    if SPLIT.get("v"):
        e = SPLIT["v"]
        out.append(
            f"**这个信号准的那一段,大部分拿不到。**{e['n']} 次触发里,信号后第一天的"
            f"平均超额是 {e['day1_pct']:+.2f}%,其中 **{e['gap_share_pct']:.0f}% 落在隔夜"
            f"跳空**(信号日结算 → 次日开盘,{e['gap_pct']:+.2f}%),真正能吃到的日内只有"
            f" {e['intraday_pct']:+.2f}%。席位排名 16:26 才公布,所有人同一时刻看到,"
            "价格在夜盘/次日开盘一步跳过去。**指标是准的,不等于这段钱赚得到。**"
            "延迟 1/2/3/5 天进场想躲开抢跑,实测全部更差。")
    out.append(
        "**成交口径:信号日收盘出信号,次日开盘成交**(DEC-090)。席位持仓排名是"
        "收盘后才公布的(大商所约 15:30-16:00、郑商所约 16:26),按信号日结算价"
        "成交做不到。这条口径下的数字比原来低很多——玻璃从 +5247% 降到 +436%(毛)"
        "——因为收益几乎全部集中在信号后第一天,而那一天恰恰是拿不到的。"
        "未模拟涨跌停与流动性冲击。")
    return out


def _perf(daily: pd.Series) -> dict:
    """一条日收益序列的累计/夏普/最大回撤。策略与基准共用,口径才对得上。"""
    dd = daily.fillna(0)
    eq = (1 + dd).cumprod()
    return {
        "cum_pct": round((float(eq.iloc[-1]) - 1) * 100, 1),
        "sharpe": round(float(dd.mean() / dd.std() * np.sqrt(242)), 2) if dd.std() > 0 else None,
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
    }


# ---------------------------------------------------------------- 产物

def retail_series(seat: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """散户三家的合计净持仓、变化,以及**反向**信号的无量纲强度。

    反向:散户加多(净持仓上升)对应看跌,所以信号取负号。名单固定不重选。
    """
    have = [m for m in RULES["retail_seed"] if m in set(seat["member_key"])]
    if len(have) < 2:
        return pd.DataFrame(index=dates, columns=["net", "chg", "rz"], dtype=float), have
    # 散户那一路吃同一条前视 —— 他们也会掉榜、也会被回榜反推。同样处理。
    off, full = _pit_pair(seat[seat["member_key"].isin(have)])
    s = off.reindex(dates)
    chg = s - full.shift(RULES["sig_win"]).reindex(dates)
    rz = -(chg - chg.rolling(RULES["z_win"], min_periods=60).mean()) /          chg.rolling(RULES["z_win"], min_periods=60).std()
    return pd.DataFrame({"net": s, "chg": chg, "rz": rz}), have


def build_payload(sig: pd.DataFrame, mkt: pd.DataFrame, seat: pd.DataFrame,
                  groups: pd.Series, log: list, cuts: list | None = None,
                  op: pd.DataFrame | None = None, st: pd.DataFrame | None = None) -> dict:
    d = mkt.index[-1]
    z = sig["z"].get(d, np.nan)
    # 散户那路要先算出来:方案 C 的进出场都靠它(见 entry_exit_signals)
    rdf, rhave = retail_series(seat, mkt.index)
    trades, pos, daily = replay(sig, mkt, rdf, op, st)
    open_trade = trades[-1] if trades and trades[-1]["exit_date"] is None else None
    closed = [t for t in trades if t["exit_date"]]

    prev_d = mkt.index[-1 - RULES["sig_win"]] if len(mkt) > RULES["sig_win"] else None
    grp = list(groups.get(d) or ())
    today = seat[(seat["trade_date"] == d) & (seat["member_key"].isin(grp))]
    prev = seat[(seat["trade_date"] == prev_d) & (seat["member_key"].isin(grp))] if prev_d is not None else None
    members = []
    for m in grp:
        now = float(today[today["member_key"] == m]["net"].sum()) if len(today) else 0.0
        was = float(prev[prev["member_key"] == m]["net"].sum()) if prev is not None and len(prev) else np.nan
        members.append({
            "member": m,
            "net": round(now),
            "change": None if not np.isfinite(was) else round(now - was),
            "on_board": bool(len(today[today["member_key"] == m])),
        })

    # —— 散户反向维度 ——
    r_now = rdf.loc[d] if d in rdf.index else None
    rz_now = float(r_now["rz"]) if r_now is not None and np.isfinite(r_now.get("rz", np.nan)) else None
    # 共振 = 聪明钱流向与散户反向流向同号。两者都以「正=看涨」为约定:
    # 聪明钱用 chg 本身(净持仓增加=减空/加多),散户已在 retail_series 里取过负号。
    smart_now = sig["chg"].get(d, np.nan)
    resonate = bool(rz_now is not None and np.isfinite(smart_now)
                    and np.sign(smart_now) == np.sign(rz_now))
    rmembers = []
    for m in rhave:
        cur = seat[(seat["trade_date"] == d) & (seat["member_key"] == m)]["net"].sum()
        prev_row = seat[(seat["trade_date"] == prev_d) & (seat["member_key"] == m)]             if prev_d is not None else None
        was = prev_row["net"].sum() if prev_row is not None and len(prev_row) else np.nan
        rmembers.append({"member": m, "net": int(round(cur)),
                         "change": None if not np.isfinite(was) else int(round(cur - was)),
                         "on_board": bool(len(seat[(seat["trade_date"] == d)
                                                   & (seat["member_key"] == m)]))})
    retail_state = {
        "members": rmembers,
        "net": None if r_now is None or not np.isfinite(r_now.get("net", np.nan))
               else int(r_now["net"]),
        "change": None if r_now is None or not np.isfinite(r_now.get("chg", np.nan))
                  else int(r_now["chg"]),
        "z": None if rz_now is None else round(rz_now, 2),
        # z 为正 = 散户在减多/加空 → 反向看涨;为负 = 散户在加多 → 反向看跌
        "resonate": resonate,
        # cost 模式下散户仍管**出场**,所以 trades 依旧为真 —— 它没有退出舞台。
        "trades": RULES["signal_source"] in ("resonance", "cost"),
        "note": ("散户三家长期站多头、长期亏钱,故反向取用;名单跨品种固定、不逐品种重选。"
                 "**本品种进场已改为机构成本信号(DEC-112),散户这一路只管出场**:"
                 "它翻向时平仓,不再参与进场判断。"
                 if RULES["signal_source"] == "cost" else
                 "散户三家长期站多头、长期亏钱,故反向取用;名单跨品种固定、不逐品种重选。"
                 "**现行策略(方案 C)就是用它进出场**:与聪明钱共振时按它的方向进场,"
                 "它翻向时出场。选它是因为回撤最小(−4.1% vs 主信号 −9.4%)、夏普最高;"
                 "但要如实知道——三个候选方案单笔均值差的 t 只有 0.22~0.49,"
                 "**统计上分不出高下**,这是一个判断,不是数据证明的最优解。"),
    }

    state = "观察中"
    if open_trade:
        state = "做空中" if open_trade["side"] == "short" else "做多中"

    # 机构方向本身要报出来,与「要不要进场」分开。运营者盯的就是这个拐点:
    # 做多支路虽然关着,但机构什么时候真的转成净多,他得第一时间看见。
    net_now = sig["net"].get(d, np.nan)
    net_ok = bool(np.isfinite(net_now))
    # 「刚转多」= 今天净多而 sig_win 天前还是净空,用来在界面上打一次提示
    prev_i = len(mkt) - 1 - RULES["sig_win"]
    prev_net = sig["net"].get(mkt.index[prev_i], np.nan) if prev_i >= 0 else np.nan
    institution = {
        "net": int(net_now) if net_ok else None,
        "side": ("net_long" if net_now > 0 else "net_short") if net_ok else None,
        "just_flipped_long": bool(net_ok and np.isfinite(prev_net)
                                  and net_now > 0 >= prev_net),
        "long_enabled": RULES["long_enabled"],
        # 关着做多时,z 上穿门槛只代表「机构在减空」,不产生进场——界面要说清,
        # 否则看的人会以为信号漏了。
        "long_signal_now": bool(np.isfinite(z) and z >= RULES["enter"]),
        # 机构卸了多少 —— **只显示,不进判据**,理由见 unload_state 的 docstring。
        "unload": unload_state(sig, seat, groups),
    }

    # 进场方向:**用进场那一路的信号**。方案 C 下 z_in 是共振后的散户信号,
    # 与上面展示的机构 z 常常不同号 —— DEC-104 的教训就在这里。
    _z_in, _ = entry_exit_signals(sig, rdf)
    _entry_side, _entry_blocked = entry_side(
        _z_in.get(d, np.nan), mkt["past"].get(d, np.nan))
    # 方案 C 下 `z_in = 散户 rz.where(共振)` —— **不共振时它也是 NaN**,
    # 被 `entry_side` 一律报成「信号未就绪」,而那两件事完全不同:
    # 预热期是「还没数」,背离是「有数但方向打架,再大也不进」。分开说。
    if _entry_blocked == "信号未就绪" and RULES["signal_source"] == "resonance"             and not resonate and np.isfinite(rz_now if rz_now is not None else np.nan):
        _entry_blocked = "机构与散户背离,方案 C 下不进场"
    # 成本进场(DEC-112)下 entry_side 只知道 z 过没过线,报不出是哪个状态条件
    # 挡的 —— 换成 attach_cost_signal 逐日记下的那条原因(带数字,能直接读)。
    if RULES["signal_source"] == "cost" and _entry_side == 0 and "cost_reason" in sig:
        _r = sig["cost_reason"].get(d)
        if isinstance(_r, str) and _r:
            _entry_blocked = _r
    # 交割窗口内只挡不进 —— 这一条 `entry_side` 不判(它不知道合约),这里补。
    _c_now = mkt["main"].get(d)
    if _entry_side != 0 and (not isinstance(_c_now, str)
                             or days_to_window_end(_c_now, d) <= RULES["exit_before_delivery"]):
        _entry_side, _entry_blocked = 0, "临近交割,窗口内只挡不进"

    SPLIT["v"] = edge_split(sig, mkt, rdf)
    wins = [t for t in closed if t["ret_pct"] > 0]

    # 与「躺着满仓做空」的对比。**这一栏必须摆在界面上**:三年单边熊市里,
    # 什么都不做地持有空单本身就有 +99% 的复利收益,不给基准,看的人会把
    # 策略的累计收益当成本事。策略真正赢的是夏普与回撤,不是绝对收益。
    # 逐日净值直接用 replay 产出的那条(DEC-090):它按结算价盯市、成交那两天算半天,
    # 连乘起来与逐笔记账完全相等。以前是在这里用 pos.shift(1)×结算价收益**另算**
    # 一条,两条对不上也没人会发现——夏普和回撤描述的是另一个策略。
    strat_daily = daily
    bench_daily = -mkt["ret"].fillna(0)
    return {
        "instrument": CURRENT["code"],
        "name": CURRENT["name"],
        "unit": CURRENT["unit"],
        "multiplier": RULES["multiplier"],
        "data_date": d.strftime("%Y-%m-%d"),
        "computed_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        # 本文件自身的指纹(DEC-099)。部署时会把安装到位那份的指纹写进
        # web/engine.json,前端两边一比就知道「这份信号是不是当前引擎算的」。
        # 为什么需要:页面读的是每日任务产出的**静态 JSON**,部署只换代码不重算
        # JSON,会出现「代码已上线、页面还是旧引擎算的数」而且看不出来
        # (2026-08-20 DEC-096 上线后就这样过了一夜)。部署现在会自动重算,
        # 这一条是那道自动化万一没跑成时的兜底 —— 让它露馅,而不是静静地错。
        "engine_fingerprint": _self_fingerprint(),
        "state": state,
        "contract": str(mkt["main"].get(d)),
        # 页面那一行显示的是**收盘价**(运营者 2026-08-21 指定)。
        # 注意它与下面所有盈亏字段不同源:盈亏一律用结算价算。
        "price": _f(mkt["close"].get(d)),
        # 散户交割纪律的当前读数(2026-08-19 运营者要求)。界面要能一眼看出
        # 「这个主力还能拿几天」——2026-08-14 玻璃主力还是 FG2609,只剩 11 个
        # 交易日,差一天就撞线,而页面当时对此只字不提。
        "delivery": {
            "window_end": window_end(mkt["main"].get(d)).strftime("%Y-%m-%d"),
            "days_left": int(mkt["dleft"].get(d, 0)),
            "limit": RULES["exit_before_delivery"],
            "must_exit": bool(mkt["dleft"].get(d, 99) <= RULES["exit_before_delivery"]),
        },
        "signal": {
            "z": None if not np.isfinite(z) else round(float(z), 2),
            "enter": RULES["enter"],
            "net": None if not np.isfinite(sig["net"].get(d, np.nan)) else int(sig["net"].get(d)),
            "change": None if not np.isfinite(sig["chg"].get(d, np.nan)) else int(sig["chg"].get(d)),
            "win": RULES["sig_win"],
            # 连续版的建议仓位强度:回测夏普比离散版更高(2.66 vs 2.26),
            # 但换手大、抗成本差,所以只作参考不作指令。
            "suggested_position": None if not np.isfinite(z) else round(float(np.clip(z, -2, 2)), 2),
            # **今天这个信号往哪边进** —— 运营者 2026-08-21:「触发信号要显示做多
            # 或者做空,一触发就显示」。由引擎算,前端不许自己推一遍(理由见
            # `entry_side`:DEC-104 就是前端自己推进场判据推错的)。
            # 用的是**进场那一路**的信号(方案 C 下是共振后的散户信号),
            # 不是上面那个 `z`(机构合计流向)—— 两者常常不同号。
            "entry_side": {-1: "short", 1: "long"}.get(_entry_side),
            "entry_blocked": _entry_blocked,
        },
        "position": open_trade,
        "institution": institution,
        "retail": retail_state,
        "members": members,
        "group_log": log[-8:],
        # 重选切点(2026-08-19 加):`group_log` 只记**换人**,阵容没变就不写。
        # 界面要能说「最近一次重选是哪天、换没换人」,否则看上去像三年没重选过。
        "reselect": {
            "last": next((c for c in reversed(cuts or []) if c <= d.strftime("%Y-%m-%d")), None),
            "next": next((c for c in (cuts or []) if c > d.strftime("%Y-%m-%d")), None),
            "changed_at": log[-1]["date"] if log else None,
        },
        "history": closed,
        "stats": {
            "trades": len(closed),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
            "avg_pct": round(float(np.mean([t["ret_pct"] for t in closed])), 2) if closed else None,
            "cum_pct": round((np.prod([1 + t["ret_pct"] / 100 for t in closed]) - 1) * 100, 1)
                       if closed else None,
            "short_trades": sum(1 for t in closed if t["side"] == "short"),
            "long_trades": sum(1 for t in closed if t["side"] == "long"),
            # 出场原因分布:策略方案页那句「实测 N 笔全部由 X 触发」由它生成,
            # 不写死——消退条件至今一次没触发过,但这是数出来的不是记住的。
            "exit_reasons": {r: sum(1 for t in closed if t["exit_reason"] == r)
                             for r in sorted({t["exit_reason"] for t in closed
                                              if t["exit_reason"]})},
        },
        "compare": {
            "strategy": _perf(strat_daily),
            "benchmark": _perf(bench_daily),
            "benchmark_name": "恒定满仓做空",
            "note": "同一段区间、同一口径(逐日复利,策略扣单边 0.05% 换手成本)。"
                    "做空的复利收益不是买入持有取反——价格跌 52.9% 对应做空 +99.2%。",
        },
        "rules": {k: v for k, v in RULES.items() if k not in ("alias",)},
        # 界面必须把这句话摆出来,不能让人以为多头信号和空头一样可信。
        # 数字一律**由实际回测结果生成**,不写死。上一版把 "+86.5% vs +99.2%"
        # 硬编码在这里,关掉做多支路后就成了错的——同一个事实两处维护,必栽。
        "caveats": _caveats(_perf(strat_daily), _perf(bench_daily), closed),
        "edge_split": SPLIT.get("v"),
        # 顶部醒目风险条(运营者 2026-08-19 要求)。门槛写死、数字实算,
        # 品种自己够不上门槛就没有条目——不是按品种硬编码的。
        "risk_flags": risk_flags(_perf(strat_daily), closed, strat_daily,
                                 _perf(bench_daily)),
    }


# ---------------------------------------------------------------- FG-SA 配对

def pair_fgsa(cache: dict, out_dir: Path) -> dict | None:
    """玻璃与纯碱的**相对**资金流向,预测 FG−SA 价差的走向。

    为什么单独做这一条:研究阶段发现,两品种流向之**差**比它们各自的绝对流向
    更有信息量(全样本 t=+5.43,比 FG 单品种 +2.96、SA 单品种 +4.41 都高),
    而平台的套利监控本来就盯着 FG-SA 这个组合——它现在只看价差位置与历史分位,
    没有「资金在往哪边调」这一维。

    口径:两品种各自取 alpha 前 5 席位的合计净持仓 5 日变化,**各自**减均值除标准差
    之后相减。必须各自标准化再减:两个品种的持仓量级差一倍以上,直接相减等于让
    量级大的那个说了算。

    信号为正 = 玻璃这边资金相对更强 → 价差(FG−SA)倾向走扩。
    """
    need = ("FG", "SA")
    if any(c not in cache for c in need):
        print("[pair] FG/SA 未都跑成,跳过配对信号", file=sys.stderr)
        return None
    zs = {}
    for c in need:
        chg = cache[c]["chg"]
        zs[c] = (chg - chg.rolling(RULES["z_win"], min_periods=60).mean()) /                 chg.rolling(RULES["z_win"], min_periods=60).std()
    joined = pd.concat([zs["FG"].rename("fg"), zs["SA"].rename("sa")], axis=1).dropna()
    if joined.empty:
        return None
    d = joined.index[-1]
    z = float(joined["fg"].iloc[-1] - joined["sa"].iloc[-1])
    payload = {
        "pair": "FG-SA",
        "data_date": d.strftime("%Y-%m-%d"),
        "computed_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "z": round(z, 2),
        "fg_z": round(float(joined["fg"].iloc[-1]), 2),
        "sa_z": round(float(joined["sa"].iloc[-1]), 2),
        "direction": "widen" if z > 0 else ("narrow" if z < 0 else "flat"),
        "note": "玻璃与纯碱的**相对**资金流向。正=玻璃这边资金相对更强,价差(FG−SA)"
                "倾向走扩;负=倾向收窄。**这是背景不是交易信号**——它预测的是价差方向,"
                "不含进出场与仓位。",
        "evidence": "全样本偏相关 +0.140(t=+5.43,N=1480),比 FG 单品种 +2.96、"
                    "SA 单品种 +4.41 都高;逐年 6 正 1 负;滚动样本外四个截点 "
                    "+3.31/+0.76/+4.74/+1.77 全正无反向;五档两端差 114 元/吨。",
        "caveats": [
            "**2026 年是负的**(t=−1.16,不显著)——当下正处在这个信号的哑火年份。",
            "五档里中间档不单调,它能分辨两端、说不清中间。",
            "预测的是**价差方向**,不是某一条腿的方向,也没有出场规则。",
        ],
    }
    out = out_dir / "pair_fgsa.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    print(f"[pair] {payload['data_date']} 写出 {out}  z={payload['z']} "
          f"({'走扩' if z > 0 else '收窄'})")
    return payload


def run_one(code: str, src: str, out_dir: Path) -> dict | None:
    """跑一个品种。失败只告警不抛——一个品种挂了不该拖垮其余两个。"""
    global CURRENT
    v = use(code)
    CURRENT = {"code": code, **v}
    try:
        if src == "csv":
            price_raw, seat_raw = load_from_csv(
                code, Path(os.environ.get("CSV_DIR", "../research/data")))
        else:
            price_raw, seat_raw = load_from_pg(
                code,
                os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
                os.environ.get("PG_USER", "futures_app"),
                os.environ.get("PG_DB", "futures_platform"))
        price = clean_price(price_raw)
        seat = clean_seat(seat_raw)
        mkt = main_series(price)
        op, st = contract_prices(price)
        mkt = mkt[mkt.index >= pd.Timestamp(RULES["replay_start"])]
        groups, log, cuts = rolling_groups(seat, price, mkt.index)
        sig = signal_series(seat, groups)
        if RULES["signal_source"] == "cost":
            sig = attach_cost_signal(sig, seat, mkt, groups)
        payload = build_payload(sig, mkt, seat, groups, log, cuts, op, st)
    except Exception as e:                      # noqa: BLE001
        print(f"[{code}] 失败,保留上一版:{e}", file=sys.stderr)
        return None
    SIG_CACHE[code] = sig

    out_path = out_dir / v["out"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)   # 原子替换,避免前端读到半截文件
    st = payload["stats"]
    print(f"[{code}] {payload['data_date']} 写出 {out_path}")
    print(f"  状态 {payload['state']} | z={payload['signal']['z']} | "
          f"席位组 {'、'.join(m['member'] for m in payload['members'])}")
    print(f"  历史 {st['trades']} 笔(空 {st['short_trades']}/多 {st['long_trades']}),"
          f"累计 {st['cum_pct']}%,胜率 {st['win_rate']}%")
    return payload


def main():
    src = os.environ.get("ENGINE_SOURCE", "pg")
    # HOG_OUT 保留兼容:老调用方传的是「生猪那个文件」的完整路径,取它的目录。
    legacy = os.environ.get("HOG_OUT")
    out_dir = (Path(legacy).parent if legacy
               else Path(os.environ.get("FLOW_OUT_DIR", "/opt/futures-platform/signals")))
    codes = [c.strip().upper() for c in
             os.environ.get("FLOW_CODES", "LH,FG,SA").split(",") if c.strip()]
    ok = 0
    for code in codes:
        if code not in VARIETIES:
            print(f"[{code}] 未在 VARIETIES 里配置,跳过", file=sys.stderr)
            continue
        if run_one(code, src, out_dir) is not None:
            ok += 1
    # FG-SA 配对信号:两个品种都跑成了才算。失败只告警,不影响单品种产物。
    if {"FG", "SA"} <= set(SIG_CACHE):
        try:
            pair_fgsa(SIG_CACHE, out_dir)
        except Exception as e:                  # noqa: BLE001
            print(f"[pair] 配对信号失败,保留上一版:{e}", file=sys.stderr)
    print(f"[flow] 完成 {ok}/{len(codes)} 个品种")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
