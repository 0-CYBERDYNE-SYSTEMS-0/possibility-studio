from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import progress as live
from .artifact import PREVIEW_CSP, preview_html, validate_html
from .assets import (
    MAX_ASSET_BYTES,
    MAX_PROJECT_ASSETS_BYTES,
    ASSET_TYPES,
    asset_snapshot,
    clean_asset_name,
    public_assets,
    render_assets,
    validate_asset,
)
from .checks import browser_check
from .provider import (
    PROVIDER_PRESETS,
    ProviderError,
    chat_completion,
    popular_openrouter_models,
    settings as provider_settings,
    validate_settings,
)
from .store import connect, data_path, init_db, json_dump, json_load, mark_unfinished_interrupted, now


MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_ACTIVE_OPERATIONS = 4
MAX_BRIEF_LENGTH = 5000
MAX_STEER_LENGTH = 1000
MAX_INSTRUCTION_LENGTH = 2000
STATIC_DIR = Path(__file__).parent / "static"

_DIRECTION_FIELDS = ("title", "thesis", "axis", "benefit", "tradeoff", "behavior_assumption")
DIRECTIONS_FORMAT = {
    "name": "directions",
    "schema": {
        "type": "object",
        "properties": {
            "directions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, **{f: {"type": "string"} for f in _DIRECTION_FIELDS}},
                    "required": ["id", *_DIRECTION_FIELDS],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["directions"],
        "additionalProperties": False,
    },
}
ARTIFACT_FORMAT = {
    "name": "prototype",
    "schema": {
        "type": "object",
        "properties": {
            "html": {"type": "string"},
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string"},
                        "status": {"type": "string", "enum": ["implemented", "needs_asset", "unsupported"]},
                        "detail": {"type": "string"},
                    },
                    "required": ["request", "status", "detail"],
                    "additionalProperties": False,
                },
            },
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["html", "requirements", "assumptions"],
        "additionalProperties": False,
    },
}
SUGGESTION_KINDS = ("refine", "expand", "twist")
MAX_SUGGEST_SOURCE = 120_000
SUGGESTIONS_FORMAT = {
    "name": "next_steps",
    "schema": {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(SUGGESTION_KINDS)},
                        "title": {"type": "string"},
                        "change": {"type": "string"},
                    },
                    "required": ["kind", "title", "change"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["suggestions"],
        "additionalProperties": False,
    },
}
IMAGE_TYPES = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/webp": (".webp", b"RIFF"),
}


class ProjectInput(BaseModel):
    brief: str = Field(min_length=1, max_length=MAX_BRIEF_LENGTH)
    title: str | None = Field(default=None, max_length=100)
    reference_intent: Literal["inspire", "reconstruct"] = "inspire"


class DirectionsInput(BaseModel):
    steer: str = Field(default="", max_length=MAX_STEER_LENGTH)


class SelectionInput(BaseModel):
    direction_operation_id: str = Field(min_length=1, max_length=100)
    direction_id: str = Field(min_length=1, max_length=100)
    additional_instruction: str = Field(default="", max_length=MAX_INSTRUCTION_LENGTH)


class RefinementTarget(BaseModel):
    name: str = Field(default="", max_length=100)
    tag: str = Field(default="", max_length=30)
    path: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=120)


class RefinementInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_LENGTH)
    targets: list[RefinementTarget] = Field(default_factory=list, max_length=5)


class SettingsInput(BaseModel):
    provider_url: str = Field(max_length=1000)
    model: str = Field(max_length=200)
    vision_supported: bool
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    code_max_tokens: int = Field(default=12000, ge=2000, le=128000)
    reasoning_effort: Literal["", "low", "medium", "high", "max"] = ""
    auto_repair: bool = True
    request_timeout: int = Field(default=180, ge=30, le=3600)


def _same_origin(origin: str, request: Request) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        target = urlsplit(str(request.base_url))
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target_port = target.port or (443 if target.scheme == "https" else 80)
        return (
            parsed.scheme.lower() == target.scheme.lower()
            and parsed.hostname.lower() == (target.hostname or "").lower()
            and origin_port == target_port
        )
    except ValueError:
        return False


def _response_headers(response: Response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )


def _now_stamp() -> str:
    return now()


def _brief_title(brief: str) -> str:
    line = next((part.strip() for part in brief.splitlines() if part.strip()), "")
    return (line[:100] or "Untitled exploration")


def _get_project(root: Path, project_id: str) -> dict[str, Any] | None:
    with connect(root) as db:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _get_revision(root: Path, revision_id: str) -> dict[str, Any] | None:
    with connect(root) as db:
        row = db.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    return dict(row) if row else None


def _operation_public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "alternative_id": row["alternative_id"],
        "status": row["status"],
        "stage": row["stage"],
        "error": row["error"],
        "model": row["model"],
        "provider_url": row["provider_url"],
        "result": json_load(row["result_json"]),
        "has_partial": bool(row["partial_output"]),
        "progress": live.snapshot(row["id"]) if row["status"] == "running" else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _set_operation_stage(root: Path, operation_id: str, stage: str | None) -> None:
    with connect(root, immediate=True) as db:
        db.execute(
            "UPDATE operations SET stage=?,updated_at=? WHERE id=? AND status='running'",
            (stage, _now_stamp(), operation_id),
        )


def _fingerprint(project_id: str, kind: str, request_data: dict[str, Any]) -> str:
    public_request = {key: value for key, value in request_data.items() if not key.startswith("_")}
    packed = json.dumps(
        {"project_id": project_id, "kind": kind, "request": public_request},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _reference_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "reference_path": record.get("reference_path"),
        "reference_type": record.get("reference_type"),
        "reference_sha256": record.get("reference_sha256"),
    }


