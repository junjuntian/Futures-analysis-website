"""焦煤跨月对冲簿(2026-08-31 运营者:「东证这个席位符合」)。

**框架与 run_fg_cal_book.py 一字不差** —— 那是玻璃跨月对冲簿用过的方法,
换品种就该用同一把尺子,不然两边数字没法比。只换三处:品种 JM、单席位换成
东证期货、样本起点由数据决定(焦煤席位史只到 2023-08,三年)。

运营者的观察(净持仓页 2026-08-31):东证 多 5,775 手 / 空 10,059 手 ——
逐合约拆开是 **空近月(JM2610 −6,151、JM2611 −3,739)、多远月(JM2701 +3,547、
JM2705 +1,227、JM2612 +1,001)**,标准的跨月套利结构。本脚本就是去问:
**照它这个结构跟,历史上赚不赚钱。**

两个候选(与玻璃同):
  E1 固定腿:主力 vs 最近的近月,两腿净持仓反向时在场,方向 = 主力腿的符号;
  E2 忠实结构:当日最大净多腿 vs 最大净空腿,要求多腿在远月(东证正是这一型)。

跑法:python research/run_jm_cal_book.py [换腿成本,默认0.002]
"""
import io
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

TURN_COST = float(sys.argv[1]) if len(sys.argv) > 1 else 0.002   # 每次换腿的双腿来回成本
D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
price = H.clean_price(pd.read_csv(D / "jm_price.csv.gz"))
seat = H.clean_seat(pd.read_csv(D / "jm_seat.csv.gz"))
H.use("JM")
mkt = H.main_series(price)
# 席位史起点是硬约束:焦煤只到 2023-08(三禾),比 replay_start 晚
LO = max(pd.Timestamp(H.RULES["replay_start"]), seat["trade_date"].min())
mkt = mkt[mkt.index >= LO]
op, st = H.contract_prices(price)
P = op.combine_first(st)                       # 执行价:开盘缺则结算
idx = mkt.index
groups, log, cuts = H.rolling_groups(seat, price, idx)
if H.RULES.get("group_overrides"):
    groups, log = H.apply_group_overrides(groups, log, cuts, H.RULES["group_overrides"], seat, price)
GRP = list(groups.dropna().iloc[-1])


def net_wide(members):
    sub = seat[seat["member_key"].isin(members)]
    return (sub.pivot_table(index="trade_date", columns="contract", values="net_off", aggfunc="sum")
              .reindex(idx).ffill())


def ym(c):
    return int("".join(ch for ch in str(c) if ch.isdigit())[:4])


def leg_ret(series_of_contract):
    """逐日:该腿当日所属合约自己的执行价日收益(换腿日用新合约自己的前一日价)。"""
    r = pd.Series(np.nan, index=idx)
    for i in range(1, len(idx)):
        c = series_of_contract.iloc[i]
        if not isinstance(c, str) or c not in P.columns:
            continue
        col = P[c].dropna()
        a, b = col.asof(idx[i - 1]), col.get(idx[i], np.nan)
        if np.isfinite(a) and np.isfinite(b) and a:
            r.iloc[i] = b / a - 1
    return r


def perf(d):
    d = pd.Series(d).dropna()
    if not len(d):
        return np.nan, np.nan, np.nan
    eq = (1 + d).cumprod()
    return ((float(eq.iloc[-1]) - 1) * 100,
            float(d.mean() / d.std() * np.sqrt(242)) if d.std() > 0 else np.nan,
            float((eq / eq.cummax() - 1).min()) * 100)


mains = mkt["main"]
active = {}
for i, d in enumerate(idx):
    m = mains.iloc[i]
    if not isinstance(m, str):
        continue
    cands = [c for c in P.columns if isinstance(c, str) and ym(c) < ym(m)
             and np.isfinite(P[c].asof(d)) and H.days_to_window_end(c, d) > 0]
    active[d] = max(cands, key=ym) if cands else None

L = [f"焦煤跨月对冲簿(样本 {idx[0].date()} ~ {idx[-1].date()},{len(idx)} 个交易日;"
     f"换腿成本 {TURN_COST*100:.3f}%)", ""]
L.append(f"当前阵营五家:{'、'.join(GRP)}")
L.append("**样本只有三年**(焦煤席位史起于 2023-08,三禾),比玻璃那份短得多 —— 闸门同样严,"
         "但过了也别当成同等强度的证据。")
