import json
import sqlite3
from dataclasses import asdict, replace

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.config import Settings
from relay.conversion import usage_to_openai
from relay.costs import Pricing, estimate_cost, money
from relay.database import MIGRATIONS, Database, RequestRecord


@pytest.fixture
def pricing():
    # Deliberately fictional test prices; not provider recommendations or defaults.
    return Pricing(
        input="3",
        output="9",
        cache_read="0.4",
        cache_write="4.5",
        cache_write_5m="4",
        cache_write_1h="6",
    )


def test_cost_breakdown_includes_cache_without_double_counting(pricing):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 300,
        "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
    }
    cost = estimate_cost(pricing, usage)
    assert cost.cost_status == "calculated"
    assert cost.uncached_input_tokens == 1000
    assert money(cost.input_cost_nanos) == "0.003000000"
    assert money(cost.output_cost_nanos) == "0.001800000"
    assert money(cost.cache_read_cost_nanos) == "0.000200000"
    assert money(cost.cache_write_cost_nanos) == "0.001400000"
    assert money(cost.total_cost_nanos) == "0.006400000"
    assert usage_to_openai(usage)["input_tokens"] == 1800


def test_unsplit_cache_write_uses_explicit_generic_rate(pricing):
    usage = {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 100}
    cost = estimate_cost(pricing, usage)
    assert cost.cache_write_5m_tokens is None
    assert money(cost.cache_write_cost_nanos) == "0.000450000"
    assert estimate_cost(replace(pricing, cache_write=None), usage).total_cost_nanos is None


def test_missing_rates_and_usage_are_unknown_not_zero():
    result = estimate_cost(Pricing(), {"input_tokens": 1, "output_tokens": 1})
    assert result.cost_status == "unconfigured"
    assert result.total_cost_nanos is None
    assert estimate_cost(Pricing(input="3", output="9"), {}).cost_status == "missing_usage"
    assert estimate_cost(Pricing(), None).total_cost_nanos is None


def test_explicit_free_rates_and_unused_missing_cache_rates():
    usage = {"input_tokens": 10, "output_tokens": 5}
    cost = estimate_cost(Pricing(input="0", output="0"), usage)
    assert cost.cost_status == "calculated" and cost.total_cost_nanos == 0
    usage["cache_read_input_tokens"] = 1
    assert estimate_cost(Pricing(input="0", output="0"), usage).cost_status == "unconfigured"


def test_decimal_money_keeps_sub_micro_currency_precision():
    cost = estimate_cost(
        Pricing(input="0.125", output="0.375"), {"input_tokens": 1, "output_tokens": 1}
    )
    assert cost.total_cost_nanos == 500
    assert money(cost.total_cost_nanos) == "0.000000500"


def test_partial_stream_usage_is_marked(pricing):
    cost = estimate_cost(pricing, {"input_tokens": 10}, complete=False)
    assert cost.cost_status == "partial"
    assert money(cost.total_cost_nanos) == "0.000030000"
    assert estimate_cost(pricing, {"input_tokens": 10}).cost_status == "missing_usage"


@pytest.mark.parametrize(
    "raw", ["-1", "NaN", "Infinity", "secret-invalid-rate", "1000001", "1e-1000000", True]
)
def test_invalid_rates_fail_without_echoing_values(raw):
    with pytest.raises(ValueError) as exc:
        Pricing(input=raw)
    assert "secret-invalid-rate" not in str(exc.value)


@pytest.mark.parametrize("currency", ["usd", "USD\n", "USDT", "上游密钥"])
def test_currency_validation(currency):
    with pytest.raises(ValueError):
        Pricing(currency=currency)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": "secret-prompt", "output_tokens": 1},
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 10,
            "cache_creation": {"ephemeral_5m_input_tokens": 3},
        },
    ],
)
def test_invalid_usage_does_not_crash_or_enter_cost_record(pricing, usage):
    cost = estimate_cost(pricing, usage)
    assert cost.cost_status == "invalid_usage" and cost.total_cost_nanos is None
    assert "secret-prompt" not in str(asdict(cost))


