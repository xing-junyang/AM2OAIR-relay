import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from relay.database import MIGRATIONS, Database, RequestRecord


def record(index=0, timestamp=1790812800, success=True, inputs=10, outputs=5):
    return RequestRecord(
        requested_at=timestamp,
        endpoint="/v1/responses",
        model="test-model",
        stream=False,
        http_status=200 if success else 503,
        duration_ms=20,
        request_id=f"req_{index}",
        input_tokens=inputs,
        output_tokens=outputs,
        total_tokens=inputs + outputs,
        success=success,
        error_category=None if success else "upstream_http_error",
    )


def test_pagination_and_filters(settings):
    db = Database(settings.database_url)
    db.initialize()
    for i in range(5):
        db.append(record(i, 1000 + i, success=i % 2 == 0))
    first = db.logs(page_size=2)
    assert first["total"] == 5
    assert [row["request_id"] for row in first["items"]] == ["req_4", "req_3"]
    assert [row["request_id"] for row in db.logs(page=2, page_size=2)["items"]] == [
        "req_2",
        "req_1",
    ]
    filtered = db.logs(status_code=503, start=1002)
    assert filtered["total"] == 1
    assert filtered["items"][0]["request_id"] == "req_3"


def test_token_aggregates(settings):
    db = Database(settings.database_url)
    db.initialize()
    # UTC-aligned hour and day boundaries.
    base = 1790812800
    db.append(record(0, base, inputs=10, outputs=2))
    db.append(record(1, base + 60, success=False, inputs=0, outputs=0))
    db.append(record(2, base + 3600, inputs=20, outputs=3))
    db.append(record(3, base + 86400, inputs=30, outputs=4))
    stats = db.stats()
    assert stats["totals"] == {
        "requests": 4,
        "successes": 3,
        "failures": 1,
        "input_tokens": 60,
        "output_tokens": 9,
        "total_tokens": 69,
        "success_rate": 75.0,
    }
    assert len(stats["series"]) == 3
    assert stats["series"][0]["requests"] == 2
    assert len(db.stats(bucket="day")["series"]) == 2
    assert db.stats(start=base, end=base + 100)["totals"]["total_tokens"] == 12


def test_empty_statistics(settings):
    db = Database(settings.database_url)
    db.initialize()
    assert db.stats()["totals"]["success_rate"] == 0
    assert db.stats()["series"] == []


def test_persistence_after_reinitialize(settings):
    db = Database(settings.database_url)
    db.initialize()
    db.append(record())
    second = Database(settings.database_url)
    second.initialize()
    assert second.logs()["items"][0]["request_id"] == "req_0"
    assert second.stats()["totals"]["total_tokens"] == 15
    with second.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
            MIGRATIONS
        )


def test_upgrade_from_schema_one_preserves_data(settings):
    db = Database(settings.database_url)
    with sqlite3.connect(db.path) as connection:
        connection.execute(MIGRATIONS[0][0])
        connection.execute("PRAGMA user_version=1")
        # Insert using the actual old schema, before running the new migrations.
        old_values = asdict(record())
        old_values.pop("cost")
        connection.execute(
            f"INSERT INTO request_logs ({','.join(old_values)}) VALUES ({','.join('?' for _ in old_values)})",
            list(old_values.values()),
        )
    db.initialize()
    assert db.logs()["total"] == 1
    with db.connect() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_logs_status_time'"
        ).fetchone()


def test_concurrent_writes(settings):
    db = Database(settings.database_url)
    db.initialize()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: db.append(record(i)), range(40)))
    assert db.stats()["totals"]["requests"] == 40
    assert db.stats()["totals"]["total_tokens"] == 600
