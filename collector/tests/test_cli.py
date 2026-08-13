import logging

from futures_collector import cli


def test_startup_failure_logs_only_exception_type(monkeypatch, caplog, tmp_path) -> None:
    """启动失败只印异常类型,不印消息。

    上游的错误体里出现过把提交内容整段回显的情况,直接 log 异常消息等于把它写进
    日志文件。原来这条验的是「读凭据失败」——凭据随导入通道一起没了(DEC-049),
    改成验采集器构造失败,守的是同一条性质。
    """

    def explode() -> None:
        raise PermissionError("must-not-appear")

    monkeypatch.setattr(cli, "AkshareAdapter", explode)

    with caplog.at_level(logging.ERROR):
        result = cli.main(
            ["--date", "2026-07-31", "--exchange", "DCE", "--dataset", "market",
             "--emit-csv", str(tmp_path)]
        )

    assert result == 1
    assert "collector_start_failed error=PermissionError" in caplog.text
    assert "must-not-appear" not in caplog.text


def test_collection_without_an_output_directory_is_refused(caplog) -> None:
    """没有 --emit-csv 就不许采。

    CSV 是唯一出口。这条以前是可选的,不给就走导入通道——通道 2026-08-13 删了,
    再不给就等于对着 404 发请求。这里必须是运行期校验而不是 argparse 的
    required,否则 --resolve-date 会被一起挡掉。
    """
    with caplog.at_level(logging.ERROR):
        result = cli.main(["--date", "2026-07-31", "--exchange", "DCE", "--dataset", "market"])

    assert result == 1
    assert "collector_start_failed error=emit_csv_is_required" in caplog.text
