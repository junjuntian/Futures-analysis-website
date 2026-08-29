"""采三禾的席位历史。

**为什么不能只用交易所官方龙虎榜**：官方只公布前二十名。掉出前二十和真的清仓是
两回事，而趋势跟随策略必须分得清这两者。2026-06-15 高盛的黄金归零、06-16 只剩
4 手，官方文件里都没有这一行——页面上看起来像「这家不做黄金」，实际是它刚清完仓。
三禾覆盖全部会员，这是它不可替代的地方。

大商所那三个品种另有一层理由：交易所自己不给（脚本 412、真实浏览器 500），东财
只到 2025-11 且到 2026 年中都残缺，三禾是唯一覆盖到 2023-08 的来源。

三禾按**会员**组织而不是按合约，所以要拼出「某合约当日的席位表」，得把会员逐个走
一遍。名单由 `sanhe_survey_all.py` 筛出（208 家里真正碰这八个品种的那些），其余
的每天都取只是白费请求。

**从最近的日期往回采**：中途停下也是手里握着最有用的那一段，而不是握着三年前的
一段、最近的反而没有。

**落盘时带 scope 标记**：2023-08 那轮只存了大商所三个品种，文件却和全品种的长得
一样。没有标记的话，「文件已存在就跳过」会把那些只有大商所的文件当成采全了，
于是另外五个品种永远补不上——而且没有任何报错。
"""

import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import requests

ROOT = Path("/opt/futures-platform/sanhe-seats")
# 铁矿石单采必须换目录(SANHE_RAW_DIR):采集按 (日期, 会员) 存文件、内容只含
# WANT 品种——落进八品种的 raw/ 会把同名文件**覆盖成只剩铁矿石**,而 scope
# 检查恰恰会放行这次覆盖(它只认本 scope)。
RAW = Path(os.environ.get("SANHE_RAW_DIR", str(ROOT / "raw")))
# 八个品种，**用三禾的叫法**：它管黄金叫「沪金」、白银叫「沪银」，而交易所与东财
# 写「黄金」「白银」。写错了不会报错，只会让金银一行都采不到。
# 解析侧由 parsers.VARIETY_BY_NAME 把这两个名字映回 AU/AG。
# 默认八品种(三禾叫法);SANHE_WANT 覆盖(DEC-158 铁矿石=「铁矿石」,三禾同名)。
WANT = set(
    v.strip() for v in os.environ.get(
        "SANHE_WANT", "焦煤,鸡蛋,生猪,苹果,玻璃,纯碱,沪金,沪银"
    ).split(",") if v.strip()
)
# 写进每个文件，标明这一份是按哪个品种集合采的。见模块 docstring。
# scope 随品种集合走:铁矿石单采标 iron-ore-v1,与八品种的文件互不冒充
# (见模块 docstring:没有标记,「文件已存在就跳过」会把部分品种的当成采全了)。
SCOPE = os.environ.get("SANHE_SCOPE", "eight-varieties-v1")
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


def already_full(target: Path) -> bool:
    """这个文件是不是已经按当前品种集合采过了。

    只看 scope 标记，不看文件存不存在：2023-08 那轮写下的文件没有这个字段，
    内容只有大商所三个品种，当成「采过了」会让另外五个品种永远补不上。
    """
    if not target.exists():
        return False
    try:
        return json.loads(target.read_text(encoding="utf-8")).get("scope") == SCOPE
    except (json.JSONDecodeError, OSError):
        # 读不动就当没采过：重采一次的代价，远小于把一个坏文件当成好数据。
        return False


def main() -> int:
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2023, 8, 10)
    end = (
        date.fromisoformat(sys.argv[2])
        if len(sys.argv) > 2
        else datetime.now(UTC).date()
    )  # 三禾窗口以 last_date 为准，下面还会再收一次口

    # 名单文件可以用参数换掉：普查完成前先跑大商所那份也能出活。
    roster = Path(os.environ.get("SANHE_ROSTER", ROOT / "all_brokers_eight.json"))
    brokers = json.loads(roster.read_text(encoding="utf-8"))
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

    # 开跑前先确认每个名字在三禾那边真的有数。
    #
    # 三禾的会员名与我们库里的写法不一定一样，而且它对不认识的名字**返回空而不是
    # 报错**：`上海中财` 与 `国投期货` 实测都回 0 个品种，活的名字是 `中财期货`
    # 与 `国投安信`。名单里混进一个死名字，这个会员就整段采不到，而日志里只有一行
    # 「命中 0」——跑完几小时才发现，那时窗口已经滚过去了。
    dead = []
    for broker in brokers:
        time.sleep(PACE)
        try:
            payload = post("broker_positions.php", {"broker": broker, "date": last.isoformat()})
        except Exception as error:  # noqa: BLE001
            print(f"ROSTER_PROBE_FAIL {broker} {type(error).__name__}", flush=True)
            dead.append(broker)
            continue
        if not ((payload.get("data") or {}).get("positions") or {}):
            dead.append(broker)
    if dead:
        print(f"这些名字在三禾查不到任何持仓，先确认写法再跑：{dead}", flush=True)
        return 1
    print(f"名单自检通过：{len(brokers)} 家在 {last} 都有数", flush=True)

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
            if already_full(target):
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
                            "scope": SCOPE,
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
