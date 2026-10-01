from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .conversion import (
    InvalidRequest,
    ToolRegistry,
    UpstreamProtocolError,
    to_anthropic,
    to_openai,
    usage_to_openai,
)
from .costs import estimate_cost
from .database import MIGRATIONS, Database, RequestRecord
from .errors import convert_upstream_error, error_payload, scrub_credentials, upstream_request_id
from .streaming import StreamTranslator, encode_event, read_sse

logger = logging.getLogger("relay")


class RelayStreamingResponse(StreamingResponse):
    """Close the upstream generator even if downstream send is interrupted.

    An ASGI send failure can leave an async generator suspended at yield;
    explicit aclose ensures its metadata finalizer runs before the request exits.
    """

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or Settings.from_env()
        db = Database(cfg.database_url)
        await run_in_threadpool(db.initialize)
        # HTTPX defaults to logging request URLs at INFO. Keep default logs quiet.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=15),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=False,
            transport=transport,
            headers={
                "user-agent": "AM2OAIR-relay/0.1",
                "anthropic-version": "2023-06-01",
                "x-api-key": cfg.api_key,
            },
        ) as client:
            app.state.settings = cfg
            app.state.db = db
            app.state.client = client
            app.state.started_monotonic = time.monotonic()
            app.state.started_at = time.time()
            yield

    app = FastAPI(
        title="AM2OAIR Relay", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        # FastAPI's default validation error echoes invalid values (possibly secrets).
        return JSONResponse(
            error_payload(
                "Invalid query parameters",
                "invalid_request_error",
                "invalid_parameter",
                "req_" + uuid.uuid4().hex,
            ),
            status_code=422,
        )

    @app.get("/healthz")
    async def healthz():
        try:
            await run_in_threadpool(app.state.db.check)
        except sqlite3.Error:
            return JSONResponse({"status": "unhealthy", "database": "unavailable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {
                    "id": app.state.settings.public_config()["model"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "relay",
                }
            ],
        }

    @app.get("/api/admin/status")
    async def status():
        healthy = await healthz()
        if isinstance(healthy, JSONResponse):
            return healthy
        return {
            "status": "running",
            "started_at": app.state.started_at,
            "uptime_seconds": round(time.monotonic() - app.state.started_monotonic, 2),
            "config": app.state.settings.public_config(),
            "database": "ok",
            "schema_version": len(MIGRATIONS),
            "pricing": app.state.settings.pricing.public(),
        }

    def time_range(start: datetime | None, end: datetime | None):
        if any(value is not None and value.tzinfo is None for value in (start, end)):
            raise HTTPException(status_code=400, detail="Time filters require a timezone")
        if start is not None and end is not None and start > end:
            raise HTTPException(status_code=400, detail="start must not exceed end")
        return start.timestamp() if start else None, end.timestamp() if end else None

    @app.get("/api/admin/logs")
    async def logs(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        status_code: int | None = Query(None, ge=100, le=599),
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        start_at, end_at = time_range(start, end)
        return await run_in_threadpool(
            app.state.db.logs, page, page_size, status_code, start_at, end_at
        )

    @app.get("/api/admin/stats")
    async def stats(
        bucket: Literal["hour", "day"] = "hour",
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        start_at, end_at = time_range(start, end)
        return await run_in_threadpool(app.state.db.stats, bucket, start_at, end_at)

    @app.post("/v1/responses")
    async def responses(request: Request):
        cfg: Settings = app.state.settings
        request_id = "req_" + uuid.uuid4().hex
        response_id = "resp_" + uuid.uuid4().hex
        requested_at, started = time.time(), time.monotonic()
        secrets = tuple(
            value
            for value in (
                cfg.api_key,
                request.headers.get("authorization", ""),
                request.headers.get("cookie", ""),
                request.headers.get("x-api-key", ""),
                request.headers.get("authorization", "").removeprefix("Bearer "),
            )
            if value
        )
        is_stream = False
        upstream_id: str | None = None
        record_done = False

        async def record(
            status_code: int,
            success: bool,
            usage: dict | None = None,
            category: str | None = None,
            anthropic_usage: dict | None = None,
            usage_complete: bool = True,
        ) -> bool:
            nonlocal record_done
            if record_done:
                return True
            record_done = True
            usage = usage or {}
            metadata = RequestRecord(
                requested_at=requested_at,
                endpoint="/v1/responses",
                model=cfg.public_config()["model"],
                stream=is_stream,
                http_status=status_code,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                request_id=request_id,
                upstream_request_id=upstream_id,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                success=success,
                error_category=category,
                cost=estimate_cost(cfg.pricing, anthropic_usage, complete=usage_complete),
            )
            try:
                await run_in_threadpool(app.state.db.append, metadata)
                return True
            except sqlite3.Error:
                logger.error("request_metadata_write_failed")
                return False

        def response_headers() -> dict:
            headers = {"x-request-id": request_id}
            if upstream_id:
                headers["x-upstream-request-id"] = upstream_id
            return headers

        async def local_error(
            status_code: int, message: str, category: str, param: str | None = None
        ):
            payload = error_payload(
                message,
                "invalid_request_error" if status_code == 400 else "server_error",
                category,
                request_id,
                upstream_id,
            )
            payload["error"]["param"] = param
            await record(status_code, False, category=category)
            return JSONResponse(payload, status_code=status_code, headers=response_headers())

        try:
            raw = await request.body()
            if len(raw) > 16 * 1024 * 1024:
                return await local_error(413, "Request body exceeds 16 MiB", "request_too_large")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise InvalidRequest("Request body must be a JSON object")
            is_stream = body.get("stream") is True
            registry = ToolRegistry(body.get("tools"), body.get("input"))
            upstream_body = to_anthropic(body, cfg, registry)
        except (ValueError, UnicodeDecodeError):
            return await local_error(400, "Request body must contain valid JSON", "invalid_json")
        except InvalidRequest as exc:
            return await local_error(400, str(exc), "invalid_request", exc.param)

        upstream: httpx.Response | None = None
        try:
            upstream_request = app.state.client.build_request(
                "POST",
                cfg.base_url.rstrip("/") + "/messages",
                json=upstream_body,
                headers={
                    "x-request-id": request_id,
                    "accept": "text/event-stream" if is_stream else "application/json",
                },
            )
            # Connect before starting downstream SSE so HTTP errors retain status.
            upstream = await app.state.client.send(upstream_request, stream=True)
            upstream_id = upstream_request_id(upstream.headers, None, secrets)
            if not 200 <= upstream.status_code < 300:
                await upstream.aread()
                try:
                    error_body = upstream.json()
                except ValueError:
                    error_body = upstream.text
                upstream_id = upstream_request_id(upstream.headers, error_body, secrets)
                payload = convert_upstream_error(
                    upstream.status_code, error_body, request_id, upstream_id, secrets
                )
                status_code = upstream.status_code
                await upstream.aclose()
                # Do not follow credential-bearing redirects; they cannot be returned
                # as a success. All upstream 4xx/5xx status codes are preserved.
                if status_code < 400:
                    status_code = 502
                await record(status_code, False, category="upstream_http_error")
                headers = response_headers()
                if value := upstream.headers.get("retry-after"):
                    if value.isdigit() and len(value) <= 10:
                        headers["retry-after"] = value
                return JSONResponse(payload, status_code=status_code, headers=headers)
            if not is_stream:
                await upstream.aread()
                try:
                    message = upstream.json()
                except ValueError:
                    raise UpstreamProtocolError("Invalid upstream JSON") from None
                result = scrub_credentials(
                    to_openai(message, cfg.public_config()["model"], response_id, registry), secrets
                )
                await upstream.aclose()
                success = result["status"] == "completed"
                if not await record(
                    200,
                    success,
                    result["usage"],
                    None if success else "max_output_tokens",
                    anthropic_usage=message.get("usage"),
                ):
                    return JSONResponse(
                        error_payload(
                            "Request metadata storage is unavailable",
                            "server_error",
                            "database_unavailable",
                            request_id,
                            upstream_id,
                        ),
                        status_code=503,
                        headers=response_headers(),
                    )
                return JSONResponse(result, headers=response_headers())
            if "text/event-stream" not in upstream.headers.get("content-type", "").lower():
                raise UpstreamProtocolError("Expected upstream text/event-stream")
        except httpx.TimeoutException:
            if upstream is not None:
                await upstream.aclose()
            return await local_error(504, "Upstream request timed out", "upstream_timeout")
        except httpx.HTTPError:
            if upstream is not None:
                await upstream.aclose()
            return await local_error(502, "Upstream connection failed", "upstream_connection_error")
        except UpstreamProtocolError:
            if upstream is not None:
                await upstream.aclose()
            return await local_error(
                502, "Upstream returned an invalid Anthropic response", "upstream_protocol_error"
            )

        translator = StreamTranslator(cfg.public_config()["model"], response_id, secrets, registry)

        async def stream_events():
            nonlocal upstream_id
            category = "client_disconnected"
            try:
                for event in translator.begin():
                    yield encode_event(event)
                async for payload in read_sse(upstream.aiter_lines()):
                    if payload.get("type") == "error":
                        upstream_id = (
                            upstream_request_id(upstream.headers, payload, secrets) or upstream_id
                        )
                        error = convert_upstream_error(
                            502, payload, request_id, upstream_id, secrets
                        )
                        for event in translator.fail(error):
                            yield encode_event(event)
                        category = "upstream_stream_error"
                        break
                    events = translator.feed(payload)
                    if translator.terminal:
                        success = translator.response["status"] == "completed"
                        category = None if success else "max_output_tokens"
                        # Persist before the terminal event: immediately refreshing
                        # the dashboard after completed sees this request.
                        if not await record(
                            200,
                            success,
                            translator.response["usage"],
                            category,
                            anthropic_usage=translator.usage,
                        ):
                            # The wire has started. Announce storage failure without
                            # pretending the request was durably accounted for.
                            yield encode_event(
                                translator.event(
                                    "error",
                                    code="database_unavailable",
                                    message="Request metadata storage is unavailable",
                                    param=None,
                                )
                            )
                        for event in events:
                            yield encode_event(event)
                        break
                    for event in events:
                        yield encode_event(event)
                if not translator.terminal:
                    raise UpstreamProtocolError("Upstream stream ended without message_stop")
            except asyncio.CancelledError:
                category = "client_disconnected"
                raise
            except (httpx.HTTPError, UpstreamProtocolError) as exc:
                category = (
                    "upstream_timeout"
                    if isinstance(exc, httpx.TimeoutException)
                    else "upstream_protocol_error"
                    if isinstance(exc, UpstreamProtocolError)
                    else "upstream_connection_error"
                )
                error = error_payload(
                    "Upstream stream interrupted", "server_error", category, request_id, upstream_id
                )
                for event in translator.fail(error):
                    yield encode_event(event)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await record(
                        200,
                        translator.response["status"] == "completed",
                        translator.response.get("usage") or usage_to_openai(translator.usage),
                        category,
                        anthropic_usage=translator.usage,
                        usage_complete=translator.response["status"] in {"completed", "incomplete"},
                    )

        return RelayStreamingResponse(
            stream_events(),
            media_type="text/event-stream",
            headers={**response_headers(), "cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # Mount only the asset subtree. API routes retain 404s and can never be masked
    # by index.html. Resolve paths before serving to prevent directory traversal.
    static_dir = (
        settings.static_dir if settings else Settings.__dataclass_fields__["static_dir"].default
    )
    static_dir = Path(static_dir).resolve()
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.split("/", 1)[0] in {"v1", "api", "healthz", "assets"}:
            raise HTTPException(status_code=404, detail="Not found")
        if not (static_dir / "index.html").is_file():
            raise HTTPException(
                status_code=404, detail="Dashboard not built; run the frontend production build"
            )
        candidate = (static_dir / path).resolve()
        if candidate.is_relative_to(static_dir) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html", headers={"cache-control": "no-cache"})

    return app


app = create_app()
