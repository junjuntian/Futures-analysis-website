"""把三禾采到的大商所席位 JSON 转成 seat_history 的装载 CSV。

三禾是按**会员**组织的：一份文件 = 某会员某日在焦煤/鸡蛋/生猪上的全部合约持仓。
交易所自己只公布每个合约的前 20 名，三禾给的是该会员的真实持仓，所以这份数据比
交易所口径更全——代价是**没有名次**，`rank` 一律留空，不编。

一条合约记录会拆成两行入库：持买单量走 `long`，持卖单量走 `short`，各自带当日增减。
买卖都是 0 且都没变动的行直接丢掉，那是三禾把该会员所有合约都列出来的填充行；
但「持仓归零而当日有减仓」是真事件，建仓过程页要用，必须留下。
"""

import csv
import json
import re
import sys
from pathlib import Path

RAW = Path("/opt/futures-platform/sanhe-seats/raw")
OUT = Path("/opt/futures-platform/load/seat_dce.csv")
SOURCE = "sanhe"
# jd2702 -> JD / JD2702
CODE = re.compile(r"^([a-z]{1,2})(\d{4})$")
COLUMNS = [
    "exchange",
    "instrument",
    "contract",
    "is_variety_total",
    "variety_total_is_computed",
    "trade_date",
    "rank_type",
    "rank",
    "member",
    "quantity",
    "change",
    "source",
]


def rows_from(payload):
    trade_date = payload.get("date")
    member = (payload.get("broker") or "").strip()
    if not trade_date or not member:
        return
    for entries in (payload.get("positions") or {}).values():
        for entry in entries:
            matched = CODE.match(entry.get("code") or "")
            if not matched:
                print(f"跳过无法解析的合约 {entry.get('code')!r}", file=sys.stderr)
                continue
            instrument = matched.group(1).upper()
            contract = instrument + matched.group(2)
            for rank_type, quantity_key, change_key in (
                ("long", "buy", "buy_chge"),
                ("short", "ss", "ss_chge"),
            ):
                quantity = entry.get(quantity_key) or 0
                change = entry.get(change_key) or 0
                if quantity == 0 and change == 0:
                    continue
                yield [
                    "DCE",
                    instrument,
                    contract,
                    "f",
                    "f",
                    trade_date,
                    rank_type,
                    "",  # 三禾不给名次
                    member,
                    quantity,
                    change,
                    SOURCE,
                ]


def main() -> int:
    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else RAW
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    out.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(raw.glob("*/*.json"))
    written = skipped = 0
    dates = set()
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # 采集被打断时最后一个文件可能是半截的，重采即可。
                print(f"跳过残缺文件 {path}", file=sys.stderr)
                skipped += 1
                continue
            for row in rows_from(payload):
                writer.writerow(row)
                written += 1
                dates.add(row[5])

    span = f"{min(dates)}..{max(dates)}" if dates else "空"
    print(f"文件 {len(files)} 个（残缺 {skipped}），写出 {written} 行，日期 {span}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
