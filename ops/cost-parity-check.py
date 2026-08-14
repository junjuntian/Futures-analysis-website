#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对拍守卫:Rust 面板与 Python 引擎的「席位建仓成本」必须算出同一个数。

为什么需要它
------------
「机构成本」这一个业务概念在系统里有两份独立实现:
  - Rust  `domain/src/seat_cost.rs` 的 build_cost_series —— 面板「建仓过程」用
  - Python `engine/smart_money.py` 的 seat_cost        —— 信号引擎定买入区间用

2026-08-14 它们被发现长期不一致:引擎那版按**品种汇总**推成本、用主力合约结算价,
移仓时(平旧约+开新约)只看到净仓大幅波动,而算法是「加仓按结算价加权、减仓不改
均价」,加仓腿把成本拉高、减仓腿不修正,反复移仓后系统性偏离。东证 2026-07-15
因此被算成 960.40,而两个实际合约都在 900 附近。引擎已改为逐合约,两边现在一致。

但**没有任何东西保证它们以后不再分叉**——这已经是本项目第二次栽在「同一概念两处
实现」上(第一次是 v51 那份权重快照与生产引擎漂移)。这个脚本就是那道闸。

有意保留的两处差异(不是 bug,守卫必须避开)
------------------------------------------
1. **多空** —— Rust 净持仓计价、多空都记账、翻向重置;Python 只记多头。
   引擎只关心机构的**多头**建仓成本(用来定买入区间),净空席位不该参与买点。

2. **反推行(reboard_inferred)** —— Rust 展示时保留(建仓过程要如实),
   Python 的 clean_seat 一律剔除。那是引擎的既定口径:推断行只覆盖「掉榜前
   一日」的零星缺口,某会员某合约有、别的没有,混进汇总会凭空造出 ΔNet 跳变
   (实测 AG 2023-04-18 净仓差 -11,454 手);对趋势跟随,假跳变比少算更糟。
   一旦某个合约的序列里出现过推断行,两边此后的成本就会合理地分叉——
   Rust 在推断日看到仓位、Python 看不到,累积均价从此不同。

因此守卫只比对**双方都为净多、且该合约整段序列不含推断行**的交易日。
剔掉这两类之后仍有分歧,才是真的分叉。

用法
----
  cost-parity-check.py --base-url http://127.0.0.1:8088 --data-dir /path/to/csv
退出码 0 = 一致;1 = 有分歧(打印分歧明细)。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOL = 0.01          # 允许的绝对误差(元):两边都是浮点/定点混算,不做逐位相等
SAMPLE_DAYS = 30    # 每个 (席位, 合约) 抽查最近多少个交易日


def fetch(base_url: str, cookie: str, instrument: str,
          member: str, contract: str) -> list[dict]:
    q = urllib.parse.urlencode({"instrument": instrument, "member": member,
                                "contract": contract})
    url = f"{base_url}/api/v1/spread-analytics/seats/building?{q}"
    req = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.load(r)
    return (body.get("data") or body).get("days", [])


def python_cost_per_contract(sm, seat, price, member, contract):
    """直接调引擎的 seat_cost,只喂这一个合约的数据。

    **不在这里重写一遍算法**——守卫要防的正是「同一概念多处实现」,
    它自己再抄一份就成了第三份。2026-08-14 首版就是这么写的,
    结果引擎修好之后守卫仍报同样的分歧,因为比的是守卫自己那份旧逻辑。
    """
    one = seat[seat["contract"] == contract]
    if one.empty:
        return {}
    ser = sm.seat_cost(one, price, member)
    return {str(d.date()): float(v) for d, v in ser.items() if v == v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--engine", default="engine/smart_money.py")
    ap.add_argument("--cookie", default="", help="形如 futures_session=xxx")
    args = ap.parse_args()

    spec = importlib.util.spec_from_file_location("smart_money", args.engine)
    sm = importlib.util.module_from_spec(spec)
    sys.modules["smart_money"] = sm
    spec.loader.exec_module(sm)

    problems, checked = [], 0
    raw_by_instrument: dict = {}
    for instrument in ("AU", "AG"):
        price, seat_raw = sm.load_from_csv(Path(args.data_dir), instrument)
        raw_by_instrument[instrument] = seat_raw
        seat, price = sm.clean_seat(seat_raw), sm.clean_price(price)
        for member in sm.RULES["group"]:
            sub = seat[(~seat["is_variety_total"]) & (seat["member"] == member)
                       & seat["contract"].notna() & (seat["contract"] != "")]
            if sub.empty:
                continue
            contract = sub["contract"].value_counts().idxmax()
            # 反推行几乎每个合约都出现过,按合约整段排除会把样本排光(实测只剩 13 个)。
            # 改为**按日截断**:该 (席位, 合约) 上第一条反推行之后的日子一律不比——
            # 在那之前两边看到的是同一份数据,必须一致;之后合理分叉(文件头第 2 条)。
            raw_all = raw_by_instrument[instrument]
            inf = raw_all[(raw_all["source"].astype(str) == "reboard_inferred")
                          & (raw_all["member"] == member)
                          & (raw_all["contract"] == contract)]["trade_date"]
            cutoff = str(inf.min().date()) if len(inf) else None
            try:
                days = fetch(args.base_url, args.cookie, instrument, member, contract)
            except Exception as exc:                       # noqa: BLE001
                problems.append(f"{instrument}/{member}/{contract}: 取 Rust 数据失败 {exc}")
                continue
            py = python_cost_per_contract(sm, seat, price, member, contract)
            n_cmp = 0
            usable = [r for r in days if cutoff is None or r["trade_date"] < cutoff]
            for row in usable[-SAMPLE_DAYS:]:
                d = row["trade_date"]
                rc, np_ = row.get("cost"), row.get("net_position")
                if cutoff is not None and d >= cutoff:
                    continue                                # 反推行之后:有意的差异
                if rc is None or np_ is None or float(np_) <= 0:
                    continue                                # 净空/掉榜:有意的差异,跳过
                if d not in py:
                    problems.append(f"{instrument}/{member}/{contract} {d}: "
                                    f"Rust 有成本 {rc},Python 无")
                    continue
                diff = abs(float(rc) - py[d])
                n_cmp += 1
                if diff > TOL:
                    problems.append(f"{instrument}/{member}/{contract} {d}: "
                                    f"Rust {float(rc):.4f} vs Python {py[d]:.4f} 差 {diff:.4f}")
            checked += n_cmp
            print(f"  {instrument} {member} {contract}: 比对 {n_cmp} 个交易日")

    print(f"\n共比对 {checked} 个 (席位, 合约, 交易日) 组合,容差 {TOL} 元")
    if problems:
        print(f"\n✗ 发现 {len(problems)} 处分歧:")
        for p in problems[:20]:
            print(f"    {p}")
        if len(problems) > 20:
            print(f"    ...(另有 {len(problems) - 20} 处)")
        print("\n两处实现又分叉了。改动前先读 research/PITFALLS.md 第 9 条。")
        return 1
    if checked < 50:
        print("\n✗ 比对样本太少(<50),守卫形同虚设——检查数据与端点是否正常")
        return 1
    print("\n✓ Rust 面板与 Python 引擎的建仓成本一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
