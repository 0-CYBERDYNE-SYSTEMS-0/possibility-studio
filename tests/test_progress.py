from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from studio import progress
from studio.provider import ProviderError, ProviderSettings, chat_completion

URL = "https://provider.example/v1/chat/completions"


def sse(*events: dict, done: bool = True) -> bytes:
    lines = ["data: " + json.dumps(event) + "\n" for event in events]
    if done:
        lines.append("data: [DONE]\n")
    return "\n".join(lines).encode()


def delta(text: str = "", finish: str | None = None, **extra) -> dict:
    return {"choices": [{"delta": {"content": text, **extra} if text or extra else {}, "finish_reason": finish}]}


def patch_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "studio.provider.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


def config() -> ProviderSettings:
    return ProviderSettings(URL, "model", "key", False)


def test_decoder_reads_partial_html_from_streaming_json():
    buffer = '{"html": "<p class=\\"a\\">hi\\nthere \\u00e9 \\ud83d\\ude00'
    assert progress.decode_json_string(buffer, "html") == '<p class="a">hi\nthere é 😀'


def test_decoder_stops_at_incomplete_escape_and_closing_quote():
    assert progress.decode_json_string('{"html": "abc\\', "html") == "abc"
    assert progress.decode_json_string('{"html": "abc\\u00', "html") == "abc"
    assert progress.decode_json_string('{"html": "abc\\ud83d', "html") == "abc"
    assert progress.decode_json_string('{"html": "done", "requirements": []}', "html") == "done"
    assert progress.decode_json_string('{"req', "html") is None


def test_draft_returns_only_new_text_and_resets_on_new_phase():
    progress.start("op", "generating", 1000)
    record = progress.recorder("op")
    record('{"html": "<b>one')
    first = progress.draft("op", 0, 0)
    assert first["text"] == "<b>one" and first["length"] == 6
    record('two')
    second = progress.draft("op", first["serial"], first["length"])
    assert second["text"] == "two"
    progress.start("op", "repairing", 1000)
    again = progress.draft("op", first["serial"], 8)
    assert again["serial"] == first["serial"] + 1 and again["text"] == ""
    progress.finish("op")
    assert progress.draft("op", 0, 0)["active"] is False
    assert progress.snapshot("op") is None


def test_snapshot_counts_tokens_and_reasoning():
    progress.start("snap", "generating", 128000)
    record = progress.recorder("snap")
    record("", reasoning=True)
    record("abcdefgh" * 10)
    state = progress.snapshot("snap")
    assert state["tokens"] == 20 and state["max_tokens"] == 128000
    assert state["reasoning_chunks"] == 1 and state["phase"] == "generating"
    progress.finish("snap")


def test_stream_collects_content_and_reports_deltas(monkeypatch):
    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        body = sse(delta("Hel"), delta("", reasoning_content="think"), delta("lo"), delta("", "stop"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    patch_client(monkeypatch, handler)
    result = asyncio.run(
        chat_completion(
            config(), [{"role": "user", "content": "x"}], max_tokens=10,
            on_delta=lambda text, reasoning=False: seen.append((text, reasoning)),
        )
    )
    assert result["content"] == "Hello"
    assert seen == [("Hel", False), ("", True), ("lo", False)]


def test_stream_keeps_partial_text_when_the_output_limit_is_hit(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse(delta("cut off"), delta("", "length"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    patch_client(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(chat_completion(config(), [{"role": "user", "content": "x"}], max_tokens=10, on_delta=lambda *a, **k: None))
    assert caught.value.partial == "cut off"


def test_stream_accepts_a_plain_json_reply(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}]})

    patch_client(monkeypatch, handler)
    result = asyncio.run(
        chat_completion(config(), [{"role": "user", "content": "x"}], max_tokens=10, on_delta=lambda text, reasoning=False: seen.append(text))
    )
    assert result["content"] == "whole" and seen == ["whole"]


def test_stream_retries_without_json_schema_after_a_rejection(monkeypatch):
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("response_format" in json.loads(request.content))
        if calls[-1]:
            return httpx.Response(400, json={"error": "no schema"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(delta("ok"), delta("", "stop")))

    patch_client(monkeypatch, handler)
    schema = {"name": "x", "schema": {"type": "object"}}
    result = asyncio.run(
        chat_completion(config(), [{"role": "user", "content": "x"}], max_tokens=10, json_schema=schema, on_delta=lambda *a, **k: None)
    )
    assert result["content"] == "ok" and calls == [True, False]
