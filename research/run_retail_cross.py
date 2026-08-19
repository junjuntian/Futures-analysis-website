"""散户是不是跨品种的反向指标。

运营者 2026-08-19:「散户确实是反向指标,验证一下」。缘起是玻璃那轮发现——
垫底三家(徽商 −21.5 亿、东方财富 −15.8 亿、中信建投 −15.1 亿)平均净持仓全是正的,
十四年一直做多一直亏;而生猪那边垫底的也是同一批名字。

**这个假说比逐品种做值钱在哪**:如果同一份名单在所有品种上都反向有效,那名单就不用
每个品种单独选——挑人的过拟合风险直接消失,而且新品种上线不需要重新训练。

检验纪律(玻璃那轮的教训):
  - 名单**跨品种固定**,不许每个品种各选各的;
  - 名单只用**早期数据**选,后面那段做样本外;
  - 逐年看符号,不只看全样本 t。
"""
from __future__ import annotations
import numpy as np, pandas as pd
import lhlib as L
from run_flow_skill import build, seat_alpha, power

CODES = ["AU", "AG", "LH", "FG", "SA"]
data = {}
for c in CODES:
    try:
        data[c] = build(c)
    except Exception as e:
        print(f"  {c} 跳过:{e}")
print("品种:" + "  ".join(
    f"{c}({data[c][0]['trade_date'].nunique()}日/{data[c][0]['member_key'].nunique()}家)" for c in data))

print("\n① 各品种 alpha 垫底 8 家(看名单重不重合)")
losers = {}
for c, (df, _) in data.items():
    a = seat_alpha(df, c, min_days=200).sort_values("alpha")
    losers[c] = a.head(8)
    print(f"  {c}: " + "、".join(f"{m}({v:+.0f})" for m, v in a.head(6)["alpha"].items()))

cnt = pd.Series(0, index=sorted({m for v in losers.values() for m in v.index}), dtype=int)
for v in losers.values():
    for m in v.index: cnt[m] += 1
print(f"\n  在 ≥3 个品种上都垫底的席位:")
common = cnt[cnt >= 3].sort_values(ascending=False)
print("    " + "、".join(f"{m}({n}个品种)" for m, n in common.items()) if len(common) else "    无")

print("\n② 用**早期数据**定一份跨品种散户名单,后面全做样本外")
CUT = pd.Timestamp("2021-01-01")
early = {}
for c, (df, _) in data.items():
    sub = df[df["trade_date"] < CUT]
    if sub["trade_date"].nunique() < 250: continue
    a = seat_alpha(sub, c, min_days=150)
    if not a.empty: early[c] = a
score = pd.Series(0.0, index=sorted({m for v in early.values() for m in v.index}))
seen = pd.Series(0, index=score.index, dtype=int)
for c, a in early.items():
    r = a["alpha"].rank(pct=True)     # 0=最亏
    for m in r.index: score[m] += r[m]; seen[m] += 1
score = (score[seen >= 2] / seen[seen >= 2]).sort_values()
retail = score.head(5).index.tolist()
print(f"  只看 {CUT:%Y-%m} 之前、且至少在 2 个品种上够样本的席位,平均分位最低 5 家:")
print(f"    {'、'.join(retail)}")
print(f"  (它们的平均分位 {score.head(5).round(3).to_dict()})")

print(f"\n③ 样本外({CUT:%Y-%m} 之后):反着跟这份**固定名单**,逐品种看")
print(f"  {'品种':6s}{'偏相关':>9s}{'t':>8s}{'N':>7s}   逐年符号")
tot_pos = tot_all = 0
for c, (df, main) in data.items():
    te = df[df["trade_date"] >= CUT]
    tm = main[main.index >= CUT]
    if te["trade_date"].nunique() < 200: continue
    have = [m for m in retail if m in set(te["member_key"])]
    if len(have) < 3: 
        print(f"  {c:6s}  名单里只有 {len(have)} 家在该品种上,跳过")
        continue
    sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    r = power(sig, tm)
    j = pd.concat([sig.rename("sig"), tm], axis=1, sort=True); j["y"] = j.index.year
    marks = []
    for y, g in j.groupby("y"):
        rr = power(g["sig"], g)
        if rr:
            marks.append(f"{y}{'+' if rr[0] > 0 else '-'}")
            tot_all += 1; tot_pos += 1 if rr[0] > 0 else 0
    if r: print(f"  {c:6s}{r[0]:>+9.3f}{r[1]:>+8.2f}{r[2]:>7d}   {' '.join(marks)}")
