import json
import os
import subprocess
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.database import Database, RequestRecord
from relay.errors import CredentialFilter
from relay.streaming import StreamTranslator


def test_credential_filter_across_deltas():
    key = "sk-private-example"
    filter_ = CredentialFilter((key,))
    result = "".join(filter_.push(character) for character in "Before " + key + " after")
    result += filter_.push("", final=True)
    assert result == "Before [REDACTED] after"
    assert key not in result


def test_success_response_does_not_return_upstream_key(settings, message):
    message["content"][0]["text"] = "echo " + settings.api_key
    with TestClient(
        create_app(settings, httpx.MockTransport(lambda request: httpx.Response(200, json=message)))
    ) as client:
        response = client.post("/v1/responses", json={"input": "Hi"})
        assert response.status_code == 200
        assert settings.api_key not in response.text
        assert response.json()["output_text"] == "echo [REDACTED]"


def test_streaming_secret_split_between_events(settings):
    translator = StreamTranslator(settings.model, "resp_secret", (settings.api_key,))
    events = translator.begin()
    events += translator.feed({"type": "message_start", "message": {"usage": {}}})
    events += translator.feed(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    )
    for letter in settings.api_key:
        events += translator.feed(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": letter},
            }
        )
    events += translator.feed({"type": "content_block_stop", "index": 0})
    events += translator.feed({"type": "message_stop"})
    deltas = "".join(
        event["delta"] for event in events if event["type"] == "response.output_text.delta"
    )
    assert deltas == "[REDACTED]"
    assert events[-1]["response"]["output_text"] == "[REDACTED]"
    assert settings.api_key not in json.dumps(events)


def test_backup_restore_and_cleanup(settings):
    db = Database(settings.database_url)
    db.initialize()
    db.append(
        RequestRecord(
            requested_at=1000,
            endpoint="/v1/responses",
            model=settings.model,
            stream=False,
            http_status=200,
            duration_ms=10,
            request_id="req_backup",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            success=True,
        )
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "db_admin.py"
    import sys

    env = {**os.environ, "AM2OAIR_RELAY_DATABASE_URL": settings.database_url}
    snapshot = subprocess.run(
        [sys.executable, str(script), "backup", "--stdout"],
        env=env,
        capture_output=True,
        check=True,
    ).stdout
    assert snapshot.startswith(b"SQLite format 3")
    subprocess.run(
        [sys.executable, str(script), "clear", "--all"], env=env, capture_output=True, check=True
    )
    assert db.stats()["totals"]["requests"] == 0
    subprocess.run(
        [sys.executable, str(script), "restore", "--stdin", "--service-stopped"],
        env=env,
        input=snapshot,
        capture_output=True,
        check=True,
    )
    restored = Database(settings.database_url)
    restored.initialize()
    assert restored.logs()["items"][0]["request_id"] == "req_backup"
    assert restored.stats()["totals"]["total_tokens"] == 15
