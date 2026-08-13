"""日志里只出现类型名,不出现异常消息。

上游返回的错误体里可能带着请求原样回显(2026-07 实测有把提交内容整段回显的),
直接 log 异常消息等于把它写进日志文件。只印类型名。
"""

from __future__ import annotations


def safe_error_code(error: Exception) -> str:
    return type(error).__name__