print(f"\n  合计:{tot_all} 个「品种×年」里 {tot_pos} 个为正 "
      f"({100*tot_pos/max(tot_all,1):.0f}%)")

print("\n④ 对照:同一份名单**正着**跟(应该普遍为负,否则说明信号只是噪音)")
for c, (df, main) in data.items():
    te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
    if te["trade_date"].nunique() < 200: continue
    have = [m for m in retail if m in set(te["member_key"])]
    if len(have) < 3: continue
    sig = te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    r = power(sig, tm)
    if r: print(f"  {c:6s} 偏相关 {r[0]:+.3f}  t={r[1]:+.2f}")

# ---------------------------------------------------------------- ⑤
# ② 那份名单选坏了:用「平均分位」把只在 2 个品种上有样本的小席位混了进来
# (格林大华被选成散户,而它在生猪上是 alpha 前列)。换成更硬的标准:
# **要在足够多的品种上、都排在后 20%**,才算跨品种散户。仍然只用早期数据。
print("\n⑤ 换硬标准重选名单:早期数据 + 至少 3 个品种都排后 20%")
votes = pd.Series(0, index=sorted({m for v in early.values() for m in v.index}), dtype=int)
elig = pd.Series(0, index=votes.index, dtype=int)
for c, a in early.items():
    r = a["alpha"].rank(pct=True)
    for m in r.index:
        elig[m] += 1
        if r[m] <= 0.20: votes[m] += 1
cand = pd.DataFrame({"垫底品种数": votes, "有样本品种数": elig})
cand = cand[(cand["垫底品种数"] >= 3)].sort_values("垫底品种数", ascending=False)
print("  候选:" + ("、".join(f"{m}({r['垫底品种数']}/{r['有样本品种数']})"
                             for m, r in cand.iterrows()) if len(cand) else "无"))
retail2 = cand.index.tolist()
if not retail2:
    cand = pd.DataFrame({"垫底品种数": votes, "有样本品种数": elig})
    cand = cand[cand["垫底品种数"] >= 2].sort_values("垫底品种数", ascending=False)
    retail2 = cand.head(5).index.tolist()
    print(f"  (放宽到 2 个品种):{'、'.join(retail2)}")

print(f"\n  样本外({CUT:%Y-%m} 之后)反着跟这份名单:")
print(f"  {'品种':6s}{'偏相关':>9s}{'t':>8s}{'N':>7s}   逐年符号")
p2 = a2 = 0
for c, (df, main) in data.items():
    te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
    if te["trade_date"].nunique() < 200: continue
    have = [m for m in retail2 if m in set(te["member_key"])]
    if len(have) < 2:
        print(f"  {c:6s}  名单里只有 {len(have)} 家在该品种,跳过"); continue
    sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    r = power(sig, tm)
    j = pd.concat([sig.rename("sig"), tm], axis=1, sort=True); j["y"] = j.index.year
    marks = []
    for y, g in j.groupby("y"):
        rr = power(g["sig"], g)
        if rr:
            marks.append(f"{y}{'+' if rr[0] > 0 else '-'}"); a2 += 1; p2 += 1 if rr[0] > 0 else 0
    if r: print(f"  {c:6s}{r[0]:>+9.3f}{r[1]:>+8.2f}{r[2]:>7d}   {' '.join(marks)} [用了{len(have)}家]")
print(f"\n  合计:{a2} 个「品种×年」里 {p2} 个为正 ({100*p2/max(a2,1):.0f}%)")

print("\n⑥ 分组看:贵金属 vs 化工农产品(③ 已显出分化,这里坐实)")
for group, cs in [("贵金属 AU/AG", ["AU", "AG"]), ("化工农产 LH/FG/SA", ["LH", "FG", "SA"])]:
    rs = []
    for c in cs:
        if c not in data: continue
        df, main = data[c]
        te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
        have = [m for m in retail2 if m in set(te["member_key"])]
        if len(have) < 2: continue
        sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
        r = power(sig, tm)
        if r: rs.append((c, r[0], r[1]))
    if rs:
        print(f"  {group}:" + "  ".join(f"{c} t={t:+.2f}" for c, _, t in rs))

