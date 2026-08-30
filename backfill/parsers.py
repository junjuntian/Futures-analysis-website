"""Turn each exchange's raw file into the two agreed shapes.

Nothing here guesses. Where a source does not publish something -- sanhe has no
rank and no volume board -- the field is left empty rather than filled with a
plausible number.
"""

import json
import re
from datetime import date
from pathlib import Path

WANT = {"AP", "FG", "SA", "AU", "AG", "I", "IH", "SC", "JM", "JD", "LH"}
# 这里曾有一张 LISTED_YEAR（品种上市年）表，用来给三位郑商所代码补世纪。
# 那条规则是错的，见 normalise_contract 的说明——锚点必须是交易日。表已删除：
# 留着一份写着错误规则的现成数据，下一个人很容易「顺手」再用上它。
# 品种中文名 → 代码。**同一个品种在不同来源叫法不同**：交易所与东财写「黄金」
# 「白银」，三禾写「沪金」「沪银」。少一个别名不会报错，只会让那个品种一行都采不到——
# 2026-08-12 差点就这么把金银整段漏掉，因为大商所那三个品种两边叫法碰巧一致。
# 品种 → 交易所。三禾按会员组织，响应里**只有品种名没有交易所**，得由我们补。
# 原来 sanhe_seats 把 exchange 写死成 "DCE"——那时只采大商所三个品种碰巧对；
# 扩到八品种后，苹果玻璃纯碱(郑商所)与沪金沪银(上期所)都会被标成 DCE。
# 交易所是 seat_history 身份键的一部分：官方的 SHFE/AU 与三禾的 DCE/AU 不会互相
# 去重，页面上同一家会显示两遍，而两行的数字还都对——最难看出来的那种错。
EXCHANGE_BY_VARIETY = {
    "JM": "DCE",
    "JD": "DCE",
    "LH": "DCE",
    "AP": "CZCE",
    "FG": "CZCE",
    "SA": "CZCE",
    "AU": "SHFE",
    "AG": "SHFE",
    # DEC-158 三个新品种。
    "I": "DCE",
    "IH": "CFFEX",
    "SC": "INE",
}
VARIETY_BY_NAME = {
    "沪金": "AU",
    "沪银": "AG",
    "苹果": "AP",
    "玻璃": "FG",
    "纯碱": "SA",
    "黄金": "AU",
    "白银": "AG",
    "焦煤": "JM",
    "鸡蛋": "JD",
    "生猪": "LH",
    # DEC-158:铁矿石(三禾同名);上证50/原油为将来备着,三禾有没有另说。
    "铁矿石": "I",
    "上证50": "IH",
    "50上证": "IH",   # 三禾的真实叫法(2026-08-30 实探;300沪深/500中证 同构)
    "原油": "SC",
}

# 自证：每个要处理的品种都得有交易所。少配一个不会报错，只会把那个品种的行
# 标到错误的交易所去，而交易所是 seat_history 身份键的一部分——同一家会员会
# 在页面上显示两遍，两行的数字还都对。宁可导入时就炸。
assert WANT == set(EXCHANGE_BY_VARIETY), (
    f"品种与交易所对不上：缺 {WANT - set(EXCHANGE_BY_VARIETY)}，"
    f"多 {set(EXCHANGE_BY_VARIETY) - WANT}"
)
# 每个中文名都得映到我们认识的代码上，否则那个品种会被静默跳过。
assert set(VARIETY_BY_NAME.values()) <= WANT, (
    f"中文名映到了不认识的代码：{set(VARIETY_BY_NAME.values()) - WANT}"
)


def num(text):
    """A number as the exchanges write it, or empty."""
    if text is None:
        return ""
    s = str(text).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "--"):
        return ""
    try:
        float(s)
    except ValueError:
        return ""
    return s


def normalise_contract(raw, trade_date=None):
    """`AP501` and `ap2501` and `AU2412` all become the same four-digit form.

    三位代码只带一个年份数字，十年一轮回，所以必须有个锚点才能补出世纪。

    **锚点是这一行的交易日，不是品种的上市年。**原来锚在上市年上：
    `year = listed - listed % 10 + digit`，苹果上市 2017，于是数字 7/8/9 停在
    2017/2018/2019，只有 0–6 才被推到 2020 年代。结果 2026 年文件里的 `AP701`
    （真实含义 2027-01）被展成了 `AP1701`——一个 2017 年就交割完的合约，出现在
    2026 年的席位表里。生产上这样的行有 53 万条，占郑商所席位的 27%。

    正确的锚点是交易日：取数字匹配、且**交割年月不早于交易年月**的最近一个年份。
    合约不会在交割月之后还挂牌交易，所以这条规则没有歧义。对本来就正确的四位代码
    和历史上正确的三位代码，这个函数都是幂等的。

    没有交易日就拒绝解析而不是猜——猜出来的合约代码看不出对错，比解析失败糟得多。
    """
    m = re.fullmatch(r"([A-Za-z]{1,2})(\d{3,4})", str(raw).strip())
    if not m:
        return None
    variety, month = m.group(1).upper(), m.group(2)
    if len(month) == 4:
        return f"{variety}{month}"
    if trade_date is None:
        return None
    traded = date.fromisoformat(str(trade_date)) if not hasattr(trade_date, "year") else trade_date
    year_digit, mm = int(month[0]), int(month[1:])
    year = traded.year - (traded.year % 10) + year_digit
    while (year, mm) < (traded.year, traded.month):
        year += 10
    return f"{variety}{year % 100:02d}{mm:02d}"


