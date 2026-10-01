from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .costs import CostRecord, public_cost

MIGRATIONS = [
    [
        """CREATE TABLE request_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requested_at REAL NOT NULL,
        endpoint TEXT NOT NULL,
        model TEXT NOT NULL,
        stream INTEGER NOT NULL CHECK(stream IN (0,1)),
        http_status INTEGER NOT NULL,
        duration_ms REAL NOT NULL,
        request_id TEXT NOT NULL UNIQUE,
        upstream_request_id TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
        output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
        total_tokens INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0),
        success INTEGER NOT NULL CHECK(success IN (0,1)),
        error_category TEXT
    )"""
    ],
    [
        "CREATE INDEX idx_logs_time ON request_logs(requested_at DESC, id DESC)",
        "CREATE INDEX idx_logs_status_time ON request_logs(http_status, requested_at DESC)",
    ],
    [
        "ALTER TABLE request_logs ADD COLUMN uncached_input_tokens INTEGER CHECK(uncached_input_tokens >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_read_tokens INTEGER CHECK(cache_read_tokens >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_write_tokens INTEGER CHECK(cache_write_tokens >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_write_5m_tokens INTEGER CHECK(cache_write_5m_tokens >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_write_1h_tokens INTEGER CHECK(cache_write_1h_tokens >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cost_currency TEXT",
        "ALTER TABLE request_logs ADD COLUMN input_cost_nanos INTEGER CHECK(input_cost_nanos >= 0)",
        "ALTER TABLE request_logs ADD COLUMN output_cost_nanos INTEGER CHECK(output_cost_nanos >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_read_cost_nanos INTEGER CHECK(cache_read_cost_nanos >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cache_write_cost_nanos INTEGER CHECK(cache_write_cost_nanos >= 0)",
        "ALTER TABLE request_logs ADD COLUMN total_cost_nanos INTEGER CHECK(total_cost_nanos >= 0)",
        "ALTER TABLE request_logs ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'historical'",
        "ALTER TABLE request_logs ADD COLUMN pricing_snapshot TEXT",
    ],
]


@dataclass(frozen=True)
class RequestRecord:
    requested_at: float
    endpoint: str
    model: str
    stream: bool
    http_status: int
    duration_ms: float
    request_id: str
    upstream_request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    success: bool = False
    error_category: str | None = None
    cost: CostRecord = CostRecord()


class Database:
    """One short-lived connection per operation, used from FastAPI's threadpool.

    WAL allows readers alongside a writer. SQLite serializes writers using a busy
    timeout. No connection is shared across request threads or processes.
    """

    def __init__(self, url: str):
        if not url.startswith("sqlite:///") or url.removeprefix("sqlite:///") in {"", ":memory:"}:
            raise ValueError("DATABASE_URL must be a file-backed sqlite:/// URL")
        self.path = Path(url.removeprefix("sqlite:///")).resolve()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise RuntimeError("Database schema is newer than this application")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            for version, migration in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                for statement in migration:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                connection.execute(f"PRAGMA user_version={version}")

    def append(self, record: RequestRecord):
        # Accept a typed metadata record, never a request/response/error body.
        values = asdict(record)
        values.update(values.pop("cost"))
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO request_logs ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )

    @staticmethod
    def filters(
        status_code: int | None, start: float | None, end: float | None
    ) -> tuple[str, list]:
        clauses, values = [], []
        if status_code is not None:
            clauses.append("http_status = ?")
            values.append(status_code)
        if start is not None:
            clauses.append("requested_at >= ?")
            values.append(start)
        if end is not None:
            clauses.append("requested_at <= ?")
            values.append(end)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    def logs(
        self,
        page: int = 1,
        page_size: int = 20,
        status_code: int | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> dict:
        where, values = self.filters(status_code, start, end)
        with self.connect() as connection:
            # Count and rows come from one read snapshot during concurrent writes.
            connection.execute("BEGIN")
            total = connection.execute(
                "SELECT COUNT(*) FROM request_logs" + where, values
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM request_logs"
                + where
                + " ORDER BY requested_at DESC, id DESC LIMIT ? OFFSET ?",
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["stream"] = bool(item["stream"])
            item["success"] = bool(item["success"])
            item["cost"] = public_cost(item)
            # The stored price snapshot is a fixed numeric allowlist. Management
            # callers need amounts, not internal storage units or JSON columns.
            for name in CostRecord.__dataclass_fields__:
                if (
                    name.startswith("cost_")
                    or name.endswith("_nanos")
                    or name == "pricing_snapshot"
                ):
                    item.pop(name)
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def stats(
        self,
        bucket: Literal["hour", "day"] = "hour",
        start: float | None = None,
        end: float | None = None,
    ) -> dict:
        where, values = self.filters(None, start, end)
        aggregate = """COUNT(*) AS requests, COALESCE(SUM(success),0) AS successes,
            COALESCE(SUM(1-success),0) AS failures,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(total_tokens),0) AS total_tokens"""
        seconds = 3600 if bucket == "hour" else 86400
        cost_aggregate = """COUNT(*) AS priced_requests,
            SUM(CASE WHEN cost_status='partial' THEN 1 ELSE 0 END) AS partial_requests,
            SUM(input_cost_nanos) AS input_cost_nanos,
            SUM(output_cost_nanos) AS output_cost_nanos,
            SUM(cache_read_cost_nanos) AS cache_read_cost_nanos,
            SUM(cache_write_cost_nanos) AS cache_write_cost_nanos,
            SUM(total_cost_nanos) AS total_cost_nanos"""
        cost_where = where + (" AND " if where else " WHERE ") + "total_cost_nanos IS NOT NULL"
        with self.connect() as connection:
            connection.execute("BEGIN")
            totals = dict(
                connection.execute(
                    f"SELECT {aggregate} FROM request_logs{where}", values
                ).fetchone()
            )
            series = [
                dict(row)
                for row in connection.execute(
                    f"SELECT CAST(requested_at / ? AS INTEGER) * ? AS bucket_start, {aggregate} FROM request_logs{where} GROUP BY bucket_start ORDER BY bucket_start",
                    [seconds, seconds, *values],
                )
            ]
            cost_totals = [
                public_cost(dict(row), aggregate=True)
                for row in connection.execute(
                    f"SELECT cost_currency, {cost_aggregate} FROM request_logs{cost_where} GROUP BY cost_currency ORDER BY cost_currency",
                    values,
                )
            ]
            cost_series = {}
            for row in connection.execute(
                f"SELECT CAST(requested_at / ? AS INTEGER) * ? AS bucket_start, cost_currency, {cost_aggregate} FROM request_logs{cost_where} GROUP BY bucket_start, cost_currency ORDER BY bucket_start, cost_currency",
                [seconds, seconds, *values],
            ):
                cost_series.setdefault(row["bucket_start"], []).append(
                    public_cost(dict(row), aggregate=True)
                )
        totals["success_rate"] = (
            totals["successes"] / totals["requests"] * 100 if totals["requests"] else 0
        )
        priced_requests = sum(item["priced_requests"] for item in cost_totals)
        for point in series:
            point["costs"] = cost_series.get(point["bucket_start"], [])
        return {
            "totals": totals,
            "bucket": bucket,
            "timezone": "UTC",
            "series": series,
            "costs": {
                "currencies": cost_totals,
                "priced_requests": priced_requests,
                "unpriced_requests": totals["requests"] - priced_requests,
                "partial_requests": sum(item["partial_requests"] for item in cost_totals),
            },
        }

    def check(self):
        with self.connect() as connection:
            connection.execute("SELECT 1 FROM request_logs LIMIT 1").fetchone()
