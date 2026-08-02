from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from futures_collector.config import Credentials
from futures_collector.normalize import DATASET_FIELDS
from futures_collector.sources import ExchangeSource


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    status: str
    inserted: int
    skipped: int


class PlatformRequestError(RuntimeError):
    def __init__(self, stage: str, response: httpx.Response) -> None:
        code = "http_error"
        try:
            payload = response.json()
            candidate = payload.get("data", {}).get("code")
            if isinstance(candidate, str) and candidate:
                code = candidate
        except (ValueError, AttributeError):
            code = "http_error"
        self.safe_code = f"{stage}:{response.status_code}:{code}"
        self.code = code
        super().__init__(self.safe_code)


def require_success(response: httpx.Response, stage: str) -> None:
    if response.is_error:
        raise PlatformRequestError(stage, response)


class PlatformClient:
    def __init__(self, credentials: Credentials, timeout: float = 60.0) -> None:
        self.credentials = credentials
        self.client = httpx.Client(
            base_url=credentials.base_url,
            timeout=timeout,
            follow_redirects=False,
        )
        self.csrf = ""

    def __enter__(self) -> PlatformClient:
        response = self.client.post(
            "/api/v1/auth/login",
            headers={"Origin": self.credentials.origin},
            json={
                "username": self.credentials.username,
                "password": self.credentials.password,
            },
        )
        require_success(response, "login")
        roles = response.json().get("data", {}).get("user", {}).get("roles", [])
        if "analyst" not in roles:
            raise RuntimeError("collector service account must have the analyst role")
        csrf = self.client.get("/api/v1/auth/csrf")
        require_success(csrf, "csrf")
        self.csrf = str(csrf.json()["data"]["csrf_token"])
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.client.post(
                "/api/v1/auth/logout",
                headers=self._write_headers(),
            )
        finally:
            self.client.close()
            self.csrf = ""

    def submit(
        self,
        source: ExchangeSource,
        dataset_type: str,
        collection_date: date,
        rows: list[dict[str, str]],
    ) -> ImportResult:
        content = render_csv(dataset_type, rows)
        upload = self.client.post(
            "/api/v1/imports",
            headers={
                **self._write_headers(),
                "x-ingestion-mode": "automatic",
                "x-dataset-type": dataset_type,
                "x-data-source-code": source.source_code,
                "x-collection-date": collection_date.isoformat(),
                "x-template-version": f"{dataset_type}@1",
            },
            files={
                "file": (f"{source.code}-{dataset_type}-{collection_date}.csv", content, "text/csv")
            },
        )
        require_success(upload, "upload")
        import_id = str(upload.json()["data"]["id"])
        confirm = self.client.post(
            f"/api/v1/imports/{import_id}/automatic-confirm",
            headers={
                **self._write_headers(),
                "Idempotency-Key": (
                    f"collector:{source.source_code}:{dataset_type}:{collection_date}"
                ),
            },
        )
        require_success(confirm, "automatic_confirm")
        return self.wait(import_id)

    def record_failure(
        self,
        source: ExchangeSource,
        dataset_type: str,
        collection_date: date,
    ) -> str:
        row = {field: "" for field in DATASET_FIELDS[dataset_type]}
        row["exchange_code"] = source.code
        row["source_record_ref"] = f"{source.code}:source-unavailable:{collection_date}"
        try:
            self.submit(source, dataset_type, collection_date, [row])
        except PlatformRequestError as error:
            return error.code
        return "automatic_source_failed"

    def wait(self, import_id: str, timeout: float = 180.0) -> ImportResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/imports/{import_id}")
            require_success(response, "poll")
            payload: dict[str, Any] = response.json()["data"]
            status = str(payload["status"])
            if status in {"succeeded", "failed", "dead_letter"}:
                job = payload.get("job") or {}
                result = ImportResult(
                    import_id=import_id,
                    status=status,
                    inserted=int(job.get("inserted_count", 0)),
                    skipped=int(job.get("skipped_count", 0)),
                )
                if status != "succeeded":
                    raise RuntimeError(f"automatic import ended with status={status}")
                return result
            time.sleep(1.0)
        raise TimeoutError("automatic import did not reach a terminal state")

    def _write_headers(self) -> dict[str, str]:
        return {"Origin": self.credentials.origin, "x-csrf-token": self.csrf}


def render_csv(dataset_type: str, rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=DATASET_FIELDS[dataset_type], extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")
