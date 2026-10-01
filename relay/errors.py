from __future__ import annotations

import re
from typing import Any

SENSITIVE_FIELDS = {
    "api_key",
    "apikey",
    "x_api_key",
    "authorization",
    "cookie",
    "set_cookie",
    "headers",
    "request_headers",
    "input",
    "messages",
    "prompt",
    "instructions",
    "output",
    "content",
    "body",
    "request_body",
    "response_body",
    "token",
    "access_token",
}


def scrub_credentials(value: Any, secrets: tuple[str, ...], *, redact_keys: bool = False) -> Any:
    """Redact known credentials without removing normal response content."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [scrub_credentials(item, secrets, redact_keys=redact_keys) for item in value]
    if isinstance(value, dict):
        return {
            scrub_credentials(key, secrets) if redact_keys else key: scrub_credentials(
                item, secrets, redact_keys=redact_keys
            )
            for key, item in value.items()
        }
    return value


class CredentialFilter:
    """Delay only possible credential prefixes to redact across SSE deltas."""

    def __init__(self, secrets: tuple[str, ...]):
        self.secrets = tuple(secret for secret in secrets if secret)
        self.pending = ""

    def push(self, text: str, final: bool = False) -> str:
        combined = scrub_credentials(self.pending + text, self.secrets)
        self.pending = ""
        if not final:
            longest = 0
            for secret in self.secrets:
                for size in range(1, min(len(secret), len(combined) + 1)):
                    if combined.endswith(secret[:size]):
                        longest = max(longest, size)
            if longest:
                self.pending = combined[-longest:]
                combined = combined[:-longest]
        return combined


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).lower().replace("-", "_") in SENSITIVE_FIELDS
            else redact(item, secrets)
            for key, item in value.items()
        }
    return value


def safe_request_id(value: Any, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value):
        return None
    if any(secret and secret in value for secret in secrets):
        return None
    return value


def upstream_request_id(headers: Any, body: Any, secrets: tuple[str, ...]) -> str | None:
    for value in (headers.get("request-id"), headers.get("x-request-id")):
        if safe := safe_request_id(value, secrets):
            return safe
    if isinstance(body, dict):
        error = body.get("error")
        if not isinstance(error, dict):
            error = {}
        details = error.get("details")
        if not isinstance(details, dict):
            details = {}
        for value in (body.get("request_id"), error.get("request_id"), details.get("request_id")):
            if safe := safe_request_id(value, secrets):
                return safe
    return None


def error_payload(
    message: str, error_type: str, code: str, request_id: str, upstream_id: str | None = None
) -> dict:
    return {
        "error": {"message": message, "type": error_type, "code": code, "param": None},
        "request_id": request_id,
        "upstream_request_id": upstream_id,
    }


def convert_upstream_error(
    status: int, body: Any, request_id: str, upstream_id: str | None, secrets: tuple[str, ...]
) -> dict:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = redact(body["error"], secrets)
        error.setdefault("message", f"Upstream returned HTTP {status}")
        error.setdefault("type", "upstream_error")
        error.setdefault("code", error["type"])
        error.setdefault("param", None)
    else:
        # Non-JSON bodies may be proxies/WAF pages. Never echo them to logs/DB.
        text = redact(body, secrets) if isinstance(body, str) else ""
        error = {
            "message": text[:4096] or f"Upstream returned HTTP {status}",
            "type": "upstream_error",
            "code": "upstream_http_error",
            "param": None,
        }
    return {"error": error, "request_id": request_id, "upstream_request_id": upstream_id}