def _read_text(path):
    """The exchange's bytes as text, whichever encoding it used that year.

    CZCE published GBK before it published UTF-8, and the year it changed is not
    written anywhere. Decoding blind gives mojibake in the section titles, which
    is where the variety name lives -- so every 玻璃 section would silently
    vanish rather than fail.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_row(line):
    """CZCE rows are pipe separated now and were comma separated before 2015."""
    return [p.strip() for p in (line.split("|") if "|" in line else line.split(","))]


# ---------------------------------------------------------------- CZCE 行情
def czce_market(path):
    text = _read_text(path)
    day = re.search(r"\((\d{4}-\d{2}-\d{2})\)", text)
    if not day:
        return []
    trade_date = day.group(1)
    rows = []
    for line in text.splitlines():
        parts = _split_row(line)
        # Both eras put the same fourteen columns in the same order; the old one
        # simply has no header row to skip.
        if len(parts) < 14 or parts[0] in ("合约代码", ""):
            continue
        contract = normalise_contract(parts[0], trade_date)
        if not contract or contract[:2] not in WANT:
            continue
        turnover = num(parts[12])
        rows.append(
            {
                "exchange": "CZCE",
                "instrument": contract[:2],
                "contract": contract,
                "trade_date": trade_date,
                "prev_settlement": num(parts[1]),
                "open": num(parts[2]),
                "high": num(parts[3]),
                "low": num(parts[4]),
                "close": num(parts[5]),
                "settlement": num(parts[6]),
                "volume": num(parts[9]),
                # published in 万元
                "turnover": str(float(turnover) * 10000) if turnover else "",
                "open_interest": num(parts[10]),
                "open_interest_change": num(parts[11]),
                "volume_basis": "single" if trade_date >= "2020-01-01" else "double",
                "source": "czce_official",
            }
        )
    return rows


# ---------------------------------------------------------------- CZCE 席位
# 品种：棉花<TAB>日期：2012-12-03   and   品种：苹果AP            日期：2024-12-02
CZCE_SEG = re.compile(r"^(品种|合约)[：:]\s*(\S+?)\s+日期[：:]\s*(\d{4}-\d{2}-\d{2})")


def czce_seats(path):
    rows = []
    contract = instrument = trade_date = None
    is_total = False
    for line in _read_text(path).splitlines():
        header = CZCE_SEG.match(line.strip())
        if header:
            kind, label, trade_date = header.groups()
            is_total = kind == "品种"
            code = re.search(r"([A-Za-z]{1,2}\d{3,4})", label)
            if is_total:
                # The old files name the variety in Chinese only -- 玻璃, not
                # 玻璃FG -- so the code has to come from the name there.
                tail = re.search(r"([A-Za-z]{1,2})$", label)
                instrument = (
                    tail.group(1).upper()
                    if tail
                    else VARIETY_BY_NAME.get(label.strip())
                )
                contract = None
            else:
                contract = (
                    normalise_contract(code.group(1), trade_date) if code else None
                )
                instrument = contract[:2] if contract else None
            continue
        if instrument not in WANT:
            continue
        parts = _split_row(line)
        if len(parts) < 10 or not parts[0].isdigit():
            continue
        rank = parts[0]
        # One line, three boards, three different firms.
        for kind, name_at, qty_at, chg_at in (
            ("volume", 1, 2, 3),
            ("long", 4, 5, 6),
            ("short", 7, 8, 9),
        ):
            member, qty = parts[name_at], num(parts[qty_at])
            if not member or not qty:
                continue
            rows.append(
                {
                    "exchange": "CZCE",
                    "instrument": instrument,
                    "contract": contract,
                    "is_variety_total": is_total,
                    "trade_date": trade_date,
                    "rank_type": kind,
                    "rank": rank,
                    "member": member,
                    "quantity": qty,
                    "change": num(parts[chg_at]),
                    "source": "czce_official",
                }
            )
    return rows


def _shfe_trade_date(path, payload):
    """The date the file is for, from the file itself when it says.

    The pre-2015 ranking files carry no date at all -- no `report_date`, no
    `o_day` -- so the only statement of it is the name we saved it under. Where
    the file does say, the two must agree: a mismatch means the fetch asked for
    one day and was handed another, and every row would land on the wrong date.
    """
    stamp = str(payload.get("report_date") or "").strip()
    digits = re.sub(r"\D", "", stamp)[:8]
    from_file = None
    if len(digits) == 8:
        from_file = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    from_name = Path(path).stem
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_name):
        from_name = None
    if from_file and from_name and from_file != from_name:
        raise ValueError(f"{path}: 文件内日期 {from_file} 与文件名 {from_name} 不一致")
    return from_file or from_name


# ---------------------------------------------------------------- SHFE 行情
def shfe_market(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    trade_date = _shfe_trade_date(path, payload)
    if not trade_date:
        return []
    rows = []
    # SHFE added TURNOVER later: the 2011 files carry no such column, so
    # turnover stays empty for those years rather than being invented.
    for row in payload.get("o_curinstrument") or []:
        product = str(row.get("PRODUCTID") or "").strip()
        month = str(row.get("DELIVERYMONTH") or "").strip()
        variety = re.sub(r"_.*$", "", product).upper()
        if variety not in WANT or not month.isdigit():
            continue
        contract = f"{variety}{month}"
        turnover = num(row.get("TURNOVER"))
        rows.append(
            {
                "exchange": "SHFE",
                "instrument": variety,
                "contract": contract,
                "trade_date": trade_date,
                "prev_settlement": num(row.get("PRESETTLEMENTPRICE")),
                "open": num(row.get("OPENPRICE")),
                "high": num(row.get("HIGHESTPRICE")),
                "low": num(row.get("LOWESTPRICE")),
                "close": num(row.get("CLOSEPRICE")),
                "settlement": num(row.get("SETTLEMENTPRICE")),
                "volume": num(row.get("VOLUME")),
                # published in 万元
                "turnover": str(float(turnover) * 10000) if turnover else "",
                "open_interest": num(row.get("OPENINTEREST")),
                "open_interest_change": num(row.get("OPENINTERESTCHG")),
                "volume_basis": "double",
                "source": "shfe_official",
            }
        )
    return rows


# ---------------------------------------------------------------- SHFE 席位
def shfe_seats(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    trade_date = _shfe_trade_date(path, payload)
    if not trade_date:
        return []
    rows = []
    for row in payload.get("o_cursor") or []:
        raw = str(row.get("INSTRUMENTID") or "").strip()
        total = raw.endswith("all")
        code = raw[:-3] if total else raw
        m = re.fullmatch(r"([a-zA-Z]{1,2})(\d{0,4})", code)
        if not m:
            continue
        variety = m.group(1).upper()
        if variety not in WANT:
            continue
        contract = None if total else f"{variety}{m.group(2)}"
        rank = str(row.get("RANK") or "").strip()
        # `auall` is not a per-member variety ranking, whatever the name
        # suggests: it holds exactly two lines, RANK -1 for 期货公司 and RANK 0
        # for 非期货公司, each a grand total rather than a firm. SHFE publishes
        # no variety-level member ranking at all, so 品种汇总 for 黄金 and 白银
        # has to be summed from the per-contract boards -- unlike CZCE, which
        # does publish one.
        if rank in ("", "-1", "0", "999"):
            continue
        for kind, name_key, qty_key, chg_key in (
            ("volume", "PARTICIPANTABBR1", "CJ1", "CJ1_CHG"),
            ("long", "PARTICIPANTABBR2", "CJ2", "CJ2_CHG"),
            ("short", "PARTICIPANTABBR3", "CJ3", "CJ3_CHG"),
        ):
            member = str(row.get(name_key) or "").strip()
            qty = num(row.get(qty_key))
            if not member or not qty:
                continue
            rows.append(
                {
                    "exchange": "SHFE",
                    "instrument": variety,
                    "contract": contract,
                    "is_variety_total": total,
                    "trade_date": trade_date,
                    "rank_type": kind,
                    "rank": rank,
                    "member": member,
                    "quantity": qty,
                    "change": num(row.get(chg_key)),
                    "source": "shfe_official",
                }
            )
    return rows


# ---------------------------------------------------------------- 三禾 席位
def sanhe_seats(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    trade_date = payload.get("date")
    member = payload.get("broker")
    rows = []
    for name, contracts in (payload.get("positions") or {}).items():
        variety = VARIETY_BY_NAME.get(name)
        if variety not in WANT:
            continue
        for item in contracts:
            contract = normalise_contract(item.get("code"), trade_date)
            if not contract:
                continue
            for kind, qty_key, chg_key in (
                ("long", "buy", "buy_chge"),
                ("short", "ss", "ss_chge"),
            ):
                qty = num(item.get(qty_key))
                if not qty:
                    continue
                rows.append(
                    {
                        "exchange": EXCHANGE_BY_VARIETY[variety],
                        "instrument": variety,
                        "contract": contract,
                        "is_variety_total": False,
                        "trade_date": trade_date,
                        # sanhe publishes neither a rank nor a volume board.
                        "rank_type": kind,
                        "rank": "",
                        "member": member,
                        "quantity": qty,
                        "change": num(item.get(chg_key)),
                        "source": "sanhe",
                    }
                )
    return rows
