"""Survey pass: which firms ever appear on the three DCE varieties.

Sanhe organises seats by firm, so rebuilding a contract's seat table means
walking every firm. Most of the 208 never touch 焦煤, 鸡蛋 or 生猪, and walking
them for every trading day would triple the request count for nothing. This
samples a few dates with the full list, and writes down the firms that actually
turn up. Raw responses are kept on disk -- collect first, look at what arrived,
then design the table.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

OUT = Path("/opt/futures-platform/sanhe-seats")
RAW = OUT / "raw"
WANT = {"焦煤", "鸡蛋", "生猪"}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sanheshuju.com/",
    "User-Agent": UA,
}
PACE = float(os.environ.get("SANHE_PACE", "1.5"))
SAMPLE_DATES = sys.argv[1:] or [
    "2025-01-02",
    "2025-06-03",
    "2025-11-03",
    "2026-03-02",
    "2026-08-07",
]


def post(path, data=None):
    last = None
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://www.sanheshuju.com/ajax/{path}",
                data=data or {},
                headers=HEADERS,
                timeout=40,
            )
            r.raise_for_status()
            return r.json()
        except Exception as error:  # noqa: BLE001 - retried, then reported
            last = error
            time.sleep(3 * (attempt + 1))
    raise last


RAW.mkdir(parents=True, exist_ok=True)
brokers = [
    b["name"] for b in (post("all_brokers.php").get("data") or []) if b.get("name")
]
(OUT / "brokers.json").write_text(
    json.dumps(brokers, ensure_ascii=False, indent=1), encoding="utf-8"
)
print(f"会员 {len(brokers)} 家，采样日期 {SAMPLE_DATES}", flush=True)

hits: dict[str, int] = {}
issued = 0
for day in SAMPLE_DATES:
    day_dir = RAW / day
    day_dir.mkdir(parents=True, exist_ok=True)
    found_today = 0
    for index, broker in enumerate(brokers, start=1):
        target = day_dir / f"{broker}.json"
        if target.exists():
            continue
        time.sleep(PACE)
        try:
            payload = post("broker_positions.php", {"broker": broker, "date": day})
        except Exception as error:  # noqa: BLE001
            print(f"  FAIL {day} {broker} {type(error).__name__}", flush=True)
            continue
        issued += 1
        data = payload.get("data") or {}
        positions = data.get("positions") or {}
        # Only keep a file when the firm actually held one of ours: 208 empty
        # files a day would bury the ones that matter.
        mine = {k: v for k, v in positions.items() if k in WANT}
        if mine:
            target.write_text(
                json.dumps(
                    {
                        "date": data.get("date"),
                        "broker": data.get("broker"),
                        "positions": mine,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hits[broker] = hits.get(broker, 0) + 1
            found_today += 1
        if index % 50 == 0:
            print(f"  {day} {index}/{len(brokers)} 命中 {found_today}", flush=True)
    print(f"{day}: {found_today} 家持有焦煤/鸡蛋/生猪", flush=True)

ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
(OUT / "dce_brokers.json").write_text(
    json.dumps([name for name, _ in ranked], ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(
    f"\n请求 {issued} 次；{len(ranked)}/{len(brokers)} 家曾持有这三个品种", flush=True
)
print("前 20:", [f"{n}×{c}" for n, c in ranked[:20]], flush=True)
