"""把采集结果写成 CSV 落盘,而不是经审计导入通道提交。

为什么有这个出口
----------------
导入通道(上传→暂存→逐行校验→冲突检测→人工确认→血缘→canonical→投影→宽表)
是为「人工上传文件、需要预览和回滚」设计的。运营者 2026-08-13 说明:那个入口
当初是给已取消的 AI 分析功能用的,他从没手工导过数据——**每天的自动采集却一直
被迫走这条七层流水线**。生产实测的代价:中间产物(暂存行 1448 MB、变更记录
832 MB、校验错误 66 MB、血缘 292 MB)加起来和最终的业务数据一样大,而且每天都在长。

采集器的数据不需要那套:格式是它自己规范化的、来源是白名单里的公开接口、
错了明天重采一遍就有。它需要的只是「把这批行写进宽表」。

这个 sink 就是那条直路,与 `PlatformClient` 接口一致(`submit` / `record_failure` /
上下文管理器),所以 `CollectionRunner` 一行都不用改——采集、去重、回退、失败隔离
那些逻辑照旧,只是终点从 HTTP 变成了文件。

落盘之后由 `deploy/collector/load-*.sql` 用 psql 直接灌进宽表。那条路生产上已经
跑了几个月(新浪大商所日行情走的就是它,见 load-dce-daily.sql),不是新发明。

失败也要留痕
------------
`record_failure` 写一个只有表头的空 CSV 并在文件名上标 `.failed`。装载脚本据此
跳过,而运维能一眼看出「这个数据集今天试过但没采到」——与「压根没跑」区分开。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from futures_collector.normalize import DATASET_FIELDS
from futures_collector.sources import ExchangeSource


@dataclass(frozen=True)
class CsvWriteResult:
    """与 ImportResult 同形,好让 runner 的日志与统计不用分支。"""

    import_id: str
    status: str
    inserted: int
    skipped: int


def _render(dataset_type: str, rows: list[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=DATASET_FIELDS[dataset_type], extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


class CsvSink:
    """把每个 (交易所, 数据集, 交易日) 写成一个 CSV。

    一个数据集一个文件,而不是合并成大文件:装载脚本可以逐个灌、逐个报错,
    一家采失败不影响另外四家——这与 runner 的来源隔离契约是同一个道理。
    """

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)

    def __enter__(self) -> CsvSink:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _path(self, source: ExchangeSource, dataset_type: str, collection_date: date) -> Path:
        return self.out_dir / f"{source.code}-{dataset_type}-{collection_date.isoformat()}.csv"

    def submit(
        self,
        source: ExchangeSource,
        dataset_type: str,
        collection_date: date,
        rows: list[dict[str, str]],
        *,
        skipped_source_item_count: int = 0,
    ) -> CsvWriteResult:
        target = self._path(source, dataset_type, collection_date)
        # 先写临时文件再原子改名:装载脚本可能与采集并行(不同数据集),
        # 半截的 CSV 被读到会灌进残缺的一天。
        staging = target.with_suffix(".csv.partial")
        staging.write_text(_render(dataset_type, rows), encoding="utf-8")
        staging.replace(target)
        # 上一轮如果失败过,这次成功要把失败标记清掉,否则装载脚本会一直跳过它。
        failed_marker = target.with_suffix(".csv.failed")
        failed_marker.unlink(missing_ok=True)
        return CsvWriteResult(
            import_id=target.name,
            status="succeeded",
            inserted=len(rows),
            skipped=skipped_source_item_count,
        )

    def record_failure(
        self,
        source: ExchangeSource,
        dataset_type: str,
        collection_date: date,
        *,
        skipped_source_item_count: int = 0,
    ) -> str:
        """采集失败:留一个空的失败标记,不写数据文件。

        **不写出只有表头的数据 CSV**——那会被装载脚本当成「今天这个交易所一行
        数据都没有」而照单全收,把已经在库里的当日数据 upsert 成空值。失败的
        语义是「不知道」,不是「没有」。
        """
        marker = self._path(source, dataset_type, collection_date).with_suffix(".csv.failed")
        marker.write_text(
            f"{source.code} {dataset_type} {collection_date.isoformat()} "
            f"skipped={skipped_source_item_count}\n",
            encoding="utf-8",
        )
        # 同时删掉可能存在的旧数据文件:这一轮既然失败,就不该让装载脚本
        # 拿上一轮的文件当成今天的结果重灌一遍。
        self._path(source, dataset_type, collection_date).unlink(missing_ok=True)
        return "automatic_source_failed"
