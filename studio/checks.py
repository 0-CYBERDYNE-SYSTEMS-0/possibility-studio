from __future__ import annotations

import asyncio
import re
from typing import Any


async def browser_check(source: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unavailable",
        "errors": [],
        "desktop": None,
        "narrow": None,
        "browser": "chromium",
        "repair_attempted": False,
    }
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["errors"] = ["Playwright is not installed."]
        return result

    errors: list[str] = []
    try:
        observations = await asyncio.wait_for(_check_in_chromium(async_playwright, source, errors), timeout=20)
        unique_errors = list(dict.fromkeys(errors))[:20]
        result.update(observations)
        result["errors"] = unique_errors
        has_overflow = any(item["horizontal_overflow"] for item in observations.values())
        result["status"] = "failed" if unique_errors or has_overflow else "passed"
        for name, item in observations.items():
            if item["horizontal_overflow"]:
                result["errors"].append(
                    _format_overflow_error(
                        name, item["viewport_width"], item.get("overflow_culprits", [])
                    )
                )
        return result
    except Exception as exc:
        result["status"] = "unavailable"
        result["errors"] = ["Browser check unavailable: " + str(exc)[:250]]
        return result


async def _check_in_chromium(async_playwright, source: str, errors: list[str]) -> dict[str, Any]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            async def block_network(route):
                await route.abort()
            await context.route("**/*", block_network)
            observations: dict[str, Any] = {}
            instrumented_source = _instrument_webgl(source)
            for name, width, height in (("desktop", 1280, 800), ("narrow", 390, 844)):
                page = await context.new_page()
                await page.set_viewport_size({"width": width, "height": height})
                page.on("pageerror", lambda error: errors.append(str(error)[:300]))
                page.on(
                    "console",
                    lambda message: errors.append(message.text[:300])
                    if message.type == "error"
                    else None,
                )
                await page.set_content(instrumented_source, wait_until="load", timeout=8000)
                try:
                    await page.wait_for_function(
                        "() => [...document.querySelectorAll('video')].every(video => video.readyState >= 2)",
                        timeout=1800,
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(250)
                observations[name] = await page.evaluate(
                    """() => {
                      const root = document.documentElement;
                      const body = document.body;
                      const videos = [...document.querySelectorAll('video')];
                      const overflowCulprits = (() => {
                        const viewport = root.clientWidth;
                        const candidates = [...body.querySelectorAll('*')].flatMap(element => {
                          const rect = element.getBoundingClientRect();
                          if (!rect.width || !rect.height) return [];
                          for (let ancestor = element.parentElement; ancestor && ancestor !== body.parentElement; ancestor = ancestor.parentElement) {
                            if (['auto', 'scroll', 'hidden', 'clip'].includes(getComputedStyle(ancestor).overflowX)) return [];
                          }
                          const style = getComputedStyle(element);
                          const scale = element.offsetWidth ? rect.width / element.offsetWidth : 1;
                          const paintedRight = style.overflowX === 'visible'
                            ? Math.max(rect.right, rect.right + Math.max(0, element.scrollWidth - element.clientWidth) * scale)
                            : rect.right;
                          if (paintedRight <= viewport + 1 && rect.left >= -1) return [];
                          const classes = [...element.classList].slice(0, 4).map(name => '.' + name).join('');
                          const selector = element.tagName.toLowerCase() + (element.id ? '#' + element.id : '') + classes;
                          const text = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
                          return [{
                            selector: selector.slice(0, 140),
                            width: Math.round(rect.width),
                            right: Math.round(paintedRight),
                            left: Math.round(rect.left),
                            text
                          }];
                        });
                        const seen = new Set();
                        return candidates
                          .sort((a, b) => Math.max(b.right - viewport, -b.left) - Math.max(a.right - viewport, -a.left))
                          .filter(item => {
                            const key = item.selector + ':' + item.left + ':' + item.right;
                            if (seen.has(key)) return false;
                            seen.add(key);
                            return true;
                          })
                          .slice(0, 5);
                      })();
                      return {
                        canvas_count: document.querySelectorAll('canvas').length,
                        webgl_context_count: window.__STUDIO_WEBGL_CANVASES?.length || 0,
                        video_count: videos.length,
                        video_ready_count: videos.filter(video => video.readyState >= 2).length,
                        video_data_source_count: videos.filter(video => video.currentSrc.startsWith('data:')).length,
                        viewport_width: window.innerWidth,
                        horizontal_overflow: Math.max(root.scrollWidth, body.scrollWidth) > root.clientWidth + 1 || overflowCulprits.length > 0,
                        overflow_culprits: overflowCulprits
                      };
                    }"""
                )
                await page.close()
            await context.close()
            return observations
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=3)
            except Exception:
                pass


def _instrument_webgl(source: str) -> str:
    script = """<script>(() => {
      const original = HTMLCanvasElement.prototype.getContext;
      const canvases = [];
      window.__STUDIO_WEBGL_CANVASES = canvases;
      HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const context = original.call(this, type, ...args);
        if (context && /^(webgl2?|experimental-webgl)$/i.test(type) && !canvases.includes(this)) canvases.push(this);
        return context;
      };
    })();</script>"""
    instrumented = re.sub(r"(?i)(<head\b[^>]*>)", lambda match: match.group(1) + script, source, count=1)
    if instrumented == source:
        instrumented = re.sub(r"(?i)(<body\b[^>]*>)", lambda match: match.group(1) + script, source, count=1)
    return instrumented


def _format_overflow_error(viewport: str, width: int, culprits: list[dict[str, Any]]) -> str:
    if not culprits:
        return f"Horizontal overflow at {viewport} viewport ({width}px); no single element was identified."
    details = []
    for item in culprits[:5]:
        detail = (
            f"{str(item.get('selector', 'element'))[:140]} "
            f"(width={int(item.get('width', 0))}px, right={int(item.get('right', 0))}px"
        )
        left = item.get("left")
        if left is not None:
            detail += f", left={int(left)}px"
        text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()[:80]
        if text:
            detail += ", text='" + text.replace("'", "’") + "'"
        details.append(detail + ")")
    return f"Horizontal overflow at {viewport} viewport ({width}px): " + "; ".join(details)
