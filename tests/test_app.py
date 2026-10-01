import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.database import Database


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        # Deliberately split unicode and SSE lines across arbitrary chunks.
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]


def stream_response(payloads):
    wire = ":keepalive\r\n\r\n" + "".join(
        "event: " + p["type"] + "\r\ndata: " + json.dumps(p, ensure_ascii=False) + "\r\n\r\n"
        for p in payloads
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": "up_stream_id"},
        stream=Chunks(wire.encode()),
    )


def client_for(settings, handler):
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def parse_events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_nonstream_endpoint_and_metadata(settings, message):
    def handler(request):
        assert str(request.url) == "https://upstream.test/v1/messages"
        assert request.headers["x-api-key"] == settings.api_key
        assert request.headers["user-agent"] == "AM2OAIR-relay/0.1"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        payload = json.loads(request.content)
        assert payload["system"] == "Be brief"
        assert payload["model"] == settings.model
        return httpx.Response(200, json=message, headers={"request-id": "up_nonstream_id"})

    with client_for(settings, handler) as client:
        result = client.post("/v1/responses", json={"input": "Hi", "instructions": "Be brief"})
        assert result.status_code == 200
        assert result.json()["output_text"] == "Hello"
        assert result.headers["x-upstream-request-id"] == "up_nonstream_id"
        logs = client.get("/api/admin/logs").json()
        assert logs["total"] == 1
        row = logs["items"][0]
        assert row["request_id"] == result.headers["x-request-id"]
        assert row["upstream_request_id"] == "up_nonstream_id"
        assert row["success"] is True
        assert row["duration_ms"] >= 0
        assert row["total_tokens"] == 15
        assert client.get("/api/admin/stats").json()["totals"]["requests"] == 1


def test_sse_endpoint(settings, text_events):
    with client_for(settings, lambda request: stream_response(text_events)) as client:
        response = client.post("/v1/responses", json={"input": "Hello", "stream": True})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_events(response)
        assert events[-1]["type"] == "response.completed"
        assert events[-1]["response"]["output_text"] == "Hello 世界"
        assert [event["sequence_number"] for event in events] == list(range(len(events)))
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["stream"] is True and row["success"] is True
        assert row["input_tokens"] == 13
        assert client.get("/api/admin/stats").json()["totals"]["total_tokens"] == 18


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 503])
def test_upstream_http_errors(settings, status_code):
    error = {
        "error": {
            "type": "model_overloaded",
            "message": "Model is busy",
            "details": {"request_id": "up_error_id"},
        }
    }
    with client_for(
        settings,
        lambda request: httpx.Response(status_code, json=error, headers={"retry-after": "5"}),
    ) as client:
        response = client.post("/v1/responses", json={"input": "Hello", "stream": True})
        assert response.status_code == status_code
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["x-upstream-request-id"] == "up_error_id"
        assert response.headers["retry-after"] == "5"
        assert response.json()["error"]["type"] == "model_overloaded"
        assert response.json()["error"]["details"]["request_id"] == "up_error_id"
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["http_status"] == status_code and row["success"] is False
        assert row["error_category"] == "upstream_http_error"
        assert row["upstream_request_id"] == "up_error_id"


def test_plaintext_waf_error(settings):
    with client_for(
        settings, lambda request: httpx.Response(403, text="error code: 1010\n")
    ) as client:
        response = client.post("/v1/responses", json={"input": "Hi"})
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "error code: 1010\n"


@pytest.mark.parametrize(
    "exception,status_code",
    [
        (httpx.ConnectError("secret connection data"), 502),
        (httpx.ReadTimeout("secret timeout data"), 504),
    ],
)
def test_transport_errors(settings, exception, status_code):
    def handler(request):
        raise exception

    with client_for(settings, handler) as client:
        response = client.post("/v1/responses", json={"input": "Hi"})
        assert response.status_code == status_code
        assert "secret" not in response.text
        assert client.get("/api/admin/stats").json()["totals"]["failures"] == 1


def test_stream_interruption(settings, text_events):
    with client_for(settings, lambda request: stream_response(text_events[:-1])) as client:
        response = client.post("/v1/responses", json={"input": "Hi", "stream": True})
        events = parse_events(response)
        assert events[-2]["type"] == "error"
        assert events[-1]["type"] == "response.failed"
        row = client.get("/api/admin/logs").json()["items"][0]
        assert row["http_status"] == 200
        assert row["success"] is False
        assert row["error_category"] == "upstream_protocol_error"


def test_upstream_sse_error(settings, text_events):
    events = text_events[:2] + [
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "Busy", "request_id": "up_sse_error"},
        }
    ]
    with client_for(settings, lambda request: stream_response(events)) as client:
        response = client.post("/v1/responses", json={"input": "Hi", "stream": True})
        out = parse_events(response)
        assert out[-2]["type"] == "error"
        assert out[-1]["response"]["status"] == "failed"
        assert out[-2]["upstream_request_id"] == "up_stream_id"
        assert (
            client.get("/api/admin/logs").json()["items"][0]["error_category"]
            == "upstream_stream_error"
        )


