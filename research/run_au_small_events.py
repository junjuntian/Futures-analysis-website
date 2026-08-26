"""黄金小手数事件裁决(运营者问:海通+170为何不静默):分布/预测力/静默线反事实。
跑法:python research/run_au_small_events.py(纯研究,金银引擎一个字节不改)"""
import sys, os, pathlib, io
import numpy as np, pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import smart_money as SM

D = pathlib.Path(__file__).resolve().parent / "data"
price, seat = SM.load_from_csv(D, "AU")
gold_usd = SM.load_gold_usd(D)
eng = SM.MarketEngine("AU", price, seat, crash_ref=gold_usd)

ev = eng.ev_long.copy()
ev["ah"] = ev["hands"].abs()
L = [f"黄金增多事件解剖(数据至 {eng.dates[-1].date()};事件 {len(ev)} 个,阈值=席位自身250日|flow|80分位)", ""]

# 1. 分布
for lo, hi, tag in ((0, 300, "<300手"), (300, 1000, "300~1000"), (1000, 10**9, ">1000")):
    sub = ev[(ev.ah >= lo) & (ev.ah < hi)]
    L.append(f"  {tag}: {len(sub)} 个({len(sub)/len(ev)*100:.0f}%)  强度中位 {sub['strength'].median():.2f}")
by_m = ev.groupby("member")["ah"].median().sort_values()
L.append("  各席位事件手数中位: " + "; ".join(f"{m}:{v:,.0f}" for m, v in by_m.items()))

# 2. 预测力:事件后10日主力收益(按手数桶)
c = eng.cont["adj_close"]
fwd = c.shift(-10) / c - 1
L.append("")
L.append("事件后10日黄金收益(按手数桶):")
for lo, hi, tag in ((0, 300, "<300手"), (300, 1000, "300~1000"), (1000, 10**9, ">1000")):
    sub = ev[(ev.ah >= lo) & (ev.ah < hi)]
    r = fwd.reindex(sub["trade_date"]).dropna()
    if len(r):
        L.append(f"  {tag}: n={len(r)}  均 {r.mean()*100:+.2f}%  胜率 {(r>0).mean()*100:.0f}%")
allr = fwd.reindex(eng.dates).dropna()
L.append(f"  无条件基线: 均 {allr.mean()*100:+.2f}%  胜率 {(allr>0).mean()*100:.0f}%")

# 3. 反事实:静默 |hands|<FLOOR 后,历史买点变不变(monkeypatch detect_events,只在本进程)
orig_detect = SM.detect_events
def trades_key(trades):
    return [(t.signal_date.strftime("%Y-%m-%d"), t.is_relay) for t in trades]
base_trades = eng.replay()
base_key = trades_key(base_trades)
base_ret = float(np.prod([1 + t.ret_pct / 100 for t in base_trades if t.ret_pct is not None]) - 1) * 100
L.append("")
for FLOOR in (300, 500, 1000):
    def patched(md, cont, members, direction="long", _f=FLOOR):
        out = orig_detect(md, cont, members, direction)
        return out[out["hands"].abs() >= _f].reset_index(drop=True)
    SM.detect_events = patched
    e2 = SM.MarketEngine("AU", price, seat, crash_ref=gold_usd)
    t2 = e2.replay()
    k2 = trades_key(t2)
    r2 = float(np.prod([1 + t.ret_pct / 100 for t in t2 if t.ret_pct is not None]) - 1) * 100
    changed = [x for x in base_key if x not in k2] + [x for x in k2 if x not in base_key]
    L.append(f"静默 <{FLOOR}手: 笔数 {len(base_key)}→{len(k2)}  复利 {base_ret:+.1f}%→{r2:+.1f}%  "
             f"买点变动 {len(changed)} 处" + (f"  变动: {changed[:6]}" if changed else "(历史买点一字不变)"))
SM.detect_events = orig_detect

# 4. 海通 8/25 事件复核
ht = eng.md[eng.md["member"] == "海通期货"].set_index("trade_date").sort_index()
flow = (ht["dnet"] / eng.cont["oi_total"].reindex(ht.index)).dropna()
thr = flow.abs().rolling(SM.RULES["event_window"], min_periods=SM.RULES["event_min_hist"]).quantile(SM.RULES["event_q"]).shift(1)
d = pd.Timestamp("2026-08-25")
if d in flow.index:
    L.append("")
    L.append(f"海通 8/25 复核: dnet=+{ht.loc[d,'dnet']:.0f} 手  flow={flow[d]*1e4:.3f}bp  "
             f"阈值={thr[d]*1e4:.3f}bp  强度={flow[d]/thr[d]:.2f}  "
             f"海通近一年日净变动中位 {ht['dnet'].abs().tail(250).median():.0f} 手")
txt = "\n".join(L)
io.open(pathlib.Path(__file__).resolve().parent / "out" / "au_small_events.txt", "w", encoding="utf-8").write(txt)