def _with_reference_snapshot(
    project: dict[str, Any], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    result = dict(project)
    for field, value in (snapshot or {}).items():
        if field in {"reference_path", "reference_type", "reference_sha256"}:
            result[field] = value
    return result


def _validate_idempotency_key(value: str | None) -> str:
    if not value or len(value) > 120 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise HTTPException(400, "Send a valid Idempotency-Key header for this operation.")
    return value


def _create_operation(
    db,
    *,
    operation_id: str,
    idempotency_key: str,
    fingerprint: str,
    project_id: str,
    alternative_id: str | None,
    kind: str,
    request_data: dict[str, Any],
) -> tuple[str, bool]:
    previous = db.execute(
        "SELECT id, fingerprint FROM operations WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise HTTPException(409, "This Idempotency-Key was already used for a different request.")
        return previous["id"], True
    pending = db.execute(
        "SELECT COUNT(*) FROM operations WHERE status IN ('queued', 'running')"
    ).fetchone()[0]
    if pending >= MAX_ACTIVE_OPERATIONS:
        raise HTTPException(429, "The studio already has several operations in progress. Try again shortly.")
    stamp = _now_stamp()
    db.execute(
        """
        INSERT INTO operations (
            id, idempotency_key, fingerprint, project_id, alternative_id, kind,
            status, request_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
        """,
        (
            operation_id,
            idempotency_key,
            fingerprint,
            project_id,
            alternative_id,
            kind,
            json_dump(request_data),
            stamp,
            stamp,
        ),
    )
    return operation_id, False


def _launch(app: FastAPI, operation_id: str) -> None:
    task = asyncio.create_task(_run_operation(app, operation_id))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def _run_operation(app: FastAPI, operation_id: str) -> None:
    root: Path = app.state.data_dir
    try:
        async with app.state.generation_lock:
            with connect(root, immediate=True) as db:
                operation = db.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                if not operation or operation["status"] != "queued":
                    return
                stamp = _now_stamp()
                config = provider_settings(root)
                model = config.model
                provider_url = config.provider_url
                db.execute(
                    "UPDATE operations SET status='running',stage='generating',model=?,provider_url=?,updated_at=? WHERE id=?",
                    (model, provider_url, stamp, operation_id),
                )
                request_data = json_load(operation["request_json"])
                kind = operation["kind"]
                project_id = operation["project_id"]
                alternative_id = operation["alternative_id"]

            if not config.ready:
                raise ProviderError(
                    "No provider is configured. Open Provider settings, choose a provider and model, then try again."
                )

            if kind == "directions":
                result, model_name = await _generate_directions(root, config, request_data)
                with connect(root, immediate=True) as db:
                    db.execute(
                        """
                        UPDATE operations
                        SET status = 'succeeded', stage=NULL, result_json = ?, model = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(result), model_name, _now_stamp(), operation_id),
                    )
                return

            if kind == "prototype":
                project = _get_project(root, project_id)
                alternative = None
                with connect(root) as db:
                    row = db.execute(
                        "SELECT * FROM alternatives WHERE id = ?", (alternative_id,)
                    ).fetchone()
                    alternative = dict(row) if row else None
                if not project or not alternative:
                    raise RuntimeError("The saved project or direction could not be found.")
                project = _with_reference_snapshot(project, request_data.get("_reference"))
                direction = json_load(alternative["direction_json"])
                instruction = request_data.get("additional_instruction", "")
                with connect(root) as db:
                    has_root = db.execute(
                        "SELECT 1 FROM revisions WHERE alternative_id=? AND parent_revision_id IS NULL LIMIT 1",
                        (alternative_id,),
                    ).fetchone()
                if has_root:
                    raise RuntimeError("This alternative already has a saved prototype.")
                outcome, model_name = await _generate_artifact(
                    root,
                    config,
                    project,
                    direction,
                    instruction,
                    mode="prototype",
                    assets=request_data.get("_assets") or asset_snapshot(root, project_id),
                    operation_id=operation_id,
                )
                _set_operation_stage(root, operation_id, "saving")
                revision_id = _publish_revision(
                    root,
                    operation_id=operation_id,
                    project_id=project_id,
                    alternative_id=alternative_id,
                    parent_revision_id=None,
                    instruction=instruction,
                    model=model_name,
                    provider_url=provider_url,
                    reference=_reference_snapshot(project),
                    assets=outcome["assets"],
                    requirements=outcome["requirements"],
                    assumptions=outcome["assumptions"],
                    checks=outcome["checks"],
                    source_html=outcome["source_html"],
                )
                _launch_suggestions(app, revision_id)
                return

            if kind == "refine":
                parent_id = request_data["parent_revision_id"]
                project = _get_project(root, project_id)
                parent = _get_revision(root, parent_id)
                if not project or not parent:
                    raise RuntimeError("The saved project or parent revision could not be found.")
                project = _with_reference_snapshot(project, _reference_snapshot(parent))
                alternative = json_load(
                    _alternative_direction(root, parent["alternative_id"])
                )
                instruction = request_data["instruction"]
                outcome, model_name = await _generate_artifact(
                    root,
                    config,
                    project,
                    alternative,
                    instruction,
                    mode="refine",
                    targets=request_data.get("targets") or [],
                    parent_html=parent["source_html"],
                    assets=request_data.get("_assets") or asset_snapshot(root, project_id),
                    operation_id=operation_id,
                )
                _set_operation_stage(root, operation_id, "saving")
                revision_id = _publish_revision(
                    root,
                    operation_id=operation_id,
                    project_id=project_id,
                    alternative_id=parent["alternative_id"],
                    parent_revision_id=parent_id,
                    instruction=instruction,
                    model=model_name,
                    provider_url=provider_url,
                    reference=_reference_snapshot(parent),
                    assets=outcome["assets"],
                    requirements=outcome["requirements"],
                    assumptions=outcome["assumptions"],
                    checks=outcome["checks"],
                    source_html=outcome["source_html"],
                )
                _launch_suggestions(app, revision_id)
                return
            raise RuntimeError("Unknown operation type.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "The operation failed unexpectedly."
        with connect(root, immediate=True) as db:
            row = db.execute(
                "SELECT alternative_id, status FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row and row["status"] in {"queued", "running"}:
                partial = getattr(exc, "partial", None)
                db.execute(
                    "UPDATE operations SET status = 'failed', stage=NULL, error = ?, partial_output = ?, updated_at = ? WHERE id = ?",
                    (message[:600], partial[:400_000] if isinstance(partial, str) and partial.strip() else None,
                     _now_stamp(), operation_id),
                )
                if row["alternative_id"]:
                    db.execute(
                        "UPDATE alternatives SET status = 'failed' WHERE id = ?",
                        (row["alternative_id"],),
                    )
    finally:
        live.finish(operation_id)


def _alternative_direction(root: Path, alternative_id: str) -> str | None:
    with connect(root) as db:
        row = db.execute(
            "SELECT direction_json FROM alternatives WHERE id = ?", (alternative_id,)
        ).fetchone()
    return row["direction_json"] if row else None


def _publish_revision(
    root: Path,
    *,
    operation_id: str,
    project_id: str,
    alternative_id: str,
    parent_revision_id: str | None,
    source_html: str,
    instruction: str,
    model: str,
    provider_url: str,
    reference: dict[str, Any] | None = None,
    assets: list[dict[str, Any]] | None = None,
    requirements: list[dict[str, Any]] | None = None,
    assumptions: list[str] | None = None,
    checks: dict[str, Any] | None = None,
) -> str:
    revision_id = str(uuid.uuid4())
    stamp = _now_stamp()
    with connect(root, immediate=True) as db:
        if parent_revision_id is None:
            existing = db.execute(
                "SELECT 1 FROM revisions WHERE alternative_id=? AND parent_revision_id IS NULL LIMIT 1",
                (alternative_id,),
            ).fetchone()
            if existing:
                raise RuntimeError("This alternative already has a saved prototype.")
        db.execute(
            """
            INSERT INTO revisions (
                id, project_id, alternative_id, parent_revision_id, source_html,
                instruction, model, provider_url, reference_path, reference_type, reference_sha256,
                assets_json, requirements_json, assumptions_json, checks_json,
                operation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                project_id,
                alternative_id,
                parent_revision_id,
                source_html,
                instruction,
                model,
                provider_url,
                (reference or {}).get("reference_path"),
                (reference or {}).get("reference_type"),
                (reference or {}).get("reference_sha256"),
                json_dump(assets or []),
                json_dump(requirements or []),
                json_dump(assumptions or []),
                json_dump(checks or {}),
                operation_id,
                stamp,
            ),
        )
        db.execute(
            """
            UPDATE operations
                SET status='succeeded',stage=NULL,result_json=?,model=?,provider_url=?,updated_at=?
            WHERE id = ?
            """,
            (json_dump({"revision_id": revision_id}), model, provider_url, stamp, operation_id),
        )
        db.execute(
            "UPDATE alternatives SET status = 'ready' WHERE id = ?",
            (alternative_id,),
        )
        db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (stamp, project_id))
        db.execute("UPDATE revisions SET suggestions_status = 'pending' WHERE id = ?", (revision_id,))
    return revision_id


async def _generate_suggestions(root: Path, config, revision: dict) -> list[dict[str, str]]:
    project = _get_project(root, revision["project_id"])
    if not project:
        raise RuntimeError("The saved project could not be found.")
    direction = json_load(_alternative_direction(root, revision["alternative_id"])) or {}
    source = revision["source_html"]
    if len(source) > MAX_SUGGEST_SOURCE:
        source = source[:80_000] + "\n<!-- middle of the source omitted -->\n" + source[-40_000:]
    checks = json_load(revision["checks_json"]) or {}
    user_text = (
        "Project title: " + project["title"] + "\n"
        + "Brief: " + project["brief"] + "\n"
        + "Chosen direction: " + str(direction.get("title", "")) + " - " + str(direction.get("thesis", "")) + "\n"
        + "Instruction that produced this version: " + (revision["instruction"] or "(starting prototype)") + "\n"
        + "Requirement report: " + json.dumps(json_load(revision["requirements_json"]) or [], ensure_ascii=False) + "\n"
        + "Assumptions: " + json.dumps(json_load(revision["assumptions_json"]) or [], ensure_ascii=False) + "\n"
        + "Browser observations: "
        + json.dumps({"status": checks.get("status"), "errors": (checks.get("errors") or [])[:5]}, ensure_ascii=False)
        + "\nCurrent source:\n" + source
    )
    prompt = (
        "You are the creative director for a browser prototype that was just built. Read the current "
        "source and decide what the person would most enjoy doing next, like choosing a path in a "
        "choose-your-own-adventure book. Write exactly three next steps. Return only one JSON object shaped as "
        '{"suggestions":[{"kind":"refine","title":"...","change":"..."}]}. '
        "Use one of each kind. refine: polish or fix something visible in this source. "
        "expand: add a feature or content that builds on what already exists. "
        "twist: a surprising creative leap that changes the experience and keeps the brief. "
        "title is at most eight words. change is one to three sentences, specific to elements in the source, "
        "that a developer can apply without asking questions. Do not ask for external resources or invented "
        "media. Do not repeat the instruction that produced this version."
    )
    response = await chat_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_text}],
        max_tokens=4000,
        json_schema=SUGGESTIONS_FORMAT,
    )
    raw = response["content"]
    payload = next((item for item in _json_objects(raw) if isinstance(item.get("suggestions"), list)), None)
    if payload is None or len(payload["suggestions"]) != 3:
        raise ProviderError("The provider did not return exactly three suggestions.", raw)
    cleaned = []
    for item in payload["suggestions"]:
        if not isinstance(item, dict) or item.get("kind") not in SUGGESTION_KINDS:
            raise ProviderError("A suggestion had an invalid kind.", raw)
        title, change = item.get("title"), item.get("change")
        if (not isinstance(title, str) or not title.strip() or len(title) > 80
                or not isinstance(change, str) or not change.strip() or len(change) > 600):
            raise ProviderError("A suggestion was incomplete or too long.", raw)
        cleaned.append({"kind": item["kind"], "title": title.strip(), "change": change.strip()})
    return cleaned


