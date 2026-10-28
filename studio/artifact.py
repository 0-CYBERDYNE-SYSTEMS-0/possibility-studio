from __future__ import annotations

import re
from html.parser import HTMLParser


MAX_ARTIFACT_BYTES = 600_000
PREVIEW_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "media-src data: blob:; "
    "connect-src data: blob:; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "worker-src 'none'; "
    "manifest-src 'none'; "
    "navigate-to 'none'; "
    "sandbox allow-scripts"
)
PREVIEW_MONITOR = """
<script>
(() => {
  const report = (message) => parent.postMessage(
    { type: "possibility-studio-preview-error", message: String(message).slice(0, 300) },
    "*"
  );
  addEventListener("error", (event) => report(event.message || "Script error"));
  addEventListener("unhandledrejection", (event) => report(event.reason || "Unhandled promise rejection"));
})();
</script>
"""
PREVIEW_INSPECTOR = """
<script>
(() => {
  let on = false;
  let outline = null;
  const send = (payload) => parent.postMessage(payload, "*");
  const describe = (node) => {
    const parts = [];
    for (let el = node; el && el !== document.documentElement; el = el.parentElement) {
      if (el === document.body) { parts.unshift("body"); break; }
      let part = el.tagName.toLowerCase();
      if (el.id) { parts.unshift(part + "#" + el.id); break; }
      const siblings = [...el.parentElement.children].filter((item) => item.tagName === el.tagName);
      if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(el) + 1) + ")";
      parts.unshift(part);
    }
    const owner = node.closest("[data-component]");
    return {
      type: "possibility-studio-pick",
      name: owner ? owner.getAttribute("data-component") : "",
      tag: node.tagName.toLowerCase(),
      path: parts.join(" > ").slice(0, 300),
      text: (node.innerText || node.getAttribute("aria-label") || "").trim().replace(/\\s+/g, " ").slice(0, 120),
    };
  };
  const mark = (node) => {
    if (!outline) {
      outline = document.createElement("div");
      outline.style.cssText = "position:fixed;z-index:2147483647;pointer-events:none;border:2px solid #c2410c;background:rgba(194,65,12,.12)";
      document.documentElement.append(outline);
    }
    const box = node.getBoundingClientRect();
    Object.assign(outline.style, { display: "block", top: box.top + "px", left: box.left + "px", width: box.width + "px", height: box.height + "px" });
  };
  const setOn = (value) => {
    on = value;
    document.documentElement.style.cursor = on ? "crosshair" : "";
    if (outline && !on) outline.style.display = "none";
  };
  addEventListener("message", (event) => {
    if (event.source === parent && event.data && event.data.type === "possibility-studio-inspect") setOn(!!event.data.on);
  });
  addEventListener("mousemove", (event) => { if (on && event.target.nodeType === 1) mark(event.target); }, true);
  addEventListener("click", (event) => {
    if (!on || event.target.nodeType !== 1) return;
    event.preventDefault();
    event.stopPropagation();
    send(describe(event.target));
  }, true);
  addEventListener("keydown", (event) => {
    if (event.key === "Escape" && on) send({ type: "possibility-studio-inspect-exit" });
    if (event.altKey && event.code === "KeyI") send({ type: "possibility-studio-inspect-toggle" });
  }, true);
})();
</script>
"""

FORBIDDEN_TAGS = {"base", "embed", "form", "frame", "iframe", "object", "portal"}
URL_ATTRIBUTES = {"src", "href", "xlink:href", "poster", "srcset"}


