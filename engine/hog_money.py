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
import re
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
# 玻纯对冲簿状态卡(DEC-142)要用的跨品种小缓存:run_one 跑 FG/SA 时各存一份
# {ya: 永安主力净持仓, ret_open, main},pair_fgsa 末尾合成。与 SIG_CACHE 分开,
# 不动它的形状(pair z 的 cache[c]["chg"] 依赖 sig 帧原样)。
PAIR_EXTRA: dict = {}

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
    # 临近交割强平之后**不许原地续仓**(DEC-131,2026-08-23 运营者拍板):做多/成本这类
    # 状态型进场信号在强平那天多半还成立,引擎原来会立刻在新主力把同方向的仓续上
    # (生猪 8/14 平 LH2609、同日开 LH2611 @12,425)。运营者:除非触发**新的**进场信号。
    # 「新」= 该方向的进场信号至少消失过一天再出现;反方向不受限。
    "rearm_after_delivery": True,
    # 换月接力(DEC-147 候选):交割纪律出场当天若无任何真实出场理由,次日开盘在新
    # 主力同向接回。**默认关**,按品种在 VARIETIES 打开(回测证据先行);
    # 与 rearm_after_delivery 分工见 replay() 内注释。
    "roll_continue": False,
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
    # 五窗对照用的五大散户席位(DEC-151,2026-08-28 运营者指定,=席位页「散户席位」
    # 收藏组)。与 retail_seed(三家,进散户反向/压力表判据)**是两份名单**:
    # retail_seed 动判据不许随便加人(四家实测更差),这五家只做五窗展示对照。
    "retail_panel": ["东方财富", "方正中期", "徽商期货", "平安期货", "中信建投"],
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
    # 出场模式(DEC-117,由 use() 按品种注入):
    #   "retail" —— 四件套:散户翻向 / 止损 / 持满 / 临近交割(默认,四个品种);
    #   "inst"   —— 机构出场:机构组方向翻转或本轮卸仓 > cost_unload_max 就走,
    #               止损与临近交割保留,**关**散户翻向与持满(焦煤,五关全过)。
    # 同一套 inst 出场在其余四品种的出场体检里全输(REPORT_EXIT_CAMPAIGN_v1),
    # 所以它只能按品种开,不许当全局规则。
    "exit_mode": "retail",
    # 做多腿来源(DEC-118,由 use() 按品种注入):
    #   "flow"          —— 进场那一路信号为正且过门槛就做多(默认);
    #   "unload_bounce" —— **只**在「机构组净空 且 本轮已卸掉 ≥ long_unload_min」时做多,
    #                      进场那一路的正值一律压掉(不许顺带做多)。生猪用:博机构减空
    #                      之后那一周的反弹(REPORT_LH_LONG_v1:5 日 +1.5%,t 2.4;
    #                      20 日归零),运营者拍板用它联动生猪向上套利。
    "long_mode": "flow",
    "long_unload_min": 0.50,
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
        # **做多腿打开,但只由「机构净空且本轮卸仓≥50%」触发(DEC-118,2026-08-23
        # 运营者知情破例)**。流量 z≥1 的做多仍然不做(DEC-084:多头逐笔 −1.5%,
        # 重扫 0.02%/t 0.02,确实没东西)。
        # 依据:机构减空之后有**一周**反弹(5 日 +1.5%,t 2.4;20 日归零,REPORT_LH_LONG_v1);
        # 隔离后的做多腿 14 笔 均值 +1.06%/t 1.18,整体 +117.4%/夏普 2.14/回撤 −8.4%
        # (只做空 +87.4%/2.34/−4.2%)。**按预注册判据不过**(夏普降、回撤翻倍),
        # 运营者拍板上:用它联动生猪向上套利(博反弹,向上套利更安全)。不是验证通过。
        "long_enabled": True,
        "long_needs_dip": False,
        "long_mode": "unload_bounce",
        # **卸仓门槛 50% → 30%(DEC-127,2026-08-23 运营者拍板)**:固定 5 家里永安基本不卸,组 2026 最高只到 46%,
        # 50% 把做多腿关死。扫 25%~50%(run_lh_unload_min.py):曲面不单调、全是噪音级 —— 45% 只多 8/6 一笔(在一个点上
        # 调出来的数),40/35% 多放进 4 月那笔亏的,30% 有 6 笔(2026 做多累计 +2.3%、胜 50%、整体回撤 −3.2%→−11.0%),
        # 25% 多出的几笔是同一波拆单。运营者取 30%。**做多腿在 2026 任何门槛下都在盈亏平衡附近,价值不在单边。**
        "long_unload_min": 0.30,
        # **做多腿只从 2026-01-01 起开(DEC-124,2026-08-23 运营者拍板)**:2026 是磨底年,
        # 之前的年份单边熊市,做多腿 13 笔 −24.6% 全在 2023~2025;2026 年这条腿一次没触发。
        # 只做空 20 笔 +74.0%/夏普 1.83/回撤 −8.0%;开着全程 33 笔 +29.5%/0.72/−28.0%。
        # 套利页反弹窗口背景(bounce_long)照常算,不受此限。
        "long_since": "2026-01-01",
        # **固定席位名单(DEC-122,2026-08-23 运营者拍板)**:国泰君安/东证/东吴/永安/浙商。
        # 同一策略只换席位组回放:近一年 固定5家 +64.2%/夏普 3.94/回撤 −3.2%(10 笔 80%),
        # 滚动 alpha 组 +69.1%/3.22/−8.2%(16 笔 62%),固定4家(去永安) +57.0%/2.95/−11.7%。
        # 运营者取回撤小的那版。**全样本固定名单只有 +29.5%/0.72/−28.0%(滚动 +117.4%)**
        # —— 名单是按今天的认知挑的,回到 2024 年并不灵,这一条如实记在 DEC-122。
        # 选人准则对比(REPORT_LH_SEAT_PICK_v1):走前「大席位挑赚钱多」t 4.15~5.15,
        # 择时收益 5.93,无一准则在所有窗口占优。
        # **第五人 2026-08-31 由浙商换成南华(DEC-163,运营者拍板)**。
        # 起因是运营者看到浙商「当日未上榜」提问。重扫查实浙商两条硬伤:
        # 2026 年上榜率仅 52%(其余四家 100%)、净持仓中位 922 手(国泰君安 18,327、
        # 永安 19,664,差 20 倍)—— 占合计 1.8%,在与不在几乎不改变信号。
        # 它当初入选靠的是**择时收益**排名,而短线账户(9 天翻一次向)的 alpha
        # 天然好看,正是 DEC-122 想避开的那个准则的产物。
        # 换人回放(前四家不动,六个候选全测):浙商 全样本 +76.2%/夏普 1.66、
        # 近一年 +66.3%/3.27/回撤 −11.0%;**南华 +101.7%/2.12、+88.7%/4.38/−3.2%**
        # —— 两段夏普都最高(+0.46/+1.11),回撤从 −11.0% 收窄到 −3.2%,近一年胜率 77%。
        # 运营者先按「持仓高」点了国投(11,985 手),看过跨品种画像后改南华:
        # **南华在跨品种上多空分明(焦煤净多 2,312/玻璃净空 2,640/纯碱净空 2,568/
        # 黄金净多 1,138/白银净空 1,878),是在管组合风险而非单边赌,筹码位置更好**。
        # 行为对比也支持:南华生猪 95% 天数净空、**52 天才翻一次向**(浙商 9 天一次、
        # 多空各半、中位仅 922 手 —— 短线账户的择时收益天然好看,正是 DEC-122
        # 想避开的那个准则的产物)。跟一个两个月才换方向的席位,信号本就更稳。
        # 丑话:候选按 2026 的规模与盈亏挑,再用回放评它们天然占便宜;南华胜在
        # **全样本与近一年两段都赢**,而非只赢在挑选窗口。仍属事后择优,非 walk-forward。
        "fixed_members": ["国泰君安", "东证期货", "东吴期货", "永安期货", "南华期货"],
        "fixed_since": "2026-08-23",
        # **换月反弹提示(DEC-123,2026-08-23 运营者拍板「直接做」)**:主力剩 ≤22 个交易日
        # 且主力近 20 日跌 ≥5%(到期前被砸狠了)→ 提示买次主力 X+2、移动止盈出场。
        # 依据只有 2026 年:触发 2 次(03-31 买 LH2607 +3.9%/20 日、07-30 买 LH2611 +4.2%),
        # 砸得温和的 1 月/5 月周期正确跳过(次主力 −4.5%/−4.2%)。**两个样本,不是验证**,
        # 和 DEC-121 一样是按磨底年判断开的门;席位规则在 2026 单独评全负(REPORT_LH_LOWS_v1)。
        # 只是提示,不进引擎持仓,不算进回测。
        "roll_bounce": {"since": "2026-01-01", "dleft_max": 22, "drop_min": 0.05},
        # **移仓强制流压力表(DEC-136,2026-08-24)**:散户多头剩仓 + 剩时短 →
        # 近月对次主力承压(REPORT_ROLL_PRESSURE_v1:dleft≤20 锚点秩相关 −0.53,
        # 散户高剩仓组价差 −3.14%/88% 届在跌;**机构版被否**——机构能慢慢移仓,
        # 不构成单边强制流,真正"必须交易且无承接"的是散户多头)。
        # **只显示不进判据**:16 届样本、主断言被否后的第二枪、2609 刚出过反例。
        "roll_pressure": {"window": 30, "anchor": 20},
        # **策略切换为逐合约战役(campaign,DEC-133,2026-08-24 运营者拍板)**:
        # 左侧批次进场(逢跌加仓区间确认 + 价<=批次成本)x 聪明钱份额资格
        # (方向历史战役盈亏 >= 对侧25%,把"多头人格=套保接盘"挡在门外)x
        # 机构卸仓30%快出 x 交割纪律;多仓并行(逐合约独立)。
        # 回测(research/REPORT_DIP_COST_v1 第五轮 + run_smart_filter2.py):
        # 51 笔 简单加总 +118.8pp / 逐笔复利 +200.5% / 逐年全正 / 最差单笔 −4.1%,
        # 超过组内最赚钱席位(东吴 简单 +95.9%/复利 +122.7%)。
        # 丑话如实:安慰剂 p=0.159 —— 做空方向本身贡献大头,择时增量 51 笔上不显著;
        # 手数阈值按品种规模缩放(中位阵营峰值 10,644/64,800 手 = x0.164,四舍五入)。
        # 旧方案 C 的展示维度(机构流向卡/散户维度/卸仓反弹/换月提示)全部保留,
        # 只有**进出场与历史/统计/对比**换成 campaign 的产物。
        "strategy": "campaign",
        "campaign": {
            "add_min": 150.0,    # 逢跌加仓日的阵营净加门槛(手)= 焦煤1000 x 0.164
            "confirm": 800.0,    # 区间累计净加确认线(手)= 焦煤5000 x 0.164
            "gap": 3,            # 逢跌日相隔 <=gap 并为一段
            "tail": 10,          # 区间尾后仍可进场的天数
            "unload": 0.30,      # 阵营自进场峰值卸掉该比例 -> 出场
            "share": 0.25,       # 聪明钱份额资格:该向历史盈亏 >= 对侧 x share
            # **跟批加仓(DEC-135,2026-08-24 运营者拍板)**:同区间每多攒够一个
            # confirm 台阶算新一批,价格优于当前均价才跟加,最多 3 单位。
            # 缘起 LH2611:一枪版钉在机构位置最差的第一批(12,155),机构随后
            # 8/14/8/17 两批加到 12,3~12,4k;跟批后本笔三批均价 ~12,327 与机构齐平。
            # 回测(引擎口径):50 战役 13 场多单位,加总 +131.7->+140.4pp,
            # 复利 +231.4%->+261.5%,t 2.66->2.86,最差单笔 −4.1% 不变。
            "max_units": 3,
        },
        # 沉淀资金费率(DEC-151 补齐,2026-08-28 运营者供图逐格反推):常规 16%(=8%双边),
        # 临近交割 20%(LH2609 实测);LH2611/2701 两格验证命中。
        "sink": {"rate": 0.16, "near_rate": 0.20, "near_days": 22},
        "out": "hog_signals.json",
        "backtest": "固定5家 近一年 +64.2%/夏普 3.94/回撤 −3.2%(10 笔 80%);全样本 33 笔"
                    " +29.5%/0.72/−28.0%(2023-08 起)。滚动 alpha 组同策略 近一年 +69.1%/"
                    "3.22/−8.2%,全样本 +117.4%/2.14/−8.4%(DEC-122 换固定名单,知情破例)",
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
        # **手动换人(DEC-129,2026-08-23 运营者拍板)**:华泰 → 国泰君安,只管到下次重选(2026-10-01)。
        # 国泰君安 2025-10 重选时择时第 6(7.83 亿,差华泰 3 亿,因 2023 亏 9.93 亿);截至今天已升到第 5(13.77 亿)。
        # 同策略回放 2026 两组都是 0 笔(成本进场门自 2025-10 起没开过,见 DEC-129 如实记),换人对 2026 回测无影响。
        # since 写最近一个交易日 08-21(拍板日 08-23 是周日,写 08-23 要到 08-24 的数据才生效)。
        "group_overrides": [{"since": "2026-08-21", "replace": {"华泰期货": "国泰君安"}}],
        # 沉淀资金费率(DEC-151):18% 常规 / 临近交割 20%(交割月前约一个月),
        # 与运营者行情软件 2026-08-27 截图逐格对过(FG2701 52.47亿 等五格全中)。
        # 其余品种没配费率 → 窗头显示名义市值;要同款口径给一句费率即可。
        "sink": {"rate": 0.18, "near_rate": 0.20, "near_days": 22},
        # **第二引擎:跟永安(DEC-141,2026-08-24 运营者拍板)**。与现行引擎并列各管各仓,
        # 相关 +0.31,50/50 组合夏普 0.84 > 现行 0.64 > 永安 0.73,回撤 −26.4% 比两台
        # 单跑都浅(REPORT_FGSA_MODEL_v1)。为什么是永安:五家全测唯一过初筛
        # (夏普 0.78/正年 11/14),14 年样本安慰剂 p=0.000,Bonferroni ×5 后仍成立
        # ——这点**强于焦煤华泰**(华泰校正后 p≈0.17 靠 IC 旁证补)。T+2 夏普 0.77
        # 几乎不衰减,扣成本 +760.7%/0.73。翻转约 15 次/年,比华泰(22/年)还慢。
        # **丑话(note 照挂)**:①流向 IC t=1.72 不显著——它的信息在仓位水平不在
        # 5 日流向,缺华泰那种独立于回测的旁证,证据形态=「超长样本回测极强、旁证弱」;
        # ②2016 −34%、2022/2023 连亏两年(−18%/−17%);③单跑回撤 −46%,靠与主引擎
        # 组合才压浅。
        "follow_seat": {
            "member": "永安期货",
            "note": ("第二引擎,与主引擎并列、各管各的仓位(实测两者日收益相关 +0.31,"
                     "50/50 组合夏普 0.84 高于任一台单跑,回撤反而更浅)。规则:永安在"
                     "当日主力的可见净持仓方向,翻转→次日开盘反手,约每月一次多点。"
                     "**丑话**:永安流向 IC 不显著(t=1.72),信息在仓位水平,缺独立"
                     "旁证,14 年安慰剂 p=0.000(过五选一校正)是主要依据;"
                     "2016 −34%、2022-23 连亏两年(−18%/−17%)要扛得住;"
                     "单跑回撤 −46%,组合才浅。验收 REPORT_FGSA_MODEL_v1。"),
        },
        "out": "fg_signals.json",
        "backtest": "120 笔 净 +218.1%/胜率 54.2%/回撤 −31.6%/夏普 0.65"
                    "(2013-01 起,基准 −17.7%)(2026-08-22 换成本进场+还在加仓+轮龄≥2,"
                    "六关全过但属事后假设复验,见 REPORT_FG_AGE_v1 与 DEC-114;"
                    "流量信号时代 228 笔 +34.9%/0.21/−67.9% 见 DEC-111)",
    },
    "IH": {
        # **研究档(DEC-159 预备,2026-08-30 运营者:开始做 IH 跟随策略)**:
        # 只供 research/ 回测使用,引擎不产 IH payload、页面无 IH 策略页。
        # 上市 2015-04-16,席位=中金所官方三榜前 20(逐年零缺口,含增减量);
        # 会员名三代口径(裸名/(代客)/(经纪))由 clean_seat 的括号剥除统一。
        # 现金交割 —— 移仓压力表的机制前提(实物交割前的散户强制流)不存在。
        "name": "上证50 IH", "unit": "指数点", "multiplier": 300.0,
        "replay_start": "2015-04-16",
        "long_enabled": True,          # 股指双向对称,不设做空偏性
        "long_needs_dip": False,
    },
    "I": {
        # **只做跟随线,不做主引擎**(DEC-178,2026-09-02 运营者:「铁矿石也做一个
        # 资金跟随策略」)。铁矿石不在 FLOW_CODES 里,不走 run_one 那套阵营/z 分数
        # 流程 —— 那需要另立一整套主引擎研究,运营者要的是跟随。
        # 点值 100 吨/手、保证金 11%:运营者 2026-09-02 给的实盘数,不是我猜的。
        "name": "铁矿石 I", "unit": "元/吨", "multiplier": 100.0,
        "replay_start": "2023-08-30",   # 大商所席位数据里铁矿石的起点
        "long_enabled": True,
        "long_needs_dip": False,
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
        # 沉淀资金费率(DEC-151 补齐):常规 16%(=8%双边),临近交割 20%(SA2609 实测);
        # SA2611/2701 验证命中。
        "sink": {"rate": 0.16, "near_rate": 0.20, "near_days": 22},
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
        # **换月接力开(DEC-147 追加,2026-08-25 运营者:「鸡蛋也开」)**:鸡蛋月月换
        # 主力、纪律出场最频,漏仓比焦煤常见。回放:34→40 笔,+48.9%→+68.8%,
        # 夏普 1.23→1.48,回撤 −8.9%→−12.8%(加深一档如实记,来自 2025-11 那笔
        # 连环接力 −3.49%);8 笔接力大头在 +4.74/+3.21/+2.66;活体 8/17 空 JD2610。
        "roll_continue": True,
        # **移仓压力表·判据级+镜像(DEC-145,2026-08-25 运营者拍板)**。
        # 鸡蛋是这块证据最强的品种(REPORT_JD_MODEL_v1):27 届(样本最大),
        # ≤20/≤10 两档锚点秩相关同号 −0.32;判据 PIT 触发 11 届 +3.04%/胜 82%,
        # 未触发届仅 +0.41% —— 区分度是真的(焦煤"未触发也在跌"的基流问题不存在)。
        # mirror=True:散户净空届占 19%(生猪 0%),净空剩仓→到点买平托近月→
        # ⚡做多价差,4 触发 3 胜 —— 双向机制全平台首证。
        # 不配 step:鸡蛋主力序列不规则(28 届/3 年,+1/+2 月混着来),历届次主力
        # 用真实主力序列继任,当前届按 20 日均量选。window 25:鸡蛋一届只有六七周,
        # ≤30 档锚点实测无信息(秩相关 −0.04),窗口开太早全是噪音。
        "roll_pressure": {
            "window": 25, "anchor": 20, "criterion": True, "mirror": True,
            "note": ("散户(三家反向名单)带符号净剩仓到点必须离场:净多剩仓压近月、"
                     "净空剩仓托近月。鸡蛋 27 届实测(REPORT_JD_MODEL_v1,样本最大):"
                     "锚点剩≤20/≤10 日秩相关同号 −0.32,高剩仓组价差 −1.59%,低组 −0.42%。"
                     "**判据(DEC-145,双向)**:窗口内净多剩仓≥历届 Q3 → ⚡做空价差"
                     "(空近月多次主力);净空剩仓≤历届 Q1 → ⚡做多价差(多近月空次主力,"
                     "鸡蛋散户 19% 的届净空,镜像分支全平台首证 4 触发 3 胜);每届一次,"
                     "持到交割纪律日。PIT 回测触发 11 届 +3.04%/胜 82%,未触发届仅 "
                     "+0.41%(区分度真)。**丑话**:单轮测试;+10.2/+9.4 两届贡献大头;"
                     "镜像分支仅 4 例;证据等级同 DEC-137 知情上。"),
        },
        # 沉淀资金费率(DEC-151 补齐):常规 14%(=7%双边),临近交割 20%(jd2609 实测);
        # jd2610/2611 验证命中。
        "sink": {"rate": 0.14, "near_rate": 0.20, "near_days": 22},
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
        # **出场换成机构出场(DEC-117,2026-08-23 运营者拍板)**:机构组翻向或本轮
        # 卸仓 >30% 就走,止损/交割保留,关散户翻向与持满。五关全过
        # (REPORT_JM_INST_EXIT_v1):逐年 3/4、walk-forward +195% vs +70%、
        # 阈值 50% 仍 1.14、去抖 2 日仍 1.47、t +3.13。
        # **赢的方式不是「拿得更久」,是「机构一松手立刻走、再上手立刻跟」的高换手跟随**
        # (均持有 9→4 日、笔数 46→72),回撤因此砍半。来历是出场体检挑出来的,
        # 记账标事后假设复验;只在焦煤成立,其余四品种出场体检全输,不许外推。
        # 丑话:三年 72 笔、2025 一年贡献 +120%、换手翻倍实盘摩擦更多。
        "exit_mode": "inst",
        # 席位组:滚动按年重选(DEC-125 当日曾改固定 5 家又改回,DEC-126:固定名单弱于滚动的主因
        # 是失去「按年重选」本身,不是某一家;固定通道代码保留,配置不填即滚动)。
        # **第二引擎:跟华泰(DEC-139,2026-08-24 运营者拍板)**。与现行引擎并列,
        # 各管各的仓位——两者日收益相关 −0.07,50/50 组合夏普 1.82 > 单跑任一台
        # (现行 1.73 / 华泰 0.99)。**替换不成立,分散成立**(REPORT_JM_HUATAI_v1)。
        # 为什么是华泰:五家全测唯一逐年 4/4 全正,且有独立旁证(五家里唯一
        # 5 日流向 IC 显著者 t=2.32,REPORT_JM_SEAT_PICK_v1 勘误先于回测指认);
        # 东证 2 天一翻 T+1 跟不上,永安是仓位户方向无信息。
        # 闸门:安慰剂 p=0.034 / T+2 几乎不衰减(1.05→1.02)/ 扣成本 +150.6%/0.99。
        # **三条丑话**(页面照挂):①肥尾——66 段胜率 41%,利润的 130% 在 5 个长趋势段,
        # 要连吃十几段小亏;②五选一 Bonferroni p≈0.17,先验押在 IC 旁证上;
        # ③2026 年靠 6 月一波。
        # **换月接力开(DEC-147,2026-08-25 运营者指出 8/17 案)**:JM2609 纪律出场
        # +6.71% 当天机构零出货信号,2701 主升接不回来。接力条件与 DEC-131 的分工见
        # replay() 注释。回放:三年仅触发 2 次(+226.5%→+228.2%,夏普回撤不变)——
        # 对历史近中性,修的是机制:交割是日历,机构没撤就不该丢仓(七点第 7 条的
        # 对偶命题)。8/17 案接力后 = JM2701 多 @1528.5。
        "roll_continue": True,
        "follow_seat": {"member": "华泰期货"},
        # **移仓压力表·展示级(2026-08-24 运营者拍板,REPORT_JM_THREE_GAPS_v1)**。
        # criterion=False:焦煤**只显示不进判据**,与生猪(DEC-137 ⚡判据)不同——
        # 9 届样本高剩仓组 100% 届在跌(−1.86%),方向同生猪机制,但秩相关仅 −0.28、
        # ≤10 日锚点翻号、PIT 判据无区分度(未触发届也 −1.04%,赢的是近月普跌基流)。
        # step=4:焦煤主力 1/5/9 月,次主力 = +4 个月(生猪 +2,别共用默认值)。
        # 观察项:JM2701 散户当前净空(历届锚点全净多,镜像分支零样本),若 11/12 月
        # 锚点仍净空即镜像首个活体——到时是攒样本,不是改规则。
        "roll_pressure": {"window": 30, "anchor": 20, "step": 4, "criterion": False},
        # 沉淀资金费率(DEC-151 补齐):常规 24%(=12%双边);jm2609 剩 12 日仍 24%,
        # 该软件对焦煤不做临近抬升,near_rate 配同值。jm2610/2701 验证命中。
        "sink": {"rate": 0.24, "near_rate": 0.24, "near_days": 22},
        "out": "jm_signals.json",
        "backtest": "72 笔 净 +244.0%/胜率 59.7%/回撤 −13.9%/夏普 1.79"
                    "(2023-08 起,基准 +18.2%)(2026-08-23 开做多 + 机构出场,"
                    "见 DEC-116/117 与 REPORT_JM_INST_EXIT_v1;"
                    "只做空+四件套时代 21 笔 +64.2%/0.91/−14.7% 见 DEC-111;"
                    "DEC-125/126 固定 5 家试过又改回:近一年 +41.7% 对滚动 +68.0%)",
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
    RULES["exit_mode"] = v.get("exit_mode", "retail")
    # 换月接力(DEC-147 候选):按品种打开,默认关。
    RULES["roll_continue"] = bool(v.get("roll_continue", False))
    # 沉淀资金费率(DEC-151):配了才乘,没配窗头给名义市值。
    RULES["sink"] = dict(v["sink"]) if v.get("sink") else None
    RULES["long_mode"] = v.get("long_mode", "flow")
    RULES["long_unload_min"] = v.get("long_unload_min", 0.50)
    # 做多腿起始日(DEC-124):None = 全程;给了日期就只在该日起允许做多进场(之前只做空)。
    RULES["long_since"] = v.get("long_since")
    # 固定席位名单(DEC-122):None 走滚动重选;给了名单就整段回放都用这几家。
    RULES["fixed_members"] = list(v["fixed_members"]) if v.get("fixed_members") else None
    # 换月反弹提示(DEC-123):只有生猪配;None = 不出这块。
    RULES["roll_bounce"] = dict(v["roll_bounce"]) if v.get("roll_bounce") else None
    # 移仓压力表(DEC-136):只有生猪配;None = 不出这块。
    RULES["roll_pressure"] = dict(v["roll_pressure"]) if v.get("roll_pressure") else None
    # 单席位跟随第二引擎(DEC-139):只有焦煤配(华泰);None = 不出这块。
    RULES["follow_seat"] = dict(v["follow_seat"]) if v.get("follow_seat") else None
    # 手动换人(DEC-129):滚动组之上的点名替换,只管到下一次重选为止。None = 没有。
    RULES["group_overrides"] = [dict(o) for o in v["group_overrides"]] if v.get("group_overrides") else None
    # 逐合约战役策略(DEC-133):strategy="campaign" 的品种,进出场与历史/统计
    # 由 engine/campaign.py 产出;flow 系(方案C/成本)照旧。参数见 VARIETIES 注释。
    RULES["strategy"] = v.get("strategy", "flow")
    RULES["campaign"] = dict(v["campaign"]) if v.get("campaign") else None
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


CONTRACT_RE = re.compile(r"^([A-Za-z]+)(\d{4})$")


def split_contract(contract) -> tuple[str, str]:
    """把合约代码拆成(品种字母, 四位年月)。认不出来返回 ("", "")。

    **不要写 `contract[:2]` / `contract[2:]`**:铁矿石的品种代码是单字母 `I`,
    `"I2601"[2:]` 得到 `"01"` —— 主力换月那处拿它跟别人的 `"2601"` 比大小,
    比的是月份对年月,**不报错,只是静默算错**。2026-09-02 接铁矿石时才发现
    这套两字符切片在仓库里有三处(本文件两处 + `smart_money.main_contract`),
    而它们对现有的双字母品种恰好全对,所以一直没人踩到。
    """
    match = CONTRACT_RE.match(str(contract))
    return (match.group(1).upper(), match.group(2)) if match else ("", "")


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


def contract_volumes(price: pd.DataFrame) -> pd.DataFrame:
    """逐合约成交量(行=交易日,列=合约)。移仓压力表的"承接力"一栏用。"""
    return (price.pivot_table(index="trade_date", columns="contract",
                              values="volume", aggfunc="first").sort_index())


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
        return split_contract(c)[1]
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


def fixed_groups(members: list[str], seat: pd.DataFrame, price: pd.DataFrame,
                 dates: pd.DatetimeIndex, decided: str) -> tuple[pd.Series, list, list]:
    """固定名单版的 `rolling_groups`(DEC-122,2026-08-23 运营者拍板生猪用固定 5 家)。

    返回形状与 `rolling_groups` 一致,后面 `signal_series`/`build_payload` 不用分叉:
    逐日都是同一组;`log` 只有一条,日期写**拍板日**,括号里的择时收益按拍板日之前
    的数据算(只是给界面看这几家当时的成色,不参与选人);`cuts` 为空 —— 没有重选。
    **如实记**:固定名单是拿今天的认知挑的,放回 2024 年并不灵(全样本 +29.5%/回撤
    −28%,滚动组 +117%/−8.4%),运营者按近一年(+64%/夏普 3.94/回撤 −3.2%)拍板。
    """
    mem = tuple(members)
    ser = pd.Series([mem] * len(dates), index=dates, dtype=object)
    a = alpha_upto(seat, price, pd.Timestamp(decided) + pd.Timedelta(days=1))
    log = [{"date": decided, "members": list(mem),
            "alpha": {m: (round(float(a[m]) / 1e8, 2) if m in a.index else None) for m in mem}}]
    return ser, log, []


def apply_group_overrides(groups: pd.Series, log: list, cuts: list, overrides: list,
                          seat: pd.DataFrame, price: pd.DataFrame) -> tuple[pd.Series, list]:
    """运营者点名换人(DEC-129):`{"since": 日期, "replace": {旧: 新}}`,在滚动组之上,
    **只管到下一次重选切点为止** —— 切点一到,照常按择时收益重选,点名失效。
    这样既照运营者的判断换人,又不把「按年重选」这件事拆掉(DEC-126 的教训:固定名单弱的主因是失去重选)。
    换人那天写一条 log(带 manual=True),括号里是当时的择时收益,界面能看出这是手动换的。
    """
    g = groups.copy()
    for o in overrides:
        since = pd.Timestamp(o["since"])
        nxt = next((pd.Timestamp(c) for c in cuts if pd.Timestamp(c) > since), None)
        rep = dict(o["replace"])
        changed = None
        for d in g.index:
            if d < since or (nxt is not None and d >= nxt) or g[d] is None:
                continue
            new = tuple(rep.get(m, m) for m in g[d])
            if len(set(new)) != len(new):   # 新人本来就在组里 → 不重复,保留原组
                continue
            g[d] = new
            changed = changed or new
        if changed:
            a = alpha_upto(seat, price, since + pd.Timedelta(days=1))
            log.append({"date": since.strftime("%Y-%m-%d"), "members": list(changed),
                        "alpha": {m: (round(float(a[m]) / 1e8, 2) if m in a.index else None) for m in changed},
                        "manual": True, "replace": rep})
            log.sort(key=lambda x: x["date"])
    return g, log


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


def inst_exit_flags(sig: pd.DataFrame, mkt: pd.DataFrame, groups: pd.Series,
                    unload: pd.Series) -> pd.Series:
    """机构出场的逐日触发:机构组方向翻转(昨今都可见且不同)或本轮卸仓 > cost_unload_max。
    掉榜日(方向/卸仓为 NaN)不触发 —— 不知道 ≠ 走了。
    与 research/run_jm_inst_exit.py 的 flip | u30 **逐字同构**,闸门就是按它过的。"""
    cc = inst_cost_series(sig, mkt, groups)
    side = cc["side"].reindex(mkt.index)
    prev = side.shift(1)
    flip = (side.notna() & prev.notna() & (side != prev)).fillna(False)
    over = (unload.reindex(mkt.index) > RULES["cost_unload_max"]).fillna(False)
    return (flip | over).astype(bool)


def attach_inst_exit(sig: pd.DataFrame, seat: pd.DataFrame, mkt: pd.DataFrame,
                     groups: pd.Series) -> pd.DataFrame:
    """把机构出场触发(inst_exit)挂到 sig 上。exit_mode="inst" 的品种必须在
    replay 之前走这一步 —— replay 只认列。"""
    unload = unload_series(sig, seat, groups)["pct"]
    flags = inst_exit_flags(sig, mkt, groups, unload)
    return sig.assign(inst_exit=flags.reindex(sig.index).fillna(False))


def attach_bounce_long(sig: pd.DataFrame, seat: pd.DataFrame, mkt: pd.DataFrame,
                       groups: pd.Series) -> pd.DataFrame:
    """把「卸仓反弹做多」的触发列挂到 sig 上(DEC-118):
    bounce_long = 机构组净空 且 本轮已卸掉 ≥ long_unload_min;另带 bounce_unload(当日卸仓比例)
    与 bounce_side(当日机构方向)给页面说原因用。掉榜日两者 NaN → 不触发。
    与 research/run_lh_long2.py 的 (side<0)&(unl>=X) 逐字同构。"""
    unload = unload_series(sig, seat, groups)["pct"].reindex(mkt.index)
    cc = inst_cost_series(sig, mkt, groups)
    side = cc["side"].reindex(mkt.index)
    flag = ((side < 0) & (unload >= RULES["long_unload_min"])).fillna(False).astype(bool)
    return sig.assign(bounce_long=flag.reindex(sig.index).fillna(False),
                      bounce_unload=unload.reindex(sig.index),
                      bounce_side=side.reindex(sig.index))


def _apply_long_mode(z_in: pd.Series, sig: pd.DataFrame) -> pd.Series:
    """long_mode="unload_bounce" 时改写进场信号:进场那一路的正值压成 0(不许顺带做多),
    满足 bounce_long 且当天没有做空信号的日子注入 +(enter+0.5)。做空信号优先。"""
    if RULES.get("long_mode") != "unload_bounce":
        return z_in
    if "bounce_long" not in sig:
        raise ValueError("long_mode=unload_bounce 需要先 attach_bounce_long")
    amp = RULES["enter"] + 0.5
    z = z_in.where(~(z_in > 0), 0.0)
    inject = sig["bounce_long"].reindex(z.index).fillna(False).astype(bool) & ~(z <= -RULES["enter"])
    return z.where(~inject, amp)


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
        return _apply_long_mode(sig["z"], sig), sig["z"]
    # 用**标准化后的 z** 判共振,不用原始 chg。
    # chg 不需要预热,拿它判等于「聪明钱信号还没预热完成就先拿来用」——
    # 2026-08-19 对拍时抓到:席位组 2024-05-01 首次生成,z 要 60 个交易日才有值,
    # 而 chg 当天就有,于是 2024-05-17 凭一个尚不可用的信号开了一仓。
    # np.sign(NaN) 是 NaN、NaN==NaN 为 False,所以改用 z 之后预热期自动不进场。
    resonate = np.sign(sig["z"]) == np.sign(retail["rz"])
    # 生猪(DEC-118):做多只由「机构净空且卸仓≥50%」触发,共振后的正值不许顺带做多。
    return _apply_long_mode(retail["rz"].where(resonate), sig), retail["rz"]


def long_allowed(d: pd.Timestamp) -> bool:
    """做多腿是否在 d 这天开着(DEC-124):`long_since` 为空则全程开;否则 d ≥ long_since。"""
    since = RULES.get("long_since")
    return not since or pd.Timestamp(d) >= pd.Timestamp(since)


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
    # 机构出场模式(DEC-117):把研究用的两个钩子接到生产配置上 —— 触发序列来自
    # attach_inst_exit 挂在 sig 上的 inst_exit 列,关掉散户翻向与持满。
    # 理由记「机构出场」而不是「外部」,页面出场分布要能看出是谁触发的。
    inst_mode = RULES.get("exit_mode") == "inst"
    if inst_mode:
        if "inst_exit" not in sig:
            raise ValueError("exit_mode=inst 需要先 attach_inst_exit")
        extra_exit = sig["inst_exit"]
        disable_reverse = True
    exit_label = "机构出场" if inst_mode else "外部"
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
    # 临近交割强平后的「待重新触发」方向(DEC-131):0 = 不限;±1 = 该方向要等信号消失一天再出现。
    rearm = 0
    # 换月接力标记:本笔是从哪个合约接力来的(None = 正常进场)。
    roll_from = None
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
            elif (not inst_mode) and i - entry_i >= RULES["max_hold"]:
                reason = "持满"
            elif (not disable_reverse) and np.isfinite(z) and side * z <= -RULES["enter"]:
                reason = "反向"
            elif extra_exit is not None and bool(extra_exit.get(d, False)):
                reason = exit_label
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
                "rolled_from": roll_from,
                "_i": entry_i, "_j": i, "_c": entry_c, "_fill": j,
            })
            roll_from = None
            # 换月接力(DEC-147 候选,默认关):交割纪律是**日历事件不是观点**——
            # 运营者 2026-08-25 指出 JM2609 8/17 纪律出场 +6.71% 时机构零出货信号,
            # 趋势没走完的仓被日历砍掉后接不回来(进场端要等全新触发)。
            # 接力条件 = 当天除交割外**没有任何真实出场理由**(机构未触发/未止损/
            # 未反向/未消退),且新主力可进、有成交价 → 次日开盘同向接回,
            # 视作同一轮持仓的延续(trade 记 rolled_from)。
            # 与 DEC-131 的分工:131 管「进场信号驱动的续仓」(状态型信号没断不算
            # 新信号,那是**进场端**的事);接力由「出场条件未触发」驱动,是**出场端**
            # 的延续,只在配 roll_continue=True 的品种生效,两者不冲突。
            c_roll = main.get(d)
            can_roll = (reason == "临近交割" and RULES.get("roll_continue")
                        and isinstance(c_roll, str) and c_roll != entry_c
                        and days_to_window_end(c_roll, d) > RULES["exit_before_delivery"]
                        and np.isfinite(px(c_roll, i + 1, "open"))
                        and not (extra_exit is not None and bool(extra_exit.get(d, False)))
                        and cum > -RULES["stop"]
                        and not ((not disable_reverse) and np.isfinite(z)
                                 and side * z <= -RULES["enter"])
                        and not (np.isfinite(z) and abs(z) <= RULES["exit_z"]
                                 and side * z <= 0))
            if can_roll:
                roll_from = entry_c
                entry_i, entry_c = i, c_roll
                pending = None
                # side 保持;rearm 不设——接力不是新进场,轮不到 DEC-131 管。
            else:
                if reason == "临近交割" and RULES.get("rearm_after_delivery", True):
                    rearm = side
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
            # 做多腿起始日(DEC-124):之前的年份做多信号当不存在,只做空。
            if want == 1 and not long_allowed(d):
                want = 0
            # 临近交割强平后(DEC-131):同方向信号若一直没断,就不是新信号,不进;
            # 信号断过一天(want 不再是那个方向)即解除,下次出现照进。反方向不受限。
            if rearm != 0:
                if want != rearm:
                    rearm = 0
                else:
                    want = 0
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
            "exit_reason": None, "rolled_from": roll_from,
            "_i": entry_i, "_j": None, "_c": entry_c, "_fill": None,
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


