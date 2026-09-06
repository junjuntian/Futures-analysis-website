"""Load the raw files into price_history and seat_history.

Runs as futures_app, which is superuser and so is not filtered by the row level
security on these tables. Every row still carries the workspace explicitly --
being able to bypass the policy is not a reason to leave the column to chance.

Re-running is safe: each row is keyed on what identifies it at the source, and a
second pass updates in place rather than duplicating.
"""

import argparse
import glob
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/opt/futures-platform")
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

import parsers  # noqa: E402

RAW = Path("/opt/futures-platform/exchange-raw")
SANHE = Path("/opt/futures-platform/sanhe-seats/raw")
DCE_DIR = "/opt/futures-platform/dce-history"

PRICE_COLUMNS = (
    "id workspace_id exchange instrument contract trade_date open_price high_price low_price "
    "close_price settlement_price prev_settlement_price volume volume_basis turnover "
    "open_interest open_interest_change source"
).split()
SEAT_COLUMNS = (
    "id workspace_id exchange instrument contract is_variety_total variety_total_is_computed "
    "trade_date rank_type rank member quantity change source"
).split()


def n(value):
    """A number or NULL. The parsers hand back strings; empty means absent."""
    return None if value in ("", None) else value


def price_row(workspace, r):
    return (
        str(uuid.uuid4()),
        workspace,
        r["exchange"],
        r["instrument"],
        r["contract"],
        r["trade_date"],
        n(r.get("open")),
        n(r.get("high")),
        n(r.get("low")),
        n(r.get("close")),
        n(r.get("settlement")),
        n(r.get("prev_settlement")),
        n(r.get("volume")),
        r["volume_basis"],
        n(r.get("turnover")),
        n(r.get("open_interest")),
        n(r.get("open_interest_change")),
        r["source"],
    )


def seat_row(workspace, r):
    return (
        str(uuid.uuid4()),
        workspace,
        r["exchange"],
        r["instrument"],
        r.get("contract"),
        r["is_variety_total"],
        r.get("variety_total_is_computed", False),
        r["trade_date"],
        r["rank_type"],
        n(r.get("rank")),
        r["member"],
        r["quantity"],
        n(r.get("change")),
        r["source"],
    )


# 两个时间戳各司其职(2026-08-16,迁移 202608160001,口径见 load-seats-direct.sql):
# loaded_at=首次入库(default now(),upsert 不碰);updated_at=最近装载触碰
# (insert 走 VALUES 模板末尾的 now(),upsert 显式刷新)。
PRICE_SQL = f"""
insert into price_history ({", ".join(PRICE_COLUMNS)}, updated_at)
values %s
on conflict (workspace_id, contract, trade_date, source) do update set
  open_price = excluded.open_price, high_price = excluded.high_price,
  low_price = excluded.low_price, close_price = excluded.close_price,
  settlement_price = excluded.settlement_price,
  prev_settlement_price = excluded.prev_settlement_price,
  volume = excluded.volume, volume_basis = excluded.volume_basis,
  turnover = excluded.turnover, open_interest = excluded.open_interest,
  open_interest_change = excluded.open_interest_change, updated_at = now()
"""
PRICE_TEMPLATE = "(" + ", ".join(["%s"] * len(PRICE_COLUMNS)) + ", now())"

SEAT_SQL = f"""
insert into seat_history ({", ".join(SEAT_COLUMNS)}, updated_at)
values %s
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
  rank = excluded.rank, quantity = excluded.quantity,
  change = excluded.change, updated_at = now()
"""
SEAT_TEMPLATE = "(" + ", ".join(["%s"] * len(SEAT_COLUMNS)) + ", now())"


def flush(cur, sql, rows):
    if rows:
        template = PRICE_TEMPLATE if sql is PRICE_SQL else SEAT_TEMPLATE
        psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=1000)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    # dce 不在列表里:曾经列了却没有任何执行分支,--what dce 打印 0 行然后
    # 成功退出,人和调度都会把「什么都没干」读成「回填完成」。DCE 的装载
    # 走 dce_to_csv.py + load_all.sh 那条路,不从这里进。
    ap.add_argument(
        "--what", required=True, choices=["czce", "shfe", "ine", "sanhe", "all"]
    )
    ap.add_argument(
        "--workspace-id", required=True,
        help="目标 workspace 的 UUID。必填:此前按「UUID 最小的 workspace」猜,"
             "生产上最小的是 Phase 3C 的测试租户,全部历史会灌进测试空间而页面无变化。")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件，用于验收")
    args = ap.parse_args()

    conn = psycopg2.connect(os.environ["PGDSN"])
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("select id from workspaces where id = %s", (args.workspace_id,))
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"workspace {args.workspace_id} 不存在,拒绝装载")
    workspace = row[0]

    prices = seats = 0
    jobs = []
    if args.what in ("czce", "all"):
        jobs.append(("czce/market", parsers.czce_market, "price", "*.txt"))
        jobs.append(("czce/seats", parsers.czce_seats, "seat", "*.txt"))
    if args.what in ("shfe", "all"):
        jobs.append(("shfe/market", parsers.shfe_market, "price", "*.dat"))
        jobs.append(("shfe/seats", parsers.shfe_seats, "seat", "*.dat"))
    # INE **只有行情**:原油没有逐会员持仓排名,任何来源都没有(见 parsers.ine_market)。
    if args.what in ("ine", "all"):
        jobs.append(("ine/market", parsers.ine_market, "price", "*.dat"))

    for sub, fn, kind, pattern in jobs:
        files = sorted(glob.glob(str(RAW / sub / pattern)))
        if args.limit:
            files = files[: args.limit]
        batch = []
        for path in files:
            try:
                rows = fn(path)
            except Exception as error:  # noqa: BLE001 - named, then next file
                print(f"PARSE_FAIL {path}: {type(error).__name__}: {error}", flush=True)
                continue
            batch.extend(
                (price_row if kind == "price" else seat_row)(workspace, r) for r in rows
            )
            if len(batch) >= 5000:
                count = flush(cur, PRICE_SQL if kind == "price" else SEAT_SQL, batch)
                if kind == "price":
                    prices += count
                else:
                    seats += count
                conn.commit()
                batch = []
        count = flush(cur, PRICE_SQL if kind == "price" else SEAT_SQL, batch)
        if kind == "price":
            prices += count
        else:
            seats += count
        conn.commit()
        print(f"{sub}: {len(files)} 个文件", flush=True)

    if args.what in ("sanhe", "all"):
        files = sorted(glob.glob(str(SANHE / "*" / "*.json")))
        if args.limit:
            files = files[: args.limit]
        batch = []
        for path in files:
            try:
                batch.extend(seat_row(workspace, r) for r in parsers.sanhe_seats(path))
            except Exception as error:  # noqa: BLE001
                print(f"PARSE_FAIL {path}: {type(error).__name__}: {error}", flush=True)
            if len(batch) >= 5000:
                seats += flush(cur, SEAT_SQL, batch)
                conn.commit()
                batch = []
        seats += flush(cur, SEAT_SQL, batch)
        conn.commit()
        print(f"sanhe: {len(files)} 个文件", flush=True)

    print(f"\n价格 {prices} 行，席位 {seats} 行", flush=True)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