def test_cost_overflow_remains_unknown():
    cost = estimate_cost(
        Pricing(input="1000000", output="0"), {"input_tokens": 10**20, "output_tokens": 0}
    )
    assert cost.cost_status == "cost_overflow" and cost.total_cost_nanos is None


def append_usage(db, pricing, timestamp, request_id, inputs=1000, outputs=200, complete=True):
    db.append(
        RequestRecord(
            requested_at=timestamp,
            endpoint="/v1/responses",
            model="test-model",
            stream=False,
            http_status=200,
            duration_ms=1,
            request_id=request_id,
            input_tokens=inputs,
            output_tokens=outputs,
            total_tokens=inputs + outputs,
            success=complete,
            cost=estimate_cost(
                pricing, {"input_tokens": inputs, "output_tokens": outputs}, complete
            ),
        )
    )


def test_time_filtered_hourly_daily_cost_aggregates(settings, pricing):
    db = Database(settings.database_url)
    db.initialize()
    base = 1790812800
    append_usage(db, pricing, base, "req_a")
    append_usage(db, pricing, base + 60, "req_b")
    append_usage(db, pricing, base + 3600, "req_c", complete=False)
    append_usage(db, pricing, base + 86400, "req_d")
    totals = db.stats()["costs"]
    assert totals["priced_requests"] == 4 and totals["partial_requests"] == 1
    assert totals["currencies"][0]["total_cost"] == "0.019200000"
    assert db.stats()["series"][0]["costs"][0]["total_cost"] == "0.009600000"
    assert db.stats(bucket="day")["series"][0]["costs"][0]["total_cost"] == "0.014400000"
    filtered = db.stats(start=base + 60, end=base + 3600)
    assert filtered["costs"]["currencies"][0]["total_cost"] == "0.009600000"
    assert db.stats(start=base + 100000)["costs"]["currencies"] == []
    assert db.logs(start=base + 60, end=base + 3600)["total"] == 2


def test_currency_groups_never_mix(settings, pricing):
    db = Database(settings.database_url)
    db.initialize()
    append_usage(db, pricing, 1000, "req_usd")
    append_usage(db, replace(pricing, currency="CNY", input="7"), 1001, "req_cny")
    groups = db.stats()["costs"]["currencies"]
    assert [(item["currency"], item["total_cost"]) for item in groups] == [
        ("CNY", "0.008800000"),
        ("USD", "0.004800000"),
    ]


def test_unknown_cost_coverage(settings, pricing):
    db = Database(settings.database_url)
    db.initialize()
    append_usage(db, pricing, 1000, "req_priced")
    append_usage(db, Pricing(), 1001, "req_unknown")
    stats = db.stats()
    assert stats["costs"]["priced_requests"] == 1
    assert stats["costs"]["unpriced_requests"] == 1
    assert stats["costs"]["currencies"][0]["total_cost"] == "0.004800000"
    assert db.logs()["items"][0]["cost"]["total_cost"] is None


def test_schema_two_upgrade_keeps_history_unknown(settings):
    db = Database(settings.database_url)
    with sqlite3.connect(db.path) as connection:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            "INSERT INTO request_logs (requested_at, endpoint, model, stream, http_status, duration_ms, request_id, input_tokens, output_tokens, total_tokens, success) VALUES (1000, '/v1/responses', 'old-model', 0, 200, 1, 'req_old', 10, 5, 15, 1)"
        )
    db.initialize()
    db.initialize()
    assert db.stats()["totals"]["total_tokens"] == 15
    assert db.stats()["costs"]["unpriced_requests"] == 1
    log = db.logs()["items"][0]
    assert log["cost"]["status"] == "historical" and log["cost"]["total_cost"] is None
    assert log["cache_read_tokens"] is None


def api_client(settings, handler):
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def sse_response(events):
    content = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})