def next_main_contract(contract: str, step: int = 2) -> str:
    """生猪合约月 1/3/5/7/9/11,次主力 = 月份 +2(跨年进位)。"""
    code, ym = split_contract(contract)
    y, m = int(ym[:2]), int(ym[2:])
    m += step
    while m > 12:
        m -= 12
        y += 1
    return f"{code}{y:02d}{m:02d}"


def roll_bounce_payload(mkt: pd.DataFrame, st: pd.DataFrame, cfg: dict) -> dict:
    """换月反弹提示(DEC-123)。**只是提示,不进持仓、不进回测。**

    条件:主力剩 ≤ dleft_max 个交易日 **且** 主力近 20 日(`past`,逐合约连乘)跌 ≥ drop_min。
    同一个主力合约只记首次触发。`history` 自 cfg["since"] 起(规则按 2026 磨底年判断开的门,
    之前的年份不展示);每条带次主力 X+2 触发日结算价、之后 20 日(或至今)涨跌。
    """
    since = pd.Timestamp(cfg["since"])
    d = mkt.index[-1]
    main = str(mkt["main"].get(d))
    dleft = int(mkt["dleft"].get(d, 0))
    past = mkt["past"].get(d, np.nan)
    active = bool(dleft <= cfg["dleft_max"] and np.isfinite(past) and past <= -cfg["drop_min"])
    nxt = next_main_contract(main)
    hist, seen = [], set()
    for dd, row in mkt[mkt.index >= since].iterrows():
        c = str(row["main"])
        if c in seen:
            continue
        if row["dleft"] <= cfg["dleft_max"] and np.isfinite(row["past"]) and row["past"] <= -cfg["drop_min"]:
            seen.add(c)
            n = next_main_contract(c)
            px0 = st[n].get(dd, np.nan) if n in st.columns else np.nan
            later = st[n].loc[dd:].dropna().iloc[:21] if n in st.columns else pd.Series(dtype=float)
            ret = (float(later.iloc[-1]) / float(px0) - 1) * 100 if len(later) > 1 and np.isfinite(px0) and px0 else None
            hist.append({"date": dd.strftime("%Y-%m-%d"), "main": c, "days_left": int(row["dleft"]),
                         "drop20": round(float(row["past"]) * 100, 1), "next": n,
                         "next_px": _f(px0), "next_ret20": None if ret is None else round(ret, 1),
                         "days_seen": max(0, len(later) - 1)})
    return {
        "active": active, "main": main, "days_left": dleft,
        "drop20": None if not np.isfinite(past) else round(float(past) * 100, 1),
        "dleft_max": cfg["dleft_max"], "drop_min": round(cfg["drop_min"] * 100, 1),
        "next": nxt, "next_px": _f(st[nxt].get(d, np.nan)) if nxt in st.columns else None,
        "since": cfg["since"], "history": hist,
    }


