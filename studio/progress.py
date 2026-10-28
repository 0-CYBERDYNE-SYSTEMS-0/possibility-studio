from __future__ import annotations

import time

_runs: dict[str, dict] = {}

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def start(operation_id: str, phase: str, max_tokens: int) -> None:
    previous = _runs.get(operation_id)
    now = time.monotonic()
    _runs[operation_id] = {
        "serial": previous["serial"] + 1 if previous else 1,
        "phase": phase,
        "max_tokens": max_tokens,
        "started": now,
        "first": None,
        "last": now,
        "chunks": 0,
        "reasoning": 0,
        "text": "",
    }


def recorder(operation_id: str):
    def record(text: str, reasoning: bool = False) -> None:
        run = _runs.get(operation_id)
        if not run:
            return
        now = time.monotonic()
        run["last"] = now
        if reasoning:
            run["reasoning"] += 1
            return
        if run["first"] is None:
            run["first"] = now
        run["chunks"] += 1
        run["text"] += text

    return record


def finish(operation_id: str) -> None:
    _runs.pop(operation_id, None)


def snapshot(operation_id: str) -> dict | None:
    run = _runs.get(operation_id)
    if not run:
        return None
    now = time.monotonic()
    chars = len(run["text"])
    tokens = max(run["chunks"], chars // 4)
    streaming_seconds = (run["last"] - run["first"]) if run["first"] is not None else 0
    return {
        "phase": run["phase"],
        "serial": run["serial"],
        "tokens": tokens,
        "max_tokens": run["max_tokens"],
        "reasoning_chunks": run["reasoning"],
        "elapsed": round(now - run["started"], 1),
        "idle": round(now - run["last"], 1),
        "rate": round(tokens / streaming_seconds, 1) if streaming_seconds >= 2 else None,
    }


def decode_json_string(buffer: str, key: str) -> str | None:
    """Decode the string value of `key` from a JSON object that may still be streaming."""
    marker = '"' + key + '"'
    index = buffer.find(marker)
    if index < 0:
        return None
    size = len(buffer)
    index += len(marker)
    while index < size and buffer[index] in " \t\r\n":
        index += 1
    if index >= size or buffer[index] != ":":
        return ""
    index += 1
    while index < size and buffer[index] in " \t\r\n":
        index += 1
    if index >= size or buffer[index] != '"':
        return ""
    index += 1
    out: list[str] = []
    while index < size:
        char = buffer[index]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= size:
            break
        escape = buffer[index + 1]
        if escape in _ESCAPES:
            out.append(_ESCAPES[escape])
            index += 2
            continue
        if escape != "u" or index + 6 > size:
            break
        try:
            code = int(buffer[index + 2:index + 6], 16)
        except ValueError:
            break
        index += 6
        if 0xD800 <= code < 0xDC00:
            tail = buffer[index:index + 6]
            if len(tail) < 6:
                break
            try:
                low = int(tail[2:], 16) if tail.startswith("\\u") else 0
            except ValueError:
                low = 0
            if 0xDC00 <= low < 0xE000:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                index += 6
            else:
                code = 0xFFFD
        elif 0xDC00 <= code < 0xE000:
            code = 0xFFFD
        out.append(chr(code))
    return "".join(out)


def draft(operation_id: str, serial: int, offset: int) -> dict:
    run = _runs.get(operation_id)
    if not run:
        return {"active": False, "serial": 0, "length": 0, "text": ""}
    buffer = run["text"]
    decoded = decode_json_string(buffer, "html")
    if decoded is None:
        decoded = "" if buffer.lstrip().startswith("{") else buffer
    if run["serial"] != serial or offset > len(decoded):
        offset = 0
    return {
        "active": True,
        "serial": run["serial"],
        "length": len(decoded),
        "offset": offset,
        "text": decoded[offset:],
    }
