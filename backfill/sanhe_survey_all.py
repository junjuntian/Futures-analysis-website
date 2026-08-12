"""普查:哪些会员真的持有我们看的八个品种。

`sanhe_survey.py` 是这件事的大商所版（只看焦煤、鸡蛋、生猪），筛出 108 家。
现在要把上期所与郑商所也纳进来——**运营者的趋势跟随策略需要全部会员，而不是
交易所龙虎榜的前二十**：掉出前二十和真的清仓是两回事，只看官方榜分不清这两者。
黄金 2026-06-15 高盛归零、06-16 只剩 4 手，官方文件里都没有这一行。

三禾按会员组织，重建「某合约当日的席位表」得把会员逐个走一遍。208 家里多数从不
碰这八个品种，逐日全走是白费请求——所以先用几个采样日把名单收窄。

**这一步只读、只写名单文件，不动任何已抓的原始数据。**
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

OUT = Path("/opt/futures-platform/sanhe-seats")
# 八个品种。与 backfill/parsers.py 的 VARIETY_BY_NAME 一致；那边是权威，改这里
# 之前先看那张表，名字对不上会静默地把整个品种漏掉。
WANT = {"焦煤", "鸡蛋", "生猪", "苹果", "玻璃", "纯碱", "黄金", "白银"}
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
# 采样日跨越三年，避开单一时点的偶然：某家可能只在某一年做玻璃。
SAMPLE_DATES = sys.argv[1:] or [
    "2024-03-01",
    "2024-11-01",
    "2025-06-03",
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


def main() -> int:
    brokers = json.loads((OUT / "brokers.json").read_text(encoding="utf-8"))
    print(f"会员 {len(brokers)} 家，采样日 {SAMPLE_DATES}", flush=True)

    hits: dict[str, set[str]] = {}
    issued = 0
    for day in SAMPLE_DATES:
        for broker in brokers:
            time.sleep(PACE)
            try:
                payload = post("broker_positions.php", {"broker": broker, "date": day})
            except Exception as error:  # noqa: BLE001
                print(f"FAIL {day} {broker} {type(error).__name__}", flush=True)
                continue
            issued += 1
            positions = (payload.get("data") or {}).get("positions") or {}
            mine = set(positions) & WANT
            if mine:
                hits.setdefault(broker, set()).update(mine)
        print(f"{day} 累计命中 {len(hits)} 家（已发 {issued} 次）", flush=True)

    ordered = sorted(hits)
    (OUT / "all_brokers_eight.json").write_text(
        json.dumps(ordered, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 按品种报一遍，好判断名单是不是漏了谁：某个品种只命中两三家，多半是采样日选得不好。
    by_variety: dict[str, int] = {}
    for varieties in hits.values():
        for variety in varieties:
            by_variety[variety] = by_variety.get(variety, 0) + 1
    print(f"\n请求 {issued} 次，命中 {len(ordered)} 家 / 共 {len(brokers)} 家")
    for variety in sorted(WANT):
        print(f"  {variety}: {by_variety.get(variety, 0)} 家")
    print(f"名单写入 {OUT / 'all_brokers_eight.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
