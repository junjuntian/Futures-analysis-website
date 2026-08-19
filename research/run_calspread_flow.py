"""跨月价差能不能用**逐合约**的聪明钱流向来判断?(生猪 / 焦煤 / 鸡蛋)

运营者 2026-08-19 问:套利监控里玻纯引入了机构资金,生猪焦煤鸡蛋能不能也这样?

**不能直接搬**:FG-SA 是跨品种,两个品种各有自己的合计流向,相减才有方向;
而这三个品种在套利监控里全是**同品种跨月**,合计流向对两条腿是同一个数,
相减恒等于 0 —— 数学上零信息。

**但席位数据本身是逐合约的**,所以可以换一个信号:
    signal = Δ净持仓(聪明钱, 近月) − Δ净持仓(聪明钱, 远月)
同一批人在近月加空、在远月加多,说的就是「他们在做空这个价差」。
DEC-083 当初嫌「逐合约信号符号互相打架」(84.6% 的日子如此),
**对跨月价差来说,打架恰恰就是信号**。

三条纪律:
  ① 席位组用引擎那套**滚动重选**的结果,只用当时之前的数据选;
  ② 掉榜 = 未知,**不补零**;两条腿都要有数才算这一天;
  ③ 37 组不是 37 个独立样本(同一近月配多个远月),所以主结论看
     **逐日横截面多空组合**的 t 值,不看混在一起的相关系数。
"""
from __future__ import annotations

import os
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("ENGINE_SOURCE", "csv")
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
SIG_WIN, Z_WIN = 5, 120
NAMES = {"LH": "生猪", "JM": "焦煤", "JD": "鸡蛋"}


def section(t):
    print("\n" + "=" * 96); print(t); print("=" * 96)


def load(code):
    """逐 (合约, 日) 的聪明钱净持仓,席位组按引擎的滚动重选结果逐日取。"""
    H.use(code)
    price, seat = H.load_from_csv(code, DATA)
    price, seat = H.clean_price(price), H.clean_seat(seat)
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    groups, _, _ = H.rolling_groups(seat, price, mkt.index)

    # 逐合约、逐日、逐会员的净持仓 → 只留当日生效席位组里的那几家
    s = seat[["member_key", "contract", "trade_date", "net"]].copy()
    g = groups.dropna()
    grp_of = {d: set(v) for d, v in g.items()}
    s = s[s["trade_date"].isin(grp_of)]
    keep = [m in grp_of[d] for m, d in zip(s["member_key"], s["trade_date"])]
    s = s[keep]
    net = s.groupby(["contract", "trade_date"])["net"].sum().unstack("contract").sort_index()
    # 掉榜=未知不补零:reindex 到交易日后保持 NaN
    net = net.reindex(mkt.index)
    return net, mkt


def main():
    cal = pd.read_csv(DATA / "cal_spreads.csv.gz", parse_dates=["trade_date"])
    print("套利监控的跨月组合(2023-08 起):",
          ", ".join(f"{NAMES[k]} {v} 组" for k, v in
                    cal.groupby("instrument_1").apply(
                        lambda d: d.groupby(["contract_1", "contract_2"]).ngroups,
                        include_groups=False).items()))

    out = {}
    for code in ("LH", "JM", "JD"):
        net, mkt = load(code)
        sub = cal[cal["instrument_1"] == code]
        rows = []
        for (c1, c2), d in sub.groupby(["contract_1", "contract_2"]):
            if c1 not in net.columns or c2 not in net.columns:
                continue
            d = d.set_index("trade_date").sort_index()
            f1 = net[c1].diff(SIG_WIN)
            f2 = net[c2].diff(SIG_WIN)
            raw = (f1 - f2).reindex(d.index)          # 两条腿任一缺 → NaN,自动排除
            z = raw / raw.rolling(Z_WIN, min_periods=40).std()
            sp = d["spread"].astype(float)
            rows.append(pd.DataFrame({
                "combo": f"{c1}-{c2}", "z": z,
                "fwd1": sp.shift(-1) - sp,
                "fwd5": sp.shift(-5) - sp,
                "fwd20": sp.shift(-20) - sp,
            }))
        df = pd.concat(rows).dropna(subset=["z"])
        out[code] = df

    section("一、覆盖率:掉榜之后还剩多少可用样本")
    print(f"  {'品种':6s}{'组合数':>7s}{'组合-日':>9s}{'信号可用':>9s}{'可用率':>8s}{'起点':>13s}")
    for code, df in out.items():
        tot = len(cal[cal['instrument_1'] == code])
        ok = int(df["z"].notna().sum())
        print(f"  {NAMES[code]:6s}{df['combo'].nunique():>7d}{tot:>9d}{ok:>9d}"
              f"{100*ok/tot:>7.0f}%{str(df.index.min().date()):>14s}")

    section("二、混在一起的相关(**只作参考**:37 组不独立,这个 t 是虚高的)")
    print(f"  {'品种':6s}{'N':>7s}{'corr(z,次日)':>13s}{'corr(z,5日)':>12s}{'corr(z,20日)':>13s}")
    for code, df in out.items():
        c = [df["z"].corr(df[k]) for k in ("fwd1", "fwd5", "fwd20")]
        print(f"  {NAMES[code]:6s}{len(df):>7d}{c[0]:>+13.3f}{c[1]:>+12.3f}{c[2]:>+13.3f}")

    section("三、主结论:逐日横截面多空组合(做多 z 最高的三分之一,做空最低的三分之一)")
    print("  每天在同一个品种内部排序再对冲,横截面相关被抵掉,t 值才算得准。")
    print("  收益单位=价差点数(同品种同点值,可直接相加)。\n")
    print(f"  {'品种':6s}{'天数':>6s}{'日均点数':>10s}{'t值':>8s}{'年化夏普':>10s}{'胜率%':>8s}")
    for code, df in out.items():
        daily = []
        for d, g in df.groupby(level=0):
            g = g.dropna(subset=["fwd1"])
            if len(g) < 6:
                continue
            k = max(2, len(g) // 3)
            hi = g.nlargest(k, "z")["fwd1"].mean()
            lo = g.nsmallest(k, "z")["fwd1"].mean()
            daily.append(hi - lo)
        a = np.array(daily)
        if len(a) < 30:
            print(f"  {NAMES[code]:6s}  天数不足 {len(a)}")
            continue
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))
        sh = a.mean() / a.std(ddof=1) * np.sqrt(242)
        print(f"  {NAMES[code]:6s}{len(a):>6d}{a.mean():>+10.2f}{t:>+8.2f}{sh:>10.2f}{100*(a>0).mean():>8.1f}")

    section("四、五档单调性(相邻档要同向,不看极值)")
    for code, df in out.items():
        d = df.dropna(subset=["fwd5"]).copy()
        d["档"] = pd.qcut(d["z"], 5, labels=["最低", "低", "中", "高", "最高"], duplicates="drop")
        m = d.groupby("档", observed=True)["fwd5"].mean()
        print(f"  {NAMES[code]:6s}" + "  ".join(f"{k} {v:+7.1f}" for k, v in m.items()))


if __name__ == "__main__":
    main()
