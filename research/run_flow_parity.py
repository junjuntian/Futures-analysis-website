"""引擎(engine/hog_money.py)与研究实现的**逐品种**逐笔对拍。

替代原来只对生猪的 run_lh_parity.py。三个品种规则不同(生猪只做空、玻璃纯碱
双向且不要 dip),各自的差异都要能被这个脚本抓到。

2026-08-19 这类对拍已经抓到过三次真分叉:
  ①两边取席位数据一个用全量 seat、一个用与行情内连接后的 df;
  ②引擎判共振用未标准化的 chg,在信号预热完成前就开仓;
  ③引擎带着生猪时代的 long_needs_dip 去跑玻璃纯碱,少了几十笔。
每次都是先看到「笔数对不上」才查出来的——**笔数是最灵的那个警报**。
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np, pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import run_fgsa_planc as P      # noqa: E402  研究侧独立实现
import run_lh_compare_v2 as C   # noqa: E402  生猪那套

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "hog_money.py"
OUT = Path(tempfile.gettempdir()) / "flow_parity"


def engine_all() -> dict:
    env = dict(os.environ)
    env.update({"ENGINE_SOURCE": "csv", "CSV_DIR": str(Path(__file__).parent / "data"),
                "FLOW_OUT_DIR": str(OUT), "PYTHONIOENCODING": "utf-8",
                # 不给的话引擎按默认只跑 LH,FG,SA,新品种的产物根本不存在
                "FLOW_CODES": "LH,FG,SA,JD,JM"})
    subprocess.run([sys.executable, str(ENGINE)], check=True, env=env,
                   capture_output=True, text=True, encoding="utf-8")
    files = {"LH": "hog_signals.json", "FG": "fg_signals.json", "SA": "sa_signals.json",
             "JD": "jd_signals.json", "JM": "jm_signals.json"}
    return {c: pd.DataFrame(json.loads((OUT / f).read_text(encoding="utf-8"))["history"])
            for c, f in files.items()}


# 各品种的做多开关必须与 engine VARIETIES 一致 —— 对拍要复刻的是**线上那一条**,
# 不是「研究侧觉得该怎么跑」。玻璃纯碱双向,生猪/鸡蛋/焦煤只做空。
SHORT_ONLY = {"LH": True, "JD": True, "JM": True, "FG": False, "SA": False}


def research(code: str) -> pd.DataFrame:
    if code == "LH":
        return C.backtest(C.rz_res, C.rz, C.mr).rename(columns={"原因": "出场原因"})
    mr, pz, rz, _ = P.prep(code)
    rz_res = rz.where(np.sign(pz) == np.sign(rz))
    return P.bt(rz_res, rz, mr, short_only=SHORT_ONLY[code], long_needs_dip=False)


def main():
    eng = engine_all()
    ok = True
    for code in ("LH", "FG", "SA", "JD", "JM"):
        e, r = eng[code], research(code)
        head = f"{code}: 引擎 {len(e)} 笔 / 研究 {len(r)} 笔"
        if len(e) != len(r):
            print(f"✗ {head}  ← 笔数不一致"); ok = False; continue
        bad = 0
        for i in range(len(e)):
            ed = e.loc[i, "entry_date"]
            rd = r.iloc[i]["进场"].strftime("%Y-%m-%d")
            se = "空" if e.loc[i, "side"] == "short" else "多"
            gross = r.iloc[i]["收益%"] + 2 * 0.0005 * 100
            if ed != rd or se != r.iloc[i]["方向"] or abs(e.loc[i, "ret_pct"] - gross) > 0.02:
                bad += 1
                if bad <= 3:
                    print(f"  ✗ 第{i+1}笔 引擎[{ed} {se} {e.loc[i,'ret_pct']:+.2f}%] "
                          f"研究[{rd} {r.iloc[i]['方向']} {gross:+.2f}%]")
        if bad:
            print(f"✗ {head}  {bad} 笔不一致"); ok = False
        else:
            print(f"✓ {head}  逐笔一致(日期/方向/毛收益)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
