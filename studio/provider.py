from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .store import connect


class ProviderError(Exception):
    """A provider failure safe to show in the workbench."""

    def __init__(self, message: str, partial: str | None = None):
        super().__init__(message)
        self.partial = partial


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENCODE_GO_URL = "https://opencode.ai/inference/go/openai/v1/chat/completions"
COMMAND_CODE_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
CLINE_PASS_URL = "https://api.cline.bot/api/v1/chat/completions"
OPENCODE_SESSION = uuid.uuid4().hex
POPULAR_TTL_SECONDS = 3600
_popular_cache: dict = {"at": 0.0, "ids": []}

PROVIDER_PRESETS = (
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "url": OPENROUTER_URL,
        "models": [
            "google/gemini-3.1-flash-lite",
            "z-ai/glm-5.3-flash",
            "deepseek/deepseek-v4.1-flash",
        ],
    },
    {
        "id": "opencode-go",
        "label": "OpenCode Go",
        "url": OPENCODE_GO_URL,
        "models": [
            "glm-5.3",
            "glm-5.3-flash",
            "kimi-k3",
            "kimi-k2.7-code",
            "deepseek-v4-pro",
            "deepseek-v4.1-flash",
            "mimo-v2.6-pro",
            "longcat-2.0",
            "hy3",
        ],
    },
    {
        "id": "command-code",
        "label": "Command Code (GOAT)",
        "url": COMMAND_CODE_URL,
        "models": ["deepseek/deepseek-v4-flash"],
    },
    {
        "id": "cline-pass",
        "label": "Cline Pass",
        "url": CLINE_PASS_URL,
        "models": [
            "cline-pass/glm-5.2",
            "cline-pass/kimi-k3",
            "cline-pass/kimi-k2.7-code",
            "cline-pass/deepseek-v4-pro",
            "cline-pass/deepseek-v4-flash",
            "cline-pass/mimo-v2.5-pro",
            "cline-pass/minimax-m3",
            "cline-pass/qwen3.7-max",
        ],
    },
    {"id": "custom", "label": "Custom endpoint", "url": "", "models": []},
)


@dataclass(frozen=True)
class ProviderSettings:
    provider_url: str
    model: str
    api_key: str
    vision_supported: bool
    code_max_tokens: int = 12000
    reasoning_effort: str = ""
    auto_repair: bool = True
    request_timeout: int = 180

    @property
    def ready(self) -> bool:
        return bool(self.provider_url and self.model)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _valid_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and bool(parsed.path)
        )
    except ValueError:
        return False


def settings(root) -> ProviderSettings:
    stored: dict[str, str] = {}
    with connect(root) as db:
        for row in db.execute("SELECT key, value FROM settings"):
            stored[row["key"]] = row["value"]
    url = stored.get("provider_url", os.environ.get("STUDIO_PROVIDER_URL", "")).strip()
    model = stored.get("model", os.environ.get("STUDIO_MODEL", "")).strip()
    key = stored.get("api_key", os.environ.get("STUDIO_API_KEY", "")).strip()
    vision = stored.get("vision_supported")
    if vision is None:
        vision_enabled = _env_bool("STUDIO_VISION_SUPPORTED")
    else:
        vision_enabled = vision == "true"
    token_value = stored.get("code_max_tokens", os.environ.get("STUDIO_CODE_MAX_TOKENS", "12000"))
    try:
        code_max_tokens = int(token_value)
    except (ValueError, TypeError):
        code_max_tokens = 12000
    if not 2000 <= code_max_tokens <= 128000:
        code_max_tokens = 12000
    reasoning_effort = stored.get(
        "reasoning_effort", os.environ.get("STUDIO_REASONING_EFFORT", "")
    ).strip().lower()
    if reasoning_effort not in {"", "low", "medium", "high", "max"}:
        reasoning_effort = ""
    try:
        request_timeout = int(stored.get("request_timeout", os.environ.get("STUDIO_REQUEST_TIMEOUT", "180")))
    except (ValueError, TypeError):
        request_timeout = 180
    if not 30 <= request_timeout <= 3600:
        request_timeout = 180
    stored_repair = stored.get("auto_repair")
    env_repair = _env_optional_bool("STUDIO_AUTO_REPAIR")
    auto_repair = (
        stored_repair == "true"
        if stored_repair is not None
        else True if env_repair is None else env_repair
    )
    if not _valid_url(url):
        url = ""
    if len(model) > 200:
        model = ""
    return ProviderSettings(
        url, model, key, vision_enabled, code_max_tokens, reasoning_effort, auto_repair,
        request_timeout,
    )


def validate_settings(
    provider_url: str,
    model: str,
    api_key: str | None,
    vision_supported: bool,
    code_max_tokens: int = 12000,
    reasoning_effort: str = "",
    auto_repair: bool = True,
    request_timeout: int = 180,
) -> None:
    if not _valid_url(provider_url.strip()):
        raise ValueError("Use a direct HTTP(S) chat-completions URL without credentials or query parameters.")
    if not model.strip() or len(model.strip()) > 200:
        raise ValueError("Enter a model name of 1 to 200 characters.")
    if api_key is not None and (len(api_key) > 4096 or "\r" in api_key or "\n" in api_key):
        raise ValueError("The API key is too long or contains invalid line breaks.")
    if not isinstance(vision_supported, bool):
        raise ValueError("Vision support must be selected explicitly.")
    if not isinstance(code_max_tokens, int) or isinstance(code_max_tokens, bool) or not 2000 <= code_max_tokens <= 128000:
        raise ValueError("Coding output tokens must be between 2000 and 128000.")
    if reasoning_effort not in {"", "low", "medium", "high", "max"}:
        raise ValueError("Choose an available reasoning effort or leave it empty.")
    if not isinstance(auto_repair, bool):
        raise ValueError("Automatic repair must be selected explicitly.")
    if not isinstance(request_timeout, int) or isinstance(request_timeout, bool) or not 30 <= request_timeout <= 3600:
        raise ValueError("Request timeout must be between 30 and 3600 seconds.")


