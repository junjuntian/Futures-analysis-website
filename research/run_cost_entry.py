"""全新进场信号:跟机构成本差不多就上车,不等流量爆发 —— 运营者规格,正面实测。

## 起因(2026-08-22,运营者原话的翻译)

现行进场是**流量信号**:5 日净持仓变化 z ≥ 1 才进 —— 那是机构**大规模**进场
的确认时刻,趋势往往已经启动。限价回踩实验(REPORT_LIMIT_ENTRY_v1)证明了
这一点的镜像:信号响了之后愿意回踩的行情都是不走的行情。
运营者的结论:不是执行慢,是**信号本身慢**。要求做一个**状态信号**:

  1. 机构在场(有方向的净持仓);
  2. **价格不劣于机构均价**(做多:价 ≤ 机构成本;做空:价 ≥ 机构空头成本);
  3. **机构没有大规模减仓止损**(已卸掉 X% 低于阈值);
  → 三条同时成立就进场,不等 z 爆发。这样进场发生在吸筹/横盘段,成本与机构同级。

## 与已有负结果的关系(必须先说清,免得像在翻旧案)

- REPORT_INST_COST_v1 否掉的是「成本优势**单独**当预测因子」;
- REPORT_FOLLOW_BUILD_v1 说的是「**跟着流量信号**进场时,成本优势结构上拿不到
  (晚一天,摩擦 0.46~1.21pp/轮)」;
- 本实验不同:不跟流量,直接在**价位回到机构成本**时进,成本优势是**构造出来**的
  (进场价 ≤ 机构均价是进场条件本身),不是追出来的。这三者检验的是三个不同命题。

## 口径(全部复用引擎,不另写一套)

- 机构 = 引擎同一套滚动 5 家 alpha 席位组(`rolling_groups`);
- 净持仓 = `signal_series` 的 PIT 口径(当天只用官方行,DEC-108);
- 机构成本 = 逐日 VWAP 重建:**加仓那天按主力结算价加权**,减仓成本不动,
  换组/方向翻转重置,掉榜日冻结(不知道 ≠ 没动)。这是研究代理量:
  席位数据是前 20 截断、跨合约合计,重建出的是近似成本,不是交易所真值;
- 已卸掉 X% = `unload_series`(换组重置/掉榜冻结与引擎一致);
- 出场**一字不动**(散户反向/止损 6%/持满 40/临近交割 10),成交仍是次日开盘 ——
  monkey-patch `entry_exit_signals` 只换 z_in,z_out 仍是散户 rz;
- `long_needs_dip` 关掉:价格回到成本本身就是回撤条件,再叠 dip 是双重计数。

## 事先写死的判据

- **主规格**:容差 0%(价格严格不劣于成本)、卸仓阈值 30%,双向;
- 赢基线 ≥ 3/5 品种才算方向成立;参数面(容差 0/1%,卸仓 30/50%)不许翻脸;
- 玻璃纯碱是主战场(样本 13/6 年);三个大商所品种样本短,只当旁证。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"

BASE = {"SA": (111, 64.6), "FG": (228, 69.8), "JD": (26, 24.5),
        "JM": (21, 67.7), "LH": (18, 90.8)}


def load(code):
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    # 研究里的「基线」永远指**流量信号**(方案 C)。DEC-112/113 之后 use("JD"/"SA")
    # 会把 signal_source 注入成 "cost",不钉回去,基线回放就会走 cost 分支
    # 并因 sig 没有 cost_z 列而 KeyError —— 2026-08-22 玻璃实验时当场踩到。
    H.RULES["signal_source"] = "resonance"
    low = code.lower()
    price = H.clean_price(pd.read_csv(DATA / f"{low}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(DATA / f"{low}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    unload = H.unload_series(sig, seat, groups)["pct"]
    return sig, mkt, rdf, op, st, groups, unload


def cost_series(sig, mkt, groups, price_col="settle"):
    """机构均价的逐日重建。加仓按当日主力价(`price_col`)加权;减仓不动;
    换组/翻向重置;掉榜日成本冻结但**当天不发信号**(返回 NaN)。
    另带出 age(本轮已持续的可见交易日数,闸门用它查「翻向日进场」的占比)。"""
    net = sig["net"]
    px = mkt[price_col]
    side_s = pd.Series(0, index=net.index, dtype=float)
    cost_s = pd.Series(np.nan, index=net.index, dtype=float)
    age_s = pd.Series(np.nan, index=net.index, dtype=float)
    cur_grp, side, qty, cost, age = None, 0, 0.0, np.nan, 0
    for d in net.index:
        grp = groups.get(d)
        if grp != cur_grp:
            cur_grp, side, qty, cost, age = grp, 0, 0.0, np.nan, 0
        n = net.get(d, np.nan)
        p = px.get(d, np.nan)
        if not np.isfinite(n) or not np.isfinite(p):
            continue                     # 掉榜/缺价:冻结,当天不给值
        s = int(np.sign(n)) if n != 0 else 0
        if s == 0:
            side, qty, cost, age = 0, 0.0, np.nan, 0
            continue
        if s != side:
            side, qty, cost, age = s, abs(n), p, 0   # 新一轮:成本从翻向日起算
        else:
            dn = abs(n) - qty
            if dn > 0:
                cost = (cost * qty + dn * p) / (qty + dn)
            qty = abs(n)
            age += 1
        side_s[d] = side
        cost_s[d] = cost
        age_s[d] = age
    return side_s, cost_s, age_s


def build_entry(sig, mkt, groups, unload, tol, umax, need_adding=False,
                price_col="settle", min_age=0, skip_group_days=0):
    """⚠️ `unload_series` 的 pct 是 **0~1 的小数**不是百分数 —— 第一版把阈值写成
    30/50,条件从未生效,跑出来的是「无卸仓过滤」的结果。教训:接别人的列,
    先 describe() 看量纲再用。"""
    side_s, cost_s, age_s = cost_series(sig, mkt, groups, price_col)
    px = mkt[price_col]
    z = pd.Series(0.0, index=mkt.index)
    ok_unload = unload.reindex(mkt.index).fillna(np.inf) <= umax
    # 掉榜日 unload 是 NaN → fillna(inf) → 不进场(不知道 ≠ 没减仓)
    chg = sig["chg"].reindex(mkt.index)
    # 换组后的第几个交易日(闸门:排除换组余波)
    gch = pd.Series(0, index=mkt.index, dtype=int)
    prev_g, k = None, 10**6
    for d in mkt.index:
        g = groups.get(d)
        k = 0 if g != prev_g else k + 1
        prev_g = g
        gch[d] = k
    for d in mkt.index:
        s = side_s.get(d, 0)
        c = cost_s.get(d, np.nan)
        p = px.get(d, np.nan)
        if s == 0 or not np.isfinite(c) or not np.isfinite(p) or not ok_unload.get(d, False):
            continue
        if age_s.get(d, 0) < min_age or gch.get(d, 10**6) < skip_group_days:
            continue
        if need_adding:
            g = chg.get(d, np.nan)
            if not (np.isfinite(g) and np.sign(g) == s):
                continue        # 变体 B:近 sig_win 日机构还在同向加仓(「补仓我们也补」)
        if s > 0 and p <= c * (1 + tol):
            z[d] = 1.5
        elif s < 0 and p >= c * (1 - tol):
            z[d] = -1.5
    return z


def perf(tr, daily):
    closed = [t for t in tr if t["exit_date"]]
    r = [t["ret_pct"] for t in closed]
    p = H._perf(daily)
    win = sum(1 for x in r if x > 0) / max(len(r), 1) * 100
    return len(closed), win, p["cum_pct"], p["max_dd_pct"], p["sharpe"]


def main():
    orig = H.entry_exit_signals
    for code in ("FG", "SA", "JD", "JM", "LH"):
        sig, mkt, rdf, op, st, groups, unload = load(code)
        H.RULES["long_enabled"] = True
        H.RULES["long_needs_dip"] = False
        bn, bc = BASE[code]
        print("=" * 92)
        print(f"[{code}] 基线(现行流量信号) {bn} 笔 / 累计 {bc:+.1f}%")
        print("=" * 92)
        combos = [(0.0, 0.3, False), (0.0, 0.5, False), (0.01, 0.3, False),
                  (0.0, 999.0, False),          # 无卸仓过滤对照(=第一版跑出的东西)
                  (0.0, 0.3, True), (0.0, 0.5, True)]
        for tol, umax, adding in combos:
            z_in = build_entry(sig, mkt, groups, unload, tol, umax, adding)
            H.entry_exit_signals = lambda s, r, _z=z_in: (_z, r["rz"])
            try:
                tr, _, daily = H.replay(sig, mkt, rdf, op, st)
            finally:
                H.entry_exit_signals = orig
            n, win, cum, dd, sh = perf(tr, daily)
            utag = "无" if umax > 1 else f"≤{umax:.0%}"
            btag = "+还在加仓" if adding else "        "
            star = " ← 主规格" if (tol == 0.0 and umax == 0.3 and not adding) else ""
            print(f"  容差 {tol*100:.0f}% / 卸仓{utag} {btag}: {n:>4} 笔  "
                  f"胜率 {win:4.1f}%  累计 {cum:>+7.1f}%  回撤 {dd:>+6.1f}%  "
                  f"夏普 {sh}{star}")
        print()


if __name__ == "__main__":
    main()