def _f(v):
    return None if v is None or not np.isfinite(v) else round(float(v), 1)


def roll_pressure_payload(seat: pd.DataFrame, mkt: pd.DataFrame, st: pd.DataFrame,
                          cfg: dict, vols: pd.DataFrame | None = None) -> dict:
    """移仓强制流压力表(DEC-136)。**只显示,不进任何判据。**

    机制(REPORT_ROLL_PRESSURE_v1):散户多头集中在近月、窗口止点必须离场、
    小资金无承接 → 近月相对次主力被压。散户净多剩仓越大,交割前价差跌得越狠
    (dleft≤20 锚点秩相关 −0.53;高剩仓组 −3.14%、88% 的届在跌;
    散户没剩仓的届价差反而涨)。**机构版被否**:机构能买平近月+卖开次月同步移,
    不构成单边强制流 —— 别把这块改成看机构剩仓,试过了没有预测力。

    历届分布**每次实算不写死**(数字写死会随规则漂移,DEC-098 的教训);
    当前届的散户剩仓与历届锚点分布(四分位)对照着显示,不拍二值结论。
    """
    d = mkt.index[-1]
    main = str(mkt["main"].get(d))
    # 次主力:配了 step 用月份算术(生猪 +2,焦煤 +4);没配(鸡蛋,主力序列不规则)
    # 当前届按 20 日均量在更远月里选,历届一律用真实主力序列的继任(见下)。
    if "step" in cfg:
        nxt = next_main_contract(main, int(cfg["step"]))
    else:
        nxt = None
        if vols is not None:
            def _ym(c):
                raw = "".join(ch for ch in str(c) if ch.isdigit())
                return int(raw) if raw else 0
            best_v = 0.0
            for c in vols.columns:
                if not isinstance(c, str) or c == main or _ym(c) <= _ym(main):
                    continue
                vv = vols[c].dropna().tail(20)
                mv = float(vv.mean()) if len(vv) else 0.0
                if mv > best_v:
                    nxt, best_v = c, mv
    dleft = int(mkt["dleft"].get(d, 0))
    mains_seq = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
    nxt_of = {c: mains_seq[i + 1] for i, c in enumerate(mains_seq[:-1])}
    have = [m for m in RULES["retail_seed"] if m in set(seat["member_key"])]

    def retail_net_on(contract: str, upto: pd.Timestamp) -> float | None:
        sub = seat[(seat["member_key"].isin(have)) & (seat["contract"] == contract)
                   & (seat["trade_date"] <= upto)]
        if sub.empty:
            return None
        w = (sub.pivot_table(index="trade_date", columns="member_key",
                             values="net_off", aggfunc="first").ffill())
        v = float(w.iloc[-1].sum())
        return v if np.isfinite(v) else None

    # —— 历届锚点分布与逐届结果(全量重算,幂等)——
    anchor = int(cfg.get("anchor", 20))
    hist = []
    seen = set()
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str) or c == main or c in seen:
            continue
        seen.add(c)
        # 锚点按**合约自己的**行情序列算:dleft<=anchor 时主力早已换到下一届,
        # 用主力时段找锚点一届都找不齐(首版就是这么错的,16 届只剩 6 届)。
        if c not in st.columns:
            continue
        px_c = st[c].dropna()
        dl = pd.Series([days_to_window_end(c, t) for t in px_c.index], index=px_c.index)
        hitrows = dl[(dl <= anchor) & (dl > 5)]
        if not len(hitrows):
            continue
        t0 = hitrows.index[0]
        r_net = retail_net_on(c, t0)
        if r_net is None:
            continue
        # 历届次主力 = 主力序列里的继任(生猪/焦煤下等价于月份算术;鸡蛋序列不规则,
        # 算术会指到从没当过主力的合约)。继任缺失(最新一届)退回算术。
        n = nxt_of.get(c) or (next_main_contract(c, int(cfg["step"])) if "step" in cfg else None)
        if n is None:
            continue
        move_pct = None
        if n in st.columns:
            spread = (st[c] - st[n]).dropna()
            endrows = dl[dl <= 5]
            t1 = endrows.index[0] if len(endrows) else px_c.index[-1]
            s0, s1 = spread.asof(t0), spread.asof(t1)
            base = px_c.asof(t0)
            if np.isfinite(s0) and np.isfinite(s1) and np.isfinite(base) and base:
                move_pct = round(float(s1 - s0) / float(base) * 100, 2)
        hist.append({"main": c, "date": t0.strftime("%Y-%m-%d"),
                     "retail_net": int(round(r_net)), "spread_move_pct": move_pct})
    nets = sorted(h["retail_net"] for h in hist)

    def q(arr, pctl):
        return float(np.percentile(arr, pctl)) if arr else None

    q1, med, q3 = q(nets, 25), q(nets, 50), q(nets, 75)

    cur_retail = retail_net_on(main, d)
    level = None
    if cur_retail is not None and med is not None:
        level = ("high" if q3 is not None and cur_retail >= q3
                 else "low" if q1 is not None and cur_retail <= q1 else "mid")
    vr = None
    if vols is not None and main in vols.columns:
        vv = vols[main].dropna()
        if len(vv) >= 20:
            vr = round(float(vv.rolling(5).mean().iloc[-1] / vv.rolling(20).mean().iloc[-1]), 2)
    spread_now = None
    if main in st.columns and nxt in st.columns:
        sn = (st[main] - st[nxt]).dropna()
        if len(sn):
            spread_now = _f(sn.iloc[-1])
    active = bool(dleft <= int(cfg.get("window", 30)))
    # 判据(DEC-137,运营者拍板):窗口内且散户剩仓处历届高位 -> ⚡压力进场·做空价差
    # (空近月多次主力);同窗口做多价差信号降级挂⚠。判据在引擎算,前端只渲染(DEC-104)。
    # criterion=False 的品种(焦煤)**展示级**:高位照标 level,⚡永不亮
    # (REPORT_JM_THREE_GAPS_v1:9 届样本判据无区分度,不许升级)。
    # mirror=True 的品种(鸡蛋,DEC-145):镜像分支——散户**净空**剩仓处历届低位
    # -> ⚡压力进场·做多价差(多近月空次主力,被迫方到点买平托近月)。
    # 生猪不配 mirror:历届零净空样本,分支无从验证(REPORT_ROLL_PRESSURE_v1)。
    crit = cfg.get("criterion", True)
    entry_flag = bool(active and level == "high" and crit
                      and cur_retail is not None and cur_retail > 0)
    entry_flag_long = bool(cfg.get("mirror") and active and level == "low" and crit
                           and cur_retail is not None and cur_retail < 0)
    return {
        "active": active,
        "entry_flag": entry_flag,
        "suppress_long": entry_flag,
        # 镜像分支(DEC-145,只有配 mirror 的品种可能为 True):⚡做多价差。
        "entry_flag_long": entry_flag_long,
        "suppress_short": entry_flag_long,
        "main": main, "next": nxt, "days_left": dleft,
        "window": int(cfg.get("window", 30)),
        "retail_net": None if cur_retail is None else int(round(cur_retail)),
        "hist_q1": None if q1 is None else int(round(q1)),
        "hist_med": None if med is None else int(round(med)),
        "hist_q3": None if q3 is None else int(round(q3)),
        "level": level,
        "vol_ratio": vr,
        "spread_now": spread_now,
        "anchor": anchor,
        "history": hist,
        # 研究引证是品种专属的:配了 note 用配置的(鸡蛋),否则按 criterion 落回
        # 生猪判据版/焦煤展示版。统计数字(分位/历届表)本身全实算。
        "note": cfg.get("note") or (
                 "散户(三家反向名单)带符号净剩仓到点必须离场:净多剩仓压近月、"
                 "净空剩仓托近月。焦煤 9 届实测(REPORT_JM_THREE_GAPS_v1):高剩仓组"
                 "价差 −1.86%(100% 的届在跌),低组 −0.25%,方向同生猪机制;"
                 "但秩相关仅 −0.28(生猪 −0.53)、剩≤10 日锚点翻号,且判据无区分度"
                 "(未触发届也平均 −1.04%,赢的是焦煤近月交割前普跌的基流)。"
                 "**展示级:只显示,不进任何判据,⚡不亮**。历届锚点散户全为净多,"
                 "净空分支零样本——JM2701 当前净空是首个潜在镜像活体,先攒样本。"
                 if not cfg.get("criterion", True) else
                 "散户多头(三家反向名单)集中在近月、窗口止点必须离场、小资金无承接,"
                 "近月相对次主力被压。历届锚点(剩≤%d日)实测:散户剩仓与其后价差变动"
                 "秩相关 −0.53,高剩仓组价差 −3.14%%(88%% 的届在跌),低剩仓组 −0.36%%;"
                 "散户没剩仓的届价差反而涨。**机构剩仓没有预测力(机构能慢慢移仓),"
                 "别把这块改成看机构**。**DEC-137 已升级为判据**:窗口内散户剩仓处历届"
                 "高位 → ⚡压力进场·做空价差(空近月多次主力),每届一次,持到交割纪律日;"
                 "PIT 回测 7 触发/12 可判 +2.93%%/胜 86%%,未触发届 −0.88%%;同窗口做多"
                 "价差信号按 ⚠ 对待。丑话:7 次触发 6 次集中在 2025-26(散户参与度抬升的"
                 "时代效应),上一届 2609 亏过 −2.24%%;16 届样本,性质同 DEC-121 知情上。"
                 % anchor),
    }


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


