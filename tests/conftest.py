from pathlib import Path

import pytest

from relay.config import Settings


@pytest.fixture
def settings(tmp_path: Path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><div id='root'>Dashboard</div>")
    (static / "assets" / "app.js").write_text("console.log('dashboard')")
    return Settings(
        api_key="secret-test-upstream-key",
        base_url="https://upstream.test/v1",
        model="claude-opus-5-5",
        database_url="sqlite:///" + str(tmp_path / "relay.db"),
        static_dir=static,
    )


@pytest.fixture
def message():
    return {
        "id": "msg_upstream",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
def text_events():
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_upstream",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 0},
            },
        },
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "世界"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
