"""郑商所老格式里「品种：甲醇」认不出是 ME 还是 MA —— 认错就差五倍。

2014-07 ~ 2015-05 郑商所同一天发**两个** `品种：甲醇` 段头：一个是旧代码
`ME`(50 吨/手),一个是新代码 `MA`(10 吨/手)。2015-11 之前的段头只有中文名,
不带代码,两段逐字节同构。

没有这道闸门时,`VARIETY_BY_NAME["甲醇"] = "MA"` 会把**两段都**记成 MA:
品种汇总凭空翻倍,而且其中一半是 50 吨/手的持仓被当成 10 吨/手 —— 页面上
看不出任何异常,盈亏整段错。**宁可丢掉,让 compute-seat-totals.sql 自算补。**

用例是照 2014-07-01 / 2015-06-01 生产文件的真实形状写的(TAB 分段头、
逗号分隔、无表头),不是我编的格式。
"""

import importlib.util
from pathlib import Path

PARSERS = Path(__file__).resolve().parents[2] / "backfill" / "parsers.py"


def load():
    spec = importlib.util.spec_from_file_location("backfill_parsers", PARSERS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(rank, qty):
    """一行三榜：成交量/持买单量/持卖单量,各自一家会员。"""
    return f"{rank},甲席位{rank},{qty},10,乙席位{rank},{qty},10,丙席位{rank},{qty},10"


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="gbk")
    return str(path)


BOTH_ERAS = "\n".join(
    [
        "品种：甲醇\t日期：2014-07-01",
        row(1, 100),
        "品种：甲醇\t日期：2014-07-01",
        row(1, 200),
        "合约：ME409\t日期：2014-07-01",
        row(1, 300),
        "合约：MA506\t日期：2014-07-01",
        row(1, 400),
        "品种：玻璃\t日期：2014-07-01",
        row(1, 500),
        "",
    ]
)

ME_GONE = "\n".join(
    [
        "品种：甲醇\t日期：2015-06-01",
        row(1, 100),
        "合约：MA509\t日期：2015-06-01",
        row(1, 400),
        "",
    ]
)


def test_ME还在的那几个月_甲醇品种汇总一行都不要(tmp_path):
    parsers = load()
    rows = parsers.czce_seats(write(tmp_path, "2014-07-01.txt", BOTH_ERAS))

    totals = [r for r in rows if r["is_variety_total"]]
    assert [r["instrument"] for r in totals] == ["FG"] * 3, (
        "两个同名的甲醇汇总段必须一起丢掉,只剩玻璃那段"
    )
    # 逐合约行不受影响：MA506 认得出来，ME409 被 WANT 自然滤掉。
    contracts = {r["contract"] for r in rows if not r["is_variety_total"]}
    assert contracts == {"MA1506"}, contracts


def test_ME退完之后_老格式的甲醇汇总照收(tmp_path):
    parsers = load()
    rows = parsers.czce_seats(write(tmp_path, "2015-06-01.txt", ME_GONE))

    totals = [r for r in rows if r["is_variety_total"]]
    assert totals, "文件里没有 ME 合约段，甲醇就只剩一个意思，不该再丢"
    assert {r["instrument"] for r in totals} == {"MA"}
