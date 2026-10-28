from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
import sqlite3
import zlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from studio import app as app_module
from studio.app import create_app
from studio.provider import ProviderError, ProviderSettings, chat_completion
from studio.assets import render_assets, validate_asset
from studio.checks import _format_overflow_error
from studio.store import connect, json_dump, now


ORIGIN = "http://testserver"
PROVIDER_URL = "https://provider.example/v1/chat/completions"
API_KEY = "test-key-must-never-be-returned"


def png_1x1(pixel: tuple[int, int, int, int] = (190, 85, 54, 255)) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    image_data = zlib.compress(bytes((0, *pixel)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", image_data)
        + chunk(b"IEND", b"")
    )


def glb_with_document(document: dict | None = None) -> bytes:
    document = document or {"asset": {"version": "2.0"}}
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((4 - len(encoded) % 4) % 4)
    chunk = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    application = create_app(data_dir=tmp_path / "studio-data")
    async def offline_browser_check(source):
        return {
            "status": "unavailable",
            "errors": ["Playwright is skipped by the offline unit-test fixture."],
            "desktop": None,
            "narrow": None,
            "browser": "chromium",
            "repair_attempted": False,
        }
    monkeypatch.setattr(app_module, "browser_check", offline_browser_check)
    with TestClient(application, base_url=ORIGIN) as test_client:
        yield test_client


def same_origin(**headers):
    return {"Origin": ORIGIN, **headers}


def configure_provider(
    client: TestClient,
    *,
    vision: bool = True,
    code_max_tokens: int = 12000,
    reasoning_effort: str = "",
    auto_repair: bool = True,
) -> None:
    response = client.put(
        "/api/settings",
        headers=same_origin(),
        json={
            "provider_url": PROVIDER_URL,
            "model": "offline-model",
            "api_key": API_KEY,
            "vision_supported": vision,
            "code_max_tokens": code_max_tokens,
            "reasoning_effort": reasoning_effort,
            "auto_repair": auto_repair,
        },
    )
    assert response.status_code == 200
    assert API_KEY not in response.text


def create_project(client: TestClient, brief: str = "A field guide to small discoveries") -> str:
    response = client.post(
        "/api/projects",
        headers=same_origin(),
        json={"brief": brief, "reference_intent": "inspire"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def directions_payload() -> str:
    cards = [
        {
            "id": "atlas-wall",
            "title": "The Atlas Wall",
            "thesis": "Make discovery feel visual and open.",
            "axis": "Composition · image-led",
            "benefit": "Many clear ways to begin.",
            "tradeoff": "A dense field takes scanning.",
            "behavior_assumption": "Opening a card is inferred and editable.",
        },
        {
            "id": "margin-index",
            "title": "The Margin Index",
            "thesis": "Give the material a measured editorial rhythm.",
            "axis": "Hierarchy · editorial",
            "benefit": "Details stay easy to compare.",
            "tradeoff": "Strong writing carries more weight.",
            "behavior_assumption": "Expanding an entry is inferred and editable.",
        },
        {
            "id": "signal-ledger",
            "title": "The Signal Ledger",
            "thesis": "Keep an overview compact and reveal depth on request.",
            "axis": "Interaction · progressive detail",
            "benefit": "The overview stays focused.",
            "tradeoff": "Visitors open items to see their character.",
            "behavior_assumption": "Opening a note is inferred and editable.",
        },
    ]
    return json.dumps({"directions": cards})


def artifact_html(label: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{label}</title>
<style>body{{font:16px Georgia;background:#f2eee5;color:#202b29;padding:3rem}}button{{padding:1rem}}</style></head>
<body><main><h1>{label}</h1><button id="open" type="button">Open note</button><p id="note" aria-live="polite">A saved field note.</p></main>
<script>document.querySelector("#open").addEventListener("click",()=>{{document.querySelector("#note").textContent="Note opened";}});</script>
</body></html>"""


def artifact_payload(html: str, requirements: list | None = None, assumptions: list | None = None) -> str:
    return json.dumps(
        {
            "html": html,
            "requirements": requirements or [],
            "assumptions": assumptions or [],
        }
    )


def install_provider_mock(monkeypatch, *, fail: str | None = None):
    calls: list[dict] = []

    async def fake_chat(config, messages, *, max_tokens, **_):
        system = messages[0]["content"]
        if "creative director" in system:
            suggestions = [
                {"kind": "refine", "title": "Sharpen the note", "change": "Make the note card easier to read."},
                {"kind": "expand", "title": "Add a second note", "change": "Add a second saved note below the first."},
                {"kind": "twist", "title": "Night mode", "change": "Turn the page into a night-time field guide."},
            ]
            return {"content": json.dumps({"suggestions": suggestions}), "model": config.model}
        calls.append({"config": config, "messages": messages, "max_tokens": max_tokens})
        if "exactly three materially different" in system:
            content = directions_payload()
        else:
            if fail:
                raise ProviderError(fail)
            user_content = messages[1]["content"]
            joined = (
                "\n".join(part.get("text", "") for part in user_content if isinstance(part, dict))
                if isinstance(user_content, list)
                else user_content
            )
            html = artifact_html("Refined version") if "Existing source:" in joined else artifact_html("First version")
            content = artifact_payload(html, assumptions=["The note opens in place."])
        return {"content": content, "model": config.model}

    monkeypatch.setattr(app_module, "chat_completion", fake_chat)
    return calls


def wait_for_operation(client: TestClient, operation_id: str) -> dict:
    for _ in range(300):
        response = client.get("/api/operations/" + operation_id)
        assert response.status_code == 200
        operation = response.json()
        if operation["status"] in {"succeeded", "failed", "interrupted"}:
            return operation
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def upload_reference(client: TestClient, project_id: str, data: bytes | None = None) -> bytes:
    data = data or png_1x1()
    response = client.put(
        f"/api/projects/{project_id}/reference",
        headers=same_origin(**{"Content-Type": "image/png"}),
        content=data,
    )
    assert response.status_code == 200, response.text
    return data


def test_sample_workflow_refines_immutably_and_reopens_without_generation(client, monkeypatch):
    calls = install_provider_mock(monkeypatch)
    configure_provider(client)
    project_id = create_project(client)
    reference = upload_reference(client, project_id)

    request_headers = same_origin(**{"Idempotency-Key": "directions-same-request"})
    first = client.post(
        f"/api/projects/{project_id}/directions",
        headers=request_headers,
        json={"steer": "Use a quietly tactile editorial tone"},
    ).json()
    duplicate = client.post(
        f"/api/projects/{project_id}/directions",
        headers=request_headers,
        json={"steer": "Use a quietly tactile editorial tone"},
    ).json()
    assert duplicate["operation_id"] == first["operation_id"]
    directions = wait_for_operation(client, first["operation_id"])
    assert directions["status"] == "succeeded"
    assert len(directions["result"]["directions"]) == 3
    assert "inferred" in directions["result"]["directions"][0]["behavior_assumption"]
    assert calls[0]["max_tokens"] == 1600
    vision_content = calls[0]["messages"][1]["content"]
    assert isinstance(vision_content, list)
    assert any(
        part.get("image_url", {}).get("url", "").startswith("data:image/png;base64,")
        for part in vision_content
    )
    assert base64.b64encode(reference).decode("ascii") in json.dumps(vision_content)
    assert len(calls) == 1

    selected = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "choose-atlas-once"}),
        json={
            "direction_operation_id": first["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "",
        },
    ).json()
    prototype = wait_for_operation(client, selected["operation_id"])
    assert prototype["status"] == "succeeded", prototype.get("error")
    revision_id = prototype["result"]["revision_id"]
    original = client.get(f"/api/revisions/{revision_id}").json()
    assert "First version" in original["source_html"]
    assert original["model"] == "offline-model"
    assert original["provider_url"] == PROVIDER_URL
    assert original["reference"]["sha256"] == client.get(
        f"/api/projects/{project_id}"
    ).json()["alternatives"][0]["revisions"][0]["reference_sha256"]
    assert calls[1]["max_tokens"] == 12000
    assert original["assumptions"] == ["The note opens in place."]
    assert original["checks"]["status"] == "unavailable"

    preview = client.get(f"/api/revisions/{revision_id}/preview")
    assert preview.status_code == 200
    assert "sandbox allow-scripts" in preview.headers["content-security-policy"]
    assert "possibility-studio-preview-error" in preview.text
    exported = client.get(f"/api/revisions/{revision_id}/export")
    assert exported.status_code == 200
    assert "attachment;" in exported.headers["content-disposition"]
    assert "possibility-studio-preview-error" not in exported.text

    refinement = client.post(
        f"/api/revisions/{revision_id}/refine",
        headers=same_origin(**{"Idempotency-Key": "refine-keep-history"}),
        json={"instruction": "Give the item a more tactile open state."},
    ).json()
    refined = wait_for_operation(client, refinement["operation_id"])
    assert refined["status"] == "succeeded", refined.get("error")
    revision2_id = refined["result"]["revision_id"]
    revision2 = client.get(f"/api/revisions/{revision2_id}").json()
    assert revision2["parent_revision_id"] == revision_id
    assert revision2["instruction"] == "Give the item a more tactile open state."
    assert "Refined version" in revision2["source_html"]
    assert "First version" in client.get(f"/api/revisions/{revision_id}").json()["source_html"]

    call_count = len(calls)
    reopened = client.get(f"/api/revisions/{revision_id}")
    assert reopened.status_code == 200
    assert len(calls) == call_count


def test_reference_replacement_preserves_the_version_cited_by_a_revision(client, monkeypatch):
    calls = install_provider_mock(monkeypatch)
    configure_provider(client)
    project_id = create_project(client)
    old_reference = upload_reference(client, project_id)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "reference-directions"}),
        json={"steer": ""},
    ).json()
    operation = wait_for_operation(client, directions["operation_id"])
    assert operation["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "reference-prototype"}),
        json={
            "direction_operation_id": directions["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "",
        },
    ).json()
    result = wait_for_operation(client, selection["operation_id"])
    assert result["status"] == "succeeded"
    revision_id = result["result"]["revision_id"]

    new_reference = png_1x1((30, 90, 120, 255))
    upload_reference(client, project_id, new_reference)
    old_snapshot = client.get(f"/api/revisions/{revision_id}/reference")
    current_image = client.get(f"/api/projects/{project_id}/reference")
    assert old_snapshot.content == old_reference
    assert current_image.content == new_reference
    assert len(calls) == 2


def test_missing_reference_snapshot_fails_without_a_model_call(client, tmp_path, monkeypatch):
    calls = install_provider_mock(monkeypatch)
    configure_provider(client)
    project_id = create_project(client)
    upload_reference(client, project_id)
    with connect(client.app.state.data_dir, immediate=True) as db:
        row = db.execute("SELECT reference_path FROM projects WHERE id=?", (project_id,)).fetchone()
    (client.app.state.data_dir / row["reference_path"]).unlink()
    request = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "missing-reference"}),
        json={"steer": ""},
    ).json()
    operation = wait_for_operation(client, request["operation_id"])
    assert operation["status"] == "failed"
    assert "reference snapshot is missing" in operation["error"]
    assert calls == []


def test_origin_policy_idempotency_conflicts_and_reference_validation(client):
    project_id = create_project(client)
    blocked = client.post(
        f"/api/projects/{project_id}/directions",
        json={"steer": ""},
    )
    opaque = client.post(
        f"/api/projects/{project_id}/directions",
        headers={"Origin": "null", "Idempotency-Key": "null-origin"},
        json={"steer": ""},
    )
    cross_site = client.post(
        f"/api/projects/{project_id}/directions",
        headers={"Origin": "https://elsewhere.example", "Idempotency-Key": "cross-site"},
        json={"steer": ""},
    )
    assert blocked.status_code == opaque.status_code == cross_site.status_code == 403

    invalid_image = client.put(
        f"/api/projects/{project_id}/reference",
        headers=same_origin(**{"Content-Type": "image/png"}),
        content=b"this is not a png",
    )
    assert invalid_image.status_code == 415

    first = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "identity-conflict"}),
        json={"steer": "quiet"},
    )
    changed = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "identity-conflict"}),
        json={"steer": "bold"},
    )
    assert first.status_code == 202
    assert changed.status_code == 409


def test_settings_redact_keys_from_reads_and_validation_errors(client):
    configure_provider(client)
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["key_configured"] is True
    assert settings.json()["code_max_tokens"] == 12000
    assert settings.json()["reasoning_effort"] == ""
    assert settings.json()["auto_repair"] is True
    assert API_KEY not in settings.text

    too_long_key = "SENSITIVE-" + ("x" * 5000)
    rejected = client.put(
        "/api/settings",
        headers=same_origin(),
        json={
            "provider_url": PROVIDER_URL,
            "model": "offline-model",
            "api_key": too_long_key,
            "vision_supported": False,
        },
    )
    assert rejected.status_code == 422
    assert too_long_key not in rejected.text
    assert API_KEY not in rejected.text
    assert "input" not in rejected.text


def test_provider_models_route_returns_popular_list_and_degrades_offline(client, monkeypatch):
    async def popular(limit):
        return ["one/model", "two/model"][:limit]

    async def unreachable(limit):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(app_module, "popular_openrouter_models", popular)
    assert client.get("/api/provider-models").json() == {
        "models": ["one/model", "two/model"], "source": "openrouter-popular",
    }
    monkeypatch.setattr(app_module, "popular_openrouter_models", unreachable)
    assert client.get("/api/provider-models").json() == {"models": [], "source": "unavailable"}


def test_generation_without_provider_fails_with_setup_message(client, monkeypatch):
    monkeypatch.delenv("STUDIO_PROVIDER_URL", raising=False)
    monkeypatch.delenv("STUDIO_MODEL", raising=False)
    project_id = create_project(client)
    started = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "no-provider-directions"}),
        json={"steer": ""},
    ).json()
    operation = wait_for_operation(client, started["operation_id"])
    assert operation["status"] == "failed"
    assert "No provider is configured" in operation["error"]
    assert operation["result"] is None


def test_settings_expose_openrouter_model_suggestions(client):
    presets = {preset["id"]: preset for preset in client.get("/api/settings").json()["presets"]}
    assert presets["openrouter"]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "z-ai/glm-5.3-flash" in presets["openrouter"]["models"]
    assert "deepseek/deepseek-v4.1-flash" in presets["openrouter"]["models"]
    assert presets["custom"]["models"] == []


def test_restart_marks_queued_operations_interrupted(client, tmp_path):
    project_id = create_project(client)
    root = client.app.state.data_dir
    operation_id = "queued-before-restart"
    with connect(root, immediate=True) as db:
        db.execute(
            """
            INSERT INTO operations(
                id,idempotency_key,fingerprint,project_id,kind,status,stage,request_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running','checking',?,?,?)
            """,
            (
                operation_id,
                "queued-before-restart-key",
                "test-fingerprint",
                project_id,
                "directions",
                json_dump({"project_id": project_id, "steer": ""}),
                now(),
                now(),
            ),
        )
    with TestClient(create_app(data_dir=root), base_url=ORIGIN) as restarted:
        operation = restarted.get("/api/operations/" + operation_id).json()
        assert operation["status"] == "interrupted"
        assert operation["stage"] == "checking"
        assert restarted.get("/api/health").json()["interrupted_on_start"] == 1


def test_retry_uses_frozen_instruction_and_cannot_queue_duplicate_roots(client, monkeypatch):
    calls = install_provider_mock(monkeypatch, fail="deliberate offline failure")
    configure_provider(client)
    project_id = create_project(client)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "retry-directions"}),
        json={"steer": ""},
    ).json()
    assert wait_for_operation(client, directions["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "retry-selection"}),
        json={
            "direction_operation_id": directions["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "Use rust-orange accents",
        },
    ).json()
    failed = wait_for_operation(client, selection["operation_id"])
    assert failed["status"] == "failed"
    assert "rust-orange" not in failed["error"]
    alt_id = selection["alternative_id"]

    retry_key = "retry-frozen-instruction"
    calls.clear()
    async def succeed(config, messages, *, max_tokens, **_):
        calls.append(messages)
        return {"content": artifact_payload(artifact_html("Recovered version")), "model": config.model}
    monkeypatch.setattr(app_module, "chat_completion", succeed)
    retry = client.post(
        f"/api/alternatives/{alt_id}/generate",
        headers=same_origin(**{"Idempotency-Key": retry_key}),
        json={},
    ).json()
    retry_op = wait_for_operation(client, retry["operation_id"])
    assert retry_op["status"] == "succeeded", retry_op.get("error")
    artifact_prompt = calls[0][1]["content"]
    assert "Additional user direction: Use rust-orange accents" in artifact_prompt

    same_retry = client.post(
        f"/api/alternatives/{alt_id}/generate",
        headers=same_origin(**{"Idempotency-Key": retry_key}),
        json={},
    )
    assert same_retry.json()["operation_id"] == retry["operation_id"]
    second_retry = client.post(
        f"/api/alternatives/{alt_id}/generate",
        headers=same_origin(**{"Idempotency-Key": "retry-after-success"}),
        json={},
    )
    assert second_retry.status_code == 409


def test_provider_transport_sets_bounds_and_rejects_truncation(monkeypatch):
    observed = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            observed.update({"url": url, "headers": headers, "json": json})
            return httpx.Response(200, json=
                {
                    "choices": [
                        {
                            "message": {"content": "done"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

    monkeypatch.setattr("studio.provider.httpx.AsyncClient", lambda **kwargs: FakeClient())
    config = ProviderSettings(PROVIDER_URL, "offline-model", API_KEY, True)
    response = asyncio.run(
        chat_completion(config, [{"role": "user", "content": "hello"}], max_tokens=42)
    )
    assert response["content"] == "done"
    assert observed["url"] == PROVIDER_URL
    assert observed["headers"]["Authorization"] == "Bearer " + API_KEY
    assert observed["json"]["max_tokens"] == 42
    assert "reasoning_effort" not in observed["json"]
    assert "reasoning" not in observed["json"]

    class TruncatedClient(FakeClient):
        async def post(self, url, *, headers, json):
            return httpx.Response(200, json=
                {
                    "choices": [
                        {
                            "message": {"content": ""},
                            "finish_reason": "length",
                        }
                    ]
                },
            )

    monkeypatch.setattr("studio.provider.httpx.AsyncClient", lambda **kwargs: TruncatedClient())
    with pytest.raises(ProviderError, match="output limit"):
        asyncio.run(
            chat_completion(config, [{"role": "user", "content": "hello"}], max_tokens=42)
        )

    router = ProviderSettings(
        "https://openrouter.ai/api/v1/chat/completions",
        "offline-model",
        API_KEY,
        True,
        reasoning_effort="high",
    )
    monkeypatch.setattr("studio.provider.httpx.AsyncClient", lambda **kwargs: FakeClient())
    asyncio.run(chat_completion(router, [{"role": "user", "content": "hello"}], max_tokens=42))
    assert observed["json"]["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in observed["json"]

    generic = ProviderSettings(
        PROVIDER_URL, "offline-model", API_KEY, True, reasoning_effort="medium"
    )
    asyncio.run(chat_completion(generic, [{"role": "user", "content": "hello"}], max_tokens=42))
    assert observed["json"]["reasoning_effort"] == "medium"
    assert "reasoning" not in observed["json"]


def test_json_schema_is_sent_and_dropped_once_when_the_provider_rejects_it(monkeypatch):
    sent: list[dict] = []
    statuses = [400, 200]

    class RejectingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            sent.append(dict(json))
            status = statuses.pop(0)
            if status != 200:
                return httpx.Response(status, json={"error": "unsupported"})
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
            )

    monkeypatch.setattr("studio.provider.httpx.AsyncClient", lambda **kwargs: RejectingClient())
    config = ProviderSettings(PROVIDER_URL, "offline-model", API_KEY, True)
    schema = {"name": "shape", "schema": {"type": "object"}}
    result = asyncio.run(
        chat_completion(config, [{"role": "user", "content": "x"}], max_tokens=42, json_schema=schema)
    )
    assert result["content"] == "{}"
    assert sent[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "shape", "strict": True, "schema": {"type": "object"}},
    }
    assert len(sent) == 2 and "response_format" not in sent[1]

    sent.clear()
    statuses[:] = [401]
    with pytest.raises(ProviderError, match="HTTP 401"):
        asyncio.run(
            chat_completion(config, [{"role": "user", "content": "x"}], max_tokens=42, json_schema=schema)
        )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "wrap",
    [
        lambda raw: "```json\n" + raw + "\n```",
        lambda raw: "Here are three directions:\n" + raw + "\nLet me know what you think.",
    ],
    ids=["fenced", "prose-wrapped"],
)
def test_directions_accept_fenced_and_prose_wrapped_json(client, monkeypatch, wrap):
    async def wrapped(config, messages, *, max_tokens, **_):
        return {"content": wrap(directions_payload()), "model": config.model}

    monkeypatch.setattr(app_module, "chat_completion", wrapped)
    configure_provider(client)
    project_id = create_project(client)
    started = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "tolerant-directions"}),
        json={"steer": ""},
    ).json()
    operation = wait_for_operation(client, started["operation_id"])
    assert operation["status"] == "succeeded", operation.get("error")
    assert len(operation["result"]["directions"]) == 3


def test_directions_failure_keeps_the_raw_reply_for_viewing(client, monkeypatch):
    raw = "I would rather describe the directions in prose than in JSON."

    async def prose(config, messages, *, max_tokens, **_):
        return {"content": raw, "model": config.model}

    monkeypatch.setattr(app_module, "chat_completion", prose)
    configure_provider(client)
    project_id = create_project(client)
    started = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "raw-kept-directions"}),
        json={"steer": ""},
    ).json()
    operation = wait_for_operation(client, started["operation_id"])
    assert operation["status"] == "failed"
    assert operation["has_partial"] is True
    assert client.get(f"/api/operations/{started['operation_id']}/partial").text == raw


def test_asset_upload_is_immutable_and_revision_export_embeds_its_frozen_snapshot(client, monkeypatch):
    configure_provider(client)
    project_id = create_project(client)
    first_bytes = png_1x1()
    uploaded = client.post(
        f"/api/projects/{project_id}/assets?name=../field-note.png",
        headers=same_origin(**{"Content-Type": "image/png"}),
        content=first_bytes,
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["name"] == "field-note.png"
    assert "path" not in asset and "sha256" not in asset

    calls = []

    async def with_asset(config, messages, *, max_tokens, **_):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        if "exactly three materially different" in messages[0]["content"]:
            content = directions_payload()
        else:
            html = artifact_html("Asset-backed note").replace(
                "<main>", f'<main><img src="studio-asset:{asset["id"]}" alt="Field colors">'
            )
            content = artifact_payload(html)
        return {"content": content, "model": config.model}

    monkeypatch.setattr(app_module, "chat_completion", with_asset)
    direction_request = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "asset-directions"}),
        json={"steer": "Use the uploaded image"},
    ).json()
    assert wait_for_operation(client, direction_request["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "asset-prototype"}),
        json={
            "direction_operation_id": direction_request["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "",
        },
    ).json()
    operation = wait_for_operation(client, selection["operation_id"])
    assert operation["status"] == "succeeded", operation.get("error")
    revision_id = operation["result"]["revision_id"]
    revision = client.get(f"/api/revisions/{revision_id}").json()
    assert revision["source_html"].find("studio-asset:" + asset["id"]) >= 0
    assert [item["id"] for item in revision["assets"]] == [asset["id"]]
    encoded_first = base64.b64encode(first_bytes).decode("ascii")
    for call in calls:
        assert encoded_first not in json.dumps(call["messages"])

    second_bytes = png_1x1((40, 120, 180, 255))
    second = client.post(
        f"/api/projects/{project_id}/assets?name=second.png",
        headers=same_origin(**{"Content-Type": "image/png"}),
        content=second_bytes,
    )
    assert second.status_code == 201
    assert len(client.get(f"/api/projects/{project_id}/assets").json()["assets"]) == 2

    preview = client.get(f"/api/revisions/{revision_id}/preview")
    exported = client.get(f"/api/revisions/{revision_id}/export")
    assert preview.status_code == exported.status_code == 200
    assert "data:image/png;base64," + encoded_first in preview.text
    assert "data:image/png;base64," + encoded_first in exported.text
    assert base64.b64encode(second_bytes).decode("ascii") not in preview.text


def test_missing_video_asset_is_reported_and_visible_without_a_fake_video(client, monkeypatch):
    configure_provider(client)
    project_id = create_project(client, "An editorial page with an immersive background-video.")
    async def generated_fake_video(config, messages, *, max_tokens, **_):
        if "exactly three materially different" in messages[0]["content"]:
            content = directions_payload()
        else:
            fake_video = '<video autoplay src="data:video/mp4;base64,ZmFrZQ=="></video>'
            content = artifact_payload(artifact_html("Static fallback").replace("</main>", fake_video + "</main>"))
        return {"content": content, "model": config.model}
    monkeypatch.setattr(app_module, "chat_completion", generated_fake_video)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "video-directions"}),
        json={"steer": ""},
    ).json()
    assert wait_for_operation(client, directions["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "video-prototype"}),
        json={
            "direction_operation_id": directions["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "",
        },
    ).json()
    operation = wait_for_operation(client, selection["operation_id"])
    assert operation["status"] == "succeeded"
    revision_id = operation["result"]["revision_id"]
    revision = client.get(f"/api/revisions/{revision_id}").json()
    assert any(item["status"] == "needs_asset" for item in revision["requirements"])
    assert "<video" not in revision["source_html"]
    preview = client.get(f"/api/revisions/{revision_id}/preview")
    assert "needs a compatible video asset" in preview.text
    assert "<video" not in preview.text


def test_explicit_feature_cues_respect_latest_removal_and_inherit_when_unspecified():
    text = "Build a three-dimensional room with background-video."
    assert app_module._feature_requests(text) == (True, True)
    assert app_module._feature_requests(
        text, "Remove the video and replace 3D with a flat CSS treatment."
    ) == (False, False)
    assert app_module._feature_requests(text, "Keep the palette calm.") == (True, True)
    assert app_module._feature_requests("A simple card index", "Add a WebGL object and video.") == (True, True)


def test_asset_signatures_accept_self_contained_glb_and_reject_external_glb():
    assert validate_asset("image/png", "small.png", png_1x1())[0] == "image/png"
    assert validate_asset("video/mp4", "sample.mp4", b"\x00\x00\x00\x18ftypisomrest")[0] == "video/mp4"
    assert validate_asset("video/webm", "sample.webm", b"\x1a\x45\xdf\xa3rest")[0] == "video/webm"
    good = glb_with_document()
    assert validate_asset("model/gltf-binary", "mesh.glb", good)[0] == "model/gltf-binary"
    external = glb_with_document({"asset": {"version": "2.0"}, "buffers": [{"uri": "mesh.bin"}]})
    with pytest.raises(ValueError, match="declared asset type"):
        validate_asset("model/gltf-binary", "mesh.glb", external)


def test_asset_renderer_injects_dictionary_before_code_and_rejects_unknown_ids(tmp_path):
    root = tmp_path / "asset-render"
    asset_id = "8b9b1111-2222-4333-8444-555555555555"
    relative = Path("assets") / (asset_id + ".png")
    target = root / relative
    target.parent.mkdir(parents=True)
    data = png_1x1()
    target.write_bytes(data)
    snapshot = [{
        "id": asset_id,
        "project_id": "p",
        "name": "field.png",
        "media_type": "image/png",
        "size": len(data),
        "path": relative.as_posix(),
        "sha256": __import__("hashlib").sha256(data).hexdigest(),
    }]
    source = (
        '<!doctype html><html><head><script>const image=STUDIO_ASSETS["'
        + asset_id
        + '"];</script></head><body></body></html>'
    )
    rendered = render_assets(root, source, snapshot, vendor_path=Path("missing-three.js"))
    assert rendered.index("window.STUDIO_ASSETS") < rendered.index("const image")
    assert "data:image/png;base64," + base64.b64encode(data).decode() in rendered
    with pytest.raises(ValueError, match="not in this revision"):
        render_assets(
            root,
            source.replace(asset_id, "9b9b1111-2222-4333-8444-555555555555"),
            snapshot,
            vendor_path=Path("missing-three.js"),
        )


def test_one_evidence_driven_repair_uses_configured_token_bound(client, monkeypatch):
    configure_provider(
        client, code_max_tokens=8400, reasoning_effort="low", auto_repair=True
    )
    project_id = create_project(client)
    calls = []

    async def repair_once(config, messages, *, max_tokens, **_):
        calls.append({"messages": messages, "max_tokens": max_tokens, "config": config})
        if "exactly three materially different" in messages[0]["content"]:
            return {"content": directions_payload(), "model": config.model}
        if len([call for call in calls if "exactly three materially different" not in call["messages"][0]["content"]]) == 1:
            return {"content": "not complete JSON", "model": config.model}
        return {"content": artifact_payload(artifact_html("Repaired")), "model": config.model}

    monkeypatch.setattr(app_module, "chat_completion", repair_once)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "repair-directions"}),
        json={"steer": ""},
    ).json()
    assert wait_for_operation(client, directions["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "repair-prototype"}),
        json={
            "direction_operation_id": directions["operation_id"],
            "direction_id": "atlas-wall",
            "additional_instruction": "",
        },
    ).json()
    operation = wait_for_operation(client, selection["operation_id"])
    assert operation["status"] == "succeeded", operation.get("error")
    assert sum(call["max_tokens"] == 8400 for call in calls) == 2
    revision = client.get(f"/api/revisions/{operation['result']['revision_id']}").json()
    assert revision["checks"]["repair_attempted"] is True
    assert revision["source_html"].find("Repaired") >= 0
    assert calls[-1]["config"].reasoning_effort == "low"


def test_additive_migration_keeps_old_projects_and_revisions_schema_usable(tmp_path):
    root = tmp_path / "legacy-data"
    root.mkdir()
    db = sqlite3.connect(root / "studio.sqlite3")
    db.executescript(
        """
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,
            reference_intent TEXT NOT NULL,reference_path TEXT,reference_type TEXT,
            reference_sha256 TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE operations(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL,project_id TEXT NOT NULL,alternative_id TEXT,kind TEXT NOT NULL,
            status TEXT NOT NULL,request_json TEXT NOT NULL,result_json TEXT,error TEXT,model TEXT,
            provider_url TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE alternatives(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,direction_json TEXT NOT NULL,
            status TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE revisions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,alternative_id TEXT NOT NULL,
            parent_revision_id TEXT,source_html TEXT NOT NULL,instruction TEXT NOT NULL,model TEXT,
            provider_url TEXT,reference_path TEXT,reference_type TEXT,reference_sha256 TEXT,
            operation_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
        """
    )
    db.close()
    from studio.store import init_db
    init_db(root)
    init_db(root)
    with sqlite3.connect(root / "studio.sqlite3") as migrated:
        operation_columns = {row[1] for row in migrated.execute("PRAGMA table_info(operations)")}
        revision_columns = {row[1] for row in migrated.execute("PRAGMA table_info(revisions)")}
        asset_table = migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone()
    assert "stage" in operation_columns
    assert {"assets_json", "requirements_json", "assumptions_json", "checks_json"} <= revision_columns
    assert asset_table


def test_feature_report_uses_source_evidence_and_does_not_contradict_model_report():
    checks = {
        "status": "unavailable",
        "desktop": None,
        "narrow": None,
        "errors": [],
    }
    report = app_module._feature_report(
        [{"request": "Build 3D", "status": "implemented", "detail": "Done."}],
        "<script>const renderer = new THREE.WebGLRenderer();</script>",
        [],
        checks,
        requested_3d=True,
        requested_video=False,
    )
    assert len(report) == 1
    assert report[0]["status"] == "implemented"
    assert "browser evidence is unavailable" in report[0]["detail"]


def test_overflow_error_includes_viewport_and_only_top_five_culprits():
    culprits = [
        {
            "selector": f"section.item-{index}",
            "width": 500 + index,
            "right": 510 + index,
            "left": 10,
            "text": "A clipped title",
        }
        for index in range(6)
    ]

    error = _format_overflow_error("narrow", 390, culprits)

    assert "narrow viewport (390px)" in error
    assert "section.item-0 (width=500px, right=510px, left=10px, text='A clipped title')" in error
    assert "section.item-4" in error
    assert "section.item-5" not in error


def test_report_parser_degrades_instead_of_rejecting():
    from studio.app import _parse_artifact_report, _REPORT_MISSING_NOTE

    html = "<!doctype html><html><body>x</body></html>"
    bad = json.dumps({"html": html, "requirements": [{"request": "a", "status": "bogus"}, "x"], "assumptions": "no"})
    parsed = _parse_artifact_report(bad)
    assert parsed["html"] == html and parsed["requirements"] == []
    assert _REPORT_MISSING_NOTE in parsed["assumptions"]

    fenced = "Here you go:\n```json\n" + json.dumps({"html": html, "requirements": [], "assumptions": []}) + "\n```"
    parsed = _parse_artifact_report(fenced)
    assert parsed["html"] == html and _REPORT_MISSING_NOTE not in parsed["assumptions"]

    raw = _parse_artifact_report("```html\n" + html + "\n```")
    assert raw["html"] == html and _REPORT_MISSING_NOTE in raw["assumptions"]

    try:
        _parse_artifact_report("sorry, no code")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_timeout_setting_and_cutoff_draft_are_kept(client, monkeypatch):
    seen = {}

    class Recorder:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "<!doctype html><html>cut"}, "finish_reason": "length"}
            ]})

    def make(**kwargs):
        seen.update(kwargs)
        return Recorder()

    monkeypatch.setattr("studio.provider.httpx.AsyncClient", make)
    config = ProviderSettings(PROVIDER_URL, "offline-model", API_KEY, True, request_timeout=240)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(chat_completion(config, [{"role": "user", "content": "x"}], max_tokens=42))
    assert seen["timeout"] == 240
    assert caught.value.partial == "<!doctype html><html>cut"

    bad = client.put(
        "/api/settings",
        headers=same_origin(),
        json={"provider_url": PROVIDER_URL, "model": "m", "vision_supported": True, "request_timeout": 5},
    )
    assert bad.status_code == 422
    configure_provider(client)
    assert client.get("/api/settings").json()["request_timeout"] == 180


def test_failed_operation_exposes_kept_raw_output(client, monkeypatch):
    async def cut_off(config, messages, *, max_tokens, **_):
        if "exactly three materially different" in messages[0]["content"]:
            return {"content": directions_payload(), "model": config.model}
        raise ProviderError("limit hit", "<!doctype html><html>partial draft")

    monkeypatch.setattr(app_module, "chat_completion", cut_off)
    configure_provider(client)
    project_id = create_project(client)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": "partial-directions"}),
        json={"steer": ""},
    ).json()
    assert wait_for_operation(client, directions["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": "partial-selection"}),
        json={"direction_operation_id": directions["operation_id"], "direction_id": "atlas-wall",
              "additional_instruction": ""},
    ).json()
    failed = wait_for_operation(client, selection["operation_id"])
    assert failed["status"] == "failed" and failed["has_partial"] is True
    raw = client.get("/api/operations/" + selection["operation_id"] + "/partial")
    assert raw.status_code == 200 and raw.text == "<!doctype html><html>partial draft"
    assert raw.headers["content-type"].startswith("text/plain")
    assert client.get("/api/operations/" + directions["operation_id"] + "/partial").status_code == 404


def build_revision(client, key: str) -> str:
    project_id = create_project(client)
    directions = client.post(
        f"/api/projects/{project_id}/directions",
        headers=same_origin(**{"Idempotency-Key": key + "-directions"}),
        json={"steer": ""},
    ).json()
    assert wait_for_operation(client, directions["operation_id"])["status"] == "succeeded"
    selection = client.post(
        f"/api/projects/{project_id}/alternatives",
        headers=same_origin(**{"Idempotency-Key": key + "-select"}),
        json={"direction_operation_id": directions["operation_id"], "direction_id": "atlas-wall",
              "additional_instruction": ""},
    ).json()
    built = wait_for_operation(client, selection["operation_id"])
    assert built["status"] == "succeeded", built.get("error")
    return built["result"]["revision_id"]


def test_preview_carries_the_canvas_inspector_and_export_does_not(client, monkeypatch):
    install_provider_mock(monkeypatch)
    configure_provider(client)
    revision_id = build_revision(client, "inspector")
    preview = client.get(f"/api/revisions/{revision_id}/preview").text
    assert "possibility-studio-pick" in preview
    assert "possibility-studio-inspect" in preview
    assert r'.replace(/\s+/g, " ")' in preview
    exported = client.get(f"/api/revisions/{revision_id}/export").text
    assert "possibility-studio-pick" not in exported


def test_refine_sends_picked_parts_to_the_model_and_limits_them(client, monkeypatch):
    calls = install_provider_mock(monkeypatch)
    configure_provider(client)
    revision_id = build_revision(client, "targets")
    assert "data-component" in calls[1]["messages"][0]["content"]

    target = {"name": "note-card", "tag": "p", "path": "body > main > p#note", "text": "A saved field note."}
    response = client.post(
        f"/api/revisions/{revision_id}/refine",
        headers=same_origin(**{"Idempotency-Key": "refine-targets"}),
        json={"instruction": "Make this softer.", "targets": [target]},
    )
    assert response.status_code == 202
    assert wait_for_operation(client, response.json()["operation_id"])["status"] == "succeeded"
    user_text = calls[-1]["messages"][1]["content"]
    assert "- note-card | <p> | \"A saved field note.\" | body > main > p#note" in user_text
    assert "leave every other part unchanged" in user_text

    too_many = client.post(
        f"/api/revisions/{revision_id}/refine",
        headers=same_origin(**{"Idempotency-Key": "refine-too-many"}),
        json={"instruction": "Make this softer.", "targets": [target] * 6},
    )
    assert too_many.status_code == 422
    without = client.post(
        f"/api/revisions/{revision_id}/refine",
        headers=same_origin(**{"Idempotency-Key": "refine-no-targets"}),
        json={"instruction": "Make this softer."},
    )
    assert wait_for_operation(client, without.json()["operation_id"])["status"] == "succeeded"
    assert "picked these parts" not in calls[-1]["messages"][1]["content"]


def wait_for_suggestions(client, revision_id: str) -> dict:
    for _ in range(300):
        revision = client.get("/api/revisions/" + revision_id).json()
        if revision["suggestions_status"] != "pending":
            return revision
        time.sleep(0.01)
    raise AssertionError("suggestions did not finish")


def test_each_saved_version_gets_three_saved_suggestions(client, monkeypatch):
    install_provider_mock(monkeypatch)
    configure_provider(client)
    revision_id = build_revision(client, "suggest")
    revision = wait_for_suggestions(client, revision_id)
    assert revision["suggestions_status"] == "ready"
    assert [item["kind"] for item in revision["suggestions"]] == ["refine", "expand", "twist"]
    refined = client.post(
        f"/api/revisions/{revision_id}/refine",
        headers=same_origin(**{"Idempotency-Key": "suggest-refine"}),
        json={"instruction": revision["suggestions"][1]["change"]},
    ).json()
    child_id = wait_for_operation(client, refined["operation_id"])["result"]["revision_id"]
    assert wait_for_suggestions(client, child_id)["suggestions_status"] == "ready"
    assert client.get("/api/revisions/" + revision_id).json()["suggestions"] == revision["suggestions"]


def test_failed_suggestions_can_be_requested_again(client, monkeypatch):
    install_provider_mock(monkeypatch)
    configure_provider(client)
    revision_id = build_revision(client, "suggest-retry")
    wait_for_suggestions(client, revision_id)

    async def broken(config, messages, **_):
        raise ProviderError("The provider is down.")

    monkeypatch.setattr(app_module, "chat_completion", broken)
    assert client.post(f"/api/revisions/{revision_id}/suggestions", headers=same_origin()).status_code == 202
    failed = wait_for_suggestions(client, revision_id)
    assert failed["suggestions_status"] == "failed"
    assert failed["suggestions_error"] == "The provider is down."
    assert failed["suggestions"] == []
    install_provider_mock(monkeypatch)
    assert client.post(f"/api/revisions/{revision_id}/suggestions", headers=same_origin()).status_code == 202
    assert wait_for_suggestions(client, revision_id)["suggestions_status"] == "ready"
    assert client.post("/api/revisions/missing/suggestions", headers=same_origin()).status_code == 404