# ---------------------------------------------------------------- ⑦
# 82% 为正很好看,但还有两个必须排除的:会不会是名单里某一家撑起来的?
# 信号强度和收益成不成正比(不成正比就只能当观察指标,做不成策略)?
print("\n⑦ 留一:逐个去掉名单里的一家,看结论稳不稳")
def cross_t(members):
    out = {}
    for c, (df, main) in data.items():
        te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
        have = [m for m in members if m in set(te["member_key"])]
        if len(have) < 2 or te["trade_date"].nunique() < 200: continue
        sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
        r = power(sig, tm)
        if r: out[c] = r[1]
    return out
base = cross_t(retail2)
print(f"  全名单:" + "  ".join(f"{c} {t:+.2f}" for c, t in base.items()))
for drop in retail2:
    o = cross_t([m for m in retail2 if m != drop])
    worse = sum(1 for c in o if c in base and o[c] < base[c] - 0.5)
    print(f"  去掉 {drop:6s}:" + "  ".join(f"{c} {t:+.2f}" for c, t in o.items())
          + (f"   ← {worse} 个品种明显变差" if worse else ""))

print("\n⑧ 五档:信号强度和收益成不成正比(玻璃那轮就栽在最强档失效)")
for c in ("SA", "FG", "LH", "AU"):
    if c not in data: continue
    df, main = data[c]
    te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
    have = [m for m in retail2 if m in set(te["member_key"])]
    if len(have) < 2: continue
    sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    j = pd.concat([sig.rename("sig"), tm], axis=1, sort=True).dropna()
    if len(j) < 200: continue
    j["b"] = pd.qcut(j["sig"], 5, labels=["最负", "偏负", "中", "偏正", "最正"])
    g = j.groupby("b", observed=True)["fwd"].mean() * 100
    mono = "单调↑" if g.is_monotonic_increasing else ("两端对但中间乱" if g.iloc[0] < g.iloc[-1] else "不成立")
    print(f"  {c}: " + "  ".join(f"{b} {v:+5.2f}%" for b, v in g.items()) + f"   [{mono}]")

print("\n⑨ AG 为什么例外:名单里这几家在 AG 上是什么样子")
if "AG" in data:
    a = seat_alpha(data["AG"][0], "AG", min_days=200)
    for m in retail2:
        if m in a.index:
            r = a.loc[m]
            print(f"  {m:6s} alpha {r['alpha']:+7.1f} 亿  平均净持仓 {r['avg_net']:+9,.0f} 手")

# ---------------------------------------------------------------- ⑩
# 最后一个要排除的:如果这批席位**持续**加多、而品种在跌,那「反着跟」可能只是
# 伪装的做空 beta。把信号去掉自身均值(滚动标准化)再测——真信号活得下来,
# beta 会死掉。
print("\n⑩ 去掉信号自身均值(排除伪装的方向 beta)")
print(f"  {'品种':6s}{'原始 t':>9s}{'去均值 t':>10s}{'信号均值':>11s}  判定")
for c, (df, main) in data.items():
    te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
    have = [m for m in retail2 if m in set(te["member_key"])]
    if len(have) < 2 or te["trade_date"].nunique() < 200: continue
    raw = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    dm = (raw - raw.rolling(120, min_periods=60).mean()) / raw.rolling(120, min_periods=60).std()
    r1, r2 = power(raw, tm), power(dm, tm)
    if r1 and r2:
        verdict = "真信号" if r2[1] > 1.5 else ("削弱但在" if r2[1] > 0.8 else "疑似 beta")
        print(f"  {c:6s}{r1[1]:>+9.2f}{r2[1]:>+10.2f}{raw.mean():>+11,.0f}  {verdict}")

print("\n  档位区分度(最正档 − 最负档,已扣掉各品种自身趋势的影响):")
for c in ("SA", "FG", "LH", "AU"):
    if c not in data: continue
    df, main = data[c]
    te = df[df["trade_date"] >= CUT]; tm = main[main.index >= CUT]
    have = [m for m in retail2 if m in set(te["member_key"])]
    if len(have) < 2: continue
    sig = -te[te["member_key"].isin(have)].groupby("trade_date")["net"].sum().sort_index().diff(5)
    j = pd.concat([sig.rename("sig"), tm], axis=1, sort=True).dropna()
    if len(j) < 200: continue
    j["b"] = pd.qcut(j["sig"], 5, labels=list("12345"))
    g = j.groupby("b", observed=True)["fwd"].mean() * 100
    print(f"    {c}: {g.iloc[-1] - g.iloc[0]:+.2f} 个百分点")
