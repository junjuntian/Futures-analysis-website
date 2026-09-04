# -*- coding: utf-8 -*-
"""PLAN_FOLLOW_V3_v1 的跑数脚本 —— 资金上限定在 12%,东证能不能过全部闸门。

**先读 PLAN_FOLLOW_V3_v1.md。** 闸门与结局处置在那边事前钉死。

回测机器直接复用 `run_follow_v2.py`(同一套口径:拆掉对冲态状态门、预热 250 日、
PIT 强度、成本 2 元/手/边、T+1 开盘成交、不设换手阈值)。
**唯一改动是 use 代表格 20% → 12%**;另按预注册新增 G0(相邻档同向)、
G2 加严(走前拼接绝对值 > 0)。

复用而不是重写一份:同一件事两处实现,口径迟早对不上(research/PITFALLS #9)。

用法:CSV_DIR=research/data python research/run_follow_v3.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("v2", HERE / "run_follow_v2.py")
V2 = importlib.util.module_from_spec(_spec)
with redirect_stdout(io.StringIO()):          # v2 在导入时会打印它自己那份报告
    _spec.loader.exec_module(V2)

USE = 0.12                                    # 本次代表格,预注册写死
CARD = "东证期货"
IDX = V2.IDX


def run(m, use=USE, fee=V2.FEE):
    return V2.run(m, use=use, thresh=0, fee=fee)


def stat(s):
    return V2.stat(s)


print(f"样本 {IDX[0].date()} ~ {IDX[-1].date()};预热 {V2.WARM} 日;"
      f"成本 {V2.FEE:.0f} 元/手/边;**代表格 use={USE:.0%}**;不设换手阈值\n")

S = run(CARD)
st = stat(S)
print("=== 主数字(东证,use=12%)===")
print(f"  累计 {st['累计%']:+.1f}%   夏普 {st['夏普']}   "
      f"回撤 {st['回撤%']}%   有仓 {st['有仓天']} 天")

print("\n【G0 相邻档同向(本次新增)】8% 与 20% 也须累计>0,且三档回撤随 use 单调")
adj = {}
for u in (0.08, 0.12, 0.20):
    a = stat(run(CARD, use=u))
    adj[u] = a
    print(f"  use={u:.0%}   累计 {a['累计%']:+8.1f}%   回撤 {a['回撤%']:7.1f}%")
pos_ok = all(adj[u]["累计%"] > 0 for u in (0.08, 0.20))
dds = [abs(adj[u]["回撤%"]) for u in (0.08, 0.12, 0.20)]
mono = dds[0] <= dds[1] <= dds[2]
print(f"  相邻两档累计>0:{'是' if pos_ok else '否'}   回撤随 use 单调:"
      f"{'是' if mono else '否'}   → {'过' if (pos_ok and mono) else '不过'}")

print("\n【G1 席位池安慰剂 · use=12%】需前 25%")
pool = []
for m in sorted(set(V2.ST["FG"].member_key) & set(V2.ST["SA"].member_key)):
    s = run(m)
    if int((s != 0).sum()) < 200:
        continue
    pool.append({"席位": m, **stat(s)})
df = pd.DataFrame(pool).sort_values("累计%", ascending=False).reset_index(drop=True)
df.index += 1
print(f"  池子 = {len(df)} 家;前 8:")
print(df.head(8).to_string())
r = int(df.index[df["席位"] == CARD][0])
pct = (r - 1) / len(df) * 100
print(f"  {CARD} 排 {r}/{len(df)} = 前 {pct:.0f}%   {'过' if pct < 25 else '不过'}")

print("\n【G2 走前挑人 · 加严:须跑赢池子中位数**且绝对值>0**】")
names = list(df["席位"])
curves = {m: run(m) for m in names}
seg = []
for y in sorted({d.year for d in IDX})[1:]:
    cut = pd.Timestamp(f"{y}-01-01")
    hist = {m: ((1 + curves[m][curves[m].index < cut] / 100).prod() - 1)
            for m in names}
    hist = {k: v for k, v in hist.items() if np.isfinite(v)}
    if not hist:
        continue
    pick = max(hist, key=hist.get)
    c = curves[pick]
    seg.append((y, pick,
                c[(c.index >= cut) & (c.index < pd.Timestamp(f"{y+1}-01-01"))]))
for y, pick, s in seg:
    print(f"  {y} 挑 {pick}  当年 {((1 + s / 100).prod() - 1) * 100:+.1f}%")
wf = stat(pd.concat([s for _y, _p, s in seg]))["累计%"]
med = df["累计%"].median()
print(f"  走前拼接 {wf:+.1f}%   池子中位数 {med:+.1f}%   "
      f"→ {'过' if (wf > med and wf > 0) else '不过'}"
      f"(跑赢中位数 {'是' if wf > med else '否'} / 绝对值>0 {'是' if wf > 0 else '否'})")

print("\n【G3 逐年 ≥4/6】")
yr = ((1 + S / 100).groupby(S.index.year).prod() - 1) * 100
print("  " + "  ".join(f"{y}:{v:+.1f}%" for y, v in yr.round(1).items()))
w = int((yr > 0).sum())
print(f"  {w}/{len(yr)}  {'过' if w >= 4 else '不过'}")

print("\n【G4 后半不塌】")
nz = S[S != 0]
mid = nz.index[len(nz) // 2]
half = ((1 + S[S.index >= mid] / 100).prod() - 1) * 100
print(f"  后半 {half:+.1f}%  {'过' if half >= 0 else '不过'}(分界 {mid.date()})")

print("\n【G5 回撤 ≤35%】  【G6 去掉最赚 5 天 ≥0】  【G7 成本翻倍仍正】")
top = S[S != 0].nlargest(5)
g6 = ((1 + S.drop(top.index) / 100).prod() - 1) * 100
g7 = stat(run(CARD, fee=4.0))["累计%"]
print(f"  回撤 {st['回撤%']}% {'过' if abs(st['回撤%']) <= 35 else '不过'}"
      f"   去5天 {g6:+.1f}% {'过' if g6 >= 0 else '不过'}"
      f"   成本翻倍 {g7:+.1f}% {'过' if g7 > 0 else '不过'}")

print("\n判定按 PLAN_FOLLOW_V3_v1 第五节执行,本脚本不下结论。")