def test_management_status_models_and_spa(settings, message):
    with client_for(settings, lambda request: httpx.Response(200, json=message)) as client:
        assert client.get("/healthz").json() == {"status": "ok", "database": "ok"}
        status = client.get("/api/admin/status").json()
        assert status["status"] == "running"
        assert status["uptime_seconds"] >= 0
        assert status["config"] == {"base_url": settings.base_url, "model": settings.model}
        assert client.get("/v1/models").json()["data"][0]["id"] == settings.model
        assert "Dashboard" in client.get("/").text
        assert "Dashboard" in client.get("/dashboard/requests").text
        assert client.get("/assets/app.js").status_code == 200
        for path in ("/v1/missing", "/api/admin/missing", "/healthz/missing", "/assets/missing.js"):
            assert client.get(path).status_code == 404
        assert client.get("/api/admin/logs?page=0").status_code == 422
        assert client.get("/api/admin/stats?bucket=minute").status_code == 422
        assert client.get("/api/admin/logs?start=2026-10-01T00:00:00").status_code == 400


def test_service_restart_preserves_history(settings, message):
    with client_for(settings, lambda request: httpx.Response(200, json=message)) as client:
        request_id = client.post("/v1/responses", json={"input": "Hi"}).headers["x-request-id"]
    with client_for(settings, lambda request: httpx.Response(200, json=message)) as restarted:
        assert restarted.get("/api/admin/logs").json()["items"][0]["request_id"] == request_id
        assert restarted.get("/api/admin/stats?bucket=day").json()["totals"]["total_tokens"] == 15


def test_sensitive_data_never_persisted_or_in_management_api(settings, message, caplog):
    prompt, output, authorization, cookie = (
        "SENSITIVE_PROMPT_MARKER",
        "SENSITIVE_MODEL_OUTPUT",
        "Bearer SENSITIVE_AUTH_HEADER",
        "session=SENSITIVE_COOKIE",
    )
    message["content"][0]["text"] = output
    caplog.set_level(logging.DEBUG)
    with client_for(
        settings,
        lambda request: httpx.Response(200, json=message, headers={"request-id": "up_private_id"}),
    ) as client:
        response = client.post(
            "/v1/responses",
            json={"input": prompt, "instructions": prompt},
            headers={"authorization": authorization, "cookie": cookie},
        )
        assert response.json()["output_text"] == output
        for path in (
            "/healthz",
            "/v1/models",
            "/api/admin/status",
            "/api/admin/logs",
            "/api/admin/stats",
        ):
            data = client.get(path).text
            for secret in (prompt, output, authorization, cookie, settings.api_key):
                assert secret not in data
    db = Database(settings.database_url)
    with db.connect() as connection:
        dump = "\n".join(connection.iterdump())
        columns = {row[1] for row in connection.execute("PRAGMA table_info(request_logs)")}
    assert not columns.intersection(
        {"prompt", "content", "messages", "authorization", "api_key", "headers", "output"}
    )
    for secret in (prompt, output, authorization, cookie, settings.api_key):
        assert secret not in dump
        assert secret not in caplog.text
        for file in db.path.parent.glob("relay.db*"):
            assert secret.encode() not in file.read_bytes()


def test_error_secrets_redacted_and_not_persisted(settings):
    authorization = "Bearer caller-private-token"
    error = {
        "error": {
            "type": "authentication_error",
            "message": f"Key {settings.api_key}, header {authorization}",
            "details": {
                "api_key": settings.api_key,
                "authorization": authorization,
                "prompt": "private-prompt",
                "output": "private-output",
                "request_id": "up_redacted_id",
            },
        }
    }
    with client_for(settings, lambda request: httpx.Response(401, json=error)) as client:
        response = client.post(
            "/v1/responses",
            json={"input": "private-prompt"},
            headers={"authorization": authorization},
        )
        assert settings.api_key not in response.text
        assert authorization not in response.text
        assert response.json()["error"]["details"]["prompt"] == "[REDACTED]"
        admin = client.get("/api/admin/logs").text
        for secret in (settings.api_key, authorization, "private-prompt", "private-output"):
            assert secret not in admin
    with Database(settings.database_url).connect() as connection:
        dump = "\n".join(connection.iterdump())
    assert "Key " not in dump
    assert "private-prompt" not in dump


def test_invalid_request_does_not_echo_input(settings, message):
    with client_for(settings, lambda request: httpx.Response(200, json=message)) as client:
        response = client.post("/v1/responses", content="secret-bad-json")
        assert response.status_code == 400
        assert "secret-bad-json" not in response.text
        assert "secret-query" not in client.get("/api/admin/logs?status_code=secret-query").text


def test_codex_optional_fields_dont_cause_server_error(settings, message):
    with client_for(settings, lambda request: httpx.Response(200, json=message)) as client:
        response = client.post(
            "/v1/responses",
            json={
                "input": "Hi",
                "reasoning": {"effort": "high"},
                "include": ["reasoning.encrypted_content"],
                "store": False,
                "parallel_tool_calls": True,
                "prompt_cache_key": "session-id",
                "text": {"verbosity": "low"},
            },
        )
        assert response.status_code == 200


async def test_client_disconnect_preserves_observed_usage(settings, text_events):
    import asyncio

    app = create_app(settings, httpx.MockTransport(lambda request: stream_response(text_events)))
    body = json.dumps({"input": "Hi", "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(event):
        if event["type"] == "http.response.body" and b"response.output_item.added" in event.get(
            "body", b""
        ):
            # A disconnect after message_start but before terminal usage.
            raise asyncio.CancelledError()

    async with app.router.lifespan_context(app):
        with pytest.raises(asyncio.CancelledError):
            await app(scope, receive, send)
        row = app.state.db.logs()["items"][0]
        assert row["success"] is False
        assert row["error_category"] == "client_disconnected"
        assert row["input_tokens"] == 13
        assert row["output_tokens"] == 0
        assert row["total_tokens"] == 13