class _PolicyParser(HTMLParser):
    def __init__(self, allowed_asset_ids: set[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.errors: list[str] = []
        self.has_body = False
        self.allowed_asset_ids = allowed_asset_ids

    def _allowed_asset_url(self, value: str) -> bool:
        match = re.fullmatch(r"studio-asset:([0-9a-f-]{36})", value, re.IGNORECASE)
        return bool(match and match.group(1).lower() in self.allowed_asset_ids)

    def handle_starttag(self, tag, attrs) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag, attrs) -> None:
        self._check(tag, attrs)

    def _check(self, tag: str, attrs) -> None:
        if tag.lower() in FORBIDDEN_TAGS:
            self.errors.append("contains a blocked <" + tag.lower() + "> element")
        if tag.lower() == "body":
            self.has_body = True
        attributes = dict(attrs)
        if tag.lower() == "meta" and attributes.get("http-equiv", "").lower() == "refresh":
            self.errors.append("contains a meta refresh")
        for name, raw_value in attrs:
            value = (raw_value or "").strip()
            lower_name = (name or "").lower()
            lower_value = value.lower()
            if lower_name.startswith("on"):
                self.errors.append("contains inline event handlers")
            if lower_value.startswith("javascript:"):
                self.errors.append("contains a JavaScript URL")
            if lower_name in URL_ATTRIBUTES:
                if lower_name in {"href", "xlink:href"}:
                    allowed = (
                        value.startswith("#")
                        or value.lower().startswith("data:")
                        or self._allowed_asset_url(value)
                    )
                elif lower_name == "srcset":
                    candidates = [item.strip().split()[0] for item in value.split(",") if item.strip()]
                    allowed = bool(candidates) and all(
                        item.lower().startswith("data:image/") or self._allowed_asset_url(item)
                        for item in candidates
                    )
                else:
                    allowed = value.lower().startswith(("data:", "blob:")) or self._allowed_asset_url(value)
                if not allowed:
                    self.errors.append("contains an external or relative " + lower_name)
            if lower_name == "target":
                self.errors.append("contains a browsing-context target")


def validate_html(source: str, *, allowed_asset_ids: set[str] | None = None) -> str:
    cleaned = source.lstrip("\ufeff \t\r\n")
    if len(cleaned.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("The HTML prototype is larger than the 600 KB limit.")
    if not re.match(r"(?is)^<!doctype\s+html\s*>", cleaned):
        raise ValueError("The provider response must start with a complete HTML document.")
    if not re.search(r"(?is)</html\s*>\s*$", cleaned):
        raise ValueError("The provider response ended before the HTML document closed.")
    if re.search(r"(?is)<script\b[^>]*\bsrc\s*=", cleaned):
        raise ValueError("External scripts are not allowed in a standalone prototype.")
    if re.search(r"(?is)<link\b[^>]*\bhref\s*=", cleaned):
        raise ValueError("External linked resources are not allowed in a standalone prototype.")
    if re.search(r"(?is)@import\b", cleaned):
        raise ValueError("External CSS imports are not allowed in a standalone prototype.")
    for match in re.finditer(r"(?is)\burl\(\s*(['\"]?)(.*?)\1\s*\)", cleaned):
        target = match.group(2).strip().lower()
        asset_match = re.fullmatch(r"studio-asset:([0-9a-f-]{36})", target)
        asset_allowed = bool(
            asset_match
            and allowed_asset_ids is not None
            and asset_match.group(1).lower() in allowed_asset_ids
        )
        if not target.startswith(("data:", "blob:", "#")) and not asset_allowed:
            raise ValueError("External CSS resources are not allowed in a standalone prototype.")
    if re.search(
        r"(?i)\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b",
        cleaned,
    ):
        raise ValueError("Network-capable browser APIs are not allowed in a standalone prototype.")
    if re.search(r"(?i)(?:window|document|self)\s*\.\s*location\b|\blocation\s*\.\s*(?:assign|replace)\b", cleaned):
        raise ValueError("Programmatic frame navigation is not allowed in a standalone prototype.")
    parser = _PolicyParser(allowed_asset_ids or set())
    try:
        parser.feed(cleaned)
        parser.close()
    except Exception as exc:
        raise ValueError("The HTML prototype could not be parsed completely.") from exc
    if parser.errors:
        raise ValueError("The HTML prototype " + parser.errors[0] + ".")
    if not parser.has_body or not re.search(r"(?is)</body\s*>", cleaned):
        raise ValueError("The provider response must include a complete body.")
    return cleaned


def preview_html(source: str) -> str:
    return re.sub(
        r"(?i)(<head\b[^>]*>)",
        lambda match: match.group(1) + PREVIEW_MONITOR + PREVIEW_INSPECTOR,
        source,
        count=1,
    )

