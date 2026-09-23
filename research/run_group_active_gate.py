"""在榜率门槛:六个滚动重选品种的新旧对照(预注册 PLAN_GROUP_ACTIVE_GATE_v1)。

**数字以引擎自算为准**:每个品种走 `hog_money.main()` 的原路径,读它写出的
`*_signals.json`;新旧两版靠**同一份代码**跑,只翻资格门的判据开关 —— 所以差异只可能来自这条门槛本身。

开关:`GROUP_ACTIVE_GATE` = `off` 旧口径(累计在榜天数)/ `force` 新口径且
无视逐品种开关(玻璃自己关着,不 force 就量不到它)。生产两个都不设。

用法(仓库根目录):  python research/run_group_active_gate.py
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
os.chdir(ROOT / "engine")
import hog_money as H  # noqa: E402

# 只有滚动重选的品种受影响;LH 与 FU 是固定名单。
# IH 不在列:它的「核心席位在场看板」(DEC-172)是三家点名席位、零参数,
# **根本不走 rolling_groups / alpha_upto**,这条门槛碰不到它。
# (第一版把它列进来了 —— 只看「没配 fixed_members」就判成滚动重选,是错的。)
CODES = ["FG", "SA", "JD", "JM", "I", "AP"]
OUTS = {"FG": "fg_signals.json", "SA": "sa_signals.json", "JD": "jd_signals.json",
        "JM": "jm_signals.json", "I": "i_signals.json", "AP": "ap_signals.json"}


def run(code: str, gate: bool) -> dict:
    out = Path(tempfile.mkdtemp(prefix=f"gate_{code}_"))
    os.environ.update(ENGINE_SOURCE="csv", CSV_DIR=str(ROOT / "research/data"),
                      FLOW_OUT_DIR=str(out), FLOW_CODES=code)
    os.environ["GROUP_ACTIVE_GATE"] = "force" if gate else "off"
    H.main()
    return json.loads((out / OUTS[code]).read_text(encoding="utf-8"))


def perf(d: dict) -> dict:
    c = d.get("compare", {}).get("strategy") or {}
    s = d.get("stats") or {}
    return dict(n=s.get("trades"), cum=c.get("cum_pct"), sharpe=c.get("sharpe"),
                dd=c.get("max_dd_pct"), win=s.get("win_rate"))


def groups_of(code: str, gate: bool) -> dict:
    """**逐日**的席位组,不是 payload 里的 `group_log`。

    第一版读的是 `group_log`,结果玻璃的 diff 印出来是空的 —— 而它的阵容其实
    2014-10 起就变了(中证期货被剔出)。`group_log` **只记「阵容变了」的切点**,
    再经 freeze / overrides 后处理,拿它做新旧对照会漏掉真正的差异。
    直接调 `rolling_groups` 拿逐日序列,再压成「切点 → 阵容」。
    """
    H.use(code)
    price = H.clean_price(pd.read_csv(ROOT / f"research/data/{code.lower()}_price.csv.gz"))
    seat = H.clean_seat(pd.read_csv(ROOT / f"research/data/{code.lower()}_seat.csv.gz"))
    mkt = H.main_series(price)
    mkt = mkt[mkt.index >= pd.Timestamp(H.RULES["replay_start"])]
    os.environ["GROUP_ACTIVE_GATE"] = "force" if gate else "off"
    ser, log, _cuts = H.rolling_groups(seat, price, mkt.index)
    topped = {e["date"]: e.get("topped_up") or [] for e in log}
    out, prev = {}, None
    for day, members in ser.items():
        if members != prev:
            key = day.strftime("%Y-%m-%d")
            out[key] = (list(members) if members else [], topped.get(key, []))
            prev = members
    return out


def main() -> int:
    print("=" * 104)
    print("在榜率门槛 · 六个滚动重选品种(数字来自引擎自算)")
    rows, diffs = {}, {}
    for code in CODES:
        try:
            old, new = run(code, gate=False), run(code, gate=True)
        except Exception as error:  # noqa: BLE001 - 一个品种炸了不该拖垮整张表
            print(f"  {code}: 跑不动 {type(error).__name__}: {error}")
            continue
        rows[code] = (perf(old), perf(new))
        diffs[code] = (groups_of(code, False), groups_of(code, True))

    print(f"\n{'品种':<5}{'笔数 旧→新':>14}{'复利净值 旧→新':>22}{'Δ':>9}"
          f"{'夏普 旧→新':>16}{'Δ':>8}{'回撤 旧→新':>18}")
    for code, (o, n) in rows.items():
        dc = (n["cum"] or 0) - (o["cum"] or 0)
        ds = (n["sharpe"] or 0) - (o["sharpe"] or 0)
        trades = f"{o['n']}→{n['n']}"
        cum = f"{o['cum']:+.1f}%→{n['cum']:+.1f}%"
        sharpe = f"{o['sharpe']}→{n['sharpe']}"
        dd = f"{o['dd']}%→{n['dd']}%"
        print(f"  {code:<4}{trades:>13}{cum:>22}{dc:>+9.1f}"
              f"{sharpe:>16}{ds:>+8.2f}{dd:>18}")

    print("\n[G2] 复利净值跌 >10pp 或夏普跌 >0.10 的品种 → 那个品种不上")
    bad = []
    for code, (o, n) in rows.items():
        dc = (n["cum"] or 0) - (o["cum"] or 0)
        ds = (n["sharpe"] or 0) - (o["sharpe"] or 0)
        if dc < -10 or ds < -0.10:
            bad.append(code)
            print(f"  ✗ {code}: 净值 {dc:+.1f}pp、夏普 {ds:+.2f}")
    print("  全部通过" if not bad else f"  不通过:{bad}")

    print("\n[G3] 兜底补人占切点比例 >1/3 的品种 → 那个品种不上")
    for code, (_, gnew) in diffs.items():
        n_top = sum(1 for _, (_, t) in gnew.items() if t)
        print(f"  {code}: 换人记录 {len(gnew)} 条,其中带兜底 {n_top} 条"
              f"{'  ✗' if len(gnew) and n_top / len(gnew) > 1 / 3 else ''}")

    print("\n[G1 + G4] 逐品种逐切点阵容 diff(旧 → 新)")
    for code, (gold, gnew) in diffs.items():
        print(f"\n  —— {code} ——")
        for date in sorted(set(gold) | set(gnew)):
            o = gold.get(date, (None, []))[0]
            n, topped = gnew.get(date, (None, []))
            if o == n:
                continue
            print(f"    {date}")
            print(f"       旧 {'、'.join(o) if o else '(无此切点)'}")
            print(f"       新 {'、'.join(n) if n else '(无此切点)'}"
                  f"{'   [兜底补入 ' + '、'.join(topped) + ']' if topped else ''}")
            if o and n:
                gone, came = [m for m in o if m not in n], [m for m in n if m not in o]
                if gone or came:
                    print(f"       换出 {'、'.join(gone) or '—'}   换入 {'、'.join(came) or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
