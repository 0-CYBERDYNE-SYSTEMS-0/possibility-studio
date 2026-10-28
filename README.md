# Possibility Studio

Possibility Studio is a local FastAPI workbench for exploring creative directions as runnable, standalone HTML prototypes. It keeps its projects in a local SQLite database and talks to any OpenAI-compatible chat-completions provider that you configure.

## Run

From this directory, create an environment and install the package:

    python3 -m venv .venv
    .venv/bin/python -m pip install -e '.[dev]'

Configure the endpoint and model in **Provider settings** in the app, or set these environment variables before launch:

    export STUDIO_PROVIDER_URL="https://your-provider.example/v1/chat/completions"
    export STUDIO_MODEL="your-configured-model"
    export STUDIO_API_KEY="your-key"
    export STUDIO_VISION_SUPPORTED="false"
    export STUDIO_CODE_MAX_TOKENS="12000"
    export STUDIO_REASONING_EFFORT=""
    export STUDIO_AUTO_REPAIR="true"

STUDIO_PROVIDER_URL is the complete OpenAI-compatible chat-completions URL. Set STUDIO_VISION_SUPPORTED=true only when you have explicitly selected a model that accepts image input. Keys stay server-side and are never returned by the settings API. URL credentials and query parameters are rejected.

Start the server:

    .venv/bin/python -m uvicorn studio.app:app --host 0.0.0.0 --port 8310

Open the workbench on the host. A provider and model must be configured before any generation runs; without them, requests fail with a message that points to Provider settings. The default data directory is .studio-data/; set STUDIO_DATA_DIR to move it.

## Saved work

Projects store their brief, visual reference, and immutable image, video, and GLB assets. Reference and asset uploads are validated by type, size, and file signature, then stored under generated names. Replacing a reference keeps its earlier file for operations and revisions that cite it; media assets cannot be replaced or deleted through the workbench. Each asset is limited to 20 MiB and each project to 50 MiB. GLB files must be self-contained and version 2.

Operations are written to SQLite before provider work begins. One request runs at a time, with up to four queued or running operations. Retries use a new operation identity and reuse the failed attempt’s saved instruction and reference snapshot. Revisions are immutable rows; refining one creates a child revision. Opening or exporting a saved revision makes no provider call.

The coding request receives only each asset's ID, filename, media type, and size. Source can refer to an asset as `studio-asset:<id>` in HTML/CSS or `STUDIO_ASSETS["<id>"]` in JavaScript. Saved source keeps those references. Preview and export verify the frozen revision snapshot and inline its data URLs; source that uses `THREE` also receives the locally vendored Three.js bundle. Exports are standalone HTML and contain the assets and bundle they use.

## Prototype checks

The provider is asked for strict JSON containing the HTML, a requirement report, and assumptions. The requirement report records the model's implementation intent. Explicit 3D/WebGL and video requests are checked against source and browser observations. If video is requested without an uploaded MP4/WebM, the report says `needs_asset` and preview/export show a visible asset request rather than simulated playback.

Coding output is bounded by `code_max_tokens` (2,000–128,000, default 12,000). Reasoning effort is empty by default. When set, the OpenAI-compatible request sends it as `reasoning.effort` for OpenRouter hosts and `reasoning_effort` for other hosts. Endpoint and model support varies; provider errors remain visible. Automatic repair is enabled by default and makes at most one extra coding request, only after source validation, browser runtime, or explicit feature evidence identifies an issue. The repair uses that evidence and the configured token bound.

Browser checking is optional. Install the extra and Chromium to enable it:

    .venv/bin/python -m pip install -e '.[browser]'
    .venv/bin/python -m playwright install chromium

Checks use an isolated temporary Chromium context, block network requests, and inspect desktop and narrow layouts, JavaScript errors, horizontal overflow, WebGL context creation, and video readiness. These are limited runtime observations, not proof of visual fidelity. If Playwright or Chromium is unavailable, the saved report says `unavailable`.

## Preview boundaries

The preview is served as HTML with a restrictive Content Security Policy and loaded in an iframe with sandbox="allow-scripts" and no same-origin permission. The policy blocks network connections, external resources, forms, and nested frames. The iframe's opaque origin prevents access to the workbench origin's storage and APIs. Mutating APIs require an exact same-origin Origin; missing and opaque (null) origins are rejected. No CORS access is enabled.

The browser-enforced response-header CSP and iframe sandbox are the preview isolation controls. Source validation catches incomplete documents and common external-resource or navigation patterns, but it is only a completeness guard: it is not a JavaScript proof and cannot recognize every computed navigation attempt. Browser support for the CSP navigate-to directive varies; the sandbox still prevents top-level navigation without a user-granted sandbox token. Runtime error and unhandled-rejection events are reported in the workbench when the browser emits them. Exports are downloaded as standalone HTML and should be treated as user-controlled code.

## Tests

Run the offline API, media, migration, and provider tests with:

    .venv/bin/python -m pytest -q

Tests use temporary SQLite directories, mocked browser checks, and mocked provider responses. They make no live provider requests.

## Pointing at parts of the prototype

After every saved version, a separate model call reads the saved source and report and proposes three next steps: one refine, one expand, one twist. They appear above the refine box as choices. Picking one runs a refinement with that text as the instruction. The three steps are saved with the version, so reopening an older version shows its original choices. If the call fails, "Suggest next steps" retries it.

Long builds stream from the provider. The activity panel shows the build steps with a timer for each, an approximate token count against the output budget, and tokens per second. Open "Watch the build" to read the code as it arrives or to see a static draft preview. Progress is kept in memory only, so it is gone after a restart.

Press Inspect (or Alt+I), then click a part of the prototype in the canvas. Each pick becomes a chip above the refine box (up to five). The refine request sends the picked parts, and the model is told to change only those. Generated pages carry data-component names, so chips read as "card-list" and not as raw tags. The inspector script is added to the preview only, never to saved source or exports.

## License

MIT. See LICENSE. The bundled Three.js runtime keeps its own license in `studio/static/vendor/THREE-LICENSE.txt`.
