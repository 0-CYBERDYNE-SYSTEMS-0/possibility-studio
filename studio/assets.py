from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import struct
import uuid
from pathlib import Path
from typing import Any

from .store import connect


MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_PROJECT_ASSETS_BYTES = 50 * 1024 * 1024
ASSET_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "model/gltf-binary": ".glb",
}
ASSET_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def clean_asset_name(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char >= " " and char != "\x7f").strip()
    name = name[:255]
    return name or "Untitled asset"


def validate_asset(media_type: str, name: str, data: bytes) -> tuple[str, str]:
    canonical = media_type.strip().lower()
    if canonical == "application/octet-stream" and name.lower().endswith(".glb"):
        canonical = "model/gltf-binary"
    extension = ASSET_TYPES.get(canonical)
    if not extension:
        raise ValueError("Attach a PNG, JPEG, WebP, MP4, WebM, or GLB asset.")
    if not data or len(data) > MAX_ASSET_BYTES:
        raise ValueError("Each asset must be smaller than 20 MB.")
    if canonical == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and b"IEND" in data[-16:]
    elif canonical == "image/jpeg":
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    elif canonical == "image/webp":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    elif canonical == "video/mp4":
        valid = len(data) >= 12 and data[4:8] == b"ftyp"
    elif canonical == "video/webm":
        valid = data.startswith(b"\x1a\x45\xdf\xa3")
    else:
        valid = _valid_glb(data)
    if not valid:
        raise ValueError("The file content does not match its declared asset type.")
    return canonical, extension


def _valid_glb(data: bytes) -> bool:
    if len(data) < 20:
        return False
    magic, version, declared_size = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or declared_size != len(data):
        return False
    offset = 12
    document = None
    chunk_index = 0
    while offset + 8 <= len(data):
        chunk_size, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + chunk_size
        if end > len(data) or chunk_size % 4:
            return False
        chunk = data[offset:end]
        if chunk_index == 0 and chunk_type != 0x4E4F534A:
            return False
        if chunk_type == 0x4E4F534A:
            if document is not None:
                return False
            try:
                document = json.loads(chunk.rstrip(b" \t\r\n\0").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
        offset = end
        chunk_index += 1
    if offset != len(data) or not isinstance(document, dict):
        return False
    if not isinstance(document.get("asset"), dict) or document["asset"].get("version") != "2.0":
        return False

    def only_embedded_uris(value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "uri" and not (isinstance(child, str) and child.startswith("data:")):
                    return False
                if not only_embedded_uris(child):
                    return False
        elif isinstance(value, list):
            return all(only_embedded_uris(child) for child in value)
        return True

    if not only_embedded_uris(document):
        return False
    return True


def asset_snapshot(root: Path, project_id: str) -> list[dict[str, Any]]:
    with connect(root) as db:
        rows = db.execute(
            "SELECT id,project_id,name,media_type,size,path,sha256 FROM assets "
            "WHERE project_id=? ORDER BY created_at,id",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def public_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": item["id"], "name": item["name"], "media_type": item["media_type"], "size": item["size"]}
        for item in assets
    ]


def render_assets(
    root: Path,
    source_html: str,
    assets: list[dict[str, Any]],
    *,
    vendor_path: Path,
    needs_asset: bool = False,
) -> str:
    by_id = {item["id"]: item for item in assets}
    raw_references = re.findall(r"studio-asset:([^\s\"'<>),;]+)", source_html, re.IGNORECASE)
    referenced = set(raw_references)
    for asset_id in referenced:
        if not ASSET_ID_RE.fullmatch(asset_id) or asset_id not in by_id:
            raise ValueError("The prototype refers to an asset that is not in this revision.")
    literal_keys = re.findall(
        r"STUDIO_ASSETS\s*\[\s*['\"]([^'\"]+)['\"]\s*\]",
        source_html,
        re.IGNORECASE,
    )
    if any(not ASSET_ID_RE.fullmatch(key) or key.lower() not in by_id for key in literal_keys):
        raise ValueError("The prototype refers to an asset that is not in this revision.")

    data_urls: dict[str, str] = {}
    for item in assets:
        relative = Path(item["path"])
        file_path = root / relative
        try:
            if not file_path.resolve().is_relative_to(root.resolve()):
                raise ValueError("The saved asset path is invalid.")
            content = file_path.read_bytes()
        except OSError as exc:
            raise ValueError("A saved asset is missing; this revision cannot be rendered.") from exc
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("A saved asset changed; this revision cannot be rendered.")
        data_urls[item["id"]] = (
            "data:" + item["media_type"] + ";base64," + base64.b64encode(content).decode("ascii")
        )

    rendered = re.sub(
        r"studio-asset:([0-9a-f-]{36})",
        lambda match: data_urls[match.group(1)],
        source_html,
        flags=re.IGNORECASE,
    )
    serialized = json.dumps(data_urls, ensure_ascii=True, separators=(",", ":"))
    serialized = serialized.replace("</", "<\\/")
    injections = []
    if assets or re.search(r"\bSTUDIO_ASSETS\b", source_html):
        injections.append('<script>window.STUDIO_ASSETS=Object.freeze(' + serialized + ');</script>')
    if re.search(r"\bTHREE\b", source_html):
        try:
            vendor = vendor_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("The bundled 3D renderer is unavailable.") from exc
        injections.append("<script>" + re.sub(r"(?i)</script", r"<\\/script", vendor) + "</script>")
    head_injection = "".join(injections)
    rendered_with_head = re.sub(
        r"(?i)(<head\b[^>]*>)",
        lambda match: match.group(1) + head_injection,
        rendered,
        count=1,
    )
    if rendered_with_head == rendered:
        rendered = re.sub(
            r"(?i)(<body\b[^>]*>)",
            lambda match: match.group(1) + head_injection,
            rendered,
            count=1,
        )
    else:
        rendered = rendered_with_head
    if needs_asset:
        message = html.escape("This request needs a compatible video asset before it can play.")
        notice = (
            '<aside role="status" style="margin:1rem;padding:1rem;border:1px solid #a65;'
            'background:#fff4dd;color:#342719">' + message + "</aside>"
        )
        rendered = re.sub(
            r"(?i)(<body\b[^>]*>)",
            lambda match: match.group(1) + notice,
            rendered,
            count=1,
        )
    return rendered