L.append("")
rng = np.random.default_rng(61)
results = {}
detail = {}
for src_name, members in (("东证", ["东证期货"]), ("阵营", GRP)):
    W = net_wide(members)
    for cand in ("E1 固定腿(主力vs近月)", "E2 忠实结构(最大多腿vs最大空腿)"):
        far_c = pd.Series(index=idx, dtype=object)
        near_c = pd.Series(index=idx, dtype=object)
        pos = pd.Series(0.0, index=idx)
        for i, d in enumerate(idx):
            m = mains.iloc[i]
            if not isinstance(m, str):
                continue
            if cand.startswith("E1"):
                n = active.get(d)
                if not n or m not in W.columns or n not in W.columns:
                    continue
                a, b = W[m].iloc[i], W[n].iloc[i]
                if not (np.isfinite(a) and np.isfinite(b)) or a * b >= 0:
                    continue      # 同向或缺数据 -> 不在场
                far_c.iloc[i], near_c.iloc[i] = m, n
                pos.iloc[i] = np.sign(a)
            else:
                row = W.iloc[i].dropna()
                row = row[[c for c in row.index if isinstance(c, str)
                           and np.isfinite(P[c].asof(d)) and H.days_to_window_end(c, d) > 0]]
                if row.empty:
                    continue
                longs, shorts = row[row > 0], row[row < 0]
                if longs.empty or shorts.empty:
                    continue
                f, n = longs.idxmax(), shorts.idxmin()
                if ym(f) <= ym(n):
                    continue      # 要求多腿在远月,否则不在场
                far_c.iloc[i], near_c.iloc[i] = f, n
                pos.iloc[i] = 1.0
        ret_sp = (leg_ret(far_c) - leg_ret(near_c)).fillna(0)
        held = pos.shift(2)
        base = (held * ret_sp).dropna()
        cum, sh, mdd = perf(base)
        inmkt = float((held != 0).mean() * 100)
        flips = int((pos != pos.shift()).sum())
        arr, n_ = pos.values, len(pos)
        sh_l = []
        for k in range(500):
            off = int(rng.integers(20, n_ - 20))
            d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret_sp).dropna()
            sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
        p_pl = float((np.array(sh_l) >= sh).mean()) if np.isfinite(sh) else 1.0
        _, sh_t2, _ = perf(pos.shift(3) * ret_sp)
        turn = (pos.shift(2) != pos.shift(3)).astype(float)
        cum_n, sh_n, _ = perf(held * ret_sp - turn * TURN_COST)
        ys = {y: (np.prod(1 + g) - 1) * 100 for y, g in base.groupby(base.index.year)}
        ok = p_pl < 0.05 and (np.isfinite(sh_t2) and sh_t2 >= 0.8 * sh) and cum_n > 0
        key = f"{src_name}|{cand}"
        results[key] = base
        detail[key] = (far_c, near_c, pos, ret_sp)
        L.append(f"{src_name} {cand}: 在场 {inmkt:.0f}%  累计 {cum:+.1f}%  夏普 {sh:.2f}  回撤 {mdd:+.1f}%  切换 {flips}")
        L.append(f"  闸门: 安慰剂 p={p_pl:.3f}(×4={min(p_pl*4,1):.3f})  T+2 {sh_t2:.2f}(需≥{0.8*sh:.2f})"
                 f"  扣成本 {cum_n:+.1f}%/{sh_n:.2f}  -> {'全过' if ok else '不过'}")
        L.append("  逐年: " + "  ".join(f"{y}:{vv:+.0f}%" for y, vv in sorted(ys.items())))
        L.append("")

# 东证 E2 当前在场结构(给界面用的材料,先在这里核对一遍)
far_c, near_c, pos, ret_sp = detail["东证|E2 忠实结构(最大多腿vs最大空腿)"]
L.append("## 东证 E2 最近 10 个交易日的实际两腿")
L.append(f"{'日期':<12}{'多腿(远)':>10}{'空腿(近)':>10}{'在场':>6}{'当日价差收益':>12}")
L.append("-" * 52)
for d in idx[-10:]:
    L.append(f"{str(d.date()):<12}{str(far_c.get(d)):>10}{str(near_c.get(d)):>10}"
             f"{'是' if pos.get(d) else '否':>6}{ret_sp.get(d)*100:>+11.2f}%")
io.open(OUT / "jm_cal_book.txt", "w", encoding="utf-8").write("\n".join(L))
print("done")

# ---------------------------------------------------------------- 全席位扫描
# 运营者点的是东证,但「东证在做跨月」与「跟东证做跨月赚钱」是两件事。
# 既然框架现成,把所有席位的 E2 结构都测一遍,看有没有更值得看的那一家。
# **这是事后择优**,所以同时报安慰剂 p、Bonferroni 阈值与前后半 —— 与 IH 那轮
# 同一套纪律(见 research/PITFALLS 第 5、10 条)。
def e2_for(members):
    W = net_wide(members)
    far_c = pd.Series(index=idx, dtype=object)
    near_c = pd.Series(index=idx, dtype=object)
    pos = pd.Series(0.0, index=idx)
    for i, d in enumerate(idx):
        row = W.iloc[i].dropna()
        row = row[[c for c in row.index if isinstance(c, str)
                   and np.isfinite(P[c].asof(d)) and H.days_to_window_end(c, d) > 0]]
        if row.empty:
            continue
        longs, shorts = row[row > 0], row[row < 0]
        if longs.empty or shorts.empty:
            continue
        f, nn = longs.idxmax(), shorts.idxmin()
        if ym(f) <= ym(nn):
            continue
        far_c.iloc[i], near_c.iloc[i] = f, nn
        pos.iloc[i] = 1.0
    ret = (leg_ret(far_c) - leg_ret(near_c)).fillna(0)
    return pos, ret


