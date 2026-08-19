"""生猪:引擎(engine/hog_money.py)与研究回测(run_lh_phase2.py)逐笔对拍。

两处各写了一遍同一套规则,这是**必然会分叉**的结构——PITFALLS 二那条
「同一个事实两处维护、只改了一处」就是这么栽的。所以每次改任一边,
都要跑这个脚本,逐笔比日期、方向、收益。

引擎的 ret_pct 是**毛**收益,研究版扣了双边成本,所以收益比对时把成本加回去。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import lhlib as L                      # noqa: E402
import run_lh_phase2 as P2             # noqa: E402

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "hog_money.py"


def engine_trades() -> pd.DataFrame:
    out = Path(tempfile.gettempdir()) / "hog_parity.json"
    env = {"ENGINE_SOURCE": "csv", "CSV_DIR": str(Path(__file__).parent / "data"),
           "HOG_OUT": str(out), "PYTHONIOENCODING": "utf-8"}
    import os
    e = dict(os.environ); e.update(env)
    subprocess.run([sys.executable, str(ENGINE)], check=True, env=e,
                   capture_output=True, text=True, encoding="utf-8")
    payload = json.loads(out.read_text(encoding="utf-8"))
    tr = pd.DataFrame(payload["history"])
    return tr


def research_trades() -> pd.DataFrame:
    price = L.load_price()
    seat = L.load_seat()
    df = seat.merge(price[["contract", "trade_date", "settle"]],
                    on=["contract", "trade_date"], how="inner")
    mr = P2.main_returns(price)
    mr = mr[mr.index >= df["trade_date"].min()]
    groups = P2.rolling_groups(df, mr.index)
    z = P2.zscore(P2.signal_series(df, groups))
    # 引擎的做多支路默认关闭(RULES["long_enabled"]=False),对拍要传同样的口径,
    # 否则研究侧多出 15 笔做多,会报成假失败。
    tr, _ = P2.backtest_discrete(z, mr["ret"], mr["settle"].pct_change(20),
                                 long_enabled=False)
    return tr


def main():
    eng = engine_trades()
    res = research_trades()
    print(f"引擎 {len(eng)} 笔 / 研究 {len(res)} 笔")
    ok = True
    if len(eng) != len(res):
        print("✗ 笔数不一致")
        ok = False

    n = min(len(eng), len(res))
    e = eng.head(n).reset_index(drop=True)
    r = res.head(n).reset_index(drop=True)
    side_map = {"short": "空", "long": "多"}
    bad = 0
    for i in range(n):
        ed, xd = e.loc[i, "entry_date"], e.loc[i, "exit_date"]
        rd, rx = r.loc[i, "进场"].strftime("%Y-%m-%d"), r.loc[i, "出场"].strftime("%Y-%m-%d")
        s_e, s_r = side_map[e.loc[i, "side"]], r.loc[i, "方向"]
        # 研究版扣了双边成本,加回去再比
        gross_r = r.loc[i, "收益%"] + 2 * P2.COST_ONEWAY * 100
        if ed != rd or xd != rx or s_e != s_r or abs(e.loc[i, "ret_pct"] - gross_r) > 0.02:
            bad += 1
            if bad <= 5:
                print(f"✗ 第{i+1}笔 引擎[{ed}→{xd} {s_e} {e.loc[i,'ret_pct']:+.2f}%] "
                      f"研究[{rd}→{rx} {s_r} {gross_r:+.2f}%]")
    if bad:
        print(f"✗ 共 {bad} 笔不一致")
        ok = False
    else:
        print(f"✓ {n} 笔逐笔一致(日期/方向/毛收益)")

    print("\n汇总对拍:")
    print(f"  引擎 累计{(np.prod(1 + eng['ret_pct'] / 100) - 1) * 100:+.1f}%(毛) "
          f"胜率{(eng['ret_pct'] > 0).mean() * 100:.1f}% "
          f"空{(eng['side'] == 'short').sum()}/多{(eng['side'] == 'long').sum()}")
    print(f"  研究 累计{(np.prod(1 + res['收益%'] / 100) - 1) * 100:+.1f}%(净) "
          f"胜率{(res['收益%'] > 0).mean() * 100:.1f}% "
          f"空{(res['方向'] == '空').sum()}/多{(res['方向'] == '多').sum()}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
