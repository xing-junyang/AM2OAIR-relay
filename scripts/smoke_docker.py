#!/usr/bin/env python3
"""Live Docker acceptance test, using metadata-only console output.

Run after docker compose up -d --build. Makes two small real upstream calls,
checks frontend + APIs + SQL, then restarts and recreates the relay container.
Does not reset or remove its database volume.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

URL = "http://127.0.0.1:8787"


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Docker operation failed")
    return result.stdout.strip()


def fetch(path: str, body: dict | None = None) -> tuple[bytes, dict]:
    request = Request(
        URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
    )
    # macOS urllib inherits system proxies, which can intercept localhost. Docker
    # smoke must connect directly to the requested 127.0.0.1 listener.
    with build_opener(ProxyHandler({})).open(request, timeout=200) as response:
        return response.read(), dict(response.headers)


def get_json(path: str) -> dict:
    return json.loads(fetch(path)[0])


def ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if get_json("/healthz")["status"] == "ok":
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Relay did not become healthy")


def main():
    ready()
    html, _ = fetch("/")
    assert b'id="root"' in html
    # Also fetch the actual built JS and CSS rather than only an HTML shell.
    import re

    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html.decode())
    assert assets
    for path in assets:
        assert fetch(path)[0]
    assert b'id="root"' in fetch("/dashboard/requests")[0]
    status = get_json("/api/admin/status")
    assert status["status"] == "running" and status["schema_version"] >= 3
    assert status["pricing"]["unit"] == "per_million_tokens"
    assert get_json("/api/admin/logs")["page"] == 1
    assert get_json("/v1/models")["data"]
    before = get_json("/api/admin/stats")["totals"]
    ids: list[str] = []
    token_delta = 0
    nonstream_body, headers = fetch(
        "/v1/responses", {"input": "Reply with OK.", "max_output_tokens": 32, "stream": False}
    )
    response = json.loads(nonstream_body)
    assert response["status"] == "completed" and response["output_text"]
    ids.append(next(v for k, v in headers.items() if k.lower() == "x-request-id"))
    token_delta += response["usage"]["total_tokens"]
    stream_body, headers = fetch(
        "/v1/responses", {"input": "Reply with OK.", "max_output_tokens": 32, "stream": True}
    )
    assert any(
        k.lower() == "content-type" and v.startswith("text/event-stream")
        for k, v in headers.items()
    )
    events = [
        json.loads(line[6:])
        for line in stream_body.decode().splitlines()
        if line.startswith("data: ")
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output_text"]
    ids.append(next(v for k, v in headers.items() if k.lower() == "x-request-id"))
    token_delta += events[-1]["response"]["usage"]["total_tokens"]
    after_snapshot = get_json("/api/admin/stats")
    after = after_snapshot["totals"]
    assert after["requests"] >= before["requests"] + 2
    assert after["successes"] >= before["successes"] + 2
    assert after["total_tokens"] >= before["total_tokens"] + token_delta > before["total_tokens"]
    assert get_json("/api/admin/stats?bucket=day")["series"]
    new_rows = [
        row
        for row in get_json("/api/admin/logs?page_size=100")["items"]
        if row["request_id"] in ids
    ]
    assert len(new_rows) == 2
    for row in new_rows:
        assert row["cost"]["currency"] == status["pricing"]["currency"]
        assert row["uncached_input_tokens"] is not None
        if not status["pricing"]["configured"]:
            assert row["cost"]["status"] == "unconfigured"
            assert row["cost"]["total_cost"] is None
    query = urlencode(
        {
            "bucket": "day",
            "start": datetime.fromtimestamp(
                min(row["requested_at"] for row in new_rows) - 1, UTC
            ).isoformat(),
            "end": datetime.fromtimestamp(
                max(row["requested_at"] for row in new_rows) + 1, UTC
            ).isoformat(),
        }
    )
    selected = get_json("/api/admin/stats?" + query)
    assert selected["totals"]["requests"] >= 2
    assert (
        selected["costs"]["priced_requests"] + selected["costs"]["unpriced_requests"]
        == selected["totals"]["requests"]
    )

    container = docker("compose", "ps", "-q", "relay")
    exposed = json.loads(docker("inspect", "--format", "{{json .Config.ExposedPorts}}", container))
    ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
    assert set(exposed) == {"8787/tcp"} and set(ports) == {"8787/tcp"}
    assert ports["8787/tcp"][0]["HostIp"] == "127.0.0.1"
    sql_check = "import os,sqlite3,json; p=os.environ['AM2OAIR_RELAY_DATABASE_URL'].removeprefix('sqlite:///'); c=sqlite3.connect(p); print(json.dumps({'requests':c.execute('select count(*) from request_logs').fetchone()[0],'tokens':c.execute('select coalesce(sum(total_tokens),0) from request_logs').fetchone()[0]}))"
    sql = json.loads(docker("compose", "exec", "-T", "relay", "python", "-c", sql_check))
    assert sql["requests"] >= after["requests"] and sql["tokens"] >= after["total_tokens"]
    print(
        json.dumps(
            {
                "frontend": "passed",
                "management_apis": "passed",
                "relay_json": "passed",
                "relay_sse": "passed",
                "database": "passed",
                "cost_metadata_and_time_query": "passed",
                "new_requests": 2,
                "new_tokens": token_delta,
                "exposed_port": 8787,
            }
        ),
        flush=True,
    )

    docker("compose", "restart", "relay")
    ready()
    restarted = get_json("/api/admin/stats")
    assert restarted == after_snapshot
    assert set(ids).issubset(
        {row["request_id"] for row in get_json("/api/admin/logs?page_size=100")["items"]}
    )
    print("Container restart persistence: passed", flush=True)
    docker("compose", "up", "-d", "--force-recreate", "--no-build", "relay")
    ready()
    assert get_json("/api/admin/stats") == after_snapshot
    assert set(ids).issubset(
        {row["request_id"] for row in get_json("/api/admin/logs?page_size=100")["items"]}
    )
    print("Container recreation persistence: passed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Upstream/client bodies and Docker inspect environment remain private.
        print(
            f"Docker smoke failed ({type(exc).__name__}); no sensitive body printed",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