def test_cost_api_persistence_and_price_snapshot(settings, pricing, message):
    first_settings = replace(settings, pricing=pricing)
    with api_client(first_settings, lambda _: httpx.Response(200, json=message)) as client:
        response = client.post(
            "/v1/responses",
            json={"input": "private-prompt"},
            headers={"authorization": "Bearer private-auth", "cookie": "private-cookie"},
        )
        assert response.status_code == 200
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["cost"]["total_cost"] == "0.000075000"
        assert row["cost"]["status"] == "calculated"
        assert client.get("/api/admin/status").json()["pricing"]["rates"]["input"] == "3"
        first_stats = client.get("/api/admin/stats").json()
    with api_client(
        replace(settings, pricing=Pricing(input="7", output="11")),
        lambda _: httpx.Response(200, json=message),
    ) as client:
        assert client.get("/api/admin/stats").json() == first_stats
        client.post("/v1/responses", json={"input": "private-prompt"})
        assert (
            client.get("/api/admin/stats").json()["costs"]["currencies"][0]["total_cost"]
            == "0.000200000"
        )
        management = "".join(
            client.get(path).text
            for path in ("/api/admin/status", "/api/admin/stats", "/api/admin/logs")
        )
    db = Database(settings.database_url)
    with db.connect() as connection:
        dump = "\n".join(connection.iterdump())
        snapshots = [
            json.loads(row[0])
            for row in connection.execute("SELECT pricing_snapshot FROM request_logs ORDER BY id")
        ]
    assert snapshots[0]["rates"]["input"] == "3" and snapshots[1]["rates"]["input"] == "7"
    for sensitive in (
        settings.api_key,
        "private-prompt",
        "private-auth",
        "private-cookie",
        message["content"][0]["text"],
    ):
        assert sensitive not in dump and sensitive not in management


def test_sse_cost_uses_raw_cache_usage(settings, pricing, text_events):
    with api_client(
        replace(settings, pricing=pricing), lambda _: sse_response(text_events)
    ) as client:
        response = client.post("/v1/responses", json={"input": "Hello", "stream": True})
        assert "response.completed" in response.text
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["input_tokens"] == 13 and row["uncached_input_tokens"] == 10
        assert row["cache_read_tokens"] == 3
        assert row["cost"]["total_cost"] == "0.000076200"


def test_stream_failure_keeps_partial_cost(settings, pricing, text_events):
    events = text_events[:2] + [
        {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}
    ]
    with api_client(replace(settings, pricing=pricing), lambda _: sse_response(events)) as client:
        response = client.post("/v1/responses", json={"input": "Hello", "stream": True})
        assert "response.failed" in response.text
        row = client.get("/api/admin/logs").json()["items"][0]
        assert not row["success"] and row["cost"]["status"] == "partial"
        assert row["cost"]["total_cost"] == "0.000031200"
        assert client.get("/api/admin/stats").json()["costs"]["partial_requests"] == 1


def test_cost_api_filters_timezone_and_no_usage_errors(settings, pricing):
    with api_client(
        replace(settings, pricing=pricing),
        lambda _: httpx.Response(
            503, json={"error": {"type": "overloaded_error", "message": "Busy"}}
        ),
    ) as client:
        client.post("/v1/responses", json={"input": "Hello"})
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["cost"]["status"] == "missing_usage" and row["cost"]["total_cost"] is None
        append_usage(client.app.state.db, pricing, 1790812800, "req_filtered")
        filtered = client.get(
            "/api/admin/stats",
            params={"start": "2026-10-01T08:00:00+08:00", "end": "2026-10-01T08:00:01+08:00"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["costs"]["priced_requests"] == 1
        assert filtered.json()["costs"]["currencies"][0]["total_cost"] == "0.004800000"
        assert (
            client.get("/api/admin/stats", params={"start": "2026-10-01T00:00:00"}).status_code
            == 400
        )


def test_pricing_environment_does_not_expose_other_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AM2OAIR_RELAY_API_KEY", "env-secret-key")
    monkeypatch.setenv("AM2OAIR_RELAY_COST_INPUT_PER_MILLION", "0.125")
    monkeypatch.setenv("AM2OAIR_RELAY_COST_OUTPUT_PER_MILLION", "0")
    monkeypatch.setenv("AM2OAIR_RELAY_COST_CURRENCY", "CNY")
    pricing = Settings.from_env().pricing.public()
    assert pricing["currency"] == "CNY" and pricing["configured"]
    assert pricing["rates"]["output"] == "0"
    assert "env-secret-key" not in json.dumps(pricing)
