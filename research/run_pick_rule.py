"""选人规则对比:现行(1 年 · 择时收益前 5)vs 运营者提议(2 年 · 总盈亏前 10 → 成本最佳 5)。

跑法:`python research/run_pick_rule.py`(产出 `research/out/pick_rule.txt`)

运营者 2026-09-03:「按 2 年计算一次,每 2 年合计收益最多的 10 大席位里面选,
择时收益感觉有点问题,这个应该选持仓成本最佳的席位,最后赚钱了,说明他判断方向准,
择时也厉害。」

**这不等于「按总盈亏选」**(那条平台早测过,样本外单笔 t=3.57 输给择时收益的 5.22)。
运营者提的是**两段式**:总盈亏只当粗筛,真正挑人的是成本。两段式没测过,所以要测。

口径(先定死,否则测的不是他说的那条):
- 合计收益 = 窗口内 `Σ(日结算价差 × 前一日净持仓)`,取前 10;
- 持仓成本最佳 = **建仓价优势**:加仓日结算价按加仓量加权,与该家同期在场均价比,
  **做空建得高为好、做多建得低为好**,统一成「正 = 占便宜」。带方向、可跨席位比。
- **加减仓一律按绝对值判**(PITFALLS 四:净持仓的代数差会把空头减仓读成加仓)。

纪律:
- **walk-forward**:每个重选切点只用切点之前的数据,两条规则一视同仁;
- **对比脚本第一件事是复刻已上线那一路**(PITFALLS 六),下面 `assert` 钉住;
- 只换选人,**进出场四件套一字不动** —— 否则比的不是选人规则。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import hog_money as H  # noqa: E402

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = pathlib.Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
YR = 242
L: list[str] = []


def load(code):
    H.use(code)
    price = H.clean_price(pd.read_csv(D / f"{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(D / f"{code.lower()}_seat.csv.gz"))
    rs = pd.Timestamp(H.RULES["replay_start"])
    price, seat = price[price["trade_date"] >= rs], seat[seat["trade_date"] >= rs]
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= rs]
    return price, seat, mkt


def _panel(seat, price, lo, hi):
    """窗口内的逐日仓位/价差面板,后面三个指标都从它算。"""
    s = seat[(seat["trade_date"] < hi)]
    if lo is not None:
        s = s[s["trade_date"] >= lo]
    d = s.merge(price[["contract", "trade_date", "settle"]],
                on=["contract", "trade_date"], how="inner")
    if d.empty:
        return d
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)]
    return d.assign(dpx=(d["settle"] - d["prev_settle"]) * H.RULES["multiplier"]) if len(d) else d


def pick_operator(seat, price, hi, lo, k=5, top=10):
    """运营者规则:窗口内总盈亏前 `top` 家,再取建仓价优势最好的 `k` 家。"""
    d = _panel(seat, price, lo, hi)
    if d.empty:
        return None
    grp = d.groupby("member_key")
    pnl = grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False)
    days = grp["trade_date"].nunique()
    pnl = pnl[days >= H.RULES["member_min_days"]].sort_values(ascending=False)
    if len(pnl) < k:
        return None
    cand = list(pnl.head(top).index)
    adv = {}
    for m in cand:
        s = d[d["member_key"] == m].copy()
        s["add"] = s["net"].abs() - s["prev_net"].abs()      # 绝对值判加减仓
        a = s[(s["add"] > 0) & (s["prev_net"] != 0)]
        if a.empty:
            continue
        w = a["add"]
        px_add = float((a["settle"] * w).sum() / w.sum())
        px_ref = float(s["settle"].mean())
        if not np.isfinite(px_add) or not px_ref:
            continue
        sign = -float(np.sign((np.sign(a["prev_net"]) * w).sum()))   # 空为 -1 → sign=+1
        adv[m] = sign * (px_add - px_ref) / px_ref * 100
    if len(adv) < k:
        return None
    return tuple(sorted(adv, key=adv.get, reverse=True)[:k])


def groups_of(seat, price, dates, months, picker):
    """按 `months` 个月一个切点,用 picker 选人,做成逐日生效的组序列。"""
    start = dates.min() + pd.Timedelta(days=H.RULES["warmup_days"])
    cuts = pd.date_range(start, dates.max(), freq=f"{months}MS")
    picks, cur, log = {}, None, []
    for c in cuts:
        lo = None if picker is pick_now else c - pd.DateOffset(months=months)
        new = picker(seat, price, c, lo)
        if new:
            if new != cur:
                log.append((c.strftime("%Y-%m-%d"), list(new)))
            cur = new
        picks[c] = cur
    ser = pd.Series(index=dates, dtype=object)
    for d0 in dates:
        v = [c for c in cuts if c <= d0]
        ser[d0] = picks[v[-1]] if v else None
    return ser, log


def pick_now(seat, price, hi, lo=None, k=5):
    """现行:有史以来累计择时收益前 k。"""
    a = H.alpha_upto(seat, price, hi)
    return tuple(a.head(k).index) if len(a) >= k else None


def pick_alpha_win(seat, price, hi, lo, k=5):
    """只把周期/窗口改成 2 年,判据仍是择时收益 —— 拆开运营者提案的第一半。"""
    a = H.alpha_upto(seat, price, hi, lo=lo)
    return tuple(a.head(k).index) if len(a) >= k else None


def pick_alpha_win2y(seat, price, hi, lo, k=5):
    """择时收益只看最近 2 年,**周期不变**(1 年重选)—— 单独测「加时间衰减」。"""
    a = H.alpha_upto(seat, price, hi, lo=hi - pd.DateOffset(months=24))
    return tuple(a.head(k).index) if len(a) >= k else None


def pick_pnl_only(seat, price, hi, lo, k=5):
    """只把判据换成总盈亏,不做成本那一步 —— 拆开第二半。"""
    d = _panel(seat, price, lo, hi)
    if d.empty:
        return None
    grp = d.groupby("member_key")
    pnl = grp.apply(lambda s: (s["dpx"] * s["prev_net"]).sum(), include_groups=False)
    days = grp["trade_date"].nunique()
    pnl = pnl[days >= H.RULES["member_min_days"]].sort_values(ascending=False)
    return tuple(pnl.head(k).index) if len(pnl) >= k else None


def size_of(seat, members, dates):
    """选出来这几家近一年的平均持仓规模 —— 看「成本好」是不是小仓位换来的。"""
    s = seat[seat["member_key"].isin(members) & seat["trade_date"].isin(dates)]
    if s.empty:
        return float("nan")
    return float(s.groupby(["member_key", "trade_date"])["net"].sum().abs().mean())


def run(seat, price, mkt, groups):
    """只换 groups,进出场四件套一字不动。"""
    op, st = H.contract_prices(price)
    sig = H.signal_series(seat, groups)
    rdf, _ = H.retail_series(seat, mkt.index)
    if H.RULES.get("signal_source") == "cost":
        sig = H.attach_cost_signal(sig, seat, mkt, groups)
    if H.RULES.get("exit_mode") == "inst":
        sig = H.attach_inst_exit(sig, seat, mkt, groups)
    if H.RULES.get("long_mode") == "unload_bounce":
        sig = H.attach_bounce_long(sig, seat, mkt, groups)
    closed, _o, daily = H.replay(sig, mkt, rdf, op, st)[:3]
    dl = pd.Series(daily).dropna()
    cum = (np.prod([1 + t["ret_pct"] / 100 for t in closed]) - 1) * 100 if closed else 0.0
    sh = float(dl.mean() / dl.std() * np.sqrt(YR)) if len(dl) > 2 and dl.std() > 0 else np.nan
    eq = (1 + dl).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100) if len(eq) else np.nan
    win = 100 * np.mean([t["ret_pct"] > 0 for t in closed]) if closed else np.nan
    return {"n": len(closed), "cum": cum, "sharpe": sh, "dd": dd, "win": win}


L.append("# 选人规则对比:现行(1 年·择时收益前 5)vs 提议(2 年·总盈亏前 10 → 成本最佳 5)")
L.append("")
L.append("只换选人,进出场一字不动;两条规则都 walk-forward(切点只用切点前的数据)。")
L.append("")
for code in ("SA", "FG", "JM", "JD", "I"):
    try:
        price, seat, mkt = load(code)
    except FileNotFoundError:
        L.append(f"\n## {code}:没有本地快照,跳过")
        continue
    if H.RULES.get("strategy") == "campaign":
        L.append(f"\n## {code}:走战役制(campaign),不在本次对比范围")
        continue
    g_now, log_now = groups_of(seat, price, mkt.index, H.RULES["reselect_months"], pick_now)
    g_new, log_new = groups_of(seat, price, mkt.index, 24, pick_operator)
    if g_new.dropna().empty:
        L.append(f"\n## {code}:提议规则选不出组(样本不足),跳过")
        continue
    # 复刻已上线那一路:与生产同一套 rolling_groups 的组序列必须逐日一致
    prod, _lg, _ct = H.rolling_groups(seat, price, mkt.index)
    if H.RULES.get("group_overrides"):
        prod, _lg = H.apply_group_overrides(prod, _lg, _ct, H.RULES["group_overrides"], seat, price)
        g_now = prod          # 玻璃有点名换人,复刻线上就得带上
    else:
        assert (prod.dropna() == g_now.dropna()).all(), f"{code}:没复刻出线上那一路"
    g_b, _ = groups_of(seat, price, mkt.index, 24, pick_alpha_win)
    g_c, _ = groups_of(seat, price, mkt.index, 24, pick_pnl_only)
    # E 只加时间衰减:周期仍是 1 年,但择时收益只看最近 2 年 —— B 同时改了周期和
    # 窗口两样,不拆开就说不清纯碱变好到底是哪一半的功劳。
    g_e, _ = groups_of(seat, price, mkt.index, H.RULES["reselect_months"], pick_alpha_win2y)
    arms = [("A 现行 1年重选·有史以来择时前5", g_now),
            ("B 2年重选·近2年择时前5", g_b),
            ("C 2年重选·近2年总盈亏前5", g_c),
            ("D 2年重选·盈亏前10→成本最佳5(原案)", g_new),
            ("E 1年重选·近2年择时前5(只加时间衰减)", g_e)]
    L.append("")
    L.append(f"## {code}({H.VARIETIES[code]['name']})")
    L.append("")
    L.append(f"{'规则':<30}{'笔数':>6}{'累计%':>10}{'夏普':>8}{'回撤%':>9}{'胜率%':>8}{'均仓手':>10}")
    for nm, gg in arms:
        if gg is None or gg.dropna().empty:
            L.append(f"{nm}  选不出组")
            continue
        r = run(seat, price, mkt, gg)
        sz = size_of(seat, list(gg.dropna().iloc[-1]), mkt.index[-242:])
        pad = 30 - sum(2 if ord(ch) > 127 else 1 for ch in nm)
        L.append(f"{nm}{' ' * max(pad, 1)}{r['n']:>6}{r['cum']:>10.1f}{r['sharpe']:>8.2f}"
                 f"{r['dd']:>9.1f}{r['win']:>8.1f}{sz:>10,.0f}")
    L.append("")
    for nm, gg in arms:
        if gg is not None and not gg.dropna().empty:
            L.append(f"  {nm} 最后选出:{list(gg.dropna().iloc[-1])}")

(OUT / "pick_rule.txt").write_text("\n".join(L), encoding="utf-8")
print("\n".join(L))