async def _run_suggestions(app: FastAPI, revision_id: str) -> None:
    root: Path = app.state.data_dir
    suggestions, error = None, None
    try:
        config = provider_settings(root)
        revision = _get_revision(root, revision_id)
        if not revision:
            return
        if not config.ready:
            raise ProviderError("No provider is configured.")
        suggestions = await _generate_suggestions(root, config, revision)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = (str(exc) if isinstance(exc, (ProviderError, ValueError)) else "The suggestions failed unexpectedly.")[:600]
    with connect(root, immediate=True) as db:
        db.execute(
            "UPDATE revisions SET suggestions_status = ?, suggestions_json = ?, suggestions_error = ? WHERE id = ?",
            ("ready" if suggestions else "failed", json_dump(suggestions) if suggestions else None, error, revision_id),
        )


def _launch_suggestions(app: FastAPI, revision_id: str) -> None:
    task = asyncio.create_task(_run_suggestions(app, revision_id))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def _generate_directions(root: Path, config, request_data: dict) -> tuple[dict, str]:
    project = _get_project(root, request_data["project_id"])
    if not project:
        raise RuntimeError("The saved project could not be found.")
    project = _with_reference_snapshot(project, request_data.get("_reference"))
    reference = _reference_data(root, project)

    user_text = (
        "Project title: " + project["title"] + "\n"
        + "Brief: " + project["brief"] + "\n"
        + "Reference intent: " + project["reference_intent"] + "\n"
        + "Additional exploration: " + (request_data.get("steer") or "(none)") + "\n"
        + "Uploaded asset manifest (metadata only): "
        + json.dumps(public_assets(request_data.get("_assets") or []), ensure_ascii=False)
        + "\n"
    )
    if reference and config.vision_supported:
        user_content = [
            {"type": "text", "text": user_text + "Use the attached reference as requested."},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:" + reference["media_type"] + ";base64,"
                    + base64.b64encode(reference["bytes"]).decode("ascii")
                },
            },
        ]
    else:
        if reference:
            user_text += "A reference image exists, but vision is disabled for this model configuration."
        user_content = user_text
    prompt = (
        "Create exactly three materially different creative directions for a browser UI. "
        "Treat a reference as visual evidence only; do not claim it specifies behavior. "
        "Use the stated intent and keep each direction specific. "
        "Return only one JSON object shaped as "
        '{"directions":[{"id":"short-kebab-id","title":"...","thesis":"...",'
        '"axis":"...", "benefit":"...", "tradeoff":"...", '
        '"behavior_assumption":"..."}]}. '
        "For every behavior assumption, explicitly say it is inferred and editable. "
        "If no behavior assumption applies, say so."
    )
    response = await chat_completion(
        config,
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=1600,
        json_schema=DIRECTIONS_FORMAT,
    )
    raw = response["content"]
    payload = next((item for item in _json_objects(raw) if isinstance(item.get("directions"), list)), None)
    if payload is None:
        raise ProviderError("The provider did not return valid direction JSON.", raw)
    directions = payload["directions"]
    if len(directions) != 3:
        raise ProviderError("The provider must return exactly three directions.", raw)
    safe_directions = []
    ids: set[str] = set()
    for card in directions:
        if not isinstance(card, dict):
            raise ProviderError("A direction card was incomplete.", raw)
        direction_id = card.get("id")
        if not isinstance(direction_id, str) or not re.fullmatch(r"[a-z0-9-]{1,50}", direction_id):
            raise ProviderError("A direction card had an invalid identifier.", raw)
        if direction_id in ids:
            raise ProviderError("Direction identifiers must be distinct.", raw)
        ids.add(direction_id)
        cleaned = {"id": direction_id}
        for field in (
            "title",
            "thesis",
            "axis",
            "benefit",
            "tradeoff",
            "behavior_assumption",
        ):
            value = card.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 600:
                raise ProviderError("A direction card was incomplete or too long.", raw)
            cleaned[field] = value.strip()
        cleaned["layout"] = "generated"
        safe_directions.append(cleaned)
    return {"directions": safe_directions}, response["model"]


def _reference_data(root: Path, project: dict) -> dict[str, Any] | None:
    relative = project.get("reference_path")
    media_type = project.get("reference_type")
    digest = project.get("reference_sha256")
    if not relative:
        if media_type or digest:
            raise ValueError("The saved reference snapshot is incomplete.")
        return None
    if media_type not in IMAGE_TYPES or not digest:
        raise ValueError("The saved reference snapshot is incomplete.")
    file_path = root / relative
    try:
        if not file_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("The saved reference snapshot path is invalid.")
    except OSError as exc:
        raise ValueError("The saved reference snapshot could not be read.") from exc
    try:
        data = file_path.read_bytes()
    except OSError:
        raise ValueError("The saved reference snapshot is missing; no result was generated.")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("The saved reference snapshot changed; no result was generated.")
    return {"bytes": data, "media_type": media_type}


