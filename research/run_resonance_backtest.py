"""共振事件式策略 Phase 2:出场规则 + 回测 + 成本。

2026-08-19 运营者拍板做这个形态。信号来自 REPORT_RETAIL_V2/V5:

  进场 = 聪明钱流向与散户反向流向**同号**(共振)且 |散户反向 z| ≥ enter
  方向 = 散户反向信号的符号(散户加多 → 看跌)
  出场 = 持满 N 日 / 硬止损 / 信号反向

为什么是事件式而不是连续持仓:这个信号「两端对、中间乱」——五档里中间三档
基本没信息,连续持仓等于把噪音也拿去交易。实测「共振+极端」触发后 20 日
超额 +1.64~+2.74 个百分点(LH/FG/SA)。

三条纪律照旧(PITFALLS 六):
  - 席位名单**只用样本外之前的数据**选(散户三家是运营者定的固定名单,
    聪明钱按各品种自己的早期数据选);
  - 参数看「相邻档同向」,不挑峰值;
  - 计价逐合约,换月日用新合约自己的前一日结算价。

**重叠处理**:持仓期间忽略新信号,平掉之后才能再进。不这么做,24~28% 的触发率
会造出一堆互相重叠的仓位,回测收益会被重复计算。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import lhlib as L
from run_flow_skill import build, seat_alpha
from run_lh_phase2 import main_returns

pd.set_option("display.width", 210)

SEED = ["东方财富", "平安期货", "徽商期货"]   # 运营者 2026-08-19 定的散户三家
CUTS = {"LH": pd.Timestamp("2024-01-01"),      # 生猪席位 2023-08 才有,自己的时间轴
        "FG": pd.Timestamp("2021-01-01"),
        "SA": pd.Timestamp("2021-01-01")}
COST = 0.0005          # 单边成本(手续费+滑点)
SIG_WIN = 5
Z_WIN = 120


def section(t): print("\n" + "=" * 84); print(t); print("=" * 84)


def prepare(code: str):
    """返回 (信号表, 主力日收益)。信号表含 smart / retail / resonate。"""
    cut = CUTS[code]
    price = L.load_price(code)
    seat = L.load_seat(code)
    df = seat.merge(price[["contract", "trade_date", "settle"]],
                    on=["contract", "trade_date"], how="inner")
    mr = main_returns(price)
    mr = mr[mr.index >= cut]

    # 聪明钱:只用 cut 之前的数据选,选完定死
    tr = seat_alpha(df[df["trade_date"] < cut], code, min_days=80 if code == "LH" else 150)
    smart5 = tr.sort_values("alpha", ascending=False).head(5).index.tolist()
    te = df[df["trade_date"] >= cut]
    smart = te[te["member_key"].isin(smart5)].groupby("trade_date")["net"].sum().sort_index().diff(SIG_WIN)

    have = [m for m in SEED if m in set(te["member_key"])]
    retail = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(SIG_WIN)
    # 无量纲化:各品种、各时期的持仓量级差很多,绝对手数不能直接当阈值
    rz = (retail - retail.rolling(Z_WIN, min_periods=60).mean()) / retail.rolling(Z_WIN, min_periods=60).std()

    sig = pd.DataFrame({"smart": smart, "retail": retail, "rz": rz}).reindex(mr.index)
    sig["resonate"] = np.sign(sig["smart"]) == np.sign(sig["retail"])
    return sig, mr, smart5, have


def backtest(sig, mr, enter=1.0, hold=20, stop=0.06, cost=COST,
             need_resonance=True) -> pd.DataFrame:
    """事件式:触发进场,持满/止损/反向出场。持仓期间忽略新信号(不重叠)。"""
    idx = mr.index
    trades, side, entry_i, cum = [], 0, None, 0.0
    for i, d in enumerate(idx):
        z = sig["rz"].get(d, np.nan)
        res = bool(sig["resonate"].get(d, False))
        r = mr["ret"].get(d, np.nan)
        if side != 0:
            cum = (1 + cum) * (1 + side * (r if np.isfinite(r) else 0)) - 1
        reason = None
        if side != 0:
            if cum <= -stop:
                reason = "止损"
            elif i - entry_i >= hold:
                reason = "持满"
            elif np.isfinite(z) and side * z <= -enter:
                reason = "反向"
        if reason:
            trades.append({"进场": idx[entry_i], "出场": d,
                           "方向": "多" if side > 0 else "空",
                           "收益%": (cum - 2 * cost) * 100,
                           "持有": i - entry_i, "出场原因": reason})
            side, cum = 0, 0.0
        # 持仓期间不进新仓——不然 24~28% 的触发率会造出一堆重叠仓位
        if side == 0 and np.isfinite(z) and abs(z) >= enter and (res or not need_resonance):
            side, entry_i, cum = int(np.sign(z)), i, 0.0
    return pd.DataFrame(trades)


def stats(tr: pd.DataFrame, mr: pd.DataFrame, label: str, quiet=False, cost=COST):
    if tr.empty:
        if not quiet: print(f"  {label:26s} 无交易")
        return None
    r = tr["收益%"]
    # 逐日净值:先铺一条仓位序列再乘收益。
    # **不能用区间赋值** `daily.loc[进场:出场] = seg*side`——那样进场当日会被计入
    # (实际是 T+1 才有仓),而且前一笔的出场日与后一笔的进场日重合时会互相覆盖。
    # 2026-08-19 就是这么错的:逐笔连乘 +1183% 而日收益累乘 +5010%,差 3827 个点。
    # 对账纪律:这两条路算出来的累计必须接近,差太多就是这里有 bug。
    pos = pd.Series(0.0, index=mr.index)
    for _, t in tr.iterrows():
        seg = mr.loc[t["进场"]:t["出场"]].index[1:]      # 去掉进场日 = T+1
        pos.loc[seg] = 1.0 if t["方向"] == "多" else -1.0
    turn = pos.diff().abs().fillna(abs(pos.iloc[0]))
    # 成本用**传入的** cost,不是全局常量——2026-08-19 这里写死过 COST,
    # 导致成本敏感性四档跑出完全相同的数字而没人一眼看出来。
    daily = pos * mr["ret"].fillna(0) - turn * cost
    eq = (1 + daily).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    sh = daily.mean() / daily.std() * np.sqrt(242) if daily.std() > 0 else np.nan
    expo = (daily != 0).mean()
    out = {"笔数": len(tr), "累计%": (float(eq.iloc[-1]) - 1) * 100, "均值%": r.mean(),
           "逐笔累计%": (np.prod(1 + r / 100) - 1) * 100,
           "胜率%": (r > 0).mean() * 100, "最差%": r.min(), "回撤%": dd * 100,
           "夏普": sh, "在场%": expo * 100}
    if not quiet:
        print(f"  {label:26s} {out['笔数']:3d}笔 累计{out['累计%']:+8.1f}%"
              f"(逐笔{out['逐笔累计%']:+8.1f}%) 均值{out['均值%']:+5.2f}% "
              f"胜率{out['胜率%']:5.1f}% 最差{out['最差%']:+6.1f}% 回撤{out['回撤%']:6.1f}% "
              f"夏普{out['夏普']:5.2f} 在场{out['在场%']:4.0f}%")
    return out


def main():
    prep = {}
    for c in ("LH", "FG", "SA"):
        prep[c] = prepare(c)

    section("A 基准形态(进场 |z|≥1 + 共振,持满 20 日,止损 6%)")
    for c, (sig, mr, smart5, have) in prep.items():
        print(f"\n  【{c}】样本外自 {CUTS[c]:%Y-%m};聪明钱={'、'.join(smart5[:3])}…;"
              f"散户={'、'.join(have)}")
        bh = (1 + mr["ret"].fillna(0)).prod() - 1
        sh_ = (1 - mr["ret"].fillna(0)).prod() - 1
        print(f"    对照:买入持有 {bh*100:+.1f}%   恒定满仓做空 {sh_*100:+.1f}%")
        tr = backtest(sig, mr)
        stats(tr, mr, "共振事件式")
        stats(backtest(sig, mr, need_resonance=False), mr, "不要共振(仅 |z|≥1)")

    section("B 参数敏感性(要的是相邻档同向,不是挑峰值)")
    for name, kw_list in [("进场门槛", [{"enter": e} for e in (0.8, 1.0, 1.2, 1.5)]),
                          ("持有天数", [{"hold": h} for h in (10, 15, 20, 30)]),
                          ("止损", [{"stop": s} for s in (0.04, 0.06, 0.08, 0.10)])]:
        print(f"\n  {name}:")
        for kw in kw_list:
            k, v = list(kw.items())[0]
            row = f"    {k}={v:<5}"
            for c, (sig, mr, _, _) in prep.items():
                o = stats(backtest(sig, mr, **kw), mr, "", quiet=True)
                row += f"  {c} {o['累计%']:+7.1f}%/{o['胜率%']:4.0f}%" if o else f"  {c} —"
            print(row)

    section("C 逐年")
    for c, (sig, mr, _, _) in prep.items():
        tr = backtest(sig, mr)
        if tr.empty: continue
        tr["年"] = tr["出场"].dt.year
        print(f"  【{c}】")
        for y, sub in tr.groupby("年"):
            o = stats(sub, mr, "", quiet=True)
            print(f"    {y}  {o['笔数']:2d}笔 累计{o['累计%']:+7.1f}% 胜率{o['胜率%']:5.1f}%")

    section("D 成本敏感性")
    print(f"  {'单边成本':10s}" + "".join(f"{c:>16s}" for c in prep))
    for cost in (0.0002, 0.0005, 0.0010, 0.0020):
        row = f"  {cost:.2%}     "
        for c, (sig, mr, _, _) in prep.items():
            o = stats(backtest(sig, mr, cost=cost), mr, "", quiet=True, cost=cost)
            row += f"{o['累计%']:>+15.1f}%" if o else f"{'—':>16s}"
        print(row)

    section("D2 收益到底是不是高波动带来的")
    print("  年化收益 / 年化波动 / 品种自身波动,三个一起看才知道是本事还是行情大")
    print(f"  {'品种':5s}{'年数':>5s}{'策略年化':>10s}{'策略波动':>10s}{'夏普':>7s}"
          f"{'品种波动':>10s}{'年化/品种波动':>14s}")
    for c, (sig, mr, _, _) in prep.items():
        tr = backtest(sig, mr)
        pos = pd.Series(0.0, index=mr.index)
        for _, t in tr.iterrows():
            pos.loc[mr.loc[t["进场"]:t["出场"]].index[1:]] = 1.0 if t["方向"] == "多" else -1.0
        daily = pos * mr["ret"].fillna(0) - pos.diff().abs().fillna(0) * COST
        yrs = len(mr) / 242
        cagr = ((1 + daily).prod()) ** (1 / yrs) - 1
        vol = daily.std() * np.sqrt(242)
        ivol = mr["ret"].std() * np.sqrt(242)
        print(f"  {c:5s}{yrs:>5.1f}{cagr*100:>+9.1f}%{vol*100:>9.1f}%"
              f"{cagr/vol:>7.2f}{ivol*100:>9.1f}%{cagr/ivol:>13.2f}")
    print("  (最后一列 = 年化收益 ÷ 品种自身波动。品种波动大,策略收益自然水涨船高,")
    print("   这一列把它折算掉之后才是可比的)")

    section("E 出场原因分布")
    for c, (sig, mr, _, _) in prep.items():
        tr = backtest(sig, mr)
        if tr.empty: continue
        parts = [f"{k} {len(g)}笔(均值{g['收益%'].mean():+.2f}%)"
                 for k, g in tr.groupby("出场原因")]
        print(f"  {c}: " + "  ".join(parts))


if __name__ == "__main__":
    main()
