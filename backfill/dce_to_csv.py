"""大商所年度文件 -> price_history 形状的 CSV。

跑在采集器容器里，因为宿主机没有 pandas。文件是交易所官网自己发布的年度逐合约
行情，2013–2024 每品种每年一个。

两处格式差异（`docs/RAW_FIELD_INVENTORY.md`）：交易所在 2019 年改过自己的表头，
2024 年发布成 .xls 而不是 .xlsx。两者都读。
"""

import argparse
import csv
import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd

# 默认三品种;--want 可覆盖(DEC-158 铁矿石回填加 I,文件名如 i_2018.xlsx)。
DEFAULT_WANT = "JM,JD,LH"
COLUMNS = [
    "exchange",
    "instrument",
    "contract",
    "trade_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "settlement_price",
    "prev_settlement_price",
    "volume",
    "volume_basis",
    "turnover",
    "open_interest",
    "open_interest_change",
    "source",
]
# 左边是交易所在某个年代用的列名，右边是我们要的含义。
# 2013–2018 用「合约名称 / 交易日期」，2019 起改成「合约 / 日期」。
ALIASES = {
    "contract": ("合约名称", "合约"),
    "trade_date": ("交易日期", "日期"),
    "open": ("开盘价",),
    "high": ("最高价",),
    "low": ("最低价",),
    "close": ("收盘价",),
    "settlement": ("结算价",),
    "prev_settlement": ("前结算价",),
    "volume": ("成交量",),
    "turnover": ("成交额",),
    "open_interest": ("持仓量",),
    "open_interest_change": ("持仓量变化",),
}


def num(value):
    if value is None:
        return ""
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "--", "nan", "None"):
        return ""
    try:
        float(s)
    except ValueError:
        return ""
    return s


def pick(row, names):
    for name in names:
        if name in row and str(row[name]).strip() not in ("", "nan"):
            return row[name]
    return None


def read_table(path):
    """xlsx/xls 走 pandas.read_excel;2013-2016 打包里的 csv 是 GBK。
    dtype=str 与原版一致:下游按字符串清洗,让 pandas 自作聪明转数值会毁掉它。"""
    if path.suffix == ".csv":
        return pd.read_csv(path, encoding="gbk", dtype=str)
    return pd.read_excel(path, dtype=str)


def rows_from(path):
    frame = read_table(path)
    # 交易所的表头带前后空格，不 strip 的话下面的列名匹配全部落空。
    frame.columns = [str(c).strip() for c in frame.columns]
    out = []
    for _, raw in frame.iterrows():
        row = {k: v for k, v in raw.items()}
        contract = str(pick(row, ALIASES["contract"]) or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,2}\d{4}", contract):
            continue
        stamp = re.sub(r"\D", "", str(pick(row, ALIASES["trade_date"]) or ""))[:8]
        if len(stamp) != 8:
            continue
        close = num(pick(row, ALIASES["close"]))
        settlement = num(pick(row, ALIASES["settlement"]))
        if not close and not settlement:
            continue
        opening = num(pick(row, ALIASES["open"]))
        high = num(pick(row, ALIASES["high"]))
        low = num(pick(row, ALIASES["low"]))
        # 当日无成交时交易所把四项写成 0。那是「没有成交」，不是「以 0 成交」。
        if {opening, high, low, close} == {"0"}:
            opening = high = low = ""
        out.append(
            [
                "DCE",
                "".join(c for c in contract if c.isalpha()),
                contract,
                f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}",
                opening,
                high,
                low,
                close,
                settlement,
                num(pick(row, ALIASES["prev_settlement"])),
                num(pick(row, ALIASES["volume"])),
                # 大商所页面注明成交量按双边计算。
                "double",
                # 大商所的成交额单位就是元，无需换算。
                num(pick(row, ALIASES["turnover"])),
                num(pick(row, ALIASES["open_interest"])),
                num(pick(row, ALIASES["open_interest_change"])),
                "dce_official_history",
            ]
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/srv/dce-history")
    ap.add_argument("--out", default="/srv/load/price_dce.csv")
    ap.add_argument("--want", default=DEFAULT_WANT)
    args = ap.parse_args()

    want = {v.strip().upper() for v in args.want.split(",") if v.strip()}
    # 文件名形如 jm_2013.xlsx / i_2018.xlsx:按下划线取品种码,别按前两个字符
    # (单字码品种 i 会带上下划线永远匹配不上,而且悄无声息)。
    files = sorted(
        p
        for p in Path(args.dir).iterdir()
        if p.suffix in (".xlsx", ".xls", ".csv") and p.stem.split("_")[0].upper() in want
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    total = failed = 0
    with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for path in files:
            try:
                rows = rows_from(path)
            except Exception as error:  # noqa: BLE001 - named, then next file
                failed += 1
                print(f"FAIL {path.name}: {type(error).__name__}: {error}", flush=True)
                continue
            writer.writerows(rows)
            total += len(rows)
            print(f"  {path.name}: {len(rows)} 行", flush=True)
    print(f"\n{len(files)} 个文件，失败 {failed}，共 {total} 行 -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