def zone_band(hi: pd.Series, lo: pd.Series, last: float, days: int = 5) -> dict | None:
    """筹码地图的两条带(DEC-152,运营者 2026-08-28 口径)。

    **低带**(5 日最低两天的低点)= 多单进场 / 空单出场;**高带**(最高两天的高点)=
    空单进场 / 多单出场。

    带的两端**都是盘面真出现过的价**,不是算出来的百分比 —— 第一版拿"高低差×20%"
    凑带宽,运营者当场否掉(2026-08-28):「你的筹码位置有点怪,因为大部分时间到不了
    924 以上,基本都能到 900 附近,可以看 5 日的最低最高价」。

    **口径三改(同日定稿,运营者「按你的建议定」)——两条带有意不对称**:
      · **低带 = 第 3、4 低的低点**(FG2701 = 899~906):它是多单进场/空单出场,
        要的是**确定接得到货**,挂在插针价(最低那天)上等于不成交。
      · **高带 = 最高两天的高点**(FG2701 = 926~932):它是空单进场/多单出场,
        进场挂在**更优的一侧**没有损失 —— 挂不上只是不成交,挂上就是最优筹码。
    这一版正好复现运营者手算的 900~905 / 922~932(盘面无 900、905 整数价)。
    极值仍在 high/low 里给,逐日明细在 lows/highs 里给(页面列出来,要挪档一眼可指)。
    成本锚(机构最优空/多成本)在前端叠加:成本走净持仓引擎,引擎侧拿不到(DEC-143)。
    """
    h = hi.dropna().tail(days)
    lw = lo.dropna().tail(days)
    if len(h) < 2 or len(lw) < 2:
        return None
    lows = sorted(float(x) for x in lw)                    # 升序:最低在前
    highs = sorted((float(x) for x in h), reverse=True)    # 降序:最高在前
    if not (np.isfinite(lows[0]) and np.isfinite(highs[0])) or highs[0] <= lows[0]:
        return None
    # 低带优先第 3、4 低;天数不够就逐级退回,不硬造价位。
    if len(lows) >= 4:
        lo_band = [lows[2], lows[3]]
    elif len(lows) == 3:
        lo_band = [lows[1], lows[2]]
    else:
        lo_band = [lows[0], lows[1]]
    hi_band = [highs[1], highs[0]]                         # 高带恒取最高两天
    return {
        "days": int(min(len(h), len(lw))),
        "high": round(highs[0], 1), "low": round(lows[0], 1),
        "high_band": [round(min(hi_band), 1), round(max(hi_band), 1)],
        "low_band": [round(min(lo_band), 1), round(max(lo_band), 1)],
        # 逐日明细(低点升序 / 高点降序):页面列出来供运营者核口径。
        "lows": [round(x, 1) for x in lows],
        "highs": [round(x, 1) for x in highs],
        "last": None if not np.isfinite(last) else round(float(last), 1),
    }


def contracts_panel(seat: pd.DataFrame, grp: list, d: pd.Timestamp,
                    prev_d: pd.Timestamp | None, limit: int | None = None,
                    retail: list | None = None,
                    oi: pd.DataFrame | None = None, st: pd.DataFrame | None = None,
                    mult: float | None = None, sink_cfg: dict | None = None,
                    hi: pd.DataFrame | None = None, lo: pd.DataFrame | None = None,
                    close: pd.DataFrame | None = None) -> list[dict]:
    """合约小窗(DEC-134;DEC-151 改版,运营者 2026-08-28):**全部活跃合约开窗**
    (不再截 5 个),每窗:机构 5 家 vs 五大散户席位(retail_panel)对照,窗头挂
    沉淀资金。成本仍由前端走净持仓接口取(DEC-143 口径)。

    活跃 = 近 7 个自然日内(机构组或散户五家)任何一家在该合约上有行,且未过窗口
    止点;到期/看不到持仓自动滑出,新合约有行了自动补上。
    沉淀资金 = 全市场持仓 × 结算 × 点值 × 保证金率(sink_cfg 配了费率才乘;
    临近交割 near_days 内用 near_rate;没配费率给名义市值,rate=None 标明)。
    费率是配置的近似,不是交易所实时费率 —— FG 18%/20% 与运营者行情软件逐格对过。"""
    retail = retail or []
    everyone = list(dict.fromkeys(list(grp) + retail))
    sub = seat[seat["member_key"].isin(everyone)]
    recent = sub[sub["trade_date"] >= d - pd.Timedelta(days=7)]
    cands = []
    for c in recent["contract"].unique():
        if not isinstance(c, str):
            continue
        if days_to_window_end(c, d) <= 0:
            continue
        cands.append(c)
    cands.sort(key=lambda c: c[-4:])
    if limit is not None:
        cands = cands[:limit]

    def member_row(today, prev, m):
        now_rows = today[today["member_key"] == m]
        was_rows = (prev[prev["member_key"] == m] if prev is not None else None)
        now = float(now_rows["net"].sum()) if len(now_rows) else 0.0
        was = (float(was_rows["net"].sum())
               if was_rows is not None and len(was_rows) else np.nan)

        # 逐腿变化(DEC-149):某腿任一天掉榜/帧缺腿列 = 不可知给 None。
        def leg_chg(col: str):
            if col not in now_rows.columns or (was_rows is not None and col not in was_rows.columns):
                return None
            if not len(now_rows) or was_rows is None or not len(was_rows):
                return None
            a = now_rows[col].sum(min_count=1)
            b = was_rows[col].sum(min_count=1)
            if not (np.isfinite(a) and np.isfinite(b)):
                return None
            return round(float(a - b))

        return {
            "member": m,
            "net": round(now),
            "change": None if not np.isfinite(was) else round(now - was),
            "change_long": leg_chg("long_q"),
            "change_short": leg_chg("short_q"),
            "on_board": bool(len(now_rows)),
        }

    out = []
    for c in cands:
        today = sub[(sub["trade_date"] == d) & (sub["contract"] == c)]
        prev = (sub[(sub["trade_date"] == prev_d) & (sub["contract"] == c)]
                if prev_d is not None else None)
        members = [member_row(today, prev, m) for m in grp]
        retail_rows = [member_row(today, prev, m) for m in retail]
        # 沉淀资金(DEC-151):量价缺哪个都不硬算,置 None 前端不显示。
        sink = None
        if oi is not None and st is not None and mult and c in oi.columns and c in st.columns:
            o_ = oi[c].asof(d)
            px_ = st[c].asof(d)
            if np.isfinite(o_) and np.isfinite(px_) and o_ > 0 and px_ > 0:
                notional = float(o_) * float(px_) * float(mult)
                rate = None
                if sink_cfg and sink_cfg.get("rate"):
                    near = days_to_window_end(c, d) <= int(sink_cfg.get("near_days", 22))
                    rate = float(sink_cfg.get("near_rate", sink_cfg["rate"]) if near
                                 else sink_cfg["rate"])
                amount = notional * rate if rate else notional
                sink = {"yi": round(amount / 1e8, 2), "rate": rate,
                        "oi": int(o_)}
        # 筹码地图两条带(DEC-152):只用该合约自己的 5 日 K 线,取不到就 None。
        zones = None
        if hi is not None and lo is not None and c in hi.columns and c in lo.columns:
            last_px = np.nan
            if close is not None and c in close.columns:
                last_px = close[c].asof(d)
            if not np.isfinite(last_px) and st is not None and c in st.columns:
                last_px = st[c].asof(d)
            zones = zone_band(hi[c].loc[:d], lo[c].loc[:d], last_px)
        out.append({"contract": c,
                    "days_left": int(days_to_window_end(c, d)),
                    "sink": sink,
                    "zones": zones,
                    "members": members,
                    "retail": retail_rows})
    return out


def seat_follow_payload(seat: pd.DataFrame, mkt: pd.DataFrame, cfg: dict) -> dict:
    """单席位跟随第二引擎(DEC-139,焦煤跟华泰)。

    规则(零参数,验收见 REPORT_JM_HUATAI_v1):该席位在**当日主力合约**上的可见
    净持仓(net_off,按合约 ffill)方向;收盘定、次日开盘反手。与现行引擎并列
    显示、各管各的仓位 —— 相关 −0.07,是分散不是替换。
    统计每次全量重算(幂等),不写死数字。
    """
    member = cfg["member"]
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    net_now_s = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        wf = w.reindex(days.union(w.index)).ffill().reindex(days)
        sig.loc[days] = wf.values
        net_now_s.loc[days] = wf.values
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    pos = pos.ffill()
    adjo = (1 + mkt["ret_open"].fillna(0)).cumprod()
    # 翻转与逐段
    flips = []       # (下标, 新方向)
    prev = None
    for i, d in enumerate(mkt.index):
        p_ = pos.iloc[i]
        if not np.isfinite(p_):
            continue
        if prev is None or p_ != prev:
            flips.append((i, int(p_)))
        prev = p_
    runs = []
    for (i, s_), nxt in zip(flips, flips[1:] + [None]):
        j = nxt[0] if nxt else len(mkt.index) - 1
        a = float(adjo.iloc[min(i + 1, len(adjo) - 1)])
        b = float(adjo.iloc[min(j + 1, len(adjo) - 1)])
        runs.append({"date": mkt.index[i].strftime("%Y-%m-%d"),
                     "side": "long" if s_ > 0 else "short",
                     "contract": str(mkt["main"].iloc[i]),
                     "entry_px": _f(mkt["open"].iloc[min(i + 1, len(mkt) - 1)]),
                     "hold_days": j - i,
                     "ret_pct": round(s_ * (b / a - 1) * 100, 2),
                     "open": nxt is None})
    # 当前段浮动按最新结算补一段(开→结算)
    if runs and runs[-1]["open"] and len(flips):
        i, s_ = flips[-1]
        a = float(adjo.iloc[min(i + 1, len(adjo) - 1)])
        b = float(adjo.iloc[-1])
        o2c = mkt["o2c"].iloc[-1]
        b_mark = b * (1 + (o2c if np.isfinite(o2c) else 0.0))
        runs[-1]["ret_pct"] = round(s_ * (b_mark / a - 1) * 100, 2)
    # 出场日/出场价 = 下一段的翻向日/进场价——反手在同一个次日开盘上完成,老仓的
    # 出场成交与新仓的进场成交是同一口价(DEC-150 二改:历史页与主引擎同规格)。
    # 最后一段持有中为 None。
    for r_, nx_ in zip(runs, runs[1:]):
        r_["exit_date"], r_["exit_px"] = nx_["date"], nx_["entry_px"]
    if runs:
        runs[-1]["exit_date"] = runs[-1]["exit_px"] = None
    # 扣成本净值(单边 0.05%,翻转日双边)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    daily = (pos.shift(2) * mkt["ret_open"] - turn * 0.001).dropna()
    eq = (1 + daily).cumprod()
    stats = {
        "cum_pct": round((float(eq.iloc[-1]) - 1) * 100, 1),
        "sharpe": round(float(daily.mean() / daily.std() * np.sqrt(242)), 2) if daily.std() > 0 else None,
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
        "flips": len(flips),
        "yearly": {str(y): round((float(np.prod(1 + g)) - 1) * 100, 1)
                   for y, g in daily.groupby(daily.index.year)},
    }
    cur_side = pos.iloc[-1] if np.isfinite(pos.iloc[-1]) else None
    prev_side = pos.iloc[-2] if len(pos) > 1 and np.isfinite(pos.iloc[-2]) else None
    return {
        "member": member,
        "side": None if cur_side is None else ("long" if cur_side > 0 else "short"),
        "net": None if not np.isfinite(net_now_s.iloc[-1]) else int(net_now_s.iloc[-1]),
        "run_days": runs[-1]["hold_days"] if runs else None,
        "run_ret_pct": runs[-1]["ret_pct"] if runs else None,
        "entry_date": runs[-1]["date"] if runs else None,
        "entry_px": runs[-1]["entry_px"] if runs else None,
        "flipped_today": bool(cur_side is not None and prev_side is not None
                              and cur_side != prev_side),
        # 全量段(DEC-150 二改,运营者:「所有的信号…全部一样」):历史页翻页看全史;
        # 今日卡仍只取尾 8。67 段量级,payload 增量无害。
        "history": runs,
        "stats": stats,
        # 研究引证(相关/组合夏普/丑话)是品种专属的,配置给 note 就用配置的;
        # 不给就落回焦煤华泰版(DEC-139 首发品种)。统计数字本身全部实算不写死。
        "note": cfg.get("note") or (
                 "第二引擎,与主引擎并列、各管各的仓位(实测两者日收益相关 −0.07,"
                 "50/50 组合夏普 1.82 高于任一台单跑)。规则:华泰在当日主力的可见净持仓"
                 "方向,翻转→次日开盘反手,约每月两次。**丑话**:66 段胜率仅 41%、"
                 "利润集中在少数长趋势段(前 5 段占 130%),要连吃十几段小亏不动摇;"
                 "五选一有选择偏差(先验靠华泰流向 IC t=2.32 独立指认);"
                 "2026 年主要靠 6 月一波。验收 REPORT_JM_HUATAI_v1。"),
    }