async def _generate_artifact(
    root: Path,
    config,
    project: dict,
    direction: dict,
    instruction: str,
    *,
    mode: str,
    targets: list[dict[str, str]] | None = None,
    parent_html: str | None = None,
    assets: list[dict[str, Any]] | None = None,
    operation_id: str,
) -> tuple[dict[str, Any], str]:
    assets = assets or []
    allowed_asset_ids = {item["id"] for item in assets}
    reference = _reference_data(root, project)
    requested_3d, requested_video = _feature_requests(
        "\n".join((project["brief"], direction.get("title", ""), direction.get("thesis", ""))),
        instruction,
    )
    intent = project["reference_intent"]
    preamble = (
        "You are implementing a browser prototype, not writing a plan. Build one polished, responsive, "
        "accessible, standalone HTML document. Inline CSS and JavaScript. Preserve existing working "
        "behavior and all unrelated visual/code decisions during refinement. Address every explicit "
        "user requirement; do not silently drop a requirement. Use fixture content. Make declared "
        "interactions work with keyboard input and respect reduced motion. Resize canvases with the "
        "viewport. Use no network requests, external resources, package imports, forms, inline event "
        "attributes, external links, or programmatic navigation. Use addEventListener and same-document "
        "fragment links. Put a short descriptive data-component attribute on every meaningful part of "
        "the page (header, each card or panel, controls, key text) and keep those names stable when "
        "refining, so the user can point at parts. Refer to uploaded media only by the provided asset "
        "IDs: use `studio-asset:<id>` "
        "in HTML/CSS URLs or `STUDIO_ASSETS[\"<id>\"]` in JavaScript. Do not invent media URLs or "
        "inline asset bytes. The global STUDIO_ASSETS dictionary is supplied at runtime/export. "
        "If the request calls for 3D, three-dimensional, three D, or WebGL, implement a real Three.js "
        "WebGLRenderer scene; CSS perspective/depth alone does not satisfy it. Do not import THREE or "
        "use a CDN: window.THREE and THREE.GLTFLoader are supplied. If a GLB is supplied, load it via "
        "THREE.GLTFLoader using its STUDIO_ASSETS data URL. If the request calls for video or "
        "background video, use a provided MP4/WebM asset in a real <video> element with controls or "
        "muted playback. With no compatible uploaded video, show an honest static fallback and report "
        "needs_asset; never simulate video with CSS and claim it plays. Include a report of each user "
        "requirement with status implemented, needs_asset, or unsupported, and concise assumptions. "
        "The report describes your implementation intent, not verified runtime behavior. Return only "
        "one strict JSON object with `html` (the full document), `requirements` (array of objects with "
        "request/status/detail), and `assumptions` (array of strings). Do not use Markdown fences."
    )
    request_text = (
        "Project: " + project["title"] + "\n"
        + "Brief: " + project["brief"] + "\n"
        + "Reference intent: " + intent + "\n"
        + "Selected direction: " + json.dumps(direction, ensure_ascii=False) + "\n"
        + "Uploaded asset manifest (metadata only; binary content is not sent): "
        + json.dumps(public_assets(assets), ensure_ascii=False) + "\n"
    )
    if reference and config.vision_supported:
        request_text += (
            "A reference image is attached. Follow its visual language according to the stated intent. "
            "Behavior inferred from the image must be treated as an editable interpretation.\n"
        )
    elif reference:
        request_text += "A reference image is stored, but is not being sent because vision is disabled.\n"
    if mode == "prototype" and instruction:
        request_text += "Additional user direction: " + instruction + "\n"
    elif mode == "prototype":
        request_text += "Additional user direction: (none)\n"
    if mode == "refine":
        request_text += (
            "Refine the existing source. Preserve everything not affected by the instruction and keep "
            "the selected direction recognizable.\nInstruction: " + instruction + "\n"
        )
        if targets:
            request_text += (
                "The user picked these parts of the prototype. Apply the instruction to them and "
                "leave every other part unchanged. Each is given as its data-component name, tag, "
                "visible text, and CSS path:\n"
                + "\n".join(
                    "- " + (item.get("name") or "(unnamed)") + " | <" + item.get("tag", "") + "> | \""
                    + item.get("text", "") + "\" | " + item["path"]
                    for item in targets
                )
                + "\n"
            )
    messages = [{"role": "system", "content": preamble}]
    user_content: Any = request_text
    if reference and config.vision_supported:
        user_content = [
            {"type": "text", "text": request_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:" + reference["media_type"] + ";base64,"
                    + base64.b64encode(reference["bytes"]).decode("ascii")
                },
            },
        ]
    if mode == "refine" and parent_html:
        if len(parent_html.encode("utf-8")) > 350_000:
            raise ValueError("This saved prototype is too large to send for refinement.")
        if isinstance(user_content, list):
            user_content[0]["text"] += "\nExisting source:\n" + parent_html
        else:
            user_content += "\nExisting source:\n" + parent_html
    messages.append({"role": "user", "content": user_content})
    features = {"3d": requested_3d, "video": requested_video}
    _set_operation_stage(root, operation_id, "generating")
    live.start(operation_id, "generating", config.code_max_tokens)
    response = await chat_completion(
        config, messages, max_tokens=config.code_max_tokens, json_schema=ARTIFACT_FORMAT,
        on_delta=live.recorder(operation_id),
    )
    original_content = response["content"]
    repaired = False
    try:
        parsed = _parse_artifact_report(original_content)
        source = validate_html(parsed["html"], allowed_asset_ids=allowed_asset_ids)
        if requested_video and not _video_assets(assets):
            source = _strip_video_elements(source)
        rendered = render_assets(
            root,
            source,
            assets,
            vendor_path=STATIC_DIR / "vendor" / "studio-three.js",
            needs_asset=requested_video and not _video_assets(assets),
        )
        _set_operation_stage(root, operation_id, "checking")
        checks = await browser_check(rendered)
        requirements = _feature_report(
            parsed["requirements"], source, assets, checks,
            requested_3d=requested_3d, requested_video=requested_video,
        )
        evidence = _repair_evidence(
            source, assets, checks, requested_3d=requested_3d, requested_video=requested_video
        )
    except ValueError as exc:
        parsed = None
        source = ""
        checks = {
            "status": "unavailable", "errors": [], "desktop": None, "narrow": None,
            "browser": "chromium", "repair_attempted": False,
        }
        requirements = []
        evidence = [str(exc)[:500]]

    if evidence and config.auto_repair:
        repaired = True
        _set_operation_stage(root, operation_id, "repairing")
        repair_context = source or original_content[:100_000]
        if len(repair_context.encode("utf-8")) > 350_000:
            raise ProviderError("The prototype is too large to repair safely in one request.")
        repair_prompt = (
            "Repair this prototype once using only the evidence below. Preserve unrelated working "
            "design and behavior. Do not introduce external resources or invented assets. Return the "
            "same strict JSON shape: html, requirements, assumptions. The report must describe actual "
            "implementation intent and must not claim a browser check passed.\n\n"
            "Evidence:\n- " + "\n- ".join(evidence[:12])
            + "\n\nCurrent response/source to repair:\n" + repair_context
        )
        repair_messages = list(messages)
        repair_messages[0] = {
            "role": "system",
            "content": preamble + " Apply one evidence-led repair and preserve unrelated behavior.",
        }
        repair_messages.append({"role": "user", "content": repair_prompt})
        live.start(operation_id, "repairing", config.code_max_tokens)
        response = await chat_completion(
            config, repair_messages, max_tokens=config.code_max_tokens, json_schema=ARTIFACT_FORMAT,
            on_delta=live.recorder(operation_id),
        )
        try:
            parsed = _parse_artifact_report(response["content"])
            source = validate_html(parsed["html"], allowed_asset_ids=allowed_asset_ids)
            if requested_video and not _video_assets(assets):
                source = _strip_video_elements(source)
            rendered = render_assets(
                root,
                source,
                assets,
                vendor_path=STATIC_DIR / "vendor" / "studio-three.js",
                needs_asset=requested_video and not _video_assets(assets),
            )
        except ValueError as exc:
            raise ProviderError(
                "The prototype did not pass source validation after one repair: " + str(exc),
                response["content"],
            ) from exc
        _set_operation_stage(root, operation_id, "checking")
        checks = await browser_check(rendered)
        requirements = _feature_report(
            parsed["requirements"], source, assets, checks,
            requested_3d=requested_3d, requested_video=requested_video,
        )

    if parsed is None or not source:
        raise ProviderError(
            "The provider response did not produce a valid standalone prototype: "
            + (evidence[0] if evidence else "unknown reason"),
            original_content,
        )

    checks["repair_attempted"] = repaired
    # Feature rows are evidence-based. Browser reports remain observations, not fidelity claims.
    requirements = _feature_report(
        parsed["requirements"], source, assets, checks,
        requested_3d=requested_3d, requested_video=requested_video,
    )
    return {
        "source_html": source,
        "assets": assets,
        "requirements": requirements,
        "assumptions": parsed["assumptions"],
        "checks": checks,
    }, response["model"]


_REPORT_MISSING_NOTE = "The model's requirement report was missing or malformed, so it is not shown."


