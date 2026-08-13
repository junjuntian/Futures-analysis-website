"""CSV 出口的行为契约。

这个 sink 取代的是审计导入通道,而通道自带的那些保护(暂存、校验、冲突检测、
人工确认)在直路上都没有了。剩下能保护数据的只有下面这几条,所以逐条钉住。
"""

from datetime import date

import pytest

from futures_collector.csv_sink import CsvSink
from futures_collector.normalize import DATASET_FIELDS
from futures_collector.sources import SOURCES

SHFE = SOURCES["SHFE"]
DAY = date(2026, 8, 13)


def _row(**overrides):
    row = {field: "" for field in DATASET_FIELDS["daily_market_prices_v1"]}
    row.update(overrides)
    return row


def test_each_dataset_lands_in_its_own_file(tmp_path):
    # 一家采失败不该连累另外四家——这与 runner 的来源隔离是同一个契约,
    # 合并成一个大文件就做不到逐个装载、逐个报错。
    with CsvSink(tmp_path) as sink:
        sink.submit(SHFE, "daily_market_prices_v1", DAY, [_row(contract_code="AU2610")])
    written = list(tmp_path.glob("*.csv"))
    assert [p.name for p in written] == ["SHFE-daily_market_prices_v1-2026-08-13.csv"]
    assert "AU2610" in written[0].read_text(encoding="utf-8")


def test_a_failed_dataset_writes_no_data_file(tmp_path):
    # **最要紧的一条。** 采集失败时如果写出一个只有表头的 CSV,装载脚本会把它
    # 当成「今天这个交易所一行数据都没有」而照单全收,把库里已有的当日数据
    # upsert 成空值。失败的语义是「不知道」,不是「没有」——这与掉榜日不能补 0
    # 是同一个道理。
    with CsvSink(tmp_path) as sink:
        reason = sink.record_failure(SHFE, "daily_market_prices_v1", DAY)
    assert reason == "automatic_source_failed"
    assert list(tmp_path.glob("*.csv")) == []
    markers = list(tmp_path.glob("*.csv.failed"))
    assert len(markers) == 1, "失败要留痕:与「压根没跑」必须区分得开"


def test_failure_removes_a_stale_file_from_an_earlier_run(tmp_path):
    # 同一天重跑:上一轮成功、这一轮失败。旧文件必须清掉,否则装载脚本会拿
    # 上一轮的结果当成今天的重灌一遍,而实际上今天什么都没采到。
    with CsvSink(tmp_path) as sink:
        sink.submit(SHFE, "daily_market_prices_v1", DAY, [_row(contract_code="AU2610")])
        assert list(tmp_path.glob("*.csv"))
        sink.record_failure(SHFE, "daily_market_prices_v1", DAY)
    assert list(tmp_path.glob("*.csv")) == []


def test_success_clears_an_earlier_failure_marker(tmp_path):
    # 反过来:先失败后成功(兜底那一轮采到了)。失败标记不清掉的话,
    # 装载脚本会一直跳过这个数据集,数据采到了却进不了库。
    with CsvSink(tmp_path) as sink:
        sink.record_failure(SHFE, "daily_market_prices_v1", DAY)
        sink.submit(SHFE, "daily_market_prices_v1", DAY, [_row(contract_code="AU2610")])
    assert list(tmp_path.glob("*.csv.failed")) == []
    assert len(list(tmp_path.glob("*.csv"))) == 1


def test_no_partial_file_is_left_behind(tmp_path):
    # 装载可能与采集并行(不同数据集),半截的 CSV 被读到就是残缺的一天。
    # 写临时文件再原子改名,结束后目录里不该有 .partial。
    with CsvSink(tmp_path) as sink:
        sink.submit(SHFE, "daily_market_prices_v1", DAY, [_row(contract_code="AU2610")])
    assert list(tmp_path.glob("*.partial")) == []


def test_columns_match_the_dataset_contract(tmp_path):
    # 列顺序与装载 SQL 的临时表一一对应。多一列少一列都会让 \copy 静默错位,
    # 而错位的数据看起来完全正常。
    with CsvSink(tmp_path) as sink:
        sink.submit(SHFE, "seat_positions_v1", DAY, [])
    header = next(tmp_path.glob("*.csv")).read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(DATASET_FIELDS["seat_positions_v1"])


def test_an_unknown_dataset_is_rejected_not_guessed(tmp_path):
    with CsvSink(tmp_path) as sink, pytest.raises(KeyError):
        sink.submit(SHFE, "not_a_dataset", DAY, [])