def build_payload(sig: pd.DataFrame, mkt: pd.DataFrame, seat: pd.DataFrame,
                  groups: pd.Series, log: list, cuts: list | None = None,
                  op: pd.DataFrame | None = None, st: pd.DataFrame | None = None,
                  vols: pd.DataFrame | None = None,
                  oi: pd.DataFrame | None = None,
                  hi: pd.DataFrame | None = None, lo: pd.DataFrame | None = None,
                  close: pd.DataFrame | None = None) -> dict:
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
    # 卸仓反弹做多(DEC-118)下,做多条件不在 z 里,原因要另说:机构没净空 / 卸仓没到。
    if RULES.get("long_mode") == "unload_bounce" and _entry_side == 0 and "bounce_unload" in sig:
        _bs = sig["bounce_side"].get(d, np.nan)
        _bu = sig["bounce_unload"].get(d, np.nan)
        _why = ("机构未净空" if not (np.isfinite(_bs) and _bs < 0)
                else f"机构净空但本轮只卸掉 {_bu:.0%}" if np.isfinite(_bu)
                else "机构掉榜看不清")
        _entry_blocked = f"{_entry_blocked};做多需机构净空且本轮卸掉≥{RULES['long_unload_min']:.0%}({_why})"
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
    payload = {
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
        # 生猪(DEC-118/119):「卸仓反弹」窗口的当日状态,给套利监控页当背景。
        # 只在 long_mode=unload_bounce 的品种上有;其余品种 None,前端据此不画。
        "bounce_long": ({
            "active": bool(sig["bounce_long"].get(d, False)),
            # 4 位小数:_f 只留 2 位,0.4975 会被写成 0.50,页面就会说「只卸掉 50%,未到 50%」。
            "unload": (round(float(sig["bounce_unload"].get(d)), 4)
                       if np.isfinite(sig["bounce_unload"].get(d, np.nan)) else None),
            "side": ({-1: "net_short", 1: "net_long"}.get(int(sig["bounce_side"].get(d)))
                     if np.isfinite(sig["bounce_side"].get(d, np.nan)) else None),
            "min": RULES["long_unload_min"],
            # 文案里的数字必须跟着配置走(DEC-127):原验证是滚动组 + 50%(窗口内 20 日 +107 元/吨、
            # 涨 53%,REPORT_LH_SPREAD_SIGNAL_v1);改固定 5 家后复验,窗口内反而弱于无条件 ——
            # 50%:−315 元/吨、涨 13%(85 天);30%:−177 元/吨、涨 29%(254 天);无条件 −123、37%。
            # 这段话是写给看页面的人的,不能再引用已失效的 +107/53%。
            "note": "机构席位组净空时,写「本轮已卸掉多少 + 反弹参考区间 + 价差高位/低位」(DEC-128)。"
                    "反弹参考区间 = 2026 年历次生猪跨月价差**触底**那天机构已卸掉的比例:17 次独立触底日 "
                    "最小 0% / 四分位 10%~26% / 中位 15% / 最大 33%(固定 5 家席位)。**触底时的卸仓比例与"
                    "平时分布(四分位 8%~25%)差不多,它标不出底部** —— 价差拐头领先机构减仓约 7 日(DEC-121),"
                    "这里只是把历次触底时的卸仓范围写给人看。只有 2026 一年;磨底年过去要回头重验。"
                    "背景,不是进场信号。",
        } if "bounce_long" in sig else None),
        # 逐日历史(DEC-120):套利监控页选了别的交易日,要显示**那天**的窗口状态,
        # 不能一直显示最新的 —— 运营者要拿它手动验证信号。每天一行,PIT 口径
        # (当天可见的机构方向与卸仓比例)。掉榜日 unload/side 为 None。
        "bounce_history": ([
            {"d": dd.strftime("%Y-%m-%d"),
             "active": bool(a),
             "unload": (round(float(u), 4) if np.isfinite(u) else None),
             "side": ({-1: "net_short", 1: "net_long"}.get(int(sd)) if np.isfinite(sd) else None)}
            for dd, a, u, sd in zip(sig.index, sig["bounce_long"].fillna(False),
                                    sig["bounce_unload"], sig["bounce_side"])
        ] if "bounce_long" in sig else None),
        "institution": institution,
        "retail": retail_state,
        # 换月反弹提示(DEC-123):只有生猪配了 roll_bounce;其余品种 None。
        "roll_bounce": (roll_bounce_payload(mkt, st, RULES["roll_bounce"])
                        if RULES.get("roll_bounce") and st is not None else None),
        # 移仓压力表(DEC-136,只有生猪配):散户多头剩仓 → 近月承压。只显示。
        "roll_pressure": (roll_pressure_payload(seat, mkt, st, RULES["roll_pressure"], vols)
                          if RULES.get("roll_pressure") and st is not None else None),
        # 单席位跟随第二引擎(DEC-139,只有焦煤配):跟华泰,与主引擎并列各管各仓。
        "seat_follow": (seat_follow_payload(seat, mkt, RULES["follow_seat"])
                        if RULES.get("follow_seat") else None),
        "members": members,
        # 合约小窗(DEC-134→146→151):全部活跃合约开窗,机构 vs 五大散户对照,
        # 窗头沉淀资金;括号变化=较上一交易日(DEC-146),「组内各家」摘要卡仍是
        # sig_win 日口径,两卡口径**有意不同**。
        "contracts_panel": contracts_panel(
            seat, grp, d, mkt.index[-2] if len(mkt) > 1 else None,
            retail=[m for m in RULES.get("retail_panel", []) if m in set(seat["member_key"])],
            oi=oi, st=st, mult=RULES.get("multiplier"), sink_cfg=RULES.get("sink"),
            hi=hi, lo=lo, close=close),
        # 选人方式(DEC-122):rolling=按择时收益滚动重选;fixed=运营者拍板的固定名单。
        # 界面「换人历史」「怎么算的」两处文案都看它,别再各写一套。
        "group_mode": "fixed" if RULES.get("fixed_members") else "rolling",
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
    # —— 逐合约战役策略(DEC-133):进出场/历史/统计/对比换成 campaign 的产物 ——
    # 上面按旧口径算完的展示维度(机构流向卡、散户维度、卸仓反弹、换月提示、
    # 席位组)**原样保留**——它们是运营者每天看的背景信息,与哪套策略在交易无关。
    if RULES.get("strategy") == "campaign" and RULES.get("campaign"):
        import campaign as _campaign
        camp = _campaign.run(seat, mkt,
                             op if op is not None else pd.DataFrame(),
                             st if st is not None else pd.DataFrame(),
                             list(groups.get(d) or ()), RULES)
        c_trades = camp["trades"]
        c_open = [t for t in c_trades if t["exit_date"] is None]
        c_closed = [t for t in c_trades if t["exit_date"] is not None]
        c_daily = camp["daily"]
        c_wins = [t for t in c_closed if t["ret_pct"] > 0]
        sides = {t["side"] for t in c_open}
        payload["state"] = ("观察中" if not c_open else
                            ("做空中" if sides == {"short"} else
                             "做多中" if sides == {"long"} else "多空并持")
                            + (f"×{len(c_open)}" if len(c_open) > 1 else ""))
        # 状态条仍用单笔 position(取最新进场那笔);完整清单在 campaign.positions。
        payload["position"] = (sorted(c_open, key=lambda t: t["entry_date"])[-1]
                               if c_open else None)
        payload["history"] = c_closed
        payload["stats"] = {
            "trades": len(c_closed),
            "win_rate": round(100 * len(c_wins) / len(c_closed), 1) if c_closed else None,
            "avg_pct": round(float(np.mean([t["ret_pct"] for t in c_closed])), 2) if c_closed else None,
            "cum_pct": round((np.prod([1 + t["ret_pct"] / 100 for t in c_closed]) - 1) * 100, 1)
                       if c_closed else None,
            "short_trades": sum(1 for t in c_closed if t["side"] == "short"),
            "long_trades": sum(1 for t in c_closed if t["side"] == "long"),
            "exit_reasons": {r: sum(1 for t in c_closed if t["exit_reason"] == r)
                             for r in sorted({t["exit_reason"] for t in c_closed
                                              if t["exit_reason"]})},
        }
        payload["compare"] = {
            "strategy": _perf(c_daily),
            "benchmark": _perf(bench_daily),
            "benchmark_name": "恒定满仓做空",
            "note": "同一段区间、同一口径(逐日复利,策略扣单边 0.05% 换手成本)。"
                    "campaign 策略多仓并行,资金曲线按「每仓 1 单位等权、当日取均值」;"
                    "历史表的逐笔收益是各仓自己的连乘,与该曲线口径不同,别互相求和核对。",
        }
        payload["caveats"] = _caveats(_perf(c_daily), _perf(bench_daily), c_closed)
        payload["risk_flags"] = risk_flags(_perf(c_daily), c_closed, c_daily,
                                           _perf(bench_daily))
        # 进场条件:campaign 的观察列表里有就绪的流,报它的方向;没有就报最接近的原因。
        ready = [w for w in camp["watch"] if w["entry_ready"]]
        if ready:
            payload["signal"]["entry_side"] = ready[0]["side"]
            payload["signal"]["entry_blocked"] = None
        else:
            payload["signal"]["entry_side"] = None
            live = [w for w in camp["watch"] if w["blocked"] not in (None, "已持仓")]
            live.sort(key=lambda w_: -w_["zone_add"])
            payload["signal"]["entry_blocked"] = (
                f"{live[0]['contract']} {'多' if live[0]['side'] == 'long' else '空'}:{live[0]['blocked']}"
                if live else "无进行中的建仓区间")
        # 散户维度降级为纯展示:campaign 的进出场不读它。文案跟着改,
        # 否则页面还在说「方案 C 用它进出场」——同一件事两处说法相反必出事(DEC-104)。
        payload["retail"]["trades"] = False
        payload["retail"]["note"] = (
            "散户三家长期站多头、长期亏钱,故反向取用;名单跨品种固定、不逐品种重选。"
            "**本品种已切换为逐合约战役策略(DEC-133),散户这一路只作展示**,"
            "不参与进出场;它仍是独立的第二意见:与机构一致还是背离,比方向本身更有信息量。")
        # 跨期对冲结构识别(DEC-137):**合格的**反向战役共存(空近+多远)时标出来。
        # 生猪多头人格现被份额资格挡着,共存要等资格门自然打开;资格豁免版已回测否决
        # (对冲腿 15 笔 −1.84%/笔、组合夏普 1.95→1.72,见 REPORT_ROLL_PRESSURE_v1 附录)
        # —— 对冲腿也必须是聪明钱,别再试豁免。
        pairs = []
        for lg in c_open:
            if lg["side"] != "long":
                continue
            for sh in c_open:
                if sh["side"] == "short" and sh["contract"][2:6] < lg["contract"][2:6]:
                    pairs.append({"short": sh["contract"], "long": lg["contract"]})
        payload["campaign"] = {
            "params": dict(RULES["campaign"]),
            "pairs": pairs,
            "positions": c_open,
            "watch": camp["watch"],
            "qual": camp["qual"],
            "note": "逐合约战役:左侧批次进场(逢跌加仓区间+价≤批次成本)、"
                    "聪明钱份额资格(该向历史战役盈亏≥对侧25%)、机构卸仓30%快出、"
                    "交割纪律;多仓并行,每届合约每方向一个独立的流。",
        }
    return payload


# ---------------------------------------------------------------- FG-SA 配对

def _member_main_net(seat: pd.DataFrame, mkt: pd.DataFrame, member: str) -> pd.Series:
    """某席位在**当日主力合约**上的可见净持仓(net_off,按合约 ffill)。

    与 seat_follow_payload 里的循环同口径(那边还要顺手攒第二条序列,没抽公共函数,
    改任何一边记得看另一边)。
    """
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=mkt.index)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = mkt.index[mkt["main"] == c]
        sig.loc[days] = w.reindex(days.union(w.index)).ffill().reindex(days).values
    return sig


# 跟随方案的默认参数(DEC-154,2026-08-28 运营者:「总资金 50 万」)。
# **保证金率是运营者的真实值**(2026-08-28 他给:玻璃 9%、纯碱 8%);第一版按行业惯例
# 估成 11%/10%,手数因此少开了约两成 —— 与手续费那次同一个教训:参数要问不要猜。
# use=0.35 是三档里的"中",留 65% 现金应对价差反向(实测最坏日 ±2.1%)。
FOLLOW_PLAN = {"capital": 500000.0, "use": 0.35, "margin": {"FG": 0.09, "SA": 0.08},
               "mult": {"FG": 20.0, "SA": 20.0}}


