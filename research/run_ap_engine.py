"""苹果 AP 上机构资金页:两格 + 基准 + 逐年 + 两腿(预注册 PLAN_AP_ENGINE_v1)。

**数字以引擎自算为准**:两格都走 `hog_money.main()` 的原路径(FLOW_CODES=AP),
读它写出的 `ap_signals.json`,不在这里另写一套回放 —— 燃油那次研究脚本与引擎
差了一笔未平仓(DEC-242),页面以引擎为准。本脚本只做三件引擎不直接给的事:
B 格(改 `signal_source` 再跑一遍)、主力买入持有基准、逐年与两腿拆分。

用法(仓库根目录):  python research/run_ap_engine.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
os.chdir(ROOT / "engine")                       # 引擎按 ../research/data 找 CSV
import hog_money as H  # noqa: E402


def run_cell(overrides: dict) -> dict:
    """按引擎原路径跑一格,回 ap_signals.json 的内容。"""
    out = Path(tempfile.mkdtemp(prefix="ap_cell_"))
    base = dict(H.VARIETIES["AP"])
    H.VARIETIES["AP"] = dict(base, **overrides)
    os.environ.update(ENGINE_SOURCE="csv", CSV_DIR=str(ROOT / "research/data"),
                      FLOW_OUT_DIR=str(out), FLOW_CODES="AP")
    try:
        H.main()
    finally:
        H.VARIETIES["AP"] = base
    return json.loads((out / "ap_signals.json").read_text(encoding="utf-8"))


def legs(hist: list) -> dict:
    t = pd.DataFrame(hist)
    out = {}
    for side, g in t.groupby("side"):
        out[side] = dict(n=len(g), sum=float(g["ret_pct"].sum()),
                         mean=float(g["ret_pct"].mean()),
                         win=float((g["ret_pct"] > 0).mean() * 100))
    return out


def by_year(hist: list) -> dict:
    t = pd.DataFrame(hist)
    t["y"] = t["entry_date"].str[:4]
    return {y: round(float(g["ret_pct"].sum()), 1) for y, g in t.groupby("y")}


def buy_hold(first_entry: str) -> float:
    """主力合约买入持有,从第一笔进场那天起(与策略同一段区间)。"""
    H.use("AP")
    price = H.clean_price(pd.read_csv(ROOT / "research/data/ap_price.csv.gz"))
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(first_entry)]
    eq = (1 + mkt["ret_open"].fillna(0)).cumprod()
    return (float(eq.iloc[-1]) - 1) * 100


def show(tag: str, d: dict):
    s, c = d["stats"], d["compare"]["strategy"]
    print(f"\n[{tag}]  {s['trades']} 笔(空 {s['short_trades']} / 多 {s['long_trades']})"
          f"  胜率 {s['win_rate']}%  单笔均值 {s['avg_pct']:+}%  累计(单利和) {s['cum_pct']:+}%")
    print(f"   复利净值(扣 0.05% 换手) {c['cum_pct']:+}%  夏普 {c['sharpe']}  回撤 {c['max_dd_pct']}%")
    print(f"   出场原因 {s['exit_reasons']}")
    for side, v in legs(d["history"]).items():
        print(f"   {'做空' if side == 'short' else '做多'}腿 {v['n']} 笔 合计 {v['sum']:+.1f}%"
              f"  均值 {v['mean']:+.2f}%  胜率 {v['win']:.0f}%")
    print(f"   逐年(按进场年,单笔收益加总) {by_year(d['history'])}")
    for f in d.get("risk_flags", []):
        print(f"   风险条 · {f['key']}: {f['text']}")


def main() -> int:
    a = run_cell({})
    b = run_cell({"signal_source": "cost"})
    print("=" * 96)
    print("苹果 AP · PLAN_AP_ENGINE_v1(数字全部来自引擎自算的 ap_signals.json)")
    show("A 默认(共振进场·代表格)", a)
    show("B 成本进场(玻纯同款,只报不选)", b)
    first = min(x["entry_date"] for x in a["history"])
    bh = buy_hold(first)
    print(f"\n[基准] 主力买入持有(自 {first} 起,与 A 格第一笔同日){bh:+.1f}%;"
          f"引擎内置基准「{a['compare']['benchmark_name']}」{a['compare']['benchmark']['cum_pct']:+}%")
    es = a["edge_split"]
    print(f"[拿不到的那段] 触发 {es['n']} 次,次日超额 {es['day1_pct']:+.3f}%,"
          f"其中隔夜跳空 {es['gap_pct']:+.3f}%(占 {es['gap_share_pct']}%)、日内 {es['intraday_pct']:+.3f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
