"""采三禾的大商所席位历史。

焦煤、鸡蛋、生猪的席位，交易所自己不给（脚本 412、真实浏览器 500），东财只到
2025-11 且到 2026 年中都残缺。三禾是唯一覆盖到 2023-08-10 的来源。

三禾按**会员**组织而不是按合约，所以要拼出「某合约当日的席位表」，得把会员逐个走
一遍。`sanhe_survey.py` 已经筛出真正持有这三个品种的 108 家（208 家里的一半），
其余的每天都取只是白费请求。

**从最近的日期往回采**：这样中途停下也是手里握着最有用的那一段，而不是握着三年前
的一段、最近的反而没有。
"""

import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import requests

ROOT = Path("/opt/futures-platform/sanhe-seats")
RAW = ROOT / "raw"
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
PACE = float(os.environ.get("SANHE_PACE", "1.2"))


def post(path, data=None):
    last = None
    for attempt in range(3):
        try:
            response = requests.post(
                f"https://www.sanheshuju.com/ajax/{path}",
                data=data or {},
                headers=HEADERS,
                timeout=40,
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:  # noqa: BLE001 - retried, then reported
            last = error
            time.sleep(3 * (attempt + 1))
    raise last


def main() -> int:
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2023, 8, 10)
    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(UTC).date()  # 三禾窗口以 last_date 为准，下面还会再收一次口

    brokers = json.loads((ROOT / "dce_brokers.json").read_text(encoding="utf-8"))
    window = post("broker_dates.php").get("data") or {}
    # 三禾是滚动窗口，早于 first_date 的日期请求了也只会拿到空。
    first = date.fromisoformat(window["first_date"])
    last = date.fromisoformat(window["last_date"])
    start = max(start, first)
    end = min(end, last)
    print(
        f"会员 {len(brokers)} 家，区间 {start}..{end}（三禾窗口 {first}..{last}）",
        flush=True,
    )

    days = [
        day
        for day in (
            date.fromordinal(o)
            for o in range(end.toordinal(), start.toordinal() - 1, -1)
        )
        if day.weekday() < 5
    ]
    issued = kept = 0
    for index, day in enumerate(days, start=1):
        stamp = day.isoformat()
        day_dir = RAW / stamp
        day_dir.mkdir(parents=True, exist_ok=True)
        found = 0
        for broker in brokers:
            target = day_dir / f"{broker}.json"
            if target.exists():
                continue
            time.sleep(PACE)
            try:
                payload = post(
                    "broker_positions.php", {"broker": broker, "date": stamp}
                )
            except Exception as error:  # noqa: BLE001
                print(f"FAIL {stamp} {broker} {type(error).__name__}", flush=True)
                continue
            issued += 1
            data = payload.get("data") or {}
            mine = {k: v for k, v in (data.get("positions") or {}).items() if k in WANT}
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
                found += 1
                kept += 1
        print(
            f"{stamp} ({index}/{len(days)}) 命中 {found}，累计写出 {kept}", flush=True
        )
    print(f"\n请求 {issued} 次，写出 {kept} 个文件", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