def follow_plan_payload(cfg: dict | None = None) -> dict | None:
    """「永安跟随策略」建议仓位(DEC-154,展示级,**不是下单指令**)。

    按永安**当日真实结构**等比缩放到运营者的资金:FG 取它净多最大的合约作多腿、
    净空最大的作空腿,SA 取绝对值最大的合约代表纯碱那条腿;比例照抄它的手数比,
    再按保证金预算缩放。永安改仓位,这里第二天自动跟着改。

    只在**对冲态**(FG 净方向与 SA 净方向相反)给方案 —— 同向时它不是在做对冲簿,
    这套配比没有意义(DEC-142 同一个状态门)。
    """
    cfg = {**FOLLOW_PLAN, **(cfg or {})}
    if not ({"FG", "SA"} <= set(PAIR_EXTRA)):
        return None
    ya = {k: PAIR_EXTRA[k].get("ya_all") or {} for k in ("FG", "SA")}
    px = {k: PAIR_EXTRA[k].get("px_all") or {} for k in ("FG", "SA")}
    if not ya["FG"] or not ya["SA"]:
        return None
    fg_net = sum(ya["FG"].values())
    sa_net = sum(ya["SA"].values())
    if fg_net * sa_net >= 0:
        return {"state": "same", "member": "永安期货", "fg_net": int(round(fg_net)),
                "sa_net": int(round(sa_net)), "legs": [],
                "note": "永安当前在玻璃与纯碱**同向**(不是对冲簿),这套跨品种配比不适用;"
                        "等它回到一多一空的对冲态再看。"}
    # 选腿:**手数比例照各方向的合计**(永安的纯碱空单分散在 7 个合约,只取单一最大腿
    # 会把对冲腿低估一半、净敞口从 8% 虚增到 44%);**下单合约优先当日主力**
    # (运营者 2026-08-28:「纯碱空腿选 SA2701」——主力盘口最深,挂得上才谈得上筹码),
    # 主力上没有该方向持仓时才退回持仓最大的那个合约。
    # 实际效果:FG 多腿=主力 FG2701(永安多单也压在这)、FG 空腿=主力方向不对→退回
    # 最大空腿 FG2611、SA 腿=主力 SA2701。三条腿 = FG 顺向 / FG 反向 / SA。
    mains = {k: (PAIR_EXTRA[k].get("main").iloc[-1]
                 if PAIR_EXTRA[k].get("main") is not None and len(PAIR_EXTRA[k]["main"]) else None)
             for k in ("FG", "SA")}

    def pick(d, sign, kind=None):
        m = mains.get(kind)
        if isinstance(m, str) and d.get(m, 0.0) * sign > 0:
            return m                                        # 主力方向对得上,优先主力
        cand = {c: v for c, v in d.items() if v * sign > 0}
        return max(cand, key=lambda c: abs(cand[c])) if cand else None

    def tot(d, sign):
        return sum(v for v in d.values() if v * sign > 0)
    fg_sign = 1.0 if fg_net > 0 else -1.0
    legs_raw = []
    for c, k, w in ((pick(ya["FG"], fg_sign, "FG"), "FG", tot(ya["FG"], fg_sign)),
                    (pick(ya["FG"], -fg_sign, "FG"), "FG", tot(ya["FG"], -fg_sign)),
                    (pick(ya["SA"], -fg_sign, "SA"), "SA", sum(ya["SA"].values()))):
        if c and c in px[k] and w:
            legs_raw.append((c, k, w))
    if len(legs_raw) < 2:
        return None
    base = min(abs(w) for _, _, w in legs_raw)              # 以最小的一方向合计为 1
    unit = [(c, k, w / base) for c, k, w in legs_raw]
    per_group = sum(abs(u) * cfg["mult"][k] * px[k][c] * cfg["margin"][k] for c, k, u in unit)
    if per_group <= 0:
        return None
    budget = cfg["capital"] * cfg["use"]
    # 保证金**宁少不多**(运营者 2026-09-01):原来是四舍五入,手数一取整就可能超预算
    # (焦煤那张 6/9 手实测 37.2%,而卡头写的是 35%)。改成保比例整体缩,
    # 见 `_fit_within_budget` —— 两腿比例是这张卡的全部意义,不能为了取整把它改掉。
    kind_of = {c: k for c, k, _ in unit}
    sized = _fit_within_budget(
        [(c, k, abs(u) * (budget / per_group)) for c, k, u in unit], budget,
        lambda c, n: n * cfg["mult"][kind_of[c]] * px[kind_of[c]][c] * cfg["margin"][kind_of[c]])
    if not sized:
        return None
    legs, margin, notional, net_val = [], 0.0, 0.0, 0.0
    for c, k, u in unit:
        lots = sized[c]
        if lots <= 0:
            continue
        val = lots * cfg["mult"][k] * px[k][c]
        sd = 1.0 if u > 0 else -1.0
        margin += val * cfg["margin"][k]
        notional += val
        net_val += sd * val
        legs.append({"contract": c, "instrument": k,
                     "side": "long" if sd > 0 else "short", "lots": lots,
                     "px": round(px[k][c], 1),
                     "member_net": int(round(ya[k][c])),
                     "value_wan": round(val / 1e4, 1)})
    if not legs:
        return None
    fg_val = sum((1 if lg["side"] == "long" else -1) * lg["lots"] * cfg["mult"]["FG"] * lg["px"]
                 for lg in legs if lg["instrument"] == "FG")
    sa_val = sum((1 if lg["side"] == "long" else -1) * lg["lots"] * cfg["mult"]["SA"] * lg["px"]
                 for lg in legs if lg["instrument"] == "SA")
    return {
        "state": "opposite", "member": "永安期货",
        "capital": int(cfg["capital"]), "use_pct": round(cfg["use"] * 100),
        "fg_net": int(round(fg_net)), "sa_net": int(round(sa_net)),
        "legs": legs,
        "margin": int(round(margin)), "margin_pct": round(margin / cfg["capital"] * 100, 1),
        "notional_wan": round(notional / 1e4),
        "leverage": round(notional / cfg["capital"], 1),
        "fg_net_wan": round(fg_val / 1e4, 1), "sa_net_wan": round(sa_val / 1e4, 1),
        "net_exposure_wan": round(net_val / 1e4, 1),
        "net_exposure_pct": round(abs(net_val) / cfg["capital"] * 100),
        # 占总名义:与永安自己的中性度直接可比(它 8/27 是 8.1%)。
        "net_of_notional_pct": round(abs(net_val) / notional * 100, 1) if notional else None,
        # 单日损益两情形:玻纯同涨跌 1%(对冲生效)/ 价差反向各 1%(最坏)
        "risk_same": int(round(net_val * 0.01)),
        "risk_spread": int(round((abs(fg_val) + abs(sa_val)) * 0.01)),
        "note": ("按永安**当日真实持仓比例**等比缩到 %d 万,保证金用 %d%%(留足现金扛价差反向)。"
                 "它改仓位,这里第二天自动跟着改。**这是展示不是下单指令**:手数取整有偏差,"
                 "保证金率按估算(FG %d%%/SA %d%%),实际以你的账户为准;建仓用筹码地图分批挂,"
                 "别一次打满。" % (cfg["capital"] / 1e4, cfg["use"] * 100,
                                   cfg["margin"]["FG"] * 100, cfg["margin"]["SA"] * 100)),
    }


# 跨月跟随方案的每品种配置(DEC-168)。
#
# **`margin` 是单边保证金率,13% 由运营者 2026-09-01 给定**(此前我按沉淀资金那个
# 24% 双边折半估成 12%,是估的)。同一句话里他还给了第二件更要紧的事:
# **「套利只收单边保证金」** —— 跨期套利指令的占用是**两腿里较大的那一腿**,
# 不是两腿相加。这不是费率微调,是把占用砍掉近一半、同样预算下手数接近翻倍。
# 见 `_cal_plan_sized` 里的 `aggregate=max`。
#
# **只改焦煤这一张。** 玻纯那张(FOLLOW_PLAN)是跨品种组合,交易所收不收单边
# 我没有依据,账户类参数不猜 —— 等运营者给了再动。
CAL_FOLLOW = {
    # 2026-09-01 运营者拍板由东证换成中泰。理由与丑话都在 `caveat` 里,原样印在卡上。
    # 换人依据:`REPORT_JM_CAL_PICK_v1` —— 按「它做跨月的那些天价差赚不赚」排,
    # 东证是 −8,125 万(日均 −53.5 万),中信更差(−9,947 万,16 家垫底),
    # 中泰 +1.80 亿(日均 +86.1 万)、跨月天数 209 天全场最多、100% 在榜。
    "JM": {"member": "中泰期货", "capital": 500000.0, "use": 0.35,
           "margin": 0.13, "mult": 60.0,
           "caveat": "**丑话**:中泰做跨月的 209 天里价差 +1.80 亿(日均 +86 万,"
                     "全场最多的跨月天数、100% 在榜),但**利润几乎全在 2026 一年、"
                     "四年里只有两年为正**;而 2026 同时是东证 −1.09 亿、中信 −1.03 亿"
                     "的一年 —— 那更像曲线一次大移动有人站对边,不是重复出现的本事。"
                     "它也是我从 57 家里事后挑的,事前选不选得出是另一回事"
                     "(REPORT_JM_CAL_PICK_v1)。"},
}
# 两腿手数悬殊到什么程度就不算套利(运营者 2026-08-31:「正常套利是 1:2、1:1,
# 最多 1:3,超过 1:3 的就算纯趋势」)。**这个常数现在只有这一份**:净持仓页那段
# 「跨月结构」曾共用它,2026-09-01 运营者说删掉,那边已随之移除(DEC-167 撤回)。
CAL_MAX_RATIO = 3.0


def _fit_within_budget(unit, budget, margin_of, aggregate=sum):
    """把每条腿的手数取整到**保证金不超预算**(运营者 2026-09-01:「改成宁少不多」)。

    为什么不直接向下取整:两腿手数本来就小(五到十手),各自 floor 会把比例也
    一起改掉 —— 而这张卡的全部意义就在那个比例。做法改成**保比例整体缩**:
    先按四舍五入定手数,超预算就把整体规模缩 2% 再算一遍,直到不超为止。
    比例基本不动,只是仓位小一点。

    `unit` 是 [(合约, 方向, 相对份额)],`margin_of(合约, 手数)` 给该腿的保证金占用。
    `aggregate` 决定两腿怎么合成总占用:**跨期套利指令只收单边**(运营者
    2026-09-01),那时传 `max`;各腿独立收取时传 `sum`。
    返回 {合约: 手数};缩到有腿为 0 就返回 None(资金撑不起这个配比)。
    """
    scale = 1.0
    for _ in range(60):
        lots = {c: int(round(scale * u)) for c, _, u in unit}
        if any(v <= 0 for v in lots.values()):
            return None
        if aggregate(margin_of(c, v) for c, v in lots.items()) <= budget:
            return lots
        scale *= 0.98
    return None


def cal_follow_plan_payload(code: str, net_by_contract: dict, px_all: dict,
                            cfg: dict | None = None) -> dict | None:
    """「某席位跨月跟随策略」建议仓位(DEC-168,展示级,**不是下单指令**)。

    与玻纯那张卡(`follow_plan_payload`,DEC-154)**同一个模子**,只是两条腿从
    「两个品种」换成「同品种的远月与近月」。运营者 2026-08-31 在焦煤净持仓页看出
    东证在做空近月多远月,要的就是这张卡摆到机构资金页、和永安那张同一个位置。

    选腿沿用玻纯那套已经踩过坑的规矩:
      * **手数比例照各方向的合计**,不是单一最大腿 —— 永安的纯碱空单分散在 7 个
        合约,只取最大腿会把对冲腿低估一半、净敞口虚增五倍(DEC-154 实测);
      * **下单合约优先当日主力**,主力方向不对才退回该方向持仓最大的合约 ——
        主力盘口最深,挂得上才谈得上筹码(运营者 2026-08-28)。

    **不在套利态就不给方案**:两腿手数比超过 1:3 那是重仓单边挂了一条零头腿,
    属于纯趋势(与净持仓页跨月结构同一个门槛)。
    """
    cfg = {**(CAL_FOLLOW.get(code) or {}), **(cfg or {})}
    if not cfg or not net_by_contract or not px_all:
        return None
    member = cfg["member"]
    longs = {c: v for c, v in net_by_contract.items() if v > 0 and c in px_all}
    shorts = {c: v for c, v in net_by_contract.items() if v < 0 and c in px_all}
    long_tot = sum(longs.values())
    short_tot = -sum(shorts.values())
    base = {"state": "trend", "member": member, "code": code,
            "capital": int(cfg["capital"]), "use_pct": round(cfg["use"] * 100),
            "long_lots": int(round(long_tot)), "short_lots": int(round(short_tot)),
            "legs": []}
    if not longs or not shorts:
        base["note"] = ("%s 当前在%s上是**单边持仓**(只有一个方向),不是跨月套利簿,"
                        "这套两腿配比不适用。" % (member, CURRENT.get("name", code)))
        return base
    weak, strong = min(long_tot, short_tot), max(long_tot, short_tot)
    ratio = strong / weak if weak else float("inf")
    if weak * CAL_MAX_RATIO < strong:
        base["ratio"] = round(ratio, 2)
        base["note"] = ("%s 当前两腿手数 %s:%s ≈ **1:%.1f**,超过 1:3 —— 那是重仓单边"
                        "挂了一条零头腿,属于纯趋势不是套利簿,这套配比不适用。"
                        "等它回到 1:3 以内再看。"
                        % (member, f"{int(long_tot):,}", f"{int(short_tot):,}", ratio))
        return base
    return _cal_plan_sized(code, cfg, member, longs, shorts, long_tot, short_tot, px_all, ratio)


def _cal_plan_sized(code, cfg, member, longs, shorts, long_tot, short_tot, px_all, ratio):
    """套利态下按资金缩放。拆出来只为让上面那段判据读起来是一条直线。"""
    main = None
    if CURRENT.get("_main_contract"):
        main = CURRENT["_main_contract"]

    def pick(d):
        if isinstance(main, str) and main in d:
            return main                                   # 主力方向对得上,优先主力
        return max(d, key=lambda c: abs(d[c]))

    long_c, short_c = pick(longs), pick(shorts)
    mult, margin_rate = cfg["mult"], cfg["margin"]
    # 以手数少的那一方向为 1,另一方向按合计比例放大 —— 与玻纯版同一套。
    unit = [(long_c, "long", long_tot / min(long_tot, short_tot)),
            (short_c, "short", short_tot / min(long_tot, short_tot))]
    # **套利只收单边保证金**(运营者 2026-09-01):跨期套利指令的占用是两腿里
    # 较大的那一腿,不是相加。所以定规模、卡预算、报占用三处都用 max 不用 sum。
    def leg_margin(c, n):
        return n * mult * px_all[c] * margin_rate

    per_group = max(u * mult * px_all[c] * margin_rate for c, _, u in unit)
    if per_group <= 0:
        return None
    budget = cfg["capital"] * cfg["use"]
    sized = _fit_within_budget(
        [(c, side, groups_u * (budget / per_group)) for c, side, groups_u in unit],
        budget, leg_margin, aggregate=max)
    if not sized:
        return None
    legs, margin, notional, net_val = [], 0.0, 0.0, 0.0
    splits = []
    for c, side, _u in unit:
        lots = sized[c]
        if lots <= 0:
            continue
        val = lots * mult * px_all[c]
        sd = 1.0 if side == "long" else -1.0
        margin = max(margin, val * margin_rate)      # 单边:取较大那一腿,不累加
        notional += val
        net_val += sd * val
        legs.append({"contract": c, "instrument": code, "side": side, "lots": lots,
                     "px": round(px_all[c], 1),
                     "member_net": int(round((longs if side == "long" else shorts)[c])),
                     "value_wan": round(val / 1e4, 1)})
        # **远近由合约月份定,不是由多空定**(2026-09-01)。原来写死「多=远月、
        # 空=近月」,那只对「多远空近」型对得上;换成中泰这种**多近空远**的席位,
        # 两个标签会整个标反 —— 而这张卡是给人照着下单看的,标反比不标更糟。
        later = long_c > short_c            # 合约代码同长度,字典序即时间序
        is_far = (side == "long") == later
        splits.append({"label": ("远月净" if is_far else "近月净"),
                       "wan": round(sd * val / 1e4, 1)})
    if len(legs) < 2:
        return None
    return {
        "state": "spread", "member": member, "code": code,
        "capital": int(cfg["capital"]), "use_pct": round(cfg["use"] * 100),
        "long_lots": int(round(long_tot)), "short_lots": int(round(short_tot)),
        "ratio": round(ratio, 2), "legs": legs, "splits": splits,
        "margin": int(round(margin)), "margin_pct": round(margin / cfg["capital"] * 100, 1),
        "notional_wan": round(notional / 1e4),
        "leverage": round(notional / cfg["capital"], 1),
        "net_exposure_wan": round(net_val / 1e4, 1),
        "net_exposure_pct": round(abs(net_val) / cfg["capital"] * 100),
        "net_of_notional_pct": round(abs(net_val) / notional * 100, 1) if notional else None,
        # 单日损益两情形:两腿同涨跌 1%(对冲生效)/ 价差反向各 1%(最坏)
        "risk_same": int(round(net_val * 0.01)),
        "risk_spread": int(round(sum(abs(lg["lots"]) * cfg["mult"] * lg["px"]
                                     for lg in legs) * 0.01)),
        "note": ("按%s**当日真实持仓比例**等比缩到 %d 万,保证金用 %d%%(留足现金扛价差反向)。"
                 "它改仓位,这里第二天自动跟着改。**这是展示不是下单指令**:手数取整有偏差,"
                 "保证金按 %s **%d%% 单边**、且**套利指令只收较大那一腿**(运营者给定),"
                 "实际以你的账户为准;"
                 "建仓用筹码地图分批挂,别一次打满。"
                 % (member, cfg["capital"] / 1e4, cfg["use"] * 100, code, cfg["margin"] * 100)
                 + (cfg.get("caveat") or "")),
    }