def _json_objects(content: str):
    """Yield JSON objects found in a model reply: the whole text, a fenced block, or the outermost braces."""
    text = content.strip()
    candidates = [text]
    fence = re.search(r"```[A-Za-z]*[ \t]*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    first, last = text.find("{"), text.rfind("}")
    if 0 <= first < last:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _load_report_payload(content: str) -> dict[str, Any] | None:
    for payload in _json_objects(content):
        if isinstance(payload.get("html"), str):
            return payload
    document = re.search(r"(?is)<!doctype\s+html\b.*</html\s*>", content.strip())
    return {"html": document.group(0)} if document else None


def _parse_artifact_report(content: str) -> dict[str, Any]:
    if not isinstance(content, str) or len(content.encode("utf-8")) > 900_000:
        raise ValueError("The provider response exceeded the report size limit.")
    payload = _load_report_payload(content)
    if payload is None:
        raise ValueError("The provider response did not contain a complete HTML document.")
    degraded = False
    cleaned_requirements = []
    requirements = payload.get("requirements")
    if isinstance(requirements, list):
        for item in requirements[:40]:
            request = item.get("request") if isinstance(item, dict) else None
            status = item.get("status") if isinstance(item, dict) else None
            detail = item.get("detail", "") if isinstance(item, dict) else None
            if (
                not isinstance(request, str) or not request.strip() or len(request) > 300
                or status not in {"implemented", "needs_asset", "unsupported"}
                or not isinstance(detail, str) or len(detail) > 600
            ):
                degraded = True
                continue
            cleaned_requirements.append(
                {"request": request.strip(), "status": status, "detail": detail.strip()}
            )
    else:
        degraded = True
    cleaned_assumptions = []
    assumptions = payload.get("assumptions")
    if isinstance(assumptions, list):
        for item in assumptions[:30]:
            if isinstance(item, str) and len(item) <= 400:
                cleaned_assumptions.append(item.strip())
            else:
                degraded = True
    else:
        degraded = True
    if degraded:
        cleaned_assumptions.append(_REPORT_MISSING_NOTE)
    return {
        "html": payload["html"],
        "requirements": cleaned_requirements,
        "assumptions": cleaned_assumptions,
    }


def _feature_requests(text: str, latest_instruction: str = "") -> tuple[bool, bool]:
    cue_3d = r"\b(?:3d|three[- ]dimensional|three d|webgl)\b"
    cue_video = r"\b(?:background[- ]?video|video(?:\s+background|\s+backdrop)?)\b"
    negative = r"\b(?:no|without|avoid|remove|drop|skip|omit|disable|replace|instead of|rather than|do not|don't)\b"

    def requested(pattern: str) -> bool:
        baseline = bool(re.search("(?i)" + pattern, text))
        if not latest_instruction.strip():
            return baseline
        escaped_negative = r"(?i)(?:" + negative + r").{0,60}" + pattern + r"|" + pattern + r".{0,40}(?:instead|rather than)"
        if re.search(escaped_negative, latest_instruction):
            return False
        if re.search("(?i)" + pattern, latest_instruction):
            return True
        return baseline

    return requested(cue_3d), requested(cue_video)


def _video_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in assets if item.get("media_type") in {"video/mp4", "video/webm"}]


def _strip_video_elements(source: str) -> str:
    source = re.sub(r"(?is)<video\b[^>]*>.*?</video\s*>", "", source)
    return re.sub(r"(?is)<video\b[^>]*/\s*>", "", source)


def _feature_report(
    model_requirements: list[dict[str, Any]],
    source: str,
    assets: list[dict[str, Any]],
    checks: dict[str, Any],
    *,
    requested_3d: bool,
    requested_video: bool,
) -> list[dict[str, Any]]:
    requirements = []
    for item in model_requirements:
        request_text = item["request"]
        is_feature_row = requested_3d and bool(
            re.search(r"(?i)\b(?:3d|three[- ]dimensional|three d|webgl)\b", request_text)
        )
        is_feature_row = is_feature_row or requested_video and bool(
            re.search(r"(?i)\bvideo\b", request_text)
        )
        if not is_feature_row:
            requirements.append(item)
    if requested_3d:
        desktop = checks.get("desktop") or {}
        narrow = checks.get("narrow") or {}
        uses_three = bool(re.search(r"(?i)THREE\s*\.\s*WebGLRenderer", source))
        webgl_seen = bool((desktop.get("webgl_context_count") or 0) + (narrow.get("webgl_context_count") or 0))
        if not uses_three:
            status, detail = "unsupported", "No Three.js WebGLRenderer is present in the saved source."
        elif checks.get("status") == "unavailable":
            status, detail = "implemented", "Source invokes Three.js; runtime browser evidence is unavailable."
        elif checks.get("status") == "failed":
            status, detail = "unsupported", "The browser check failed; see the observed errors below."
        elif webgl_seen:
            status, detail = "implemented", "Browser checks observed a WebGL context."
        else:
            status, detail = "unsupported", "Browser checks did not observe a WebGL context."
        requirements.append({"request": "Three-dimensional WebGL experience", "status": status, "detail": detail})
    if requested_video:
        videos = _video_assets(assets)
        if not videos:
            status, detail = "needs_asset", "Upload an MP4 or WebM asset to provide real video playback."
        else:
            video_ids = {item["id"] for item in videos}
            uses_video = bool(re.search(r"(?is)<video\b", source)) and bool(
                re.search(r"STUDIO_ASSETS\s*\[\s*['\"](" + "|".join(map(re.escape, video_ids)) + r")['\"]\s*\]", source)
                or any("studio-asset:" + item_id in source for item_id in video_ids)
            )
            if not uses_video:
                status, detail = "unsupported", "The source does not connect a video element to an uploaded video asset."
            elif checks.get("status") == "unavailable":
                status, detail = "implemented", "Source connects an uploaded video; runtime readiness was not checked."
            elif checks.get("status") == "failed":
                status, detail = "unsupported", "The browser check failed; see the observed errors below."
            elif any((checks.get(view) or {}).get("video_ready_count", 0) for view in ("desktop", "narrow")):
                status, detail = "implemented", "Browser checks observed a ready video element."
            else:
                status, detail = "unsupported", "Browser checks did not observe a ready video element."
        requirements.append({"request": "Uploaded video playback", "status": status, "detail": detail})
    return requirements


def _repair_evidence(
    source: str,
    assets: list[dict[str, Any]],
    checks: dict[str, Any],
    *,
    requested_3d: bool,
    requested_video: bool,
) -> list[str]:
    evidence = []
    if checks.get("status") == "failed":
        evidence.extend(str(item)[:300] for item in checks.get("errors", []))
    if requested_3d and not re.search(r"(?i)THREE\s*\.\s*WebGLRenderer", source):
        evidence.append("The request explicitly asks for real 3D/WebGL, but Three.WebGLRenderer is absent.")
    if requested_3d and checks.get("status") == "passed":
        if not any((checks.get(view) or {}).get("webgl_context_count", 0) for view in ("desktop", "narrow")):
            evidence.append("The browser checks observed no WebGL context for the requested 3D scene.")
    videos = _video_assets(assets)
    if requested_video and videos:
        video_ids = {item["id"] for item in videos}
        has_video = bool(re.search(r"(?is)<video\b", source)) and any(
            "studio-asset:" + item_id in source
            or re.search(r"STUDIO_ASSETS\s*\[\s*['\"]" + re.escape(item_id) + r"['\"]\s*\]", source)
            for item_id in video_ids
        )
        if not has_video:
            evidence.append("The request asks for video and an uploaded video exists, but no video element uses it.")
        elif checks.get("status") == "passed" and not any(
            (checks.get(view) or {}).get("video_ready_count", 0) for view in ("desktop", "narrow")
        ):
            evidence.append("The browser checks found no ready video element for the requested background video.")
    return evidence


def _valid_reference(media_type: str, data: bytes) -> tuple[str, str]:
    if media_type not in IMAGE_TYPES:
        raise HTTPException(415, "Attach a PNG, JPEG, or WebP image.")
    extension, signature = IMAGE_TYPES[media_type]
    if media_type == "image/webp":
        if len(data) < 12 or not data.startswith(signature) or data[8:12] != b"WEBP":
            raise HTTPException(415, "The file content does not match its image type.")
    elif not data.startswith(signature):
        raise HTTPException(415, "The file content does not match its image type.")
    if media_type == "image/png" and (len(data) < 24 or b"IEND" not in data[-16:]):
        raise HTTPException(415, "The PNG reference is incomplete.")
    if media_type == "image/jpeg" and (len(data) < 4 or not data.endswith(b"\xff\xd9")):
        raise HTTPException(415, "The JPEG reference is incomplete.")
    return extension, hashlib.sha256(data).hexdigest()


