"""限价回踩进场 vs 现行次日开盘市价:逐笔归因。

运营者的主张:信号日 D 的结算价 16:26 就知道了;挂限价单在这个价,
后面几天大概率有回踩,踩到就等于按 D 日结算价上了车 —— 那段 +1.08%/日
的「拿不到」说法不成立。

测法(第一步,故意保守地**不动出场**):
  · 基线 = 干净引擎的真实成交单(与生产逐笔一致);
  · 每笔:限价 L = 信号日该合约结算价;从 D+1 起等回踩,窗口 = min(N, 实际出场日);
  · 成交判定(做多):当日 open<=L 按 open 成交(跳低开白捡),否则 low<L 按 L 成交;
    做空对称。0 价格按缺失处理(郑商所无成交写 0);
  · 成交后收益 = side*(exit_px/fill_px - 1),出场日与出场价**沿用基线** ——
    这样两组唯一的差别就是进场,归因干净;
  · 没等到回踩 = 整笔错过,收益 0。

要看的不只是成交率,更是**逆向选择**:错过的那些笔在基线下赚多少 ——
限价单的经典代价是「回踩的都是弱行情,不回踩的才是大肉」。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "engine")
import hog_money as H  # noqa: E402

DATA = pathlib.Path("research/data")
WINDOWS = (1, 2, 3, 5, 10, None)   # None = 一直等到该笔的实际出场日


def run(code):
    v = H.use(code)
    H.CURRENT = {"code": code, **v}
    low = code.lower()
    raw = pd.read_csv(DATA / f"{low}_price.csv.gz")
    price = H.clean_price(raw)
    seat = H.clean_seat(pd.read_csv(DATA / f"{low}_seat.csv.gz"))
    mkt = H.main_series(price)
    op, st = H.contract_prices(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    tr, _, _ = H.replay(sig, mkt, rdf, op, st)
    closed = [t for t in tr if t["exit_date"]]

    # 高低价透视表(0 → 缺失)
    px = price.copy()
    for c in ("high_price", "low_price"):
        px[c] = px[c].replace(0, np.nan)
    hi = px.pivot_table(index="trade_date", columns="contract", values="high_price", aggfunc="first")
    lo = px.pivot_table(index="trade_date", columns="contract", values="low_price", aggfunc="first")
    hi = hi.reindex(mkt.index)
    lo = lo.reindex(mkt.index)
    opx = op.reindex(mkt.index)
    stx = st.reindex(mkt.index)
    pos = {d: i for i, d in enumerate(mkt.index)}
    return closed, hi, lo, opx, stx, pos, mkt.index


def attribute(closed, hi, lo, opx, stx, pos, idx, win):
    filled, missed = [], []
    for t in closed:
        c = t["contract"]
        d0 = pd.Timestamp(t["entry_date"])   # 信号日
        d1 = pd.Timestamp(t["exit_date"])
        if d0 not in pos or d1 not in pos or c not in stx.columns:
            continue
        i0, i1 = pos[d0], pos[d1]
        L = stx[c].iloc[i0]                  # 限价 = 信号日结算价
        if not np.isfinite(L):
            continue
        side = 1 if t["side"] == "long" else -1
        last = i1 if win is None else min(i0 + win, i1)
        fill_px, fill_i = np.nan, None
        for k in range(i0 + 1, last + 1):
            o = opx[c].iloc[k] if c in opx.columns else np.nan
            h = hi[c].iloc[k] if c in hi.columns else np.nan
            lw = lo[c].iloc[k] if c in lo.columns else np.nan
            if side > 0:
                if np.isfinite(o) and o <= L:
                    fill_px, fill_i = o, k
                    break
                if np.isfinite(lw) and lw < L:
                    fill_px, fill_i = L, k
                    break
            else:
                if np.isfinite(o) and o >= L:
                    fill_px, fill_i = o, k
                    break
                if np.isfinite(h) and h > L:
                    fill_px, fill_i = L, k
                    break
        base_ret = t["ret_pct"]
        if fill_i is not None and np.isfinite(t["exit_px"]) and fill_px > 0:
            lim_ret = side * (t["exit_px"] / fill_px - 1) * 100
            filled.append({"base": base_ret, "lim": lim_ret, "wait": fill_i - i0})
        else:
            missed.append({"base": base_ret})
    return filled, missed


def cum(rets):
    return (np.prod([1 + r / 100 for r in rets]) - 1) * 100


def hybrid(closed, hi, lo, opx, stx, pos, idx, win):
    """先挂限价等 win 天,踩到按限价;没踩到第 win+1 天开盘市价追入。不放弃任何一笔。"""
    rets, nlim, nmkt, nskip = [], 0, 0, 0
    for t in closed:
        c = t["contract"]
        d0, d1 = pd.Timestamp(t["entry_date"]), pd.Timestamp(t["exit_date"])
        if d0 not in pos or d1 not in pos or c not in stx.columns:
            continue
        i0, i1 = pos[d0], pos[d1]
        L = stx[c].iloc[i0]
        if not np.isfinite(L):
            continue
        side = 1 if t["side"] == "long" else -1
        last = min(i0 + win, i1)
        fill = None
        for k in range(i0 + 1, last + 1):
            o = opx[c].iloc[k] if c in opx.columns else np.nan
            h = hi[c].iloc[k] if c in hi.columns else np.nan
            lw = lo[c].iloc[k] if c in lo.columns else np.nan
            if side > 0 and np.isfinite(o) and o <= L:
                fill = o
                break
            if side > 0 and np.isfinite(lw) and lw < L:
                fill = L
                break
            if side < 0 and np.isfinite(o) and o >= L:
                fill = o
                break
            if side < 0 and np.isfinite(h) and h > L:
                fill = L
                break
        if fill is not None:
            nlim += 1
        else:
            k = i0 + win + 1
            if k > i1:
                nskip += 1
                continue
            o = opx[c].iloc[k] if c in opx.columns else np.nan
            if not np.isfinite(o):
                nskip += 1
                continue
            fill = o
            nmkt += 1
        rets.append(side * (t["exit_px"] / fill - 1) * 100)
    return rets, nlim, nmkt, nskip


for code in ("SA", "FG", "JD", "JM", "LH"):
    closed, hi, lo, opx, stx, pos, idx = run(code)
    base_all = [t["ret_pct"] for t in closed]
    print("=" * 100)
    print(f"[{code}] 基线 {len(closed)} 笔,累计 {cum(base_all):+.1f}%(出场沿用基线,只换进场)")
    print("=" * 100)
    print(f"{'等待':<8}{'成交':>6}{'错过':>6}{'成交率':>8}{'中位等待':>9}"
          f"{'限价组累计':>12}{'基线累计':>10}{'错过单的基线收益':>17}")
    for win in WINDOWS:
        f, m = attribute(closed, hi, lo, opx, stx, pos, idx, win)
        tag = "到出场" if win is None else f"{win} 天"
        if not f:
            print(f"{tag:<8}{'0':>6}{len(m):>6}      —")
            continue
        lim_c = cum([x["lim"] for x in f])
        base_c = cum([x["base"] for x in f] + [x["base"] for x in m])
        miss_c = cum([x["base"] for x in m]) if m else 0.0
        waits = sorted(x["wait"] for x in f)
        print(f"{tag:<8}{len(f):>6}{len(m):>6}{len(f)/(len(f)+len(m))*100:>7.1f}%"
              f"{waits[len(waits)//2]:>8} 日{lim_c:>+11.1f}%{base_c:>+9.1f}%{miss_c:>+16.1f}%")
    print("  混合(限价等 N 天,没踩到改市价追):")
    for win in (1, 2, 3, 5):
        rets, nlim, nmkt, nskip = hybrid(closed, hi, lo, opx, stx, pos, idx, win)
        print(f"    N={win}: 限价成交 {nlim} / 市价追入 {nmkt} / 没法进 {nskip}"
              f"  → 累计 {cum(rets):+.1f}%  (基线 {cum(base_all):+.1f}%)")
    print()