# ---------------------------------------------------------------- IH 跟随引擎
#
# **这条线的完整来龙去脉,别只看结论。** 五轮研究(IH_MODEL_v1/v2、JPM_v1、
# STATE_SCAN_v1、PLAN_V3、JUDGE12、TRIO_v1、SYNC_v1、GTJA_v1)没有一轮拿到过
# 能过多重检验校正的信号,我据此两次建议「不排期」。**运营者两次否掉这个建议**,
# 理由是「主力资金会提前一段时间进场,必须做一个引擎出来,这样我能参考」——
# 而他上一次提出同一条(2026-08-30「他们钱多必须提前进场」)时是对的:
# 正是那句话让我把事件窗改成在场状态,当场捞出了摩根大通那条线。
#
# **所以这个引擎的定位是「参考看板」,不是「过闸的策略」**:它如实回答
# 「此刻这三家核心席位在不在场、什么方向、进场多久、这一轮浮盈多少」,
# 统计每次全量重算并把丑话一起印出来,**不写死任何一个漂亮数字**。
I_FOLLOW = {
    "member": "永安期货",
    # 账户参数,运营者 2026-09-02 给的实盘数 —— **不猜**(纪律见 PITFALLS,已栽两次)。
    "capital": 500000.0,      # 本金
    "use": 0.35,              # 单次动用比例,与焦煤跟随卡同口径
    "margin": 0.11,           # 铁矿石保证金率
    "mult": 100.0,            # 100 吨/手
    # **这段话里不许再写段数、胜率、集中度这类会变的数** —— 它们就在上面那行
    # 实算的统计里。2026-09-02 首版犯过:丑话里写死「57 段胜率 58%、最赚 3 段占 62%」
    # (研究定档时的数),而卡上实算的是「56 段胜 62%、占 57%」——**同一张卡上
    # 同一件事两个数**,而且每收一段就会差得更多。这里只留研究定档那天的**检验
    # 结论**(它们不随数据漂),会变的一律指回上面那行。
    "note": (
        "第二引擎,与主引擎并列、各管各的仓位。规则:永安在当日主力的可见净持仓方向,"
        "翻转→次日开盘反手。"
        "**丑话,采纳前必须认下**(检验定档于 2026-09-02,REPORT_I_FOLLOW_v1):"
        "① **第七道闸门没过**——它是 47 家候选里挑出来的第一名,而纯噪声下"
        "「47 家第一名」的夏普中位数就有 0.99,定档时实测 1.20 只排到第 83 百分位"
        "(最大统计量置换 p=0.17);"
        "② **季度重挑的走前检验 −13.2%**——「挑最好的那个」这个动作实盘做不到,"
        "2024 年它一路在挑光大期货,而光大随后连亏;"
        "③ **肥尾**——利润高度集中在少数几段,盯上面那行实算的「最赚的 3 段占…」;"
        "④ **跨品种先验只是一枚硬币**:永安在铁矿石排第 1/47,而焦煤在用的华泰"
        "在这里排第 43/47(夏普 −0.44)。"
        "撑得住的那一半:安慰剂 p=0.002、前后半夏普 1.25/1.21 几乎不衰减、"
        "T+3 仍有 1.31、成本翻四倍仍有 0.96、逐年 4/4 正。"
    ),
}