def create_app(*, data_dir: Path | None = None) -> FastAPI:
    root = Path(data_dir) if data_dir else data_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(root)
        interrupted = mark_unfinished_interrupted(root)
        app.state.data_dir = root
        app.state.generation_lock = asyncio.Semaphore(1)
        app.state.tasks = set()
        app.state.interrupted_on_start = interrupted
        yield
        tasks = list(app.state.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Possibility Studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.data_dir = root
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, exc: RequestValidationError):
        safe_errors = []
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            field = ".".join(location[-2:]) or "request"
            error_type = error.get("type", "")
            if error_type in {"missing", "value_error.missing"}:
                problem = "is required"
            elif "too_long" in error_type or "string_too_long" in error_type:
                problem = "is too long"
            elif "too_short" in error_type or "string_too_short" in error_type:
                problem = "is too short"
            else:
                problem = "is invalid"
            safe_errors.append({"field": field, "problem": problem})
        return JSONResponse(
            {"detail": "Request validation failed.", "errors": safe_errors},
            status_code=422,
        )

    @app.middleware("http")
    async def protect_workbench(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            if origin == "null" or not _same_origin(origin, request):
                return JSONResponse(
                    {"detail": "This request must come from the Possibility Studio page on this origin."},
                    status_code=403,
                )
        response = await call_next(request)
        _response_headers(response)
        return response

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health(request: Request):
        return {
            "ok": True,
            "interrupted_on_start": request.app.state.interrupted_on_start,
        }

    @app.get("/api/settings")
    async def get_settings(request: Request):
        root = request.app.state.data_dir
        config = provider_settings(root)
        with connect(root) as db:
            stored = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}
        env_url = stored.get("provider_url", os.environ.get("STUDIO_PROVIDER_URL", "")).strip()
        url_is_valid = bool(config.provider_url)
        return {
            "provider_url": config.provider_url if url_is_valid else "",
            "model": config.model,
            "vision_supported": config.vision_supported,
            "code_max_tokens": config.code_max_tokens,
            "reasoning_effort": config.reasoning_effort,
            "auto_repair": config.auto_repair,
            "request_timeout": config.request_timeout,
            "key_configured": bool(config.api_key),
            "ready": config.ready,
            "presets": PROVIDER_PRESETS,
            "configuration_error": (
                "Use a direct HTTP(S) chat-completions URL without credentials or query parameters."
                if env_url and not url_is_valid
                else ""
            ),
        }

    @app.get("/api/provider-models")
    async def provider_models(request: Request):
        try:
            models = await popular_openrouter_models(20)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return {"models": [], "source": "unavailable"}
        return {"models": models, "source": "openrouter-popular"}

    @app.put("/api/settings")
    async def put_settings(body: SettingsInput, request: Request):
        root = request.app.state.data_dir
        try:
            validate_settings(
                body.provider_url,
                body.model,
                body.api_key,
                body.vision_supported,
                body.code_max_tokens,
                body.reasoning_effort,
                body.auto_repair,
                body.request_timeout,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with connect(root, immediate=True) as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES('provider_url',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (body.provider_url.strip(),),
            )
            db.execute(
                "INSERT INTO settings(key,value) VALUES('model',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (body.model.strip(),),
            )
            db.execute(
                "INSERT INTO settings(key,value) VALUES('vision_supported',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if body.vision_supported else "false",),
            )
            for key, value in (
                ("code_max_tokens", str(body.code_max_tokens)),
                ("reasoning_effort", body.reasoning_effort),
                ("auto_repair", "true" if body.auto_repair else "false"),
                ("request_timeout", str(body.request_timeout)),
            ):
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            if body.clear_api_key:
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('api_key','') "
                    "ON CONFLICT(key) DO UPDATE SET value=''"
                )
            elif body.api_key:
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('api_key',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (body.api_key,),
                )
        return await get_settings(request)

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectInput, request: Request):
        root = request.app.state.data_dir
        brief = body.brief.strip()
        if not brief:
            raise HTTPException(422, "Add a short brief before creating a project.")
        project_id = str(uuid.uuid4())
        stamp = _now_stamp()
        with connect(root, immediate=True) as db:
            db.execute(
                """
                INSERT INTO projects (
                    id,title,brief,reference_intent,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    project_id,
                    (body.title or "").strip()[:100] or _brief_title(brief),
                    brief,
                    body.reference_intent,
                    stamp,
                    stamp,
                ),
            )
        return {"id": project_id, "title": (body.title or "").strip()[:100] or _brief_title(brief)}

    @app.put("/api/projects/{project_id}/reference")
    async def attach_reference(project_id: str, request: Request):
        root = request.app.state.data_dir
        project = _get_project(root, project_id)
        if not project:
            raise HTTPException(404, "Project not found.")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in IMAGE_TYPES:
            raise HTTPException(415, "Attach a PNG, JPEG, or WebP image.")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "The reference size was invalid.") from exc
            if declared_length < 0:
                raise HTTPException(400, "The reference size was invalid.")
            if declared_length > MAX_REFERENCE_BYTES:
                raise HTTPException(413, "The reference image is larger than the 10 MB limit.")
        chunks = bytearray()
        async for piece in request.stream():
            chunks.extend(piece)
            if len(chunks) > MAX_REFERENCE_BYTES:
                raise HTTPException(413, "The reference image is larger than the 10 MB limit.")
        data = bytes(chunks)
        extension, digest = _valid_reference(media_type, data)
        relative = "references/" + str(uuid.uuid4()) + extension
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        try:
            with connect(root, immediate=True) as db:
                existing = db.execute(
                    "SELECT reference_path FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if not existing:
                    raise HTTPException(404, "Project not found.")
                db.execute(
                    """
                    UPDATE projects SET reference_path=?,reference_type=?,reference_sha256=?,updated_at=?
                    WHERE id=?
                    """,
                    (relative, media_type, digest, _now_stamp(), project_id),
                )
            # Old reference files stay immutable for operation and revision provenance.
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"attached": True, "media_type": media_type, "sha256": digest}

    @app.get("/api/projects/{project_id}/assets")
    async def list_project_assets(project_id: str, request: Request):
        root = request.app.state.data_dir
        if not _get_project(root, project_id):
            raise HTTPException(404, "Project not found.")
        return {"assets": public_assets(asset_snapshot(root, project_id))}

    @app.post("/api/projects/{project_id}/assets", status_code=201)
    async def upload_project_asset(
        project_id: str,
        request: Request,
        name: str = Query(default="Asset", max_length=1024),
    ):
        root = request.app.state.data_dir
        if not _get_project(root, project_id):
            raise HTTPException(404, "Project not found.")
        clean_name = clean_asset_name(name)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type == "application/octet-stream" and clean_name.lower().endswith(".glb"):
            media_type = "model/gltf-binary"
        if media_type not in ASSET_TYPES:
            raise HTTPException(415, "Attach a PNG, JPEG, WebP, MP4, WebM, or GLB asset.")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "The asset size was invalid.") from exc
            if declared_length < 0:
                raise HTTPException(400, "The asset size was invalid.")
            if declared_length > MAX_ASSET_BYTES:
                raise HTTPException(413, "Each asset must be smaller than 20 MB.")
        data = bytearray()
        async for piece in request.stream():
            data.extend(piece)
            if len(data) > MAX_ASSET_BYTES:
                raise HTTPException(413, "Each asset must be smaller than 20 MB.")
        try:
            media_type, extension = validate_asset(media_type, clean_name, bytes(data))
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc

        asset_id = str(uuid.uuid4())
        relative = Path("assets") / project_id / (asset_id + extension)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        digest = hashlib.sha256(data).hexdigest()
        try:
            with connect(root, immediate=True) as db:
                if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise HTTPException(404, "Project not found.")
                total = db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM assets WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                if total + len(data) > MAX_PROJECT_ASSETS_BYTES:
                    raise HTTPException(413, "This project has reached its 50 MB asset limit.")
                db.execute(
                    "INSERT INTO assets(id,project_id,name,media_type,size,path,sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        asset_id,
                        project_id,
                        clean_name,
                        media_type,
                        len(data),
                        relative.as_posix(),
                        digest,
                        _now_stamp(),
                    ),
                )
                db.execute("UPDATE projects SET updated_at=? WHERE id=?", (_now_stamp(), project_id))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"id": asset_id, "name": clean_name, "media_type": media_type, "size": len(data)}

    @app.get("/api/projects")
    async def list_projects(request: Request):
        root = request.app.state.data_dir
        with connect(root) as db:
            rows = db.execute(
                """
                SELECT p.id,p.title,p.brief,p.created_at,p.updated_at,
                    (SELECT COUNT(*) FROM alternatives a WHERE a.project_id=p.id) AS alternative_count,
                    (SELECT r.id FROM revisions r WHERE r.project_id=p.id ORDER BY r.created_at DESC LIMIT 1) AS latest_revision_id
                FROM projects p ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return {"projects": [dict(row) for row in rows]}

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str, request: Request):
        root = request.app.state.data_dir
        with connect(root) as db:
            project_row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project_row:
                raise HTTPException(404, "Project not found.")
            project = dict(project_row)
            alternative_rows = db.execute(
                "SELECT * FROM alternatives WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
            revisions = db.execute(
                "SELECT * FROM revisions WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
            operations = db.execute(
                "SELECT * FROM operations WHERE project_id=? ORDER BY created_at DESC LIMIT 40",
                (project_id,),
            ).fetchall()
            latest_revision = db.execute(
                "SELECT id FROM revisions WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            direction_op = db.execute(
                """
                SELECT * FROM operations
                WHERE project_id=? AND kind='directions' AND status='succeeded'
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        by_alternative: dict[str, list[dict]] = {}
        for revision in revisions:
            by_alternative.setdefault(revision["alternative_id"], []).append(
                {
                    "id": revision["id"],
                    "parent_revision_id": revision["parent_revision_id"],
                    "instruction": revision["instruction"],
                    "model": revision["model"],
                    "provider_url": revision["provider_url"],
                    "reference_type": revision["reference_type"],
                    "reference_sha256": revision["reference_sha256"],
                    "created_at": revision["created_at"],
                    "operation_id": revision["operation_id"],
                }
            )
        project.pop("reference_path", None)
        project.pop("reference_sha256", None)
        project["reference_url"] = (
            "/api/projects/" + project_id + "/reference" if project_row["reference_path"] else None
        )
        project["assets"] = public_assets(asset_snapshot(root, project_id))
        project["alternatives"] = [
            {
                "id": row["id"],
                "direction": json_load(row["direction_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "revisions": by_alternative.get(row["id"], []),
            }
            for row in alternative_rows
        ]
        project["latest_directions"] = (
            json_load(direction_op["result_json"]) if direction_op else None
        )
        project["latest_directions_operation_id"] = direction_op["id"] if direction_op else None
        project["latest_revision_id"] = latest_revision["id"] if latest_revision else None
        project["operations"] = [_operation_public(row) for row in operations]
        return project

    @app.get("/api/projects/{project_id}/reference")
    async def get_reference(project_id: str, request: Request):
        root = request.app.state.data_dir
        project = _get_project(root, project_id)
        if not project or not project.get("reference_path"):
            raise HTTPException(404, "Reference image not found.")
        file_path = root / project["reference_path"]
        if not file_path.is_file():
            raise HTTPException(404, "Reference image not found.")
        return FileResponse(
            file_path,
            media_type=project["reference_type"],
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/projects/{project_id}/directions", status_code=202)
    async def create_directions(
        project_id: str,
        body: DirectionsInput,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        root = request.app.state.data_dir
        project = _get_project(root, project_id)
        if not project:
            raise HTTPException(404, "Project not found.")
        key = _validate_idempotency_key(idempotency_key)
        request_data = {
            "project_id": project_id,
            "steer": body.steer.strip(),
            "_reference": _reference_snapshot(project),
            "_assets": asset_snapshot(root, project_id),
        }
        fingerprint = _fingerprint(project_id, "directions", request_data)
        op_id = str(uuid.uuid4())
        with connect(root, immediate=True) as db:
            operation_id, duplicate = _create_operation(
                db,
                operation_id=op_id,
                idempotency_key=key,
                fingerprint=fingerprint,
                project_id=project_id,
                alternative_id=None,
                kind="directions",
                request_data=request_data,
            )
        if not duplicate:
            _launch(request.app, operation_id)
        return {"operation_id": operation_id, "status": "existing" if duplicate else "queued"}

    @app.post("/api/projects/{project_id}/alternatives", status_code=202)
    async def create_alternative(
        project_id: str,
        body: SelectionInput,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        root = request.app.state.data_dir
        key = _validate_idempotency_key(idempotency_key)
        project_record = _get_project(root, project_id)
        request_data = {
            "direction_operation_id": body.direction_operation_id,
            "direction_id": body.direction_id,
            "additional_instruction": body.additional_instruction.strip(),
            "_reference": _reference_snapshot(project_record or {}),
            "_assets": asset_snapshot(root, project_id),
        }
        fingerprint = _fingerprint(project_id, "prototype", request_data)
        op_id = str(uuid.uuid4())
        alt_id = str(uuid.uuid4())
        with connect(root, immediate=True) as db:
            project = db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise HTTPException(404, "Project not found.")
            prior = db.execute(
                "SELECT id,fingerprint,alternative_id FROM operations WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise HTTPException(409, "This Idempotency-Key was already used for a different request.")
                return {
                    "operation_id": prior["id"],
                    "alternative_id": prior["alternative_id"],
                    "status": "existing",
                }
            direction_operation = db.execute(
                "SELECT result_json,status FROM operations WHERE id=? AND project_id=? AND kind='directions'",
                (body.direction_operation_id, project_id),
            ).fetchone()
            if not direction_operation or direction_operation["status"] != "succeeded":
                raise HTTPException(409, "Generate and save directions before choosing one.")
            result = json_load(direction_operation["result_json"])
            direction = next(
                (
                    item
                    for item in (result or {}).get("directions", [])
                    if item.get("id") == body.direction_id
                ),
                None,
            )
            if not direction:
                raise HTTPException(404, "That saved direction could not be found.")
            db.execute(
                "INSERT INTO alternatives(id,project_id,direction_json,status,created_at) "
                "VALUES(?,?,?,'generating',?)",
                (alt_id, project_id, json_dump(direction), _now_stamp()),
            )
            operation_id, duplicate = _create_operation(
                db,
                operation_id=op_id,
                idempotency_key=key,
                fingerprint=fingerprint,
                project_id=project_id,
                alternative_id=alt_id,
                kind="prototype",
                request_data=request_data,
            )
        if not duplicate:
            _launch(request.app, operation_id)
        return {
            "operation_id": operation_id,
            "alternative_id": alt_id,
            "status": "queued",
        }

    @app.post("/api/alternatives/{alternative_id}/generate", status_code=202)
    async def retry_prototype(
        alternative_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        root = request.app.state.data_dir
        key = _validate_idempotency_key(idempotency_key)
        with connect(root, immediate=True) as db:
            alternative = db.execute(
                """
                SELECT a.*,p.reference_path,p.reference_type,p.reference_sha256
                FROM alternatives a JOIN projects p ON p.id=a.project_id
                WHERE a.id=?
                """,
                (alternative_id,),
            ).fetchone()
            if not alternative:
                raise HTTPException(404, "Alternative not found.")
            previous = db.execute(
                """
                SELECT * FROM operations
                WHERE alternative_id=? AND kind='prototype'
                ORDER BY created_at DESC LIMIT 1
                """,
                (alternative_id,),
            ).fetchone()
            if not previous:
                raise HTTPException(409, "There is no saved prototype attempt to retry.")
            frozen_request = json_load(previous["request_json"])
            request_data = dict(frozen_request)
            request_data["_retry_of"] = previous["id"]
            request_data["_reference"] = request_data.get(
                "_reference", _reference_snapshot(dict(alternative))
            )
            if "_assets" not in request_data:
                request_data["_assets"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,project_id,name,media_type,size,path,sha256 FROM assets "
                        "WHERE project_id=? ORDER BY created_at,id",
                        (alternative["project_id"],),
                    ).fetchall()
                ]
            fingerprint_data = {
                "alternative_id": alternative_id,
                "direction_id": json_load(alternative["direction_json"])["id"],
                "additional_instruction": request_data.get("additional_instruction", ""),
            }
            fingerprint = _fingerprint(
                alternative["project_id"], "prototype", fingerprint_data
            )
            prior = db.execute(
                "SELECT id,fingerprint FROM operations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise HTTPException(409, "This Idempotency-Key was already used for a different request.")
                return {"operation_id": prior["id"], "status": "existing"}
            if previous["status"] not in {"failed", "interrupted"}:
                raise HTTPException(409, "Wait for the current attempt to finish before retrying.")
            active = db.execute(
                """
                SELECT 1 FROM operations
                WHERE alternative_id=? AND kind='prototype' AND status IN ('queued','running')
                LIMIT 1
                """,
                (alternative_id,),
            ).fetchone()
            if active:
                raise HTTPException(409, "A prototype operation is already in progress.")
            successful = db.execute(
                "SELECT 1 FROM revisions WHERE alternative_id=? LIMIT 1", (alternative_id,)
            ).fetchone()
            if successful:
                raise HTTPException(409, "This alternative already has a saved prototype. Refine it instead.")
            operation_id, duplicate = _create_operation(
                db,
                operation_id=str(uuid.uuid4()),
                idempotency_key=key,
                fingerprint=fingerprint,
                project_id=alternative["project_id"],
                alternative_id=alternative_id,
                kind="prototype",
                request_data=request_data,
            )
            db.execute(
                "UPDATE alternatives SET status='generating' WHERE id=?", (alternative_id,)
            )
        if not duplicate:
            _launch(request.app, operation_id)
        return {"operation_id": operation_id, "status": "queued"}

    @app.post("/api/revisions/{revision_id}/refine", status_code=202)
    async def refine_revision(
        revision_id: str,
        body: RefinementInput,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        root = request.app.state.data_dir
        revision = _get_revision(root, revision_id)
        if not revision:
            raise HTTPException(404, "Revision not found.")
        instruction = body.instruction.strip()
        if not instruction:
            raise HTTPException(422, "Add a refinement instruction.")
        key = _validate_idempotency_key(idempotency_key)
        request_data = {
            "parent_revision_id": revision_id,
            "instruction": instruction,
            "targets": [item.model_dump() for item in body.targets],
            "_reference": _reference_snapshot(revision),
            "_assets": asset_snapshot(root, revision["project_id"]),
        }
        fingerprint = _fingerprint(revision["project_id"], "refine", request_data)
        with connect(root, immediate=True) as db:
            operation_id, duplicate = _create_operation(
                db,
                operation_id=str(uuid.uuid4()),
                idempotency_key=key,
                fingerprint=fingerprint,
                project_id=revision["project_id"],
                alternative_id=revision["alternative_id"],
                kind="refine",
                request_data=request_data,
            )
            if not duplicate:
                db.execute(
                    "UPDATE alternatives SET status='generating' WHERE id=?",
                    (revision["alternative_id"],),
                )
        if not duplicate:
            _launch(request.app, operation_id)
        return {"operation_id": operation_id, "status": "existing" if duplicate else "queued"}

    @app.get("/api/operations/{operation_id}")
    async def get_operation(operation_id: str, request: Request):
        with connect(request.app.state.data_dir) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Operation not found.")
        return _operation_public(row)

    @app.get("/api/operations/{operation_id}/draft")
    async def get_operation_draft(operation_id: str, serial: int = 0, offset: int = Query(0, ge=0)):
        return JSONResponse(live.draft(operation_id, serial, offset), headers={"Cache-Control": "no-store"})

    @app.get("/api/operations/{operation_id}/partial")
    async def get_operation_partial(operation_id: str, request: Request):
        with connect(request.app.state.data_dir) as db:
            row = db.execute(
                "SELECT partial_output FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if not row or not row["partial_output"]:
            raise HTTPException(404, "No raw output was kept for this operation.")
        return PlainTextResponse(
            row["partial_output"],
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/revisions/{revision_id}")
    async def get_revision(revision_id: str, request: Request):
        revision = _get_revision(request.app.state.data_dir, revision_id)
        if not revision:
            raise HTTPException(404, "Revision not found.")
        return {
            "id": revision["id"],
            "project_id": revision["project_id"],
            "alternative_id": revision["alternative_id"],
            "parent_revision_id": revision["parent_revision_id"],
            "source_html": revision["source_html"],
            "instruction": revision["instruction"],
            "model": revision["model"],
            "provider_url": revision["provider_url"],
            "reference": (
                {
                    "media_type": revision["reference_type"],
                    "sha256": revision["reference_sha256"],
                    "url": "/api/revisions/" + revision_id + "/reference",
                }
                if revision["reference_path"]
                else None
            ),
            "assets": public_assets(json_load(revision["assets_json"]) or []),
            "requirements": json_load(revision["requirements_json"]) or [],
            "assumptions": json_load(revision["assumptions_json"]) or [],
            "checks": json_load(revision["checks_json"]) or {},
            "suggestions": json_load(revision["suggestions_json"]) or [],
            "suggestions_status": revision["suggestions_status"],
            "suggestions_error": revision["suggestions_error"],
            "created_at": revision["created_at"],
            "preview_url": "/api/revisions/" + revision_id + "/preview",
            "export_url": "/api/revisions/" + revision_id + "/export",
        }

    @app.post("/api/revisions/{revision_id}/suggestions", status_code=202)
    async def request_suggestions(revision_id: str, request: Request):
        with connect(request.app.state.data_dir, immediate=True) as db:
            row = db.execute(
                "SELECT suggestions_status FROM revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Revision not found.")
            if row["suggestions_status"] == "pending":
                return {"status": "pending"}
            db.execute(
                "UPDATE revisions SET suggestions_status = 'pending', suggestions_error = NULL WHERE id = ?",
                (revision_id,),
            )
        _launch_suggestions(request.app, revision_id)
        return {"status": "pending"}

    @app.get("/api/revisions/{revision_id}/reference")
    async def get_revision_reference(revision_id: str, request: Request):
        revision = _get_revision(request.app.state.data_dir, revision_id)
        if not revision or not revision["reference_path"]:
            raise HTTPException(404, "Reference image not found.")
        file_path = request.app.state.data_dir / revision["reference_path"]
        if not file_path.is_file():
            raise HTTPException(404, "Reference image not found.")
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != revision["reference_sha256"]:
            raise HTTPException(409, "Saved reference integrity check failed.")
        return FileResponse(
            file_path,
            media_type=revision["reference_type"],
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/revisions/{revision_id}/preview")
    async def preview_revision(revision_id: str, request: Request):
        revision = _get_revision(request.app.state.data_dir, revision_id)
        if not revision:
            raise HTTPException(404, "Revision not found.")
        revision_assets = json_load(revision["assets_json"]) or []
        requirements = json_load(revision["requirements_json"]) or []
        html = render_assets(
            request.app.state.data_dir,
            revision["source_html"],
            revision_assets,
            vendor_path=STATIC_DIR / "vendor" / "studio-three.js",
            needs_asset=any(item.get("status") == "needs_asset" for item in requirements),
        )
        html = preview_html(html)
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": PREVIEW_CSP,
                "Content-Disposition": "inline",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/revisions/{revision_id}/export")
    async def export_revision(revision_id: str, request: Request):
        revision = _get_revision(request.app.state.data_dir, revision_id)
        if not revision:
            raise HTTPException(404, "Revision not found.")
        revision_assets = json_load(revision["assets_json"]) or []
        requirements = json_load(revision["requirements_json"]) or []
        html = render_assets(
            request.app.state.data_dir,
            revision["source_html"],
            revision_assets,
            vendor_path=STATIC_DIR / "vendor" / "studio-three.js",
            needs_asset=any(item.get("status") == "needs_asset" for item in requirements),
        )
        return Response(
            html,
            media_type="text/html",
            headers={
                "Content-Disposition": 'attachment; filename="possibility-' + revision_id + '.html"',
                "Cache-Control": "no-store",
                "Content-Security-Policy": PREVIEW_CSP,
            },
        )

    return app


app = create_app()
