"""生猪移仓换月反弹 —— 运营者假设(2026-08-23)的机制检验,先只看事实。

## 假设(运营者原话的翻译)
生猪合约 1/3/5/7/9/11,两个月一换。主力 X 触发做空、散户共振,价格一路跌,
双方持仓打满后开始减仓,价格还在滑;**但到了临界点 —— X 还剩一个月左右到期时,
机构必须撤出来(移仓),这时下一个合约 Y(X+2 月)必然反弹**。
例:7 月主力剩一个月时,9 月合约必然反弹。

## 这一份只做机制检验,不做策略,不设通过判据
事件:每个当过主力的合约 X,它距散户窗口止点(交割月前月最后一个工作日)首次 ≤ N
个交易日的那天 d(N = 30/25/20/15/10)。次主力 Y = X 之后的下一个 LH 月份合约。
测三样,全部**同合约**、以 d+1 结算价为起点:
  ① Y 单边:之后 5/10/20/40 日的涨跌(%);
  ② 价差 Y−X:同期变化(点),= 买 Y 卖 X(次主力相对主力走强就赚);
  ③ X 单边:对照,看主力自己是不是真的在被打压。
对照基线:全样本无条件(所有交易日 × 当时的次主力)。
逐年分列 —— 运营者认为 2026 是转折年,那就让每年自己说话。
"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import hog_money as H  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
HS = (5, 10, 20, 40)
NS = (30, 25, 20, 15, 10)


def tstat(a):
    a = np.array(a, dtype=float)
    a = a[np.isfinite(a)]
    return a.mean() / a.std(ddof=1) * np.sqrt(len(a)) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan


def ym(c):
    raw = "".join(ch for ch in str(c) if ch.isdigit())
    return 2000 + int(raw[:2]), int(raw[2:])


price = H.clean_price(pd.read_csv(DATA / "lh_price.csv.gz"))
H.use("LH")
mkt = H.main_series(price)
st = price.pivot_table(index="trade_date", columns="contract", values="settle", aggfunc="first")
idx = mkt.index
st = st.reindex(idx)
pos = {d: i for i, d in enumerate(idx)}
all_codes = sorted(st.columns, key=ym)


def next_code(x):
    """X 之后的下一个 LH 月份合约(1/3/5/7/9/11,+2 月)。"""
    y, m = ym(x)
    m2, y2 = (m + 2, y) if m + 2 <= 12 else (m + 2 - 12, y + 1)
    want = f"LH{y2 % 100:02d}{m2:02d}"
    return want if want in st.columns else None


def fwd(code, i, h):
    """同合约、从 i+1 起 h 日的涨跌(%)。"""
    if code is None or code not in st.columns or i + 1 + h >= len(idx):
        return np.nan
    a, b = st[code].iloc[i + 1], st[code].iloc[i + 1 + h]
    return float(b / a - 1) * 100 if np.isfinite(a) and np.isfinite(b) and a > 0 else np.nan


def fwd_spread(y, x, i, h):
    """价差 Y−X 从 i+1 起 h 日的变化(点)。"""
    if y is None or y not in st.columns or x not in st.columns or i + 1 + h >= len(idx):
        return np.nan
    s0 = st[y].iloc[i + 1] - st[x].iloc[i + 1]
    s1 = st[y].iloc[i + 1 + h] - st[x].iloc[i + 1 + h]
    return float(s1 - s0) if np.isfinite(s0) and np.isfinite(s1) else np.nan


# 基线:每个交易日的「当时次主力」后续涨跌
base = {h: [] for h in HS}
for i, d in enumerate(idx):
    x = mkt["main"].iloc[i]
    if not isinstance(x, str):
        continue
    y = next_code(x)
    for h in HS:
        v = fwd(y, i, h)
        if np.isfinite(v):
            base[h].append(v)

print("=" * 108)
print("基线:全样本无条件 —— 每个交易日买「当时的次主力」,之后 N 日涨跌(%)")
print("=" * 108)
print("  " + "  ".join(f"{h}日 {np.mean(base[h]):+.2f}(t{tstat(base[h]):+.1f}, n={len(base[h])})" for h in HS))
print()

rows = []
for n in NS:
    ev = []
    for x in all_codes:
        if x not in set(mkt["main"].dropna()):
            continue
        # 该合约当主力期间,dleft 首次 ≤ n 的那天
        mask = (mkt["main"] == x)
        if not mask.any():
            continue
        seg = mkt[mask]
        hit = seg[seg["dleft"] <= n]
        if hit.empty:
            continue
        d = hit.index[0]
        ev.append((x, pos[d], d))
    print("=" * 108)
    print(f"事件:主力剩 ≤{n} 个交易日({len(ev)} 次) —— 买次主力 Y / 价差 Y−X / 主力 X 自己")
    print("=" * 108)
    for tag, f in (("① Y 单边(%)", lambda x, y, i, h: fwd(y, i, h)),
                   ("② Y−X 价差(点)", lambda x, y, i, h: fwd_spread(y, x, i, h)),
                   ("③ X 单边(%)", lambda x, y, i, h: fwd(x, i, h))):
        cells = []
        for h in HS:
            vals = [f(x, next_code(x), i, h) for x, i, _ in ev]
            vals = [v for v in vals if np.isfinite(v)]
            win = np.mean([v > 0 for v in vals]) * 100 if vals else np.nan
            cells.append(f"{h}日 {np.mean(vals) if vals else np.nan:>+7.2f}(t{tstat(vals):+.1f})[{win:.0f}%]")
        print(f"  {tag:<16}" + "  ".join(cells))
    if n == 20:
        for x, i, d in ev:
            y = next_code(x)
            rows.append(dict(main=x, next=y, date=d, year=d.year,
                             y20=fwd(y, i, 20), y40=fwd(y, i, 40),
                             sp20=fwd_spread(y, x, i, 20), x20=fwd(x, i, 20)))
    print()

df = pd.DataFrame(rows)
print("=" * 108)
print("主规格 剩 ≤20 日:逐笔(次主力 20/40 日涨跌、Y−X 价差 20 日、主力 20 日)")
print("=" * 108)
for _, r in df.iterrows():
    print(f"  {r.date.date()}  主力 {r.main} → 次主力 {r['next']}  "
          f"Y20 {r.y20:>+6.2f}%  Y40 {r.y40:>+6.2f}%  Y−X20 {r.sp20:>+6.0f} 点  X20 {r.x20:>+6.2f}%")
print()
print("按年(Y 20 日):")
for y, s in df.groupby("year"):
    print(f"  {y}: {len(s)} 次  均 {s.y20.mean():>+6.2f}%  胜 {(s.y20>0).mean()*100:>3.0f}%   "
          f"(Y−X 价差 均 {s.sp20.mean():>+6.0f} 点)")