def i_follow_payload(price_raw, seat_raw) -> dict | None:
    """铁矿石「跟永安」第二引擎(DEC-178,2026-09-02 运营者拍板)。

    **信号部分整个复用 `seat_follow_payload`**,一行规则都不另写 —— 跟华泰、
    跟永安(玻璃)、跟永安(铁矿)必须是同一台机器算出来的,否则三张卡迟早会
    因为「有人改了其中一处」而互相对不上(PITFALLS 第一条:同一个事实两处维护)。

    这里只多做两件 `seat_follow_payload` 不管的事:
      1. **手数方案**:按运营者给的本金 50 万 / 动用 35% / 保证金 11% / 100 吨每手
         把当前方向缩到可下单的手数;
      2. **补几个丑话要用的统计**(胜率、最赚 3 段占比)——`stats` 里原来没有,
         而这两个数正是 REPORT_I_FOLLOW_v1 里最该被看见的。

    铁矿石**不进 FLOW_CODES**:它没有主引擎(运营者要的是跟随,不是再立一套
    阵营/z 分数研究),所以与 IH 看板一样单独取数、单独写产物。
    """
    cfg = I_FOLLOW
    price = clean_price(price_raw)
    seat = clean_seat(seat_raw)
    # 用 use() 的返回值,不读 CURRENT —— CURRENT 是 run_one 设的全局量,跑完停在
    # 最后一个品种上(2026-09-01 IH 看板就是这么顶着「焦煤」的名字上线的)。
    v = use("I")
    mkt = main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(RULES["replay_start"])]
    if not len(mkt):
        return None

    follow = seat_follow_payload(seat, mkt, cfg)
    runs = follow.get("history") or []

    # ---- 补统计:胜率与集中度。平均值好看不代表每一段都好看。
    closed = [r for r in runs if not r.get("open")]
    rets = [r["ret_pct"] for r in closed if r.get("ret_pct") is not None]
    win_rate = round(sum(1 for r in rets if r > 0) / len(rets) * 100, 0) if rets else None
    top3_share = None
    if len(rets) >= 4:
        total = sum(rets)
        top3 = sum(sorted(rets)[-3:])
        top3_share = round(top3 / total * 100, 0) if total else None
    follow["stats"]["segs"] = len(rets)
    follow["stats"]["win_rate"] = win_rate
    follow["stats"]["top3_share_pct"] = top3_share

    # ---- 手数方案。**当前不在场就不给方案**:没有方向的时候给手数是无意义的。
    plan = None
    px = _f(mkt["settle"].iloc[-1])
    if follow.get("side") and px:
        per_lot_margin = px * cfg["mult"] * cfg["margin"]
        budget = cfg["capital"] * cfg["use"]
        lots = int(budget // per_lot_margin) if per_lot_margin > 0 else 0
        plan = {
            "capital": cfg["capital"],
            "use_pct": round(cfg["use"] * 100, 0),
            "margin_pct": round(cfg["margin"] * 100, 1),
            "mult": cfg["mult"],
            "price": round(px, 1),
            "contract": str(mkt["main"].iloc[-1]),
            "side": follow["side"],
            "lots": lots,
            "budget": round(budget, 0),
            "margin_used": round(lots * per_lot_margin, 0),
            "notional": round(lots * px * cfg["mult"], 0),
            # 一个跳动 = 0.5 元/吨 × 100 吨 = 50 元/手;这里按 1 元/吨给,
            # 免得把交易所最小变动价位写死在两个地方对不上。
            "per_yuan": round(lots * cfg["mult"], 0),
        }
        if lots == 0:
            plan["warn"] = (f"按 {cfg['capital']:,.0f} × {cfg['use']*100:.0f}% = "
                            f"{budget:,.0f} 元,连一手都开不起"
                            f"(一手保证金 {per_lot_margin:,.0f} 元)")
    return {
        "instrument": "I",
        "name": v["name"],
        "data_date": mkt.index[-1].strftime("%Y-%m-%d"),
        "main_contract": str(mkt["main"].iloc[-1]),
        "last_settle": _f(mkt["settle"].iloc[-1]),
        "plan": plan,
        **follow,
    }


IH_FOLLOW = {
    "seats": ["摩根大通", "高盛期货", "中财期货"],   # 运营者点名的三家核心席位
    "carry": 20,        # 掉榜沿用天数,与全部研究同档,不调参
    "lag": 2,           # T 日收盘出榜 → T+1 开盘进场 → 收益从 T+1→T+2 起算
}


def _ih_state(net_by_day, idx, carry):
    """把某席位的逐日净持仓变成在场状态向量(±1/0)。与 research 完全同一套。"""
    st = np.zeros(len(idx))
    if not len(net_by_day):
        return st
    locs = idx.get_indexer(net_by_day.index)
    sgn = np.sign(net_by_day.values)
    for i, lo in enumerate(locs):
        if lo < 0:
            continue
        nxt = locs[i + 1] if i + 1 < len(locs) else None
        end = nxt if (nxt is not None and nxt - lo <= carry) else min(lo + carry + 1, len(idx))
        st[lo:end] = sgn[i]
    return st


def _ih_segments(sig, idx, ret, lag, op=None, mains=None):
    """信号的连续同向段 → 逐轮明细(段收益按 T+1 对齐,与研究口径同)。

    **进出场价必须与收益同一口径,不能另取一个好看的价**(运营者 2026-09-01
    「进场和出场成本都要写」)。`ret` 是开→开收益,仓位后移 `lag` 格,所以
    一段从 i 到 j 实际吃到的是 `ret[i+lag] … ret[j+lag]`:
      · **进场价 = open[i+lag−1]**(信号日 T 收盘出榜 → T+1 开盘成交);
      · **出场价 = open[j+lag]**(离场信号次日开盘);仍在场则为 None,
        由前端显示现价。
    **一个必须标出来的陷阱**:段内换过月时,进出场价属于**不同的合约**,两个价
    直接相除**对不上** `mkt_pct` —— 后者是逐日按各合约自己的前收连乘的(换月纪律,
    见 main_series 开头)。实测 2024-07-16 那段:2405 → 3138.8 直除 +30.51%,
    而真实段收益 +30.76%。所以每个价都带上**它属于哪个合约**,并给出 `rolled` 标记;
    界面据此提示,免得运营者拿两个价一除发现对不上、又找不到原因。
    """
    segs, i = [], 0
    n = len(idx)
    o = None if op is None else np.asarray(op, dtype=float)
    mn = None if mains is None else list(mains)

    def px(k):
        if o is None or k < 0 or k >= n or not np.isfinite(o[k]):
            return None
        return round(float(o[k]), 1)

    def con(k):
        if mn is None or k < 0 or k >= n:
            return None
        return mn[k] if isinstance(mn[k], str) else None

    while i < n:
        if sig[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and sig[j + 1] == sig[i]:
            j += 1
        a, b = min(i + lag, n - 1), min(j + 1 + lag, n)
        seg = ret[a:b]
        if len(seg):
            mkt = (float(np.prod(1 + seg)) - 1) * 100
            mine = (float(np.prod(1 + sig[i] * seg)) - 1) * 100
            closed = j + 1 < n
            segs.append({"entry_date": idx[i].strftime("%Y-%m-%d"),
                         "exit_date": idx[j].strftime("%Y-%m-%d") if closed else None,
                         "side": "long" if sig[i] > 0 else "short",
                         "days": j - i + 1,
                         "entry_px": px(i + lag - 1),
                         "exit_px": px(j + lag) if closed else None,
                         "entry_contract": con(i + lag - 1),
                         "exit_contract": con(j + lag) if closed else con(n - 1),
                         # 段内换过月:两个价属于不同合约,直除对不上段收益
                         "rolled": len({c for c in mn[max(i + lag - 1, 0):
                                                     min(j + lag + 1, n)]
                                        if isinstance(c, str)}) > 1 if mn else False,
                         "mkt_pct": round(mkt, 2), "ret_pct": round(mine, 2)})
        i = j + 1
    return segs


def ih_follow_payload(price_raw, seat_raw) -> dict | None:
    """IH「核心席位在场看板」(DEC-172,运营者两次要求)。

    规则(零参数,与 research 同一套口径):
      * 三家核心席位,谁在榜谁在场,方向 = 它净持仓的符号,掉榜沿用 20 日;
      * **在场的那几家方向一致才给方向**;有人多有人空 → 分歧,观望;
        一个都不在场 → 观望。这直接落实运营者「三方共振」那个想法,
        并如实处理「它们其实很少同时在场」这个事实。
      * T+1 开盘执行(lag=2)。
    """
    cfg = IH_FOLLOW
    price = clean_price(price_raw)
    seat = clean_seat(seat_raw)
    # **用 use() 的返回值,不要读 CURRENT**(2026-09-01 上线后 Chrome 一眼看出来):
    # CURRENT 是 run_one 设的全局量,跑完五个品种后停在最后一个 JM 上;`use()` 只改
    # RULES 不改 CURRENT。原来读 CURRENT["name"]/["multiplier"],于是 IH 看板顶着
    # 「焦煤 JM」的名字、点值也是焦煤的 60(IH 是 300)。**这类错单元测试照不到** ——
    # 测的是状态判定,不是显示成谁。
    v = use("IH")
    mkt = main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(RULES["replay_start"])]
    if not len(mkt):
        return None
    idx = mkt.index
    ret = mkt["ret_open"].fillna(0.0).values
    n = len(idx)

    states, seats_out = {}, []
    for m in cfg["seats"]:
        g = seat[seat["member_key"] == m]
        d = g.groupby("trade_date")["net_off"].sum()
        d = d[d.index.isin(idx) & (d != 0)]
        st = _ih_state(d, idx, cfg["carry"])
        states[m] = st
        on = bool(st[-1] != 0)
        # 本轮:从当前这一段的起点算起
        entry, seg_ret = None, None
        if on:
            k = n - 1
            while k > 0 and st[k - 1] == st[-1]:
                k -= 1
            entry = idx[k]
            a = min(k + cfg["lag"], n - 1)
            seg = ret[a:]
            seg_ret = round((float(np.prod(1 + st[-1] * seg)) - 1) * 100, 2) if len(seg) else None
        seats_out.append({
            "member": m, "on": on,
            "side": (None if not on else ("long" if st[-1] > 0 else "short")),
            "net": (int(round(d.iloc[-1])) if len(d) else None),
            "last_board": (d.index.max().strftime("%Y-%m-%d") if len(d) else None),
            "entry_date": entry.strftime("%Y-%m-%d") if entry is not None else None,
            "days": (int((idx >= entry).sum()) if entry is not None else None),
            "seg_ret_pct": seg_ret,
            "rounds": len([1 for b in _ih_segments(st, idx, ret, cfg["lag"], mkt["open"].values, mkt["main"].values)]),
        })

    # 合成信号:在场的那几家方向一致才算数
    M = np.array([states[m] for m in cfg["seats"]])
    on_cnt = (M != 0).sum(axis=0)
    ssum = M.sum(axis=0)
    agree = (on_cnt >= 1) & (np.abs(ssum) == on_cnt)
    sig = np.where(agree, np.sign(ssum), 0.0)

    segs = _ih_segments(sig, idx, ret, cfg["lag"], mkt["open"].values, mkt["main"].values)
    closed = [s for s in segs if s["exit_date"]]
    wins = [s for s in closed if s["ret_pct"] > 0]
    p = np.concatenate([np.zeros(cfg["lag"]), sig[:-cfg["lag"]]])
    live = p != 0
    k = 242 / max(live.sum(), 1)
    ann = (float(np.prod(1 + (p * ret)[live]) ** k) - 1) * 100 if live.sum() else None
    lng = (float(np.prod(1 + ret[live]) ** k) - 1) * 100 if live.sum() else None
    # 利润集中度:最赚的三轮占了多少 —— 摩根那条线上是 117%(其余十轮合计为负)
    rs = sorted((s["ret_pct"] for s in closed), reverse=True)
    top3 = round(sum(rs[:3]) / sum(rs) * 100) if rs and sum(rs) else None

    d0 = idx[-1]
    state = "flat"
    if sig[-1] != 0:
        state = "long" if sig[-1] > 0 else "short"
    elif on_cnt[-1] >= 2:
        state = "split"                                    # 有人在场但方向打架
    cur = segs[-1] if segs and not segs[-1]["exit_date"] else None
    return {
        "instrument": "IH", "name": v["name"], "multiplier": v["multiplier"],
        "data_date": d0.strftime("%Y-%m-%d"),
        "main_contract": str(mkt["main"].iloc[-1]) if isinstance(mkt["main"].iloc[-1], str) else None,
        "close": float(mkt["settle"].iloc[-1]) if "settle" in mkt else None,
        "carry": cfg["carry"], "seats": seats_out,
        # 未平仓那一段的「现价」:最新开盘价,与进场价同一口径(都是可成交的开盘价),
        # 不用结算价 —— 两种价混着摆,看的人会拿去算一个对不上的浮盈。
        "last_open": (round(float(mkt["open"].iloc[-1]), 1)
                      if np.isfinite(mkt["open"].iloc[-1]) else None),
        "state": state,
        "current": cur,
        "on_count": int(on_cnt[-1]),
        "history": segs[-12:][::-1],                       # 倒序,与全站历史表一致
        "stats": {
            "rounds": len(closed), "wins": len(wins),
            "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
            "avg_pct": round(float(np.mean([s["ret_pct"] for s in closed])), 2) if closed else None,
            "in_days": int(live.sum()), "in_pct": round(live.sum() / n * 100, 1),
            "ann_pct": None if ann is None else round(ann, 1),
            "long_same_pct": None if lng is None else round(lng, 1),
            "edge_pct": None if (ann is None or lng is None) else round(ann - lng, 1),
            "top3_share_pct": top3,
        },
        "note": ("**这是参考看板,不是过闸的策略。** 规则:三家核心席位(%s)谁在榜谁在场,"
                 "方向=净持仓符号,掉榜沿用 %d 日;**在场的那几家方向一致才给方向**,"
                 "有分歧或无人在场就观望;T+1 开盘执行。"
                 "**必须一起看的丑话**:五轮研究没有一轮拿到过能过多重检验校正的信号 —— "
                 "摩根大通十二年只出手 13 轮,p(方向)=0.136 且**利润高度集中在少数几轮大跌**;"
                 "高盛 2023-09 已离场、中财在十二年官方口径下择时增益为负;"
                 "「三家同时在场」历史上一天都没出现过。"
                 "运营者知情后仍要求做出来当参考(DEC-172),依据是「主力资金提前进场,"
                 "要能看见」。**看方向、看谁在场,别把它当信号照单下注。**"
                 % ("、".join(cfg["seats"]), cfg["carry"])),
    }


def fgsa_hedge_book() -> dict | None:
    """玻纯「永安对冲簿」状态卡(DEC-142,展示级,只显示不进判据)。

    规则(REPORT_FGSA_LINK_v2,零参数):永安在 FG 主力与 SA 主力的净持仓**反向**时
    (历史约 31% 的天数)跟它的方向持价差(多玻璃空纯碱 → 做多价差;反之做空);
    同向或缺数据 → 不在场。执行工具 = 郑商所套利指令 SP FG-SA(玻璃在前纯碱在后)。
    统计每次全量重算(扣 0.2%/翻转的保守成本),不写死数字。
    **为什么是永安一家不是五家阵营**:阵营版同规则实测是死的(状态内日均 3bp 对
    永安版 20bp)——五家合计的"反向"多半是成员间打架的噪音,单席位的对冲簿才是
    真仓位表达;与 FG 单腿可跟(DEC-141)互证。
    """
    if not ({"FG", "SA"} <= set(PAIR_EXTRA)):
        return None
    fg, sa = PAIR_EXTRA["FG"], PAIR_EXTRA["SA"]
    idx = fg["ret_open"].index.intersection(sa["ret_open"].index)
    if not len(idx):
        return None
    f = np.sign(fg["ya"].reindex(idx))
    s = np.sign(sa["ya"].reindex(idx))
    pos = pd.Series(0.0, index=idx)
    m = f.notna() & s.notna() & (f * s < 0)
    pos[m] = f[m]
    ret_sp = fg["ret_open"].reindex(idx).fillna(0) - sa["ret_open"].reindex(idx).fillna(0)
    held = pos.shift(2)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    daily = (held * ret_sp - turn * 0.002).dropna()
    if not len(daily):
        return None
    eq = (1 + daily).cumprod()
    stats = {
        "cum_pct": round((float(eq.iloc[-1]) - 1) * 100, 1),
        "sharpe": round(float(daily.mean() / daily.std() * np.sqrt(242)), 2) if daily.std() > 0 else None,
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
        "in_market_pct": round(float((held != 0).mean() * 100), 1),
        "yearly": {str(y): round((float(np.prod(1 + g)) - 1) * 100, 1)
                   for y, g in daily.groupby(daily.index.year)},
    }
    # 在场段(方向恒定的连续区间;段收益按 T+1 对齐,展示用)
    segs = []
    i0, side = None, 0.0
    vals = pos.values
    for i in range(len(idx) + 1):
        v = vals[i] if i < len(idx) else 0.0
        if v != side:
            if side != 0 and i0 is not None:
                j0, j1 = min(i0 + 2, len(idx) - 1), min(i + 2, len(idx))
                r = float(np.prod(1 + side * ret_sp.iloc[j0:j1])) - 1
                # side 用多空词(运营者 2026-08-25 口径):long=多玻空碱=做多价差。
                segs.append({"start": idx[i0].strftime("%Y-%m-%d"),
                             "end": idx[i - 1].strftime("%Y-%m-%d") if i < len(idx) or vals[-1] != side else None,
                             "side": "long" if side > 0 else "short",
                             "days": i - i0, "ret_pct": round(r * 100, 2)})
            i0, side = i, v
    open_seg = bool(len(vals) and vals[-1] != 0)
    if open_seg and segs:
        segs[-1]["end"] = None
    d = idx[-1]
    fg_net = fg["ya"].reindex(idx).iloc[-1]
    sa_net = sa["ya"].reindex(idx).iloc[-1]
    state = None
    if np.isfinite(fg_net) and np.isfinite(sa_net):
        state = "opposite" if np.sign(fg_net) * np.sign(sa_net) < 0 else "same"
    return {
        "member": "永安期货",
        "data_date": d.strftime("%Y-%m-%d"),
        "state": state,
        # 多空词不是扩缩词(运营者 2026-08-25 纠正):多玻空碱=做多价差,反之做空,
        # 玻璃恒前腿;"走扩/收窄"是价格相对 0 轴的位置词,别用在仓位方向上。
        "direction": (None if state != "opposite"
                      else ("long" if fg_net > 0 else "short")),
        "fg_net": None if not np.isfinite(fg_net) else int(fg_net),
        "sa_net": None if not np.isfinite(sa_net) else int(sa_net),
        "fg_main": str(fg["main"].iloc[-1]),
        "sa_main": str(sa["main"].iloc[-1]),
        "seg_start": segs[-1]["start"] if open_seg and segs else None,
        "seg_days": segs[-1]["days"] if open_seg and segs else None,
        "seg_ret_pct": segs[-1]["ret_pct"] if open_seg and segs else None,
        "history": segs[-8:],
        "stats": stats,
        "note": ("**永安对冲簿(展示级,不进判据)**:永安在玻璃与纯碱主力的净持仓反向时"
                 "(历史约 31% 的天数)跟它的方向持价差——它多玻璃空纯碱 → **做多价差**,"
                 "空玻璃多纯碱 → **做空价差**(玻璃恒前腿)。执行 = 郑商所套利指令 "
                 "SP FG-SA(玻璃在前纯碱在后)。两腿 @成本引自净持仓页同一台成本引擎"
                 "(推算口径,非成交均价)。回放(扣 0.2%/翻转)与逐年见 stats,T+2 零衰减。"
                 "**为什么是永安一家**:五家阵营版同规则是死的,单席位对冲簿才是真"
                 "仓位表达。**丑话**:跨两轮共五个候选,全族校正 p=0.10,证据等级"
                 "「知情上」;利润前重后轻(2020-22 赚大头,近三年只是没亏);"
                 "2023/2025 是小亏年。验收 REPORT_FGSA_LINK_v2。"),
    }


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
        # 走扩/收窄以 0 轴为准(运营者 2026-08-25 纠正):收窄=向 0 轴靠近。价差为负时
        # 玻璃更强(数值上行)是收窄不是走扩——前端按当前价差在 0 轴哪侧翻文案,
        # 这里的 direction 字段只说数值方向(widen=数值上行),别直接当文案用。
        "note": "玻璃与纯碱的**相对**资金流向。正=玻璃这边资金相对更强,价差(FG−SA)"
                "数值倾向上行——价差在 0 轴下方时=收窄(向 0 轴),上方时=走扩(离 0 轴);"
                "负号反之。**这是背景不是交易信号**——它预测的是价差方向,"
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
    # 永安对冲簿状态卡(DEC-142,展示级):算不出(材料缺)就 None,不拖垮 pair 信号。
    try:
        payload["hedge_book"] = fgsa_hedge_book()
    except Exception as e:                      # noqa: BLE001
        print(f"[pair] hedge_book 失败,置空:{e}", file=sys.stderr)
        payload["hedge_book"] = None
    try:
        payload["follow_plan"] = follow_plan_payload()
    except Exception as e:                      # noqa: BLE001
        print(f"[pair] follow_plan 失败,置空:{e}", file=sys.stderr)
        payload["follow_plan"] = None
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
        if RULES.get("fixed_members"):
            groups, log, cuts = fixed_groups(RULES["fixed_members"], seat, price, mkt.index,
                                             v.get("fixed_since", "2026-08-23"))
        else:
            groups, log, cuts = rolling_groups(seat, price, mkt.index)
            if RULES.get("group_overrides"):
                groups, log = apply_group_overrides(groups, log, cuts, RULES["group_overrides"], seat, price)
        sig = signal_series(seat, groups)
        if RULES["signal_source"] == "cost":
            sig = attach_cost_signal(sig, seat, mkt, groups)
        if RULES["exit_mode"] == "inst":
            sig = attach_inst_exit(sig, seat, mkt, groups)
        if RULES["long_mode"] == "unload_bounce":
            sig = attach_bounce_long(sig, seat, mkt, groups)
        payload = build_payload(sig, mkt, seat, groups, log, cuts, op, st,
                                vols=contract_volumes(price_raw),
                                # 沉淀资金要全市场持仓(DEC-151);用 clean 后的行情表
                                # (trade_date 已保证是时间型,asof 才可用)。
                                oi=price.pivot_table(index="trade_date", columns="contract",
                                                     values="open_interest",
                                                     aggfunc="first").sort_index(),
                                # 筹码地图的 5 日高低与收盘(DEC-152)。
                                hi=price.pivot_table(index="trade_date", columns="contract",
                                                     values="high_price", aggfunc="max").sort_index(),
                                lo=price.pivot_table(index="trade_date", columns="contract",
                                                     values="low_price", aggfunc="min").sort_index(),
                                close=price.pivot_table(index="trade_date", columns="contract",
                                                        values="close_price",
                                                        aggfunc="first").sort_index())
    except Exception as e:                      # noqa: BLE001
        print(f"[{code}] 失败,保留上一版:{e}", file=sys.stderr)
        return None
    SIG_CACHE[code] = sig
    # 玻纯对冲簿(DEC-142):跨品种块在 pair_fgsa 里合成,这里只把单品种材料放进小缓存。
    # 腿成本**不在引擎算**(运营者 2026-08-25:「成本直接引用净持仓的成本,不需要你
    # 单独算」):前端拿浏览器登录态调 /seats/net-position,显示的就是净持仓页同一台
    # 成本引擎的同一个数。引擎只出状态与净持仓(net_off 口径)。
    # 跨月跟随方案(DEC-168):挂在品种自己的产物里,不像玻纯那张要等两个品种都跑完。
    if code in CAL_FOLLOW:
        _d = mkt.index[-1]
        _m = CAL_FOLLOW[code]["member"]
        # **只取当日那一行,不是「每个合约最后一次」**:后者会把 JM2309/JM2401 这些
        # 早就到期的老合约按它们最后一天的持仓算进来,首测把东证的两腿从
        # 5,775/10,059 撑成 10,391/13,724(比例 1:1.74 → 1:1.32)。口径与净持仓页
        # 一致:那页也是只看选定交易日在榜的合约。
        _rows = seat[(seat["member_key"] == _m) & (seat["trade_date"] == _d)]
        _last = (_rows.groupby("contract")["net_off"].sum()
                 if len(_rows) else pd.Series(dtype=float))
        _net = {c: float(v) for c, v in _last.items()
                if isinstance(c, str) and np.isfinite(v) and v}
        _px = {c: float(st[c].asof(_d)) for c in st.columns
               if isinstance(c, str) and np.isfinite(st[c].asof(_d))}
        # 主力合约传给选腿逻辑:优先在主力上下单(盘口最深)。
        CURRENT["_main_contract"] = (mkt["main"].iloc[-1]
                                     if isinstance(mkt["main"].iloc[-1], str) else None)
        try:
            payload["follow_plan"] = cal_follow_plan_payload(code, _net, _px)
        except Exception as e:                  # noqa: BLE001
            print(f"[{code}] follow_plan 失败,置空:{e}", file=sys.stderr)
            payload["follow_plan"] = None

    if code in ("FG", "SA"):
        _d = mkt.index[-1]
        # **只取当日那一行**(2026-08-31 修)。原来是 `<= _d` + 每合约取最后一次,
        # 于是 FG1303(2013 年)、SA2005 这些**早已到期**的合约按它们最后一天的持仓
        # 被一路带到今天:FG 129 个合约里只有 7 个是当日的,SA 更离谱 —— 卡上以为
        # 永安还有 8,508 手玻璃…不,是纯碱多单,**当日真实是 0**。
        # 注释本来就写着「最新日永安在各合约的净持仓」,实现没照办。
        # 影响的是运营者照这张卡定的手数比例,不是显示噪音。
        _rows = seat[(seat["member_key"] == "永安期货") & (seat["trade_date"] == _d)]
        _last = (_rows.groupby("contract")["net_off"].sum()
                 if len(_rows) else pd.Series(dtype=float))
        PAIR_EXTRA[code] = {"ya": _member_main_net(seat, mkt, "永安期货"),
                            "ret_open": mkt["ret_open"], "main": mkt["main"],
                            # 跟随方案(DEC-154)要逐腿:最新日永安在各合约的净持仓 + 结算价。
                            "ya_all": {c: float(v) for c, v in _last.items()
                                       if isinstance(c, str) and np.isfinite(v) and v},
                            "px_all": {c: float(st[c].asof(_d)) for c in st.columns
                                       if isinstance(c, str) and np.isfinite(st[c].asof(_d))}}

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
    # IH 核心席位看板(DEC-172):它不走 run_one 那套阵营/z 分数流程 —— 信号是
    # 「跟某几家席位的在场方向」,形状完全不同,硬塞进去只会两边都别扭。
    # 单独取数、单独写产物;失败只告警,不拖垮其它品种。
    try:
        if src == "csv":
            _p, _s = load_from_csv("IH", Path(os.environ.get("CSV_DIR", "../research/data")))
        else:
            _p, _s = load_from_pg(
                "IH",
                os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
                os.environ.get("PG_USER", "futures_app"),
                os.environ.get("PG_DB", "futures_platform"))
        _ih = ih_follow_payload(_p, _s)
        if _ih:
            _out = out_dir / "ih_signals.json"
            _tmp = _out.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_ih, ensure_ascii=False, indent=2), encoding="utf-8")
            _tmp.replace(_out)
            print(f"[IH] {_ih['data_date']} 写出 {_out} | 状态 {_ih['state']} | "
                  f"在场 {_ih['on_count']} 家 | 历史 {_ih['stats']['rounds']} 轮")
    except Exception as e:                      # noqa: BLE001
        print(f"[IH] 看板失败,保留上一版:{e}", file=sys.stderr)

    # 铁矿石「跟永安」第二引擎(DEC-178):与 IH 同构 —— 不走 run_one,单独取数、
    # 单独写产物、失败只告警。**加在这里就必须同时改 run-smart-money.sh 的两处**
    # (CSV 导出循环 + 产物拷贝清单),2026-09-01 IH 首发就是漏了导出那一行,
    # 引擎报「拿不到 CSV」、产物根本没生成。
    try:
        if src == "csv":
            _p, _s = load_from_csv("I", Path(os.environ.get("CSV_DIR", "../research/data")))
        else:
            _p, _s = load_from_pg(
                "I",
                os.environ.get("PG_CONTAINER", "futures-analysis-platform-postgres-1"),
                os.environ.get("PG_USER", "futures_app"),
                os.environ.get("PG_DB", "futures_platform"))
        _i = i_follow_payload(_p, _s)
        if _i:
            _out = out_dir / "i_signals.json"
            _tmp = _out.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_i, ensure_ascii=False, indent=2), encoding="utf-8")
            _tmp.replace(_out)
            print(f"[I] {_i['data_date']} 写出 {_out} | 跟 {_i['member']} | "
                  f"方向 {_i['side']} | 历史 {len(_i.get('history') or [])} 段")
    except Exception as e:                      # noqa: BLE001
        print(f"[I] 跟随卡失败,保留上一版:{e}", file=sys.stderr)

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