counts = seat.groupby("member_key")["trade_date"].nunique()
CAND = [m for m, c in counts.items() if c >= 200]
scan = []
for m in CAND:
    pos, ret = e2_for([m])
    held = pos.shift(2)
    base = (held * ret).dropna()
    if (held != 0).sum() < 100:
        continue
    cum, sh, mdd = perf(base)
    if not np.isfinite(sh):
        continue
    arr, n_ = pos.values, len(pos)
    sh_l = []
    for k in range(400):
        off = int(rng.integers(20, n_ - 20))
        d2 = (pd.Series(np.roll(arr, off), index=idx).shift(2) * ret).dropna()
        sh_l.append(float(d2.mean() / d2.std() * np.sqrt(242)) if d2.std() > 0 else 0.0)
    p_pl = float((np.array(sh_l) >= sh).mean())
    _, sh_t2, _ = perf(pos.shift(3) * ret)
    turn = (pos.shift(2) != pos.shift(3)).astype(float)
    cum_n, sh_n, _ = perf(held * ret - turn * TURN_COST)
    h = len(idx) // 2
    _, sh1, _ = perf((pos.shift(2) * ret).iloc[:h])
    _, sh2, _ = perf((pos.shift(2) * ret).iloc[h:])
    scan.append({"m": m, "cum": cum, "sh": sh, "p": p_pl, "t2": sh_t2, "cum_n": cum_n,
                 "sh_n": sh_n, "days": int((held != 0).sum()),
                 "half": (np.isfinite(sh1) and sh1 > 0) and (np.isfinite(sh2) and sh2 > 0)})
scan.sort(key=lambda r: r["p"])
NS = len(scan)
S = ["", "## 全席位 E2 跨月簿扫描(在场 ≥100 天的 %d 家)" % NS, ""]
S.append(f"**事后择优,按同一套纪律报**:Bonferroni 阈值 = 0.05/{NS} = {0.05/max(NS,1):.4f};")
S.append(f"纯随机下预期 {NS*0.05:.1f} 家 p<0.05。")
S.append("")
S.append(f"{'席位':<10}{'累计':>8}{'夏普':>7}{'p值':>7}{'T+2夏普':>9}{'扣成本累计':>11}{'扣成本夏普':>11}{'在场天':>7}{'分半':>5}")
S.append("-" * 76)
for r in scan[:15]:
    tag = " ← 运营者点的" if r["m"] == "东证期货" else ""
    S.append(f"{r['m']:<10}{r['cum']:>+8.1f}{r['sh']:>7.2f}{r['p']:>7.3f}{r['t2']:>9.2f}"
             f"{r['cum_n']:>+11.1f}{r['sh_n']:>+11.2f}{r['days']:>7}{'  ✓' if r['half'] else '  ✗':>5}{tag}")
dz = next((r for r in scan if r["m"] == "东证期货"), None)
if dz and dz not in scan[:15]:
    S.append("…")
    S.append(f"{dz['m']:<10}{dz['cum']:>+8.1f}{dz['sh']:>7.2f}{dz['p']:>7.3f}{dz['t2']:>9.2f}"
             f"{dz['cum_n']:>+11.1f}{dz['sh_n']:>+11.2f}{dz['days']:>7}{'  ✓' if dz['half'] else '  ✗':>5}  ← 运营者点的")
strong = [r for r in scan if r["p"] < 0.05 and r["cum_n"] > 0 and r["half"]
          and np.isfinite(r["t2"]) and r["t2"] >= 0.8 * r["sh"]]
S.append("")
S.append(f"**过闸(p<0.05 且 扣成本为正 且 T+2 不塌 且 分半均正)= {len(strong)} 家**")
for r in strong:
    S.append(f"   ★ {r['m']}: 累计 {r['cum']:+.1f}% 夏普 {r['sh']:.2f} p={r['p']:.3f} "
             f"扣成本 {r['cum_n']:+.1f}%  {'**过 Bonferroni**' if r['p'] < 0.05/max(NS,1) else '不过 Bonferroni'}")
if not strong:
    S.append("   (无)")
io.open(OUT / "jm_cal_book.txt", "a", encoding="utf-8").write("\n".join(S))
print("scan done: %d seats, %d strong" % (NS, len(strong)))