async def _post(config: ProviderSettings, headers: dict, payload: dict) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=config.request_timeout) as client:
            return await client.post(config.provider_url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise ProviderError(
            "The provider request timed out after " + str(config.request_timeout)
            + " seconds. Raise the request timeout in Settings or choose a faster model."
        ) from exc
    except httpx.RequestError as exc:
        raise ProviderError("The provider could not be reached. Check the URL and network.") from exc


def _completion_parts(payload) -> tuple[object, object]:
    try:
        choice = payload["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ProviderError("The provider response did not contain a chat message.") from exc


async def _post_stream(config: ProviderSettings, headers: dict, payload: dict, on_delta) -> tuple[int, object, object]:
    pieces: list[str] = []
    finish_reason = None

    def partial() -> str | None:
        text = "".join(pieces)
        return text if text.strip() else None

    try:
        async with httpx.AsyncClient(timeout=config.request_timeout) as client:
            async with client.stream(
                "POST", config.provider_url, headers=headers, json=dict(payload, stream=True)
            ) as response:
                if response.status_code >= 400:
                    return response.status_code, "", None
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    try:
                        content, finish_reason = _completion_parts(json.loads(await response.aread()))
                    except ValueError as exc:
                        raise ProviderError("The provider returned a response that was not JSON.") from exc
                    if isinstance(content, str):
                        on_delta(content)
                    return response.status_code, content, finish_reason
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        pieces.append(piece)
                        on_delta(piece)
                    elif delta.get("reasoning_content") or delta.get("reasoning"):
                        on_delta("", reasoning=True)
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
    except httpx.TimeoutException as exc:
        raise ProviderError(
            "The provider stopped sending data for " + str(config.request_timeout)
            + " seconds. Raise the request timeout in Settings or choose a faster model.",
            partial(),
        ) from exc
    except httpx.RequestError as exc:
        raise ProviderError("The provider connection dropped before the reply finished.", partial()) from exc
    return 200, "".join(pieces), finish_reason


async def chat_completion(
    config: ProviderSettings,
    messages: list[dict],
    *,
    max_tokens: int,
    json_schema: dict | None = None,
    on_delta=None,
) -> dict:
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = "Bearer " + config.api_key
    if (urlsplit(config.provider_url).hostname or "").lower() == "opencode.ai":
        headers["x-opencode-session"] = OPENCODE_SESSION
        headers["User-Agent"] = "possibility-studio"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }
    if config.reasoning_effort:
        host = urlsplit(config.provider_url).hostname or ""
        if host.lower() == "openrouter.ai" or host.lower().endswith(".openrouter.ai"):
            payload["reasoning"] = {"effort": config.reasoning_effort}
        else:
            payload["reasoning_effort"] = config.reasoning_effort
    if json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": json_schema["name"], "strict": True, "schema": json_schema["schema"]},
        }
    if on_delta:
        status, content, finish_reason = await _post_stream(config, headers, payload, on_delta)
        if json_schema and status in {400, 404, 422}:
            payload.pop("response_format")
            status, content, finish_reason = await _post_stream(config, headers, payload, on_delta)
        if status >= 400:
            raise ProviderError("The provider returned HTTP " + str(status) + ".")
    else:
        response = await _post(config, headers, payload)
        if json_schema and response.status_code in {400, 404, 422}:
            payload.pop("response_format")
            response = await _post(config, headers, payload)
        if response.status_code >= 400:
            raise ProviderError("The provider returned HTTP " + str(response.status_code) + ".")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("The provider returned a response that was not JSON.") from exc
        content, finish_reason = _completion_parts(payload)
    if finish_reason == "length":
        partial = content if isinstance(content, str) and content.strip() else None
        raise ProviderError(
            "The provider stopped at its output limit (" + str(max_tokens)
            + " tokens). Raise the output tokens in Settings."
            + (" The cut-off draft is kept and can be viewed, but it was not saved as a version." if partial else ""),
            partial,
        )
    if isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        content = "\n".join(text_parts)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("The provider returned an empty chat message.")
    return {"content": content, "model": config.model}


async def popular_openrouter_models(limit: int) -> list[str]:
    """Most-used OpenRouter model IDs, free variants excluded, cached for an hour."""
    if _popular_cache["ids"] and time.monotonic() - _popular_cache["at"] < POPULAR_TTL_SECONDS:
        return _popular_cache["ids"][:limit]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            OPENROUTER_MODELS_URL, params={"sort": "most-popular", "limit": "40"}
        )
    response.raise_for_status()
    ids = [
        item["id"] for item in response.json()["data"]
        if not item["id"].endswith(":free")
    ]
    _popular_cache.update(at=time.monotonic(), ids=ids)
    return ids[:limit]

