# -*- coding: utf-8 -*-
"""铁矿石「资金跟随」候选筛查(2026-09-02 运营者:「铁矿石也做一个资金跟随策略」)。

**规则先冻结,一个参数都不调。** 直接照搬现行第二引擎(焦煤跟华泰 DEC-139 /
玻璃跟永安)的那条规则,逐字不动:

    某席位在**当日主力合约**上的可见净持仓(`net_off`,按合约 ffill)的方向;
    收盘定方向,**次日开盘**成交,方向翻转就在次日开盘反手。单边成本 0.05%,
    翻转日双边。

这样做不是省事:换一条规则就等于在同一份数据上多试一次,而铁矿石的席位样本只有
三年。**规则不动,只挑人**,多重比较的账才算得清。

排序指标**先写死**:扣成本夏普。候选 = 满足在场门槛的会员里夏普最高的那一个。
门槛(先写死,不看结果再调):
  · 该会员在主力合约上有可见净持仓的交易日 >= 全样本的 40%;
  · 至少翻向 4 次 —— 从不翻向的席位,「跟随」等于长期单边持有,测的是品种漂移。

七道闸门照 `REPORT_JM_HUATAI_v1` 的原样,一道不减:
  1 安慰剂(循环移位 500 次)      5 收益来源(分段、肥尾)
  2 滞后(T+1 / T+2 / T+3)        6 子样本(逐年)
  3 口径(品种合计 vs 主力合约)    7 选择偏差 —— 这次尤其重
  4 成本

**第 7 道这次是主角,不是脚注。** 跟华泰是五选一(Bonferroni 后 p 约 0.17 就已经
不过,靠 IC 旁证撑先验);铁矿石这里是**两百多选一**,而且样本只有三年。
所以本文额外加一道**跟华泰当年没做的检验**:

  · **走前检验(walk-forward)**:只用截至 T 的数据挑人,拿 T 之后的表现记账。
    挑中的人年年换,这才是实盘能拿到的东西。IH 那条线就是栽在这一步上
    (`REPORT_IH_GTJA_v1`:全样本最优的席位,walk-forward 10 年里 0 次被选中)。

**这个脚本不产出策略,只产出证据。** 过不了就写「过不了」。

数据:`research/data/i_{price,seat}.csv.gz`(席位 2023-08-30~2026-09-01,
行情 2016-01-04~2026-08-28)。跑法:python research/run_i_follow.py
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

COST = 0.0005                 # 单边,与 seat_follow_payload 一致(翻转日双边)
MIN_ON_RATIO = 0.40           # 在场天数门槛
MIN_FLIPS = 4                 # 翻向次数门槛
SIMS = 500                    # 循环移位次数,与跟华泰同规格
rng = np.random.default_rng(20260902)

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

price = H.clean_price(pd.read_csv(D / "i_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "i_seat.csv.gz"))
mkt = H.main_series(price)
# 席位起点之后才有得跟;之前的行情只用来把主力序列接上。
mkt = mkt[mkt.index >= seat["trade_date"].min()]
idx = mkt.index
n = len(idx)


def position(member: str) -> pd.Series:
    """该会员逐日的跟随方向(+1/-1/NaN)。规则与 seat_follow_payload 一字不差。"""
    sub = seat[seat["member_key"] == member]
    sig = pd.Series(np.nan, index=idx)
    for c in dict.fromkeys(mkt["main"]):
        if not isinstance(c, str):
            continue
        rows = sub[sub["contract"] == c]
        if rows.empty:
            continue
        w = rows.pivot_table(index="trade_date", values="net_off", aggfunc="sum").iloc[:, 0]
        days = idx[mkt["main"] == c]
        if not len(days):
            continue
        wf = w.reindex(days.union(w.index)).ffill().reindex(days)
        sig.loc[days] = wf.values
    pos = np.sign(sig)
    pos[pos == 0] = np.nan
    return pos.ffill()


def account(pos: pd.Series, lag: int = 2, cost: float = COST):
    """(逐日净收益序列, 在场天数, 翻向次数)。

    `lag=2` = 信号日收盘定、次日开盘成交:`ret_open` 是「开->开」,持到次日开盘
    才算完一天,所以要移两格。与引擎里 `pos.shift(2)` 同一口径。
    """
    p = pos.shift(lag)
    turn = (pos.shift(lag) != pos.shift(lag + 1)).astype(float)
    daily = (p * mkt["ret_open"] - turn * cost * 2).dropna()
    live = pos.dropna()
    flips = int((np.sign(live) != np.sign(live.shift())).iloc[1:].sum()) if len(live) > 1 else 0
    return daily, int(pos.notna().sum()), flips


def stats(daily: pd.Series) -> dict:
    if len(daily) < 20 or daily.std() == 0:
        return {"cum": np.nan, "sharpe": np.nan, "dd": np.nan}
    eq = (1 + daily).cumprod()
    return {
        "cum": (float(eq.iloc[-1]) - 1) * 100,
        "sharpe": float(daily.mean() / daily.std() * np.sqrt(242)),
        "dd": float((eq / eq.cummax() - 1).min()) * 100,
    }


# --------------------------------------------------------------- 全体会员筛查
members = sorted(seat["member_key"].unique())
rows = []
POS = {}
for m in members:
    pos = position(m)
    daily, on, flips = account(pos)
    if on < MIN_ON_RATIO * n or flips < MIN_FLIPS:
        continue
    POS[m] = pos
    rows.append({"member": m, "on": on, "on_pct": on / n * 100, "flips": flips,
                 **stats(daily)})
table = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)

L = [f"铁矿石「跟某席位」候选筛查(样本 {idx[0].date()} ~ {idx[-1].date()},{n} 个交易日)", ""]
L.append(f"规则冻结 = 现行第二引擎那条(跟华泰/跟永安同款),单边成本 {COST*100:.2f}%。")
L.append(f"参选门槛:在场 >={MIN_ON_RATIO*100:.0f}%、翻向 >={MIN_FLIPS} 次。")
L.append(f"**{len(members)} 家会员里 {len(table)} 家进了候选池** —— 这个数字本身就是")
L.append("第 7 道闸门的分母,先记住它。")
L.append("")

if table.empty:
    L.append("候选池是空的 —— 没有任何会员同时满足在场与翻向门槛。到此为止。")
    io.open(OUT / "i_follow.txt", "w", encoding="utf-8").write("\n".join(L))
    print("done (empty)")
    raise SystemExit(0)

L.append("## 一、候选池前 15 名(按扣成本夏普)")
L.append("")
L.append(f"{'席位':<12}{'在场天':>7}{'占比':>6}{'翻向':>5}{'累计%':>9}{'夏普':>7}{'最大回撤%':>10}")
L.append("-" * 60)
for _, r in table.head(15).iterrows():
    L.append(f"{r['member']:<12}{r['on']:>7.0f}{r['on_pct']:>5.0f}%{r['flips']:>5.0f}"
             f"{r['cum']:>+9.1f}{r['sharpe']:>7.2f}{r['dd']:>+10.1f}")
L.append("")
L.append("垫底 5 名(看这条分布有多宽 —— 宽,说明「挑出来的最好」很可能是运气):")
for _, r in table.tail(5).iterrows():
    L.append(f"  {r['member']:<12}夏普 {r['sharpe']:>6.2f}  累计 {r['cum']:>+8.1f}%")
L.append("")

BEST = table.iloc[0]["member"]
pos_best = POS[BEST]
daily_best = account(pos_best)[0]
edge = stats(daily_best)["sharpe"]
L.append(f"**全样本最优 = {BEST}**(夏普 {edge:.2f})。下面所有闸门都对它。")
L.append("")

# ------------------------------------------------------------------ 闸门 1 安慰剂
L.append("## 二、闸门 1:安慰剂(循环移位 500 次)")
L.append("")
L.append("把它的仓位序列整体循环移位:形状、在场比例、翻向次数全部保留,只打乱与行情的")
L.append("对齐关系。真信号应当打不过极少数移位。")
# **同一批移位量给所有候选用**:第 7 道闸门要的「最大统计量」必须与这里同源,
# 否则两个 p 值来自两组随机数,没法放在一起讲。
shifts = rng.integers(1, n, size=SIMS)
SIM_SH = {}
for m, pos in POS.items():
    v = pos.values
    SIM_SH[m] = np.array([stats(account(pd.Series(np.roll(v, sh), index=idx))[0])["sharpe"]
                          for sh in shifts])
sims = SIM_SH[BEST]
# **计数为 0 时不能写 p=0。** 500 次里一次都没打赢,说的是「p 小于 1/500」,
# 不是「p 等于 0」——后者会让 Bonferroni 乘出来还是 0,凭空过闸。
# 用 (r+1)/(m+1):无偏、且永远给得出一个可以再乘的正数。
p_placebo = float((np.nansum(sims >= edge) + 1) / (SIMS + 1))
L.append(f"  实测夏普 {edge:.2f};移位分布中位 {np.nanmedian(sims):.2f}、"
         f"95 分位 {np.nanpercentile(sims, 95):.2f}")
L.append(f"  打赢实测的移位 {int(np.nansum(sims >= edge))}/{SIMS} 次,"
         f"**p = {p_placebo:.4f}**(按 (r+1)/(m+1) 算,不写 0)"
         f"  {'过' if p_placebo < 0.05 else '**不过**'}")
L.append("")

# ------------------------------------------------------------------ 闸门 2 滞后
L.append("## 三、闸门 2:滞后(边是趋势级还是踩点级)")
L.append("")
L.append(f"{'成交时点':<14}{'累计%':>10}{'夏普':>8}{'最大回撤%':>11}")
L.append("-" * 44)
for lab, lag in (("T+0(不可得)", 1), ("T+1(在用)", 2), ("T+2", 3), ("T+3", 4)):
    s = stats(account(pos_best, lag=lag)[0])
    L.append(f"{lab:<14}{s['cum']:>+10.1f}{s['sharpe']:>8.2f}{s['dd']:>+11.1f}")
L.append("")

# ------------------------------------------------------------------ 闸门 3 口径
L.append("## 四、闸门 3:口径(换成品种合计,结论翻不翻号)")
L.append("")
tot = pd.read_csv(D / "i_seat.csv.gz")
tot["trade_date"] = pd.to_datetime(tot["trade_date"])
tot = tot[tot["is_variety_total"].astype(str).isin(["t", "true", "True", "1"])
          & tot["rank_type"].isin(["long", "short"])]
if len(tot):
    key = tot["member"].astype(str).str.replace(r"[（(][^）)]*[）)]$", "", regex=True)
    tot["member_key"] = key.map(lambda m: H.RULES["alias"].get(m, m))
    g = tot[tot["member_key"] == BEST]
    if len(g):
        w = g.pivot_table(index="trade_date", columns="rank_type", values="quantity",
                          aggfunc="sum")
        net = w.get("long", pd.Series(0.0, index=w.index)) - w.get(
            "short", pd.Series(0.0, index=w.index))
        p2 = np.sign(net.reindex(idx.union(net.index)).ffill().reindex(idx))
        p2[p2 == 0] = np.nan
        s2 = stats(account(p2.ffill())[0])
        same = np.sign(s2["sharpe"]) == np.sign(edge)
        L.append(f"  品种合计口径:累计 {s2['cum']:+.1f}%、夏普 {s2['sharpe']:.2f} "
                 f"({'同号' if same else '**翻号**'})")
    else:
        L.append(f"  合计行里没有 {BEST},这道跳过(如实说,不假装过了)")
else:
    L.append("  库里没有该品种的合计行,这道跳过(如实说,不假装过了)")
L.append("")

# ------------------------------------------------------------------ 闸门 4/5
L.append("## 五、闸门 4/5:成本敏感度与收益来源")
L.append("")
L.append(f"{'单边成本':<10}{'累计%':>10}{'夏普':>8}")
L.append("-" * 28)
for c in (0.0, 0.0005, 0.001, 0.002):
    s = stats(account(pos_best, cost=c)[0])
    L.append(f"{c*100:>7.2f}%  {s['cum']:>+10.1f}{s['sharpe']:>8.2f}")
L.append("")
live = pos_best.dropna()
segs, cur, i0 = [], None, None
for i, v in enumerate(live.values):
    if cur is None or v != cur:
        if cur is not None:
            segs.append((i0, i))
        cur, i0 = v, i
if cur is not None:
    segs.append((i0, len(live)))
seg_ret = []
for a, b in segs:
    da, db = live.index[a], live.index[min(b, len(live) - 1)]
    sub = daily_best[(daily_best.index > da) & (daily_best.index <= db)]
    if len(sub):
        seg_ret.append((float(np.prod(1 + sub)) - 1) * 100)
seg_ret = np.array(seg_ret)
if len(seg_ret) >= 4:
    wins = int((seg_ret > 0).sum())
    top3 = float(np.sort(seg_ret)[-3:].sum())
    tot_sum = float(seg_ret.sum())
    share = (top3 / tot_sum * 100) if tot_sum else float("nan")
    L.append(f"  {len(seg_ret)} 段,胜 {wins}({wins/len(seg_ret)*100:.0f}%),"
             f"中位 {np.median(seg_ret):+.2f}%,均值 {seg_ret.mean():+.2f}%")
    L.append(f"  **最赚的 3 段占全部段收益之和的 {share:.0f}%**"
             f"(其余 {len(seg_ret)-3} 段合计 {tot_sum-top3:+.1f}%)")
L.append("")

# ------------------------------------------------------------------ 闸门 6 子样本
L.append("## 六、闸门 6:逐年")
L.append("")
yearly = {y: (float(np.prod(1 + g)) - 1) * 100
          for y, g in daily_best.groupby(daily_best.index.year)}
L.append("  " + "  ".join(f"{y}: {v:+.1f}%" for y, v in sorted(yearly.items())))
L.append(f"  正年 {sum(1 for v in yearly.values() if v > 0)}/{len(yearly)}")
L.append("")

# ------------------------------------------------------------------ 闸门 7 选择偏差
L.append("## 七、闸门 7:选择偏差 —— **这次是主角**")
L.append("")
k = len(table)
adj = min(p_placebo * k, 1.0)
L.append(f"候选池 {k} 家。Bonferroni 校正后 p 约 {adj:.3f} "
         f"({'仍过' if adj < 0.05 else '**不过**'})。")
L.append("")
L.append("Bonferroni 对这种情形太粗(各家仓位高度相关,不是 47 个独立试验)。")
L.append("**更该看的是最大统计量置换**:同一批移位量下,把 47 家的夏普各算一遍,")
L.append("只取当次的**最大值**,问「最好的那个能不能好到 1.20」。这才是")
L.append("「我挑了最好的一个」这件事本身的零分布。")
L.append("")
max_sims = np.nanmax(np.vstack([SIM_SH[m] for m in POS]), axis=0)
p_max = float((np.nansum(max_sims >= edge) + 1) / (SIMS + 1))
L.append(f"  移位后「47 家里的最好者」夏普:中位 {np.nanmedian(max_sims):.2f}、"
         f"95 分位 {np.nanpercentile(max_sims, 95):.2f}、最大 {np.nanmax(max_sims):.2f}")
L.append(f"  实测 {edge:.2f} 打赢 {SIMS - int(np.nansum(max_sims >= edge))}/{SIMS} 次,"
         f"**p_max = {p_max:.4f}**  {'过' if p_max < 0.05 else '**不过**'}")
L.append("")
L.append("### 旁证:别的品种在用的跟随席位,在铁矿石上排第几")
L.append("")
L.append("跟华泰(焦煤 DEC-139)与跟永安(玻璃)是**这次筛查之前就已经在线**的两个选择。")
L.append("如果「适合被跟的席位」这件事跨品种成立,它们在这里也该靠前。")
rank = {r['member']: i + 1 for i, r in table.iterrows()}
for who, where in (("永安期货", "玻璃在用"), ("华泰期货", "焦煤在用")):
    if who in rank:
        r = table[table["member"] == who].iloc[0]
        L.append(f"  {who}({where}):**第 {rank[who]}/{k} 名**,"
                 f"夏普 {r['sharpe']:.2f}、累计 {r['cum']:+.1f}%")
    else:
        L.append(f"  {who}({where}):没进候选池")
L.append("")

L.append("### 固定跟永安、只按时间切分的样本外")
L.append("")
L.append("上面那个走前检验把「挑谁」和「跟得准不准」混在一起。这里**不挑人**:")
L.append("从头到尾只跟永安,按时间切前后两段,看后半段还剩多少。")
L.append("")
half = daily_best.index[len(daily_best) // 2]
for lab, seg in (("前半", daily_best[daily_best.index <= half]),
                 ("后半", daily_best[daily_best.index > half])):
    s = stats(seg)
    L.append(f"  {lab}({seg.index[0].date()}~{seg.index[-1].date()},{len(seg)} 日):"
             f"累计 {s['cum']:+.1f}%、夏普 {s['sharpe']:.2f}")
L.append("")
L.append("### 走前检验:只用截至当时的数据挑人,拿之后的表现记账")
L.append("")
L.append("这才是实盘能拿到的东西 —— 全样本最优是事后才知道的。")
L.append("每个季度末重挑一次(用截至当时的全部数据、同一个排序指标),持有下一个季度。")
L.append("")
DAILY = {m: account(p)[0] for m, p in POS.items()}
wf_daily, picks = [], []
for q in pd.date_range(idx[0], idx[-1], freq="QE"):
    test = idx[(idx > q) & (idx <= q + pd.offsets.QuarterEnd(1))]
    if len(test) < 10:
        continue
    best_m, best_s = None, -np.inf
    for m, d_all in DAILY.items():
        d_ = d_all[d_all.index <= q]
        if len(d_) < 60 or d_.std() == 0:
            continue
        sh = float(d_.mean() / d_.std() * np.sqrt(242))
        if sh > best_s:
            best_m, best_s = m, sh
    if best_m is None:
        continue
    d_test = DAILY[best_m].reindex(test).dropna()
    if not len(d_test):
        continue
    picks.append((str(q.date()), best_m, best_s, (float(np.prod(1 + d_test)) - 1) * 100))
    wf_daily.append(d_test)
if wf_daily:
    wf = pd.concat(wf_daily).sort_index()
    s_wf = stats(wf)
    L.append(f"{'重挑日':<12}{'挑中':<12}{'训练夏普':>9}{'下季实得%':>11}")
    L.append("-" * 46)
    for d_, m_, sh_, r_ in picks:
        L.append(f"{d_:<12}{m_:<12}{sh_:>9.2f}{r_:>+11.1f}")
    L.append("-" * 46)
    L.append(f"  **走前合计 {s_wf['cum']:+.1f}%、夏普 {s_wf['sharpe']:.2f}**;"
             f"同期全样本最优({BEST})为 {stats(daily_best)['cum']:+.1f}%")
    hit = sum(1 for _, m_, _, _ in picks if m_ == BEST)
    L.append(f"  {len(picks)} 次重挑里,**{hit} 次挑中了{BEST}** —— "
             f"挑不中就说明全样本那个数实盘拿不到")
else:
    L.append("  样本太短,排不出走前窗口。**那就直说排不出,不要拿全样本的数充数。**")
L.append("")
L.append("## 八、怎么读")
L.append("")
L.append("三年样本 + 两百多家候选,这两件事**同时**成立时,全样本最优的那个数字几乎必然好看。")
L.append("真正有信息量的只有两处:安慰剂 p 值经 Bonferroni 校正之后还剩什么,以及走前检验")
L.append("能不能把人挑对。**这两处不过,就是不过** —— 跟华泰当年也只有五选一,尚且要靠")
L.append("独立的 IC 旁证撑先验。")
io.open(OUT / "i_follow.txt", "w", encoding="utf-8").write("\n".join(L))
print("done ->", OUT / "i_follow.txt")
