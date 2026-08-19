"""生猪 Phase 1：按 alpha 重选席位组,并检验这个选择是不是全样本挑出来的幻觉。

Phase 0(REPORT_LH_SKILL_v1.md)发现:合计流向有 t=5.4~7.5 的预测力,但那张
alpha 排行是**用全样本算的**——拿它选席位组再回头验证同一段数据,是自己考自己。
PITFALLS 六第 2 条就是这么栽的(扫描挑出的最优参数当天采纳、次日被回测否决)。

本轮四问:

  P1 席位的 alpha 排名跨年稳不稳?(逐年留一,看排名相关)
  P2 只用当下之前的数据选组,之后那段还管用吗?(滚动样本外,最接近实盘)
  P3 组里放几家?按什么排序选?(K 值与选择准则扫描,全在样本外评)
  P4 合计信号该逐合约各算各的,还是先算品种合计再落主力?

评价指标一律用**样本外**的信号预测力,不用样本内拟合优度。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import lhlib as L

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)

SIG_WIN = 5      # 合计净持仓的变化窗口。Phase 0 已定:20 日窗混入动量(相关 +0.317)
HORIZON = 20     # 主看 20 日
MIN_DAYS = 200   # 席位入选的最少在榜日


def section(t: str):
    print("\n" + "=" * 82)
    print(t)
    print("=" * 82)


# ---------------------------------------------------------------- 基础量

def build() -> tuple[pd.DataFrame, pd.DataFrame]:
    price = L.load_price()
    seat = L.load_seat()
    fwd = L.forward_returns(price, (5, 10, HORIZON))
    df = seat.merge(
        fwd[["contract", "trade_date", "settle", "open_interest", f"fwd{HORIZON}"]],
        on=["contract", "trade_date"], how="inner")
    mc = L.main_contract(price)
    m = mc.set_index("trade_date")["main"]
    px = fwd.set_index(["contract", "trade_date"])
    rows = []
    for d, c in m.items():
        if (c, d) in px.index:
            r = px.loc[(c, d)]
            rows.append((d, c, r["settle"], r[f"fwd{HORIZON}"]))
    main = pd.DataFrame(rows, columns=["trade_date", "main", "settle", "fwd"]).set_index("trade_date")
    # 过去 20 日收益也逐合约算,主力换月那天不跨合约相除
    main["past"] = main.groupby("main")["settle"].pct_change(20)
    return df, main


def seat_alpha(df: pd.DataFrame, lo=None, hi=None) -> pd.DataFrame:
    """窗口内每家的择时收益(alpha = 实际盈亏 − 恒定仓位能赚到的钱)。

    只用窗口内的数据,调用方保证窗口不含要检验的那段——这是整个 Phase 1 的关键,
    alpha 一旦用上未来数据,后面所有样本外结论都作废。
    """
    d = df
    if lo is not None:
        d = d[d["trade_date"] >= lo]
    if hi is not None:
        d = d[d["trade_date"] < hi]
    d = d.sort_values(["member_key", "contract", "trade_date"]).copy()
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)].copy()
    d["pnl"] = (d["settle"] - d["prev_settle"]) * d["prev_net"] * L.MULTIPLIER
    d["dpx"] = (d["settle"] - d["prev_settle"]) * L.MULTIPLIER

    out = d.groupby("member_key").agg(pnl=("pnl", "sum"), days=("trade_date", "nunique"),
                                      avg_net=("prev_net", "mean"))
    # beta = 把净持仓换成它自己的均值(仓位大小不变、方向不变)能赚到的钱
    out["beta"] = d.groupby("member_key").apply(
        lambda s: (s["dpx"] * s["prev_net"].mean()).sum(), include_groups=False)
    out["alpha"] = out["pnl"] - out["beta"]
    for c in ("pnl", "beta", "alpha"):
        out[c] /= 1e8
    return out[out["days"] >= MIN_DAYS * (0.4 if lo is not None else 1.0)].round(3)


# ---------------------------------------------------------------- 信号

def group_signal_variety(df: pd.DataFrame, members: list[str]) -> pd.Series:
    """品种合计口径:组内席位在**全部合约**上的净持仓相加,再取变化。"""
    s = df[df["member_key"].isin(members)].groupby("trade_date")["net"].sum().sort_index()
    return s.diff(SIG_WIN)


def group_signal_percontract(df: pd.DataFrame, members: list[str]) -> pd.DataFrame:
    """逐合约口径:每个合约各算各的合计净持仓与变化。"""
    s = (df[df["member_key"].isin(members)]
         .groupby(["contract", "trade_date"])["net"].sum().sort_index())
    chg = s.groupby(level=0).diff(SIG_WIN).rename("sig")
    return chg.reset_index()


def power_variety(sig: pd.Series, main: pd.DataFrame) -> dict:
    """品种合计信号 vs 主力合约收益。返回相关、控制动量后的偏相关与 t。"""
    j = pd.concat([sig.rename("sig"), main[["fwd", "past"]]], axis=1, sort=True).dropna()
    if len(j) < 40:
        return {}
    r = j["sig"].corr(j["fwd"])
    ry = j["fwd"] - np.polyval(np.polyfit(j["past"], j["fwd"], 1), j["past"])
    rx = j["sig"] - np.polyval(np.polyfit(j["past"], j["sig"], 1), j["past"])
    pr = np.corrcoef(ry, rx)[0, 1]
    t = pr * np.sqrt((len(j) - 3) / max(1e-12, 1 - pr ** 2))
    return {"N": len(j), "corr": r, "partial": pr, "t": t}


def power_percontract(sig: pd.DataFrame, df: pd.DataFrame) -> dict:
    """逐合约信号 vs 各合约自己的收益。

    同一天不同合约的收益高度相关,pooled 相关的 t 会虚高。所以另报**按日聚合**的
    时序 t:每天先在合约横截面上算一个 IC,再看 IC 序列的均值稳不稳——这是横截面
    因子的标准做法,自动处理了同日相关。
    """
    j = sig.merge(df[["contract", "trade_date", f"fwd{HORIZON}"]].drop_duplicates(),
                  on=["contract", "trade_date"], how="inner").dropna()
    if len(j) < 100:
        return {}
    pooled = j["sig"].corr(j[f"fwd{HORIZON}"])
    ic = (j.groupby("trade_date")
            .apply(lambda s: s["sig"].corr(s[f"fwd{HORIZON}"]) if len(s) >= 4 else np.nan,
                   include_groups=False)
            .dropna())
    t_ic = ic.mean() / (ic.std() / np.sqrt(len(ic))) if len(ic) > 5 and ic.std() > 0 else np.nan
    return {"N": len(j), "pooled": pooled, "IC均值": ic.mean(), "IC_t": t_ic, "IC天数": len(ic)}


# ---------------------------------------------------------------- 主流程

def main():
    df, main = build()
    years = sorted(df["trade_date"].dt.year.unique())
    print(f"样本 {df['trade_date'].min():%Y-%m-%d} ~ {df['trade_date'].max():%Y-%m-%d},"
          f" 年份 {years}")

    full = seat_alpha(df).sort_values("alpha", ascending=False)

    # ------------------------------------------------------------ P1
    section("P1 alpha 排名跨年稳不稳(逐年留一)")
    print("每次留出一年、用其余年份算 alpha 排名,再与全样本排名比。")
    print("排名相关低 = 这张排行是靠某一年撑起来的,不能拿来选人。\n")
    ranks = {"全样本": full["alpha"].rank(ascending=False)}
    for y in years:
        sub = df[df["trade_date"].dt.year != y]
        a = seat_alpha(sub)
        ranks[f"去掉{y}"] = a["alpha"].rank(ascending=False)
    rk = pd.DataFrame(ranks)
    common = rk.dropna()
    print(f"共同可比席位 {len(common)} 家,Spearman 排名相关:")
    print(common.corr(method="spearman").round(2).to_string())

    print("\n各口径下的前 6 家(看名单换不换人):")
    for col in rk.columns:
        top = rk[col].dropna().sort_values().head(6).index.tolist()
        print(f"  {col:10s} {'、'.join(top)}")

    # ------------------------------------------------------------ P2
    section("P2 滚动样本外:只用当下之前的数据选组")
    print("训练段选组 → 之后那段检验。这是最接近实盘的问法。\n")
    cuts = [pd.Timestamp("2025-01-01"), pd.Timestamp("2025-07-01"), pd.Timestamp("2026-01-01")]
    for k in (3, 5, 8):
        print(f"--- 组内 {k} 家 ---")
        for cut in cuts:
            tr = seat_alpha(df, hi=cut).sort_values("alpha", ascending=False)
            if tr.empty:
                continue
            grp = tr.head(k).index.tolist()
            te = df[df["trade_date"] >= cut]
            te_main = main[main.index >= cut]
            res = power_variety(group_signal_variety(te, grp), te_main)
            if res:
                print(f"  训练<{cut:%Y-%m}  测试N={res['N']:4d}  "
                      f"corr={res['corr']:+.3f}  控动量偏相关={res['partial']:+.3f}  "
                      f"t={res['t']:+.2f}   组={'、'.join(grp[:4])}"
                      + ("…" if k > 4 else ""))

    # ------------------------------------------------------------ P3
    section("P3 选择准则对比(全部在同一段样本外评)")
    cut = pd.Timestamp("2025-07-01")
    tr = seat_alpha(df, hi=cut)
    te = df[df["trade_date"] >= cut]
    te_main = main[main.index >= cut]
    print(f"训练 < {cut:%Y-%m-%d},测试 ≥ 之。\n")
    print(f"  {'准则':14s}{'K':>3s}{'corr':>9s}{'偏相关':>9s}{'t':>8s}   组")
    for label, key, asc in [("alpha 排序", "alpha", False),
                            ("总盈亏排序", "pnl", False),
                            ("beta 排序(对照)", "beta", False)]:
        for k in (3, 5, 8):
            grp = tr.sort_values(key, ascending=asc).head(k).index.tolist()
            res = power_variety(group_signal_variety(te, grp), te_main)
            if res:
                print(f"  {label:14s}{k:>3d}{res['corr']:>+9.3f}{res['partial']:>+9.3f}"
                      f"{res['t']:>+8.2f}   {'、'.join(grp[:3])}" + ("…" if k > 3 else ""))
    # 对照组:全部席位一起当"市场",看挑人到底有没有价值
    allm = tr.index.tolist()
    res = power_variety(group_signal_variety(te, allm), te_main)
    if res:
        print(f"  {'全部席位(对照)':14s}{len(allm):>3d}{res['corr']:>+9.3f}"
              f"{res['partial']:>+9.3f}{res['t']:>+8.2f}")

    # ------------------------------------------------------------ P4
    section("P4 合约口径:逐合约 vs 品种合计")
    grp = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    print(f"用同一组({'、'.join(grp)})在同一段样本外比两种口径。\n")
    v = power_variety(group_signal_variety(te, grp), te_main)
    print(f"  品种合计→主力合约:  N={v['N']}  corr={v['corr']:+.3f}  "
          f"控动量偏相关={v['partial']:+.3f}  t={v['t']:+.2f}")
    c = power_percontract(group_signal_percontract(te, grp), te)
    print(f"  逐合约各算各的:      N={c['N']}  pooled corr={c['pooled']:+.3f}  "
          f"IC均值={c['IC均值']:+.3f}  IC_t={c['IC_t']:+.2f}  (IC 天数 {c['IC天数']})")
    print("\n  注:pooled 的 N 大是因为一天有多个合约,同日收益高度相关,")
    print("     所以逐合约那行以 IC_t 为准,不要拿 pooled 的 N 与上面那行比。")

    # ------------------------------------------------------------ P5
    section("P5 逐合约信号为什么不灵:是不是被移仓换月污染了")
    print("猜想:机构的方向判断是**品种级**的,执行时分散在多个合约;从近月移到远月时,")
    print("单看近月是「减空」、远月是「加空」,方向其实没变——逐合约口径会把这")
    print("当成两个相反的信号。若真如此,同日不同合约的信号应当经常反向。\n")

    sig = group_signal_percontract(te, grp).dropna()
    per_day = sig[sig["sig"] != 0].groupby("trade_date")["sig"]
    both = per_day.apply(lambda s: (s > 0).any() and (s < 0).any())
    n_multi = per_day.size()
    mask = n_multi >= 2
    print(f"  同日有 ≥2 个合约出信号的天数: {int(mask.sum())}")
    print(f"  其中同日出现方向相反的信号:   {int(both[mask].sum())} 天 "
          f"({both[mask].mean() * 100:.1f}%)")
    # 净额 vs 绝对额:若多数是移仓,合计净额会远小于各合约绝对额之和
    net_abs = sig.groupby("trade_date")["sig"].sum().abs()
    abs_sum = sig.groupby("trade_date")["sig"].apply(lambda s: s.abs().sum())
    ratio = (net_abs / abs_sum.replace(0, np.nan)).dropna()
    print(f"  |合计净变化| / Σ|各合约变化| 的中位数: {ratio.median():.2f}")
    print("    (接近 1 = 各合约同向,只是分仓;明显小于 1 = 大量互相抵消的移仓动作)")

    print("\n品种合计信号对**各个合约**(不只主力)的预测力:")
    vsig = group_signal_variety(te, grp).rename("sig")
    jj = (te[["contract", "trade_date", f"fwd{HORIZON}"]].drop_duplicates()
          .merge(vsig, left_on="trade_date", right_index=True, how="inner").dropna())
    ic2 = (jj.groupby("trade_date")
             .apply(lambda s: s[f"fwd{HORIZON}"].mean(), include_groups=False))
    j2 = pd.concat([vsig, ic2.rename("ret")], axis=1, sort=True).dropna()
    r2 = j2["sig"].corr(j2["ret"])
    t2 = r2 * np.sqrt((len(j2) - 2) / max(1e-12, 1 - r2 ** 2))
    print(f"  品种合计信号 → 当日全部在榜合约的平均收益: N={len(j2)} "
          f"corr={r2:+.3f} t={t2:+.2f}")

    section("数据已打印,结论写进 REPORT_LH_PHASE1_v1.md")


if __name__ == "__main__":
    main()
