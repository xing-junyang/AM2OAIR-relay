#!/usr/bin/env python3
"""Independent upstream gate; requires only Python's standard library.

Environment variables take precedence over the project's .env. No prompts,
successful model output, credentials, or request headers are printed or saved.
An HTTP failure prints its status and original body, except any echoed API key.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, value = line.partition("=")
            if not separator:
                continue
            tokens = shlex.split(value, comments=True, posix=True)
            values[name.strip()] = " ".join(tokens)
    values.update(os.environ)
    return values


class NoRedirect(HTTPRedirectHandler):
    """Never forward an upstream credential to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    try:
        env = load_env()
    except (ValueError, OSError):
        print("Cannot parse .env; check its syntax locally.", file=sys.stderr)
        return 2
    key = env.get("AM2OAIR_RELAY_API_KEY", "").strip()
    if not key:
        print(
            "AM2OAIR_RELAY_API_KEY is not configured. "
            "Set it in the environment or in the project's .env before development.",
            file=sys.stderr,
        )
        return 2

    base = env.get("AM2OAIR_RELAY_BASE_URL") or "https://api.poixe.com/v1"
    model = env.get("AM2OAIR_RELAY_MODEL") or "aws-claude/claude-opus-5-5"
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        print("Invalid upstream Base URL; do not put credentials in the URL.", file=sys.stderr)
        return 2
    request = Request(
        base.rstrip("/") + "/messages",
        data=json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "stream": False,
            }
        ).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            # urllib's default client signature triggered Cloudflare 1010.
            # Use an honest application identifier, also for the future relay.
            "user-agent": "AM2OAIR-relay/0.1",
        },
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=120) as response:
            body = response.read()
            status = response.status
            request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
    except HTTPError as error:
        print(f"HTTP {error.code}", flush=True)
        body = error.read()
        # The no-secret requirement also applies if an upstream echoes the key.
        sys.stdout.buffer.write(body.replace(key.encode("utf-8"), b"[REDACTED]"))
        sys.stdout.buffer.flush()
        return 1
    except (URLError, TimeoutError, OSError):
        print(
            "Upstream connection failed; no HTTP status or response body was received.",
            file=sys.stderr,
        )
        return 3

    try:
        message = json.loads(body)
        assert isinstance(message, dict)
        assert message.get("type") == "message"
        assert message.get("role") == "assistant"
        assert isinstance(message.get("content"), list) and message["content"]
        assert any(
            block.get("type") == "text" and block.get("text") for block in message["content"]
        )
    except (ValueError, AssertionError, AttributeError, TypeError):
        print(
            f"HTTP {status}; response did not validate as an Anthropic text message.",
            file=sys.stderr,
        )
        return 4
    metadata = {
        "result": "passed",
        "http_status": status,
        "request_id": request_id,
        "input_tokens": (message.get("usage") or {}).get("input_tokens"),
        "output_tokens": (message.get("usage") or {}).get("output_tokens"),
    }
    print(json.dumps(metadata).replace(key, "[REDACTED]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
