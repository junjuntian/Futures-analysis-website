"""逐合约战役策略(campaign):左侧批次进场 + 聪明钱份额资格 + 卸仓30%快出。

2026-08-24 生猪首发(运营者拍板)。研究蓝本与验证:
  research/run_smart_filter2.py(版本A,份额过滤)+ REPORT_DIP_COST_v1 第五轮。
  生猪 51 笔 简单加总 +118.8pp / 逐笔复利 +200.5% / 逐年全正 / 最差单笔 −4.1%,
  超过组内最赚钱席位(东吴 +95.9%/复利 +122.7%)。
逐笔对拍:research/run_campaign_parity.py(引擎 vs 研究实现,进出场日期/合约/方向
  必须逐笔相同;收益容差因记账口径不同——研究用简单收益,本模块按仓里纪律用
  逐日连乘,做空时两者不相等,见 replay 的 compound 注释)。

规则速记(全部参数在 VARIETIES[code]["campaign"] 里,按品种规模缩放,不共享):
  · 阵营 = 席位组里当日在该合约上净多/净空的成员;一律可见口径 net_off
    (官方行,DEC-108 的前视教训),掉榜沿用前值。
  · 逢跌加仓日(多):阵营净加 >= add_min 且(结算<=阵营滚动VWAP 或 近5日跌);
    空头镜像。区间隔 <=gap 交易日并段;累计净加 >= confirm 激活;
    批次成本 = 区间内逐日加仓 x 当日结算 加权(增量,PIT)。
  · 聪明钱份额资格:该方向(多/空为一个"人格")在本品种全部历史合约上的累计
    盈亏(prev_net x Δ结算 x 乘数,截至信号日前一日)>= max(0, 对侧 x share)。
    生猪多头人格 +1.4 亿对空头 +35.7 亿,被它挡掉 —— 该侧是套保/接盘,不是聪明钱。
  · 进场:区间激活起至区间尾后 tail 日内,结算 <= 批次成本(空镜像)且资格通过
    -> 次日开盘;每区间首枪一笔;资格不过也烧掉该区间(区间过时效不回头)。
  · **跟批加仓(DEC-135,2026-08-24 运营者拍板)**:同一区间内机构每多攒够一个
    confirm 台阶算新一批;新批出现时若价格仍不劣于批次成本、且**优于我们当前
    持仓均价**(做空=更高,只摊好不摊坏),跟加 1 单位;每战役最多 max_units 个。
    缘起 LH2611 实盘:首枪 8/10 进 12,155,机构 8/14/8/17 又两批加到 12,3~12,4k,
    一枪版被钉在他们位置最差的第一批上。回测(引擎口径,逐单位连乘取均):
    50 战役 13 场多单位,加总 +131.7 -> +140.4pp,复利 +231.4% -> +261.5%,
    t 2.66 -> 2.86,中位 +0.87 -> +1.38%,最差单笔 −4.1% 不变。
    对拍:引擎与研究实现 51 场逐批成交日/出场原因完全一致(研究首版多出的
    2 场是其陈旧变量 bug,已勘误——先前口头报过的 15 场/+129.1pp 作废)。
  · 出场:阵营|净| < 自进场峰值 x (1-unload) -> 次日开盘;或交割纪律
    (窗口止点前 exit_before_delivery 交易日)。无价格止损(运营者框架)。
  · 多仓并行是常态(生猪有仓日 65% 同时 >=2 笔,最多 5 笔——整条曲线的空头)。
    逐日净值按「每仓 1 单位等权、当日在场仓位取均值」出资金曲线口径。
  · **散户接盘确认(DEC-138,只展示当仓位分级,不当开关)**:进场信号日散户三家
    在该合约上 5 日向**对面**加仓 = 确认。LH 实测逐年 4/4 同向:有确认 25 笔
    +4.96%/胜 68%,无确认 25 笔 +0.65%/胜 52%(t=2.29)。当硬门测过不划算
    (总账 127.9->106.9pp、t 降),它的位置是运营者模型第 3 点的「逐步加仓」:
    确认 -> 正常仓/可跟批;未确认 -> 轻仓,仓位由运营者定。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _exec_px(op_col: pd.Series, st_col: pd.Series, i: int) -> float:
    """第 i 个交易日的成交价:开盘价,缺失(郑商所 0 已在上游转 NaN)用结算兜底。"""
    v = op_col.iloc[i]
    if np.isfinite(v):
        return float(v)
    return float(st_col.iloc[i])


def qual_pnl_series(seat: pd.DataFrame, st: pd.DataFrame, members: list[str],
                    multiplier: float) -> dict[int, pd.Series]:
    """两个方向"人格"的逐日累计盈亏(全部合约)。

    用事后完整 net:资格看的是**昨天及更早**的历史,反推行滞后 1 天在此无碍。
    """
    px = st.stack().rename("settle").reset_index()
    px.columns = ["trade_date", "contract", "settle"]
    d = seat[seat["member_key"].isin(members)].merge(px, on=["contract", "trade_date"], how="inner")
    d = d.sort_values(["member_key", "contract", "trade_date"])
    g = d.groupby(["member_key", "contract"])
    d["prev_net"] = g["net"].shift()
    d["prev_settle"] = g["settle"].shift()
    gap = (d["trade_date"] - g["trade_date"].shift()).dt.days
    d = d[d["prev_net"].notna() & (gap <= 5)].copy()
    d["pnl"] = (d["settle"] - d["prev_settle"]) * d["prev_net"] * multiplier
    out = {}
    for side in (+1, -1):
        rows = d[np.sign(d["prev_net"]) == side]
        s = rows.groupby("trade_date")["pnl"].sum().sort_index().cumsum()
        out[side] = s
    return out


def _qualified(qual: dict[int, pd.Series], side: int, t: pd.Timestamp, share: float) -> bool:
    a, b = qual[side], qual[-side]
    aa = a[a.index < t]
    bb = b[b.index < t]
    av = float(aa.iloc[-1]) if len(aa) else 0.0
    bv = float(bb.iloc[-1]) if len(bb) else 0.0
    return av >= max(0.0, share * bv)


def camp_frame(seat: pd.DataFrame, contract: str, members: list[str],
               px: pd.Series) -> pd.DataFrame:
    """该合约上席位组的逐家可见净持仓宽表(net_off,按家 ffill)。"""
    sub = seat[(seat["member_key"].isin(members)) & (seat["contract"] == contract)]
    if sub.empty:
        return pd.DataFrame(index=px.index)
    return (sub.pivot_table(index="trade_date", columns="member_key",
                            values="net_off", aggfunc="first")
               .reindex(px.index).ffill())


def camp_series(w: pd.DataFrame, px: pd.Series, side: int) -> tuple[pd.Series, pd.Series]:
    """阵营合计|净|与滚动 VWAP(加仓按当日结算加权、减仓不动 —— inst_cost_series 同会计)。"""
    if w.empty:
        z = pd.Series(np.nan, index=px.index)
        return z.fillna(0.0), z
    net = w.where(np.sign(w) == side).abs().sum(axis=1)
    vwap = pd.Series(np.nan, index=px.index)
    q, cst = 0.0, np.nan
    for t in px.index:
        n, p = float(net.get(t, np.nan)), float(px[t])
        if np.isfinite(n):
            dn = n - q
            if dn > 0:
                cst = p if not np.isfinite(cst) or q <= 0 else (cst * q + dn * p) / (q + dn)
            q = n
        vwap[t] = cst
    return net, vwap


def zone_scan(px: pd.Series, net: pd.Series, vwap: pd.Series, side: int,
              cfg: dict) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """逢跌加仓区间状态机,逐日返回 (累计加仓, 批次成本, 距区间尾天数, 区间起点下标)。

    纯前向扫描,全部 PIT。区间尾距离 > gap+tail 即过期归零。
    区间起点下标是"消费标识":一个区间只许进一笔,烧毁按起点记
    ——按最近加仓日记会在区间补一天加仓后被重复消费(与研究蓝本不一致)。
    """
    chg = net.diff()
    ret5 = px.pct_change(5)
    losing = (px <= vwap) if side > 0 else (px >= vwap)
    trending = (ret5 < 0) if side > 0 else (ret5 > 0)
    dip = (chg >= cfg["add_min"]) & (losing | trending)
    z_add = pd.Series(0.0, index=px.index)
    z_cost = pd.Series(np.nan, index=px.index)
    z_age = pd.Series(999, index=px.index)   # 距最近一个逢跌加仓日的天数
    z_start = pd.Series(-1, index=px.index)  # 当前区间首个逢跌日的下标
    add = cost = 0.0
    last_i = None
    start_i = -1
    for i in range(len(px.index)):
        if last_i is not None and i - last_i > cfg["gap"] + cfg["tail"]:
            add, cost, last_i, start_i = 0.0, np.nan, None, -1
        if bool(dip.iloc[i]):
            a = float(chg.iloc[i])
            if last_i is not None and last_i >= i - (cfg["gap"] + 1):
                cost = (cost * add + a * float(px.iloc[i])) / (add + a)
                add += a
            else:
                add, cost, start_i = a, float(px.iloc[i]), i
            last_i = i
        z_add.iloc[i] = add if last_i is not None else 0.0
        z_cost.iloc[i] = cost if last_i is not None else np.nan
        z_age.iloc[i] = (i - last_i) if last_i is not None else 999
        z_start.iloc[i] = start_i if last_i is not None else -1
    return z_add, z_cost, z_age, z_start


def run(seat: pd.DataFrame, mkt: pd.DataFrame, op: pd.DataFrame, st: pd.DataFrame,
        members: list[str], rules: dict) -> dict:
    """全量回放 + 当日状态。返回 trades / daily / pos_count / watch / qual 摘要。"""
    cfg = rules["campaign"]
    limit = rules["exit_before_delivery"]
    cost_bp = rules.get("turn_cost", 0.0005)
    from hog_money import days_to_window_end   # 延迟导入避免环形
    contracts = [c for c in dict.fromkeys(mkt["main"]) if isinstance(c, str)]
    qual = qual_pnl_series(seat, st, members, rules["multiplier"])

    trades: list[dict] = []
    watch: list[dict] = []
    for c in contracts:
        if c not in st.columns:
            continue
        px = st[c].dropna()
        if len(px) < cfg.get("min_days", 40):   # 太短的合约撑不起区间/VWAP(测试夹具可调小)
            continue
        opc = (op[c] if c in op.columns else pd.Series(np.nan, index=px.index)).reindex(px.index)
        w = camp_frame(seat, c, members, px)
        # 散户三家在该合约上的可见净持仓(DEC-138 接盘确认用;名单来自 RULES)
        retail_names = [m for m in rules.get("retail_seed", []) if m in set(seat["member_key"])]
        rsub = seat[(seat["member_key"].isin(retail_names)) & (seat["contract"] == c)]
        if len(rsub):
            rw = (rsub.pivot_table(index="trade_date", columns="member_key",
                                   values="net_off", aggfunc="first").reindex(px.index).ffill())
            rnet = rw.sum(axis=1)
        else:
            rnet = pd.Series(np.nan, index=px.index)
        dleft = pd.Series([days_to_window_end(c, t) for t in px.index], index=px.index)
        for side in (+1, -1):
            def retail_state(t):
                """(散户净持仓, 5日变化, 是否向对面加=确认)。无数据全 None/False。"""
                if not rnet.notna().any():
                    return None, None, False
                rn = rnet.asof(t)
                r5 = rn - rnet.asof(t - pd.Timedelta(days=7))
                if not np.isfinite(rn):
                    return None, None, False
                conf = bool(np.isfinite(r5) and np.sign(r5) == -side and abs(r5) > 0)
                return float(rn), (float(r5) if np.isfinite(r5) else None), conf

            net, vwap = camp_series(w, px, side)
            z_add, z_cost, z_age, z_start = zone_scan(px, net, vwap, side, cfg)
            idx = list(px.index)
            pos = None
            zone_fired_at = -1   # 已消费的区间(按区间**起点**下标标识)
            max_units = int(cfg.get("max_units", 1))
            for i, t in enumerate(idx):
                # —— 出场 ——
                if pos is not None:
                    nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
                    if np.isfinite(nn):
                        pos["peak"] = max(pos["peak"], nn)
                    reason = None
                    if dleft.iloc[i] <= limit:
                        reason = "临近交割"
                    elif np.isfinite(nn) and pos["peak"] > 0 and nn < pos["peak"] * (1 - cfg["unload"]):
                        reason = f"机构卸仓{cfg['unload']:.0%}"
                    if reason is not None and i + 1 < len(idx):
                        fill = i + 1
                        p_out = _exec_px(opc, px, fill)
                        rets = [_compound(opc, px, side, u["fill_i"], fill)
                                for u in pos["units"]]
                        trades.append({
                            "side": "short" if side < 0 else "long",
                            "entry_date": idx[pos["units"][0]["sig_i"]].strftime("%Y-%m-%d"),
                            "exit_date": t.strftime("%Y-%m-%d"),
                            "entry_px": round(float(np.mean([u["px"] for u in pos["units"]])), 2),
                            "exit_px": round(p_out, 2),
                            "contract": c,
                            "ret_pct": round(float(np.mean(rets)) * 100, 2),
                            "hold_days": i - pos["units"][0]["sig_i"],
                            "exit_reason": reason,
                            "batch_cost": round(pos["batch_cost"]),
                            "units": len(pos["units"]),
                            "entries": [{"date": idx[u["fill_i"]].strftime("%Y-%m-%d"),
                                         "px": round(u["px"], 2)} for u in pos["units"]],
                            "retail_confirm": bool(pos.get("retail_confirm", False)),
                            "_units": [dict(u) for u in pos["units"]],
                            "_out_i": fill, "_c": c, "_side": side,
                        })
                        pos = None
                # —— 进场机会(区间消费独立于持仓与资格,蓝本同规则)——
                if (z_age.iloc[i] <= cfg["gap"] + cfg["tail"]
                        and z_add.iloc[i] >= cfg["confirm"]
                        and dleft.iloc[i] > limit + 5 and i + 1 < len(idx)):
                    zone_id = int(z_start.iloc[i])
                    pcx = float(px.iloc[i])
                    ok_px = (pcx <= float(z_cost.iloc[i])) if side > 0 else (pcx >= float(z_cost.iloc[i]))
                    if zone_id > zone_fired_at:
                        if ok_px:
                            # 价格条件满足即消费该区间;已持仓/资格不过也不回头。
                            zone_fired_at = zone_id
                            if pos is None and _qualified(qual, side, t, cfg["share"]):
                                fill = i + 1
                                pos = {"units": [{"sig_i": i, "fill_i": fill,
                                                  "px": _exec_px(opc, px, fill)}],
                                       "batch_cost": float(z_cost.iloc[i]),
                                       "zone": zone_id,
                                       "steps": 1,
                                       "retail_confirm": retail_state(t)[2],
                                       "peak": float(net.iloc[i]) if np.isfinite(net.iloc[i]) else 0.0}
                    elif (pos is not None and pos.get("zone") == zone_id
                          and len(pos["units"]) < max_units and ok_px):
                        # 跟批(DEC-135):同区间每多攒够一个 confirm 台阶算新一批;
                        # 只在价格优于当前均价时加 —— 摊好成本,不摊坏。
                        k = int(z_add.iloc[i] // cfg["confirm"])
                        if k > pos["steps"]:
                            avg = float(np.mean([u["px"] for u in pos["units"]]))
                            better = (pcx <= avg) if side > 0 else (pcx >= avg)
                            if better:
                                fill = i + 1
                                pos["units"].append({"sig_i": i, "fill_i": fill,
                                                     "px": _exec_px(opc, px, fill)})
                            pos["steps"] = k
            # —— 未平仓与当日状态 ——
            i = len(idx) - 1
            t = idx[i]
            if pos is not None:
                nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else np.nan
                rets = [_compound(opc, px, side, u["fill_i"], i, mark_settle=True)
                        for u in pos["units"]]
                unload = (1 - nn / pos["peak"]) if np.isfinite(nn) and pos["peak"] > 0 else None
                trades.append({
                    "side": "short" if side < 0 else "long",
                    "entry_date": idx[pos["units"][0]["sig_i"]].strftime("%Y-%m-%d"), "exit_date": None,
                    "entry_px": round(float(np.mean([u["px"] for u in pos["units"]])), 2),
                    "exit_px": None, "contract": c,
                    "ret_pct": round(float(np.mean(rets)) * 100, 2),
                    "hold_days": i - pos["units"][0]["sig_i"], "exit_reason": None,
                    "batch_cost": round(pos["batch_cost"]),
                    "units": len(pos["units"]),
                    "entries": [{"date": idx[u["fill_i"]].strftime("%Y-%m-%d"),
                                 "px": round(u["px"], 2)} for u in pos["units"]],
                    "retail_confirm": bool(pos.get("retail_confirm", False)),
                    "retail_now": (lambda st_: {"net": None if st_[0] is None else int(round(st_[0])),
                                                "chg5": None if st_[1] is None else int(round(st_[1])),
                                                "opposite_adding": st_[2]})(retail_state(t)),
                    "camp_net": int(nn) if np.isfinite(nn) else None,
                    "camp_peak": int(pos["peak"]),
                    "unload_pct": round(float(unload), 4) if unload is not None else None,
                    "_units": [dict(u) for u in pos["units"]],
                    "_out_i": None, "_c": c, "_side": side,
                })
            # 观察列表:只报还活着的流(未到交割窗口)
            if dleft.iloc[i] > limit:
                blocked = None
                if pos is not None:
                    blocked = "已持仓"
                elif z_age.iloc[i] > cfg["gap"] + cfg["tail"] or z_add.iloc[i] <= 0:
                    blocked = "无进行中的建仓区间"
                elif z_add.iloc[i] < cfg["confirm"]:
                    blocked = f"区间累计加仓 {z_add.iloc[i]:,.0f} 手,未到 {cfg['confirm']:,.0f} 手"
                elif not _qualified(qual, side, t, cfg["share"]):
                    blocked = "该方向历史战役盈亏未达对侧 25%,非聪明钱侧"
                else:
                    pcx = float(px.iloc[i])
                    zc = float(z_cost.iloc[i])
                    if (side > 0 and pcx > zc) or (side < 0 and pcx < zc):
                        blocked = f"价 {pcx:,.0f} {'高于' if side > 0 else '低于'}批次成本 {zc:,.0f},等回到成本再进"
                nn = float(net.iloc[i]) if np.isfinite(net.iloc[i]) else None
                r_now = retail_state(t)
                watch.append({
                    "contract": c, "side": "long" if side > 0 else "short",
                    "retail_net": None if r_now[0] is None else int(round(r_now[0])),
                    "retail_chg5": None if r_now[1] is None else int(round(r_now[1])),
                    "retail_confirm": r_now[2],
                    "camp_net": int(nn) if nn is not None else None,
                    "camp_vwap": round(float(vwap.iloc[i])) if np.isfinite(vwap.iloc[i]) else None,
                    "zone_add": int(z_add.iloc[i]),
                    "batch_cost": (round(float(z_cost.iloc[i]))
                                   if np.isfinite(z_cost.iloc[i]) and z_add.iloc[i] > 0 else None),
                    "zone_age": int(z_age.iloc[i]) if z_age.iloc[i] < 999 else None,
                    "qualified": _qualified(qual, side, t, cfg["share"]),
                    "entry_ready": blocked is None,
                    "blocked": blocked,
                    "settle": round(float(px.iloc[i]), 2),
                    "days_left": int(dleft.iloc[i]),
                })

    # —— 逐日净值(等权多仓)与逐笔一致性 ——
    all_idx = mkt.index
    mat = pd.DataFrame(index=all_idx)
    col_of = []   # 每列属于哪笔战役(跟批后一笔战役可占多列)
    for k, tr in enumerate(trades):
        c, side = tr["_c"], tr["_side"]
        px = st[c].dropna()
        opc = (op[c] if c in op.columns else pd.Series(np.nan, index=px.index)).reindex(px.index)
        i1 = tr["_out_i"] if tr["_out_i"] is not None else len(px.index) - 1
        for un, u in enumerate(tr["_units"]):
            i0 = u["fill_i"]
            days = [j for j in range(i0, i1 + 1)
                    if np.isfinite(opc.iloc[j]) or j in (i0, i1)]
            col = pd.Series(np.nan, index=all_idx)
            prev = None
            for j in days:
                p = _exec_px(opc, px, j)
                if prev is not None and prev[1] > 0:
                    col[px.index[j]] = side * (p / prev[1] - 1)
                prev = (j, p)
            # 成交那两天各扣一次单边成本(replay 同口径)
            if len(days) >= 1:
                col[px.index[days[0]]] = (0.0 if not np.isfinite(col[px.index[days[0]]]) else col[px.index[days[0]]]) - cost_bp
            if tr["_out_i"] is not None and len(days) >= 2:
                col[px.index[days[-1]]] = (col[px.index[days[-1]]] if np.isfinite(col[px.index[days[-1]]]) else 0.0) - cost_bp
            mat[len(col_of)] = col
            col_of.append(k)
    daily = mat.mean(axis=1, skipna=True).fillna(0.0) if len(mat.columns) else pd.Series(0.0, index=all_idx)
    pos_count = mat.notna().sum(axis=1) if len(mat.columns) else pd.Series(0, index=all_idx)

    # 一致性:每笔战役各单位逐日连乘的均值(未扣成本)必须等于记账 ret_pct
    for k, tr in enumerate(trades):
        if tr["_out_i"] is None:
            continue
        unit_rets = []
        for ci, owner in enumerate(col_of):
            if owner != k:
                continue
            col = mat[ci].dropna().copy()
            col.iloc[0] += cost_bp
            col.iloc[-1] += cost_bp
            unit_rets.append(float(np.prod(1 + col) - 1) * 100)
        by_day = float(np.mean(unit_rets))
        if abs(by_day - tr["ret_pct"]) > 0.5:
            raise AssertionError(
                f"campaign 逐日与逐笔对不上:{tr['contract']} {tr['entry_date']} "
                f"逐笔 {tr['ret_pct']:+.2f} / 逐日 {by_day:+.2f}")
    for tr in trades:
        for k in ("_units", "_out_i", "_c", "_side"):
            tr.pop(k, None)

    q_last = {s: (float(qual[s].iloc[-1]) if len(qual[s]) else 0.0) for s in (+1, -1)}
    d_last = mkt.index[-1]
    return {
        "trades": trades,
        "daily": daily,
        "pos_count": pos_count,
        "watch": sorted(watch, key=lambda x: (x["contract"], x["side"])),
        "qual": {
            "long_pnl_yi": round(q_last[+1] / 1e8, 2),
            "short_pnl_yi": round(q_last[-1] / 1e8, 2),
            "long_ok": _qualified(qual, +1, d_last, cfg["share"]),
            "short_ok": _qualified(qual, -1, d_last, cfg["share"]),
            "share": cfg["share"],
        },
    }


def _compound(opc: pd.Series, px: pd.Series, side: int, i0: int, i1: int,
              mark_settle: bool = False) -> float:
    """开盘链逐日连乘(replay 的 compound 同纪律:做空必须连乘,不能用简单收益)。

    mark_settle:未平仓估值 —— 连乘到最后一个有价日的开盘,再补开→最新结算。
    """
    days = [j for j in range(i0, i1 + 1) if np.isfinite(opc.iloc[j]) or j in (i0, i1)]
    v = 1.0
    prev = None
    for j in days:
        p = _exec_px(opc, px, j)
        if prev is not None and prev > 0:
            v *= 1 + side * (p / prev - 1)
        prev = p
    if mark_settle and prev is not None and prev > 0:
        v *= 1 + side * (float(px.iloc[i1]) / prev - 1)
    return v - 1.0
