(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const state = {
    settings: { ready: false, key_configured: false },
    popularModels: [],
    targets: [],
    suggestToken: 0,
    project: null,
    projectId: null,
    activeAlternativeId: null,
    activeRevisionId: null,
    revision: null,
    busy: false,
    showDirections: false,
    creatingProject: false,
    assetsUploading: false,
    viewEpoch: 0,
    watchers: new Map(),
    activity: null,
    activityTimer: null,
    clearKeyNext: false,
    toastTimer: null,
  };

  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const keySlot = (slot) => "studio-key:" + slot;

  function randomId() {
    if (globalThis.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    if (globalThis.crypto && typeof crypto.getRandomValues === "function") {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
      return hex.slice(0, 4).join("") + "-" + hex.slice(4, 6).join("") + "-"
        + hex.slice(6, 8).join("") + "-" + hex.slice(8, 10).join("") + "-"
        + hex.slice(10).join("");
    }
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2) + "-"
      + Math.random().toString(36).slice(2);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: { ...(options.headers || {}) },
    });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const message = payload && typeof payload === "object" ? payload.detail : "";
      throw new Error(message || "The studio request failed (" + response.status + ").");
    }
    return payload;
  }

  function idempotencyKey(slot) {
    const storageKey = keySlot(slot);
    let key = sessionStorage.getItem(storageKey);
    if (!key) {
      key = randomId();
      sessionStorage.setItem(storageKey, key);
    }
    return key;
  }

  function clearIdempotencyKey(slot) {
    if (slot) sessionStorage.removeItem(keySlot(slot));
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
  }

  function setBusy(busy, epoch = state.viewEpoch) {
    if (epoch !== state.viewEpoch) return;
    state.busy = busy;
    document.body.classList.toggle("is-busy", busy);
    document.querySelectorAll(".card-choose, #first-explore, #create-project-form button, #explore-form button, #refine-form button, .next-step, .history-retry").forEach((button) => {
      button.disabled = busy;
      button.setAttribute("aria-disabled", String(busy));
    });
    $("#project-assets-input").disabled = busy || state.assetsUploading;
    $("#asset-upload-submit").disabled = busy || state.assetsUploading;
  }

  function setBanner(message, error = false, loading = false, epoch = state.viewEpoch, rawUrl = null) {
    if (epoch !== state.viewEpoch) return;
    const banner = $("#operation-banner");
    banner.replaceChildren();
    if (!message) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.classList.toggle("error", error);
    if (loading) {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      banner.append(spinner);
    }
    const text = document.createElement("span");
    text.textContent = message;
    banner.append(text);
    if (rawUrl) {
      const link = document.createElement("a");
      link.href = rawUrl;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "View raw output ↗";
      banner.append(link);
    }
  }

  function stopActivityTimer() {
    if (state.activityTimer !== null) {
      clearInterval(state.activityTimer);
      state.activityTimer = null;
    }
  }

  function elapsedLabel(createdAtMs) {
    const seconds = Math.max(0, Math.floor((Date.now() - createdAtMs) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    const pad = (number) => String(number).padStart(2, "0");
    return hours > 0
      ? `${hours}:${pad(minutes)}:${pad(remainder)} elapsed`
      : `${pad(minutes)}:${pad(remainder)} elapsed`;
  }

  function refreshActivityElapsed(activity) {
    const elapsed = $("#activity-elapsed");
    if (!activity || activity.createdAtMs === null) {
      elapsed.hidden = true;
      elapsed.textContent = "";
      elapsed.removeAttribute("aria-label");
      return;
    }
    const label = elapsedLabel(activity.createdAtMs);
    elapsed.textContent = label;
    elapsed.setAttribute("aria-label", label);
    elapsed.hidden = false;
  }

  function setActivity(next, epoch = state.viewEpoch) {
    if (epoch !== state.viewEpoch) return;
    const panel = $("#activity-panel");
    if (!next) {
      stopActivityTimer();
      state.activity = null;
      panel.hidden = true;
      document.body.classList.remove("has-activity");
      return;
    }

    const previous = state.activity;
    const sameActivity = previous && previous.key === next.key && previous.epoch === epoch;
    if (!sameActivity) stopActivityTimer();
    const parsedCreatedAt = next.createdAt ? Date.parse(next.createdAt) : NaN;
    const createdAtMs = Number.isFinite(parsedCreatedAt)
      ? parsedCreatedAt
      : sameActivity ? previous.createdAtMs : null;
    const activity = { ...(sameActivity ? previous : {}), ...next, epoch, createdAtMs };
    state.activity = activity;

    panel.className = "activity-panel is-" + activity.status;
    panel.hidden = false;
    document.body.classList.add("has-activity");
    const title = $("#activity-title");
    if (title.textContent !== activity.title) title.textContent = activity.title;
    const detail = $("#activity-detail");
    const detailText = activity.detail || "";
    if (detail.textContent !== detailText) detail.textContent = detailText;
    detail.hidden = !activity.detail;
    const active = ["opening", "submitting", "queued", "running", "reconnecting"].includes(activity.status);
    $("#activity-spinner").hidden = !active;
    $("#activity-symbol").textContent = activity.status === "ready" ? "✓"
      : ["failed", "interrupted"].includes(activity.status) ? "!" : "";
    refreshActivityElapsed(activity);

    if (active && activity.createdAtMs !== null && state.activityTimer === null) {
      const activityKey = activity.key;
      const activityEpoch = activity.epoch;
      state.activityTimer = setInterval(() => {
        const current = state.activity;
        if (!current || current.key !== activityKey || current.epoch !== activityEpoch
            || !["queued", "running", "reconnecting"].includes(current.status)) {
          stopActivityTimer();
          return;
        }
        refreshActivityElapsed(current);
      }, 1000);
    }
    if (!active) stopActivityTimer();
  }

  function setOperationActivity(operation, projectId, epoch) {
    const isDirections = operation.kind === "directions";
    const subject = isDirections ? "directions" : "prototype code";
    const stageLabels = {
      generating: isDirections ? "Generating directions" : "Generating prototype code",
      checking: isDirections ? "Checking directions" : "Checking prototype in browser",
      repairing: "Repairing prototype from observed issues",
      saving: isDirections ? "Saving directions" : "Saving prototype and report",
    };
    const runningTitle = stageLabels[operation.stage] || (isDirections ? "Generating directions" : "Generating prototype code");
    const title = operation.status === "queued" ? `Queued · ${subject}`
      : operation.status === "running" ? runningTitle
        : operation.status === "succeeded" ? (isDirections ? "Directions ready" : "Prototype ready")
          : operation.status === "interrupted" ? (isDirections ? "Directions interrupted" : "Prototype interrupted")
            : (isDirections ? "Directions failed" : "Prototype failed");
    const detail = operation.status === "queued" ? "Waiting for the studio to begin."
      : operation.status === "running" ? operation.stage === "checking"
        ? "Recording browser observations for this version."
        : operation.stage === "repairing" ? "Applying at most one targeted repair."
          : operation.stage === "saving" ? "Writing this version and its report to history."
            : "The saved operation is currently running."
        : operation.status === "succeeded" ? "Saved to exploration history."
          : operation.status === "interrupted" ? "The operation stopped when the studio restarted."
            : operation.error || "The operation stopped before a result was saved.";
    setActivity({
      key: "operation:" + projectId + ":" + operation.id,
      status: operation.status === "succeeded" ? "ready" : operation.status,
      title,
      detail,
      createdAt: operation.created_at || null,
    }, epoch);
    if (epoch === state.viewEpoch) updateBuildView(operation);
  }

  const BUILD_STEPS = [["generating", "Generate"], ["checking", "Check"], ["repairing", "Repair"], ["saving", "Save"]];

  function formatDuration(ms) {
    const seconds = Math.max(0, Math.round(ms / 1000));
    return seconds >= 60 ? Math.floor(seconds / 60) + "m " + String(seconds % 60).padStart(2, "0") + "s" : seconds + "s";
  }

  function resetBuildView() {
    state.build = null;
    $("#build-steps").hidden = true;
    $("#build-meter").hidden = true;
    $("#build-watch").hidden = true;
    $("#build-code").textContent = "";
    $("#build-preview").removeAttribute("srcdoc");
    $("#activity-panel").classList.remove("is-building");
  }

  function renderBuildSteps(build) {
    const now = Date.now();
    const list = $("#build-steps");
    list.replaceChildren(...BUILD_STEPS
      .filter(([stage]) => stage !== "repairing" || build.acc.repairing !== undefined)
      .map(([stage, label]) => {
        const item = document.createElement("li");
        const seen = build.acc[stage] !== undefined;
        item.className = stage === build.current ? "is-current" : seen ? "is-done" : "";
        const name = document.createElement("span");
        name.textContent = label;
        item.append(name);
        if (seen) {
          const time = document.createElement("time");
          time.textContent = formatDuration(build.acc[stage] + (stage === build.current ? now - build.since : 0));
          item.append(time);
        }
        return item;
      }));
    list.hidden = false;
  }

  function renderBuildMeter(progress) {
    const meter = $("#build-meter");
    meter.hidden = !progress;
    if (!progress) return;
    const percent = Math.min(100, Math.round((progress.tokens / progress.max_tokens) * 100));
    $("#build-bar-fill").style.width = percent + "%";
    $("#build-bar").setAttribute("aria-valuenow", String(percent));
    $("#build-bar").classList.toggle("is-warn", percent >= 85);
    const number = (value) => value.toLocaleString("en-US");
    const stats = $("#build-stats");
    const stalled = progress.idle >= 30;
    stats.classList.toggle("is-stalled", stalled);
    if (progress.tokens === 0) {
      stats.textContent = progress.reasoning_chunks > 0
        ? "The model is reasoning before it writes code…"
        : "Waiting for the first token…";
      return;
    }
    stats.textContent = "≈" + number(progress.tokens) + " / " + number(progress.max_tokens) + " tokens ("
      + percent + "% of budget)" + (progress.rate ? " · " + progress.rate + " tok/s" : "")
      + (stalled ? " · no data for " + Math.round(progress.idle) + "s" : "");
  }

  async function pullBuildDraft(build) {
    if (build.fetching) return;
    build.fetching = true;
    try {
      const draft = await api("/api/operations/" + encodeURIComponent(build.id)
        + "/draft?serial=" + build.serial + "&offset=" + build.code.length);
      if (state.build !== build || !draft.active) return;
      if (draft.serial !== build.serial || draft.offset === 0) build.code = "";
      build.serial = draft.serial;
      build.code += draft.text;
      const code = $("#build-code");
      const pinned = code.scrollHeight - code.scrollTop - code.clientHeight < 40;
      code.textContent = build.code.length > 60000 ? "…\n" + build.code.slice(-60000) : build.code;
      if (pinned) code.scrollTop = code.scrollHeight;
      const now = Date.now();
      if (state.buildTab === "preview" && now - build.lastPreview > 4000 && build.code) {
        build.lastPreview = now;
        $("#build-preview").srcdoc = build.code;
      }
    } catch {
      // The draft view is optional. The next poll tries again.
    } finally {
      build.fetching = false;
    }
  }

  function updateBuildView(operation) {
    if (operation.kind === "directions" || operation.status !== "running") {
      resetBuildView();
      return;
    }
    if (!state.build || state.build.id !== operation.id) {
      resetBuildView();
      state.build = { id: operation.id, acc: {}, current: null, since: 0, serial: 0, code: "", fetching: false, lastPreview: 0 };
    }
    const build = state.build;
    const stage = operation.stage || "generating";
    if (build.current !== stage) {
      const now = Date.now();
      if (build.current) build.acc[build.current] += now - build.since;
      const updated = Date.parse(operation.updated_at);
      const sinceUpdate = now - updated;
      build.since = !build.current && sinceUpdate >= 0 && sinceUpdate < 6 * 3600 * 1000 ? updated : now;
      build.current = stage;
      if (build.acc[stage] === undefined) build.acc[stage] = 0;
    }
    $("#activity-panel").classList.add("is-building");
    renderBuildSteps(build);
    renderBuildMeter(operation.progress);
    $("#build-watch").hidden = false;
    if ($("#build-watch").open && operation.progress) pullBuildDraft(build);
  }

  function setupBuildView() {
    state.buildTab = "code";
    const showTab = (tab) => {
      state.buildTab = tab;
      $("#build-tab-code").setAttribute("aria-pressed", String(tab === "code"));
      $("#build-tab-preview").setAttribute("aria-pressed", String(tab === "preview"));
      $("#build-code").hidden = tab !== "code";
      $("#build-preview").hidden = tab !== "preview";
      if (tab === "preview" && state.build) {
        state.build.lastPreview = Date.now();
        $("#build-preview").srcdoc = state.build.code;
      }
    };
    $("#build-tab-code").addEventListener("click", () => showTab("code"));
    $("#build-tab-preview").addEventListener("click", () => showTab("preview"));
  }

  function modeBadge() {
    const badge = $("#mode-badge");
    const needsSetup = !state.settings.ready;
    badge.textContent = needsSetup ? "Provider not set up" : "Configured provider";
    badge.classList.toggle("needs-setup", needsSetup);
    $("#setup-banner").hidden = !needsSetup;
    $("#workspace-footer-status").textContent = needsSetup
      ? "Open Provider settings and choose a provider and model to start generating."
      : "Your work is stored on this machine.";
  }

  function makeButton(className, label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  async function loadSettings() {
    try {
      state.settings = await api("/api/settings");
      modeBadge();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadProjects() {
    const { projects } = await api("/api/projects");
    const nav = $("#project-list");
    nav.replaceChildren();
    $("#recent-list").replaceChildren();
    $("#recent-wrap").hidden = projects.length === 0;
    if (projects.length === 0) {
      const empty = document.createElement("p");
      empty.className = "rail-empty";
      empty.textContent = "Your studies will gather here.";
      nav.append(empty);
      return;
    }
    for (const project of projects.slice(0, 6)) {
      const recent = makeButton("project-list-item", "", () => openProject(project.id));
      const recentName = document.createElement("span");
      recentName.className = "project-name";
      recentName.textContent = project.title;
      recent.append(recentName);
      $("#recent-list").append(recent);
    }
    for (const project of projects) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "project-list-item" + (project.id === state.projectId ? " active" : "");
      button.setAttribute("aria-current", project.id === state.projectId ? "page" : "false");
      const pin = document.createElement("span");
      pin.className = "project-pin";
      pin.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.className = "project-name";
      name.textContent = project.title;
      const count = document.createElement("span");
      count.className = "project-count";
      count.textContent = String(project.alternative_count).padStart(2, "0");
      button.append(pin, name, count);
      button.addEventListener("click", () => openProject(project.id));
      nav.append(button);
    }
  }

  function drawDirections() {
    const grid = $("#directions-grid");
    grid.replaceChildren();
    const result = state.project && state.project.latest_directions;
    if (!result || !Array.isArray(result.directions) || result.directions.length === 0) {
      const empty = document.createElement("div");
      empty.className = "direction-empty";
      const number = document.createElement("span");
      number.className = "empty-number";
      number.textContent = "01 — 03";
      const copy = document.createElement("p");
      copy.textContent = "Your first directions will appear here.";
      const action = makeButton("", "Explore three directions ↗", () => requestDirections(""));
      empty.append(number, copy, action);
      grid.append(empty);
      return;
    }
    const operationId = state.project.latest_directions_operation_id;
    for (const direction of result.directions) {
      const card = document.createElement("article");
      card.className = "direction-card";
      const axis = document.createElement("span");
      axis.className = "card-axis";
      axis.textContent = direction.axis;
      const title = document.createElement("h3");
      title.textContent = direction.title;
      const thesis = document.createElement("p");
      thesis.className = "card-thesis";
      thesis.textContent = direction.thesis;
      const points = document.createElement("div");
      points.className = "card-points";
      for (const [label, value] of [["Benefit", direction.benefit], ["Tradeoff", direction.tradeoff]]) {
        const point = document.createElement("p");
        point.className = "card-point";
        const lead = document.createElement("b");
        lead.textContent = label + " · ";
        point.append(lead, document.createTextNode(value));
        points.append(point);
      }
      const inference = document.createElement("p");
      inference.className = "card-inference";
      inference.textContent = "Behavior assumption · inferred: " + direction.behavior_assumption;
      const footer = document.createElement("div");
      footer.className = "card-footer";
      const choose = makeButton("card-choose", "", () => chooseDirection(operationId, direction.id));
      const text = document.createElement("span");
      text.textContent = "Build this";
      const arrow = document.createElement("span");
      arrow.textContent = "↗";
      arrow.setAttribute("aria-hidden", "true");
      choose.append(text, arrow);
      footer.append(choose);
      card.append(axis, title, thesis, points, inference, footer);
      grid.append(card);
    }
  }

  function relativeTime(iso) {
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return "";
    const minutes = Math.round((Date.now() - then) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + " min ago";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return hours + " h ago";
    return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function drawHistory() {
    const project = state.project;
    const wrap = $("#history-wrap");
    const list = $("#history-list");
    list.replaceChildren();
    if (!project) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    $("#history-count").textContent = String(project.alternatives.length).padStart(2, "0");
    if (project.alternatives.length === 0) {
      const empty = document.createElement("p");
      empty.className = "history-empty";
      empty.textContent = "Choose a direction to begin a path.";
      list.append(empty);
      return;
    }
    const retryable = new Map();
    for (const operation of project.operations || []) {
      if (operation.kind === "prototype" && ["failed", "interrupted"].includes(operation.status) && operation.alternative_id) {
        retryable.set(operation.alternative_id, operation);
      }
    }
    const failed = [];
    for (const alternative of project.alternatives) {
      if (!alternative.revisions.length && retryable.has(alternative.id)) {
        failed.push(alternative);
        continue;
      }
      const group = document.createElement("details");
      group.className = "history-alternative";
      group.open = alternative.id === state.activeAlternativeId || alternative.revisions.length === 0;
      const heading = document.createElement("summary");
      heading.className = "history-alt-button";
      const name = document.createElement("strong");
      name.textContent = alternative.direction.title;
      const number = document.createElement("span");
      number.textContent = alternative.revisions.length ? "v" + alternative.revisions.length : "…";
      heading.append(name, number);
      group.append(heading);
      if (alternative.revisions.length) {
        const revisions = document.createElement("div");
        revisions.className = "history-revisions";
        alternative.revisions.forEach((revision, index) => {
          const button = makeButton(
            "history-revision" + (revision.id === state.activeRevisionId ? " active" : ""),
            "",
            () => openRevision(revision.id),
          );
          const head = document.createElement("span");
          head.className = "history-revision-head";
          const version = document.createElement("b");
          version.textContent = "v" + (alternative.revisions.length - index);
          const time = document.createElement("time");
          time.dateTime = revision.created_at;
          time.textContent = relativeTime(revision.created_at);
          head.append(version, time);
          const label = document.createElement("span");
          label.className = "history-revision-text";
          label.textContent = revision.instruction ? shortText(revision.instruction, 90) : "Starting prototype";
          button.append(head, label);
          if (revision.model) {
            const model = document.createElement("small");
            model.textContent = revision.model;
            button.append(model);
          }
          revisions.append(button);
        });
        group.append(revisions);
      } else {
        const status = document.createElement("p");
        status.className = "history-state";
        status.textContent = "Prototype in progress";
        group.append(status);
      }
      list.append(group);
    }
    if (failed.length) {
      const group = document.createElement("details");
      group.className = "history-failed";
      const summary = document.createElement("summary");
      summary.textContent = failed.length + (failed.length === 1 ? " failed attempt" : " failed attempts");
      group.append(summary);
      for (const alternative of failed) {
        const operation = retryable.get(alternative.id);
        const row = document.createElement("div");
        row.className = "history-failed-row";
        const text = document.createElement("span");
        text.textContent = alternative.direction.title
          + (operation.status === "interrupted" ? " · stopped after restart" : " · not saved");
        row.append(text, makeButton("history-retry", "Retry ↗", () => retryAlternative(alternative.id)));
        group.append(row);
      }
      list.append(group);
    }
  }

  function shortText(text, limit) {
    const clean = String(text || "").replace(/\s+/g, " ").trim();
    return clean.length > limit ? clean.slice(0, limit - 1) + "…" : clean;
  }

  function formatFileSize(size) {
    const bytes = Number(size) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function drawProjectAssets() {
    const list = $("#project-assets-list");
    if (!list) return;
    list.replaceChildren();
    const assets = state.project && Array.isArray(state.project.assets) ? state.project.assets : [];
    if (!assets.length) {
      const empty = document.createElement("li");
      empty.className = "asset-empty";
      empty.textContent = "No prototype assets attached yet.";
      list.append(empty);
      return;
    }
    for (const asset of assets) {
      const item = document.createElement("li");
      item.className = "asset-item";
      const name = document.createElement("strong");
      name.textContent = asset.name || "Untitled asset";
      const meta = document.createElement("small");
      meta.textContent = (asset.media_type || "File") + " · " + formatFileSize(asset.size);
      item.append(name, meta);
      list.append(item);
    }
  }

  function assetMediaType(file) {
    const extension = String(file.name || "").split(".").pop().toLowerCase();
    const types = {
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      webp: "image/webp",
      mp4: "video/mp4",
      webm: "video/webm",
      glb: "model/gltf-binary",
    };
    return types[extension] || file.type || "application/octet-stream";
  }

  async function uploadAssetFiles(projectId, files, onProgress = () => {}) {
    let uploaded = 0;
    for (const file of files) {
      onProgress("Uploading " + file.name + " · " + (uploaded + 1) + " of " + files.length + "…");
      try {
        await api("/api/projects/" + encodeURIComponent(projectId) + "/assets?name=" + encodeURIComponent(file.name), {
          method: "POST",
          headers: { "Content-Type": assetMediaType(file) },
          body: file,
        });
        uploaded += 1;
      } catch (error) {
        const remaining = uploaded ? " " + uploaded + " earlier file(s) were uploaded." : "";
        throw new Error("Could not upload “" + file.name + "”: " + error.message + remaining);
      }
    }
    return uploaded;
  }

  function setAssetUploadStatus(message, error = false) {
    const status = $("#asset-upload-status");
    status.textContent = message || "";
    status.classList.toggle("error", error);
    status.hidden = !message;
  }

  async function refreshProjectAssets(projectId, epoch = state.viewEpoch) {
    const result = await api("/api/projects/" + encodeURIComponent(projectId) + "/assets");
    if (epoch !== state.viewEpoch || projectId !== state.projectId || !state.project) return;
    state.project.assets = Array.isArray(result.assets) ? result.assets : [];
    drawProjectAssets();
  }

  function reportText(value) {
    if (typeof value === "string" || typeof value === "number") return String(value);
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (Array.isArray(value)) return value.length ? value.map(reportText).join("; ") : "none recorded";
    if (value && typeof value === "object") {
      if (value.detail || value.message || value.request || value.assumption) {
        return String(value.detail || value.message || value.request || value.assumption);
      }
      return Object.entries(value).map(([key, item]) => key.replace(/_/g, " ") + ": " + reportText(item)).join("; ");
    }
    return String(value ?? "");
  }

  function addReportItem(list, label, value) {
    const item = document.createElement("li");
    if (label) {
      const badge = document.createElement("span");
      badge.className = "report-label";
      badge.textContent = label;
      item.append(badge);
    }
    item.append(document.createTextNode(reportText(value)));
    list.append(item);
  }

  function requirementStatusLabel(status) {
    const labels = {
      implemented: "Reported · implemented",
      needs_asset: "Reported · needs asset",
      unsupported: "Reported · unsupported",
    };
    return labels[status] || "Reported · " + String(status || "status not provided").replace(/_/g, " ");
  }

  function drawRevisionReport(revision) {
    const panel = $("#revision-report");
    const requirements = Array.isArray(revision.requirements) ? revision.requirements : [];
    const assumptions = Array.isArray(revision.assumptions) ? revision.assumptions : [];
    const checks = revision.checks && typeof revision.checks === "object" ? revision.checks : null;
    const requirementList = $("#requirement-report");
    const assumptionList = $("#assumption-report");
    const checkList = $("#browser-check-report");
    requirementList.replaceChildren();
    assumptionList.replaceChildren();
    checkList.replaceChildren();

    if (requirements.length) {
      for (const requirement of requirements) {
        addReportItem(requirementList, requirementStatusLabel(requirement.status), requirement.request || requirement.detail || "Requirement");
        if (requirement.detail && requirement.request) addReportItem(requirementList, "Model detail", requirement.detail);
      }
    } else {
      addReportItem(requirementList, "", "No requirement report was saved for this version.");
    }
    if (assumptions.length) assumptions.forEach((assumption) => addReportItem(assumptionList, "", assumption));
    else addReportItem(assumptionList, "", "No assumptions were reported.");

    const status = checks && checks.status ? String(checks.status) : "unavailable";
    const statusLabel = status.replace(/_/g, " ");
    const checkStatus = $("#browser-check-status");
    checkStatus.classList.toggle("unavailable", !checks || status === "unavailable" || status === "not_run");
    checkStatus.textContent = checks ? "Browser checks · " + statusLabel : "Browser checks · unavailable for this version";
    if (!checks) {
      addReportItem(checkList, "", "No browser observations were saved for this version.");
    } else {
      const errors = [
        ...(Array.isArray(checks.errors) ? checks.errors : []),
        ...(Array.isArray(checks.console_errors) ? checks.console_errors : []),
        ...(Array.isArray(checks.runtime_errors) ? checks.runtime_errors : []),
      ];
      if (errors.length) errors.forEach((error) => addReportItem(checkList, "Observed issue", error));
      for (const key of ["canvas", "video", "overflow", "desktop", "narrow", "browser", "repair_attempted"]) {
        if (Object.prototype.hasOwnProperty.call(checks, key)) {
          addReportItem(checkList, key.replace(/_/g, " "), checks[key]);
        }
      }
      if (!errors.length && !["canvas", "video", "overflow", "desktop", "narrow", "browser", "repair_attempted"].some((key) => Object.prototype.hasOwnProperty.call(checks, key))) {
        addReportItem(checkList, "", "No additional browser details were recorded.");
      }
    }
    const implemented = requirements.filter((item) => item.status === "implemented").length;
    $("#open-details").classList.toggle("has-issue", status === "failed");
    $("#report-summary").textContent = requirements.length
      ? implemented + " of " + requirements.length + " requirements reported · browser checks " + (checks ? statusLabel : "unavailable")
      : "Browser checks " + (checks ? statusLabel : "unavailable");
    panel.hidden = false;
  }

  function syncView() {
    document.body.dataset.view = !state.project ? "start"
      : state.revision && !state.showDirections ? "shape" : "pick";
    $("#back-to-canvas").hidden = !state.revision;
  }

  function showProject() {
    const project = state.project;
    syncView();
    $("#welcome-view").hidden = Boolean(project);
    $("#project-view").hidden = !project;
    if (!project) return;
    $("#active-project-title").textContent = project.title;
    $("#active-project-brief").textContent = project.brief;
    $("#project-reference-label").textContent = project.reference_url ? "· visual reference attached" : "";
    const reference = $("#reference-preview");
    reference.hidden = !project.reference_url;
    if (project.reference_url) {
      $("#reference-thumbnail").src = project.reference_url + "?v=" + encodeURIComponent(project.updated_at);
      $("#reference-intent-label").textContent = project.reference_intent === "reconstruct"
        ? "Reconstruct intent"
        : "Inspiration intent";
    }
    drawProjectAssets();
    const guidance = $("#image-guidance");
    const hasReference = Boolean(project.reference_url);
    guidance.hidden = !hasReference || state.settings.vision_supported;
    guidance.textContent = guidance.hidden
      ? ""
      : "A reference is attached. Turn on the explicit image-input setting if you want to send it to the configured model.";
    modeBadge();
    drawDirections();
    drawHistory();
  }

  async function openProject(projectId) {
    if (state.assetsUploading || state.creatingProject) {
      showToast("Wait for this setup to finish before changing studies.");
      return;
    }
    detachWatchers();
    const epoch = ++state.viewEpoch;
    setActivity(null, epoch);
    setActivity({
      key: "opening:" + projectId,
      status: "opening",
      title: "Opening saved study",
      detail: "Loading its brief and checking saved work.",
    }, epoch);
    state.projectId = projectId;
    state.showDirections = false;
    loadProjectDrafts();
    setAssetUploadStatus("");
    state.project = null;
    state.activeRevisionId = null;
    state.revision = null;
    state.activeAlternativeId = null;
    setBusy(false, epoch);
    setBanner("Opening this study…", false, true, epoch);
    showProject();
    try {
      localStorage.setItem("studio-project-id", projectId);
      const project = await api("/api/projects/" + encodeURIComponent(projectId));
      if (epoch !== state.viewEpoch || state.projectId !== projectId) return;
      state.project = project;
      const assets = await api("/api/projects/" + encodeURIComponent(projectId) + "/assets");
      if (epoch !== state.viewEpoch || state.projectId !== projectId) return;
      state.project.assets = Array.isArray(assets.assets) ? assets.assets : [];
      showProject();
      $("#preview-empty").hidden = false;
      $("#canvas-actions").hidden = true;
      $("#canvas-meta").hidden = true;
      $("#preview-panel").hidden = true;
      $("#source-panel").hidden = true;
      $("#refine-section").hidden = true;
      $("#sandbox-note").hidden = true;
      $("#preview-error").hidden = true;
      $("#revision-report").hidden = true;
      $("#operation-banner").hidden = true;
      await loadProjects();
      if (epoch !== state.viewEpoch || state.projectId !== projectId) return;
      if (state.project.latest_revision_id) {
        await openRevision(state.project.latest_revision_id, false, { projectId, epoch });
      }
      const saved = readActiveOperation(projectId);
      const operations = state.project.operations || [];
      const pending = saved && saved.projectId === projectId
        ? saved
        : operations.find((item) => ["queued", "running"].includes(item.status))
          || (operations[0] && operations[0].status === "interrupted" ? operations[0] : null);
      if (pending) {
        watchOperation({
          id: pending.id,
          kind: pending.kind,
          alternativeId: pending.alternativeId || pending.alternative_id || null,
          slot: pending.slot || null,
          projectId,
          epoch,
        });
      } else {
        setActivity(null, epoch);
        setBanner("", false, false, epoch);
      }
    } catch (error) {
      if (epoch === state.viewEpoch && state.projectId === projectId) {
        setActivity({
          key: "open-failed:" + projectId,
          status: "failed",
          title: "Could not open this study",
          detail: error.message,
        }, epoch);
        setBanner(error.message, true, false, epoch);
        showToast(error.message);
      }
    }
  }

  function operationStorageKey(projectId) {
    return "studio-active-operation:" + projectId;
  }

  function readActiveOperation(projectId) {
    try {
      return JSON.parse(sessionStorage.getItem(operationStorageKey(projectId)) || "null")
        || JSON.parse(sessionStorage.getItem("studio-active-operation") || "null");
    }
    catch { return null; }
  }

  function readActiveOperationLegacy() {
    try { return JSON.parse(sessionStorage.getItem("studio-active-operation") || "null"); }
    catch { return null; }
  }

  async function openRevision(revisionId, refreshHistory = true, expected = null) {
    const projectId = expected ? expected.projectId : state.projectId;
    const epoch = expected ? expected.epoch : state.viewEpoch;
    const isCurrent = () => epoch === state.viewEpoch && projectId === state.projectId;
    try {
      const revision = await api("/api/revisions/" + encodeURIComponent(revisionId));
      if (!isCurrent()) return;
      state.revision = revision;
      state.activeRevisionId = revision.id;
      state.activeAlternativeId = revision.alternative_id;
      state.showDirections = false;
      syncView();
      $("#preview-empty").hidden = true;
      $("#canvas-actions").hidden = false;
      $("#canvas-meta").hidden = false;
      $("#preview-panel").hidden = false;
      $("#source-panel").hidden = true;
      $("#refine-section").hidden = false;
      fitField($("#refine-instruction"));
      $("#sandbox-note").hidden = false;
      $("#preview-error").hidden = true;
      drawRevisionReport(revision);
      renderSuggestions(revision);
      $("#revision-label").textContent = shortText(
        revision.instruction ? "Refinement · " + revision.instruction : "Starting prototype",
        72,
      );
      $("#revision-model").textContent = revision.model || "Configured provider";
      $("#source-code").textContent = revision.source_html;
      clearTargets();
      $("#prototype-frame").src = revision.preview_url + "?v=" + encodeURIComponent(revision.created_at);
      $("#export-link").href = revision.export_url;
      $("#preview-tab").classList.add("active");
      $("#preview-tab").setAttribute("aria-selected", "true");
      $("#source-tab").classList.remove("active");
      $("#source-tab").setAttribute("aria-selected", "false");
      if (refreshHistory && state.projectId) {
        const refreshed = await api("/api/projects/" + encodeURIComponent(projectId));
        if (!isCurrent()) return;
        state.project = refreshed;
        showProject();
      }
      if (!isCurrent()) return;
      drawHistory();
      if (window.matchMedia("(max-width: 820px)").matches) {
        $("#prototype-title").scrollIntoView({ block: "start", behavior: "smooth" });
      }
    } catch (error) {
      if (isCurrent()) showToast(error.message);
    }
  }

  async function createProject(event) {
    event.preventDefault();
    const epoch = state.viewEpoch;
    const form = event.currentTarget;
    const errorBox = $("#create-error");
    errorBox.hidden = true;
    const file = $("#reference-image").files[0];
    const assetFiles = Array.from($("#initial-assets").files || []);
    let createdProject = null;
    let initialAssetUploadStarted = false;
    try {
      if (file && file.size > 10 * 1024 * 1024) throw new Error("The reference image is larger than 10 MB.");
      const brief = $("#project-brief").value.trim();
      if (!brief) throw new Error("Add a short creative brief first.");
      state.creatingProject = true;
      setActivity({
        key: "create-study:" + epoch,
        status: "submitting",
        title: "Saving this study",
        detail: file ? "Saving the brief and visual reference before directions start." : "Saving the brief before directions start.",
      }, epoch);
      setBusy(true, epoch);
      createdProject = await api("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("#project-title").value.trim() || null,
          brief,
          reference_intent: $("#reference-intent").value,
        }),
      });
      if (file) {
        await api("/api/projects/" + encodeURIComponent(createdProject.id) + "/reference", {
          method: "PUT",
          headers: { "Content-Type": file.type },
          body: file,
        });
      }
      if (assetFiles.length) {
        initialAssetUploadStarted = true;
        setActivity({
          key: "create-assets:" + createdProject.id,
          status: "submitting",
          title: "Uploading study assets",
          detail: "Adding the selected files before requesting directions.",
        }, epoch);
        await uploadAssetFiles(createdProject.id, assetFiles, (message) => {
          setActivity({
            key: "create-assets:" + createdProject.id,
            status: "submitting",
            title: "Uploading study assets",
            detail: message,
          }, epoch);
        });
      }
      state.creatingProject = false;
      form.reset();
      $("#upload-name").textContent = "＋ Reference image";
      $("#initial-assets-name").textContent = "＋ Files";
      await openProject(createdProject.id);
      await requestDirections("");
    } catch (error) {
      state.creatingProject = false;
      if (createdProject) {
        await openProject(createdProject.id);
        setBanner("Study saved; its initial setup stopped: " + error.message, true, false);
        if (initialAssetUploadStarted) setAssetUploadStatus(error.message, true);
        showToast(error.message);
        await loadProjects().catch(() => {});
        return;
      }
      setBusy(false, epoch);
      setActivity({
        key: "create-failed:" + epoch,
        status: "failed",
        title: "Study was not created",
        detail: error.message,
      }, epoch);
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      await loadProjects().catch(() => {});
    }
  }

  async function uploadProjectAssets(event) {
    event.preventDefault();
    if (!state.projectId || !state.project) return;
    if (state.busy || state.assetsUploading) {
      setAssetUploadStatus("Wait for the current operation to finish before adding assets.", true);
      return;
    }
    const input = $("#project-assets-input");
    const files = Array.from(input.files || []);
    if (!files.length) {
      setAssetUploadStatus("Choose one or more files first.", true);
      return;
    }
    const projectId = state.projectId;
    const epoch = state.viewEpoch;
    state.assetsUploading = true;
    setBusy(true, epoch);
    setAssetUploadStatus("Preparing to upload " + files.length + " file(s)…");
    try {
      await uploadAssetFiles(projectId, files, setAssetUploadStatus);
      await refreshProjectAssets(projectId, epoch);
      input.value = "";
      setAssetUploadStatus("Added " + files.length + " file(s) to this study.");
    } catch (error) {
      await refreshProjectAssets(projectId, epoch).catch(() => {});
      input.value = "";
      setAssetUploadStatus(error.message, true);
      showToast(error.message);
    } finally {
      state.assetsUploading = false;
      setBusy(false, state.viewEpoch);
    }
  }

  async function startRequest({ path, body = {}, slot, kind, alternativeId = null, method = "POST" }) {
    if (state.assetsUploading) {
      showToast("Wait for the asset upload to finish before starting an operation.");
      return;
    }
    if (state.busy) return;
    const projectId = state.projectId;
    const epoch = state.viewEpoch;
    const key = idempotencyKey(slot);
    const isDirections = kind === "directions";
    setActivity({
      key: "submission:" + projectId + ":" + hashText(slot),
      status: "submitting",
      title: isDirections ? "Submitting directions request" : "Submitting prototype request",
      detail: "Saving the request before generation starts.",
    }, epoch);
    setBusy(true, epoch);
    setBanner("Saving this operation before it starts…", false, true, epoch);
    try {
      const payload = await api(path, {
        method,
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify(body),
      });
      const operation = {
        id: payload.operation_id,
        kind,
        alternativeId: alternativeId || payload.alternative_id || null,
        projectId,
        slot,
        epoch,
      };
      if (projectId) sessionStorage.setItem(operationStorageKey(projectId), JSON.stringify(operation));
      return await watchOperation(operation);
    } catch (error) {
      setBusy(false, epoch);
      setActivity({
        key: "request-failed:" + projectId + ":" + hashText(slot),
        status: "failed",
        title: "Request not started",
        detail: error.message,
      }, epoch);
      setBanner(error.message, true, false, epoch);
      if (epoch === state.viewEpoch && projectId === state.projectId) showToast(error.message);
      return null;
    }
  }

  function hashText(value) {
    let hash = 2166136261;
    for (const character of String(value)) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  async function requestDirections(steer = "") {
    if (!state.projectId) return null;
    const slot = state.projectId + ":directions:" + hashText(steer);
    return startRequest({
      path: "/api/projects/" + encodeURIComponent(state.projectId) + "/directions",
      body: { steer },
      slot,
      kind: "directions",
    });
  }

  async function chooseDirection(operationId, directionId) {
    if (!operationId || !state.projectId) return;
    const slot = state.projectId + ":select:" + operationId + ":" + directionId;
    await startRequest({
      path: "/api/projects/" + encodeURIComponent(state.projectId) + "/alternatives",
      body: {
        direction_operation_id: operationId,
        direction_id: directionId,
        additional_instruction: "",
      },
      slot,
      kind: "prototype",
    });
  }

  async function retryAlternative(alternativeId) {
    const slot = state.projectId + ":retry:" + alternativeId;
    await startRequest({
      path: "/api/alternatives/" + encodeURIComponent(alternativeId) + "/generate",
      body: {},
      slot,
      kind: "prototype",
      alternativeId,
    });
  }

  function detachWatchers() {
    for (const watcher of state.watchers.values()) {
      watcher.cancelled = true;
      if (watcher.controller) watcher.controller.abort();
    }
    state.watchers.clear();
  }

  async function watchOperation(operationInfo) {
    const { id: operationId, kind, slot, projectId, epoch } = operationInfo;
    const existing = state.watchers.get(operationId);
    if (existing) return existing.promise;
    const watcher = { cancelled: false, controller: null, promise: null };
    const isCurrent = () => !watcher.cancelled
      && epoch === state.viewEpoch
      && projectId === state.projectId;
    watcher.promise = (async () => {
      if (!isCurrent()) return null;
      const operationKey = "operation:" + projectId + ":" + operationId;
      if (!state.activity || state.activity.key !== operationKey) {
        setActivity({
          key: operationKey,
          status: "opening",
          title: "Checking saved operation",
          detail: "Reading its persisted status.",
        }, epoch);
      }
      setBusy(true, epoch);
      setBanner("Working on your " + (kind === "directions" ? "directions" : "prototype") + "…", false, true, epoch);
      let latestOperation = null;
      for (;;) {
        try {
          watcher.controller = new AbortController();
          const operation = await api("/api/operations/" + encodeURIComponent(operationId), { signal: watcher.controller.signal });
          watcher.controller = null;
          if (!isCurrent()) return null;
          latestOperation = operation;
          setOperationActivity(operation, projectId, epoch);
          if (["queued", "running"].includes(operation.status)) {
            setBanner(operation.status === "queued" ? "Waiting for the studio to begin…" : "Working on your " + (kind === "directions" ? "directions" : "prototype") + "…", false, true, epoch);
            await delay(900);
            if (!isCurrent()) return null;
            continue;
          }
          const stored = readActiveOperation(projectId);
          if (stored && stored.id === operationId) sessionStorage.removeItem(operationStorageKey(projectId));
          const legacy = readActiveOperationLegacy();
          if (legacy && legacy.id === operationId) sessionStorage.removeItem("studio-active-operation");
          clearIdempotencyKey(slot);
          setBusy(false, epoch);
          if (operation.status !== "succeeded") {
            setBanner(
              operation.error || "This operation stopped before a result was saved.", true, false, epoch,
              operation.has_partial ? "/api/operations/" + encodeURIComponent(operationId) + "/partial" : null,
            );
            await reloadProjectAfterOperation(projectId, epoch);
            return operation;
          }
          setBanner(kind === "directions" ? "Three directions are saved. Choose one to make it runnable." : "A new version is saved in exploration history.", false, false, epoch);
          await reloadProjectAfterOperation(projectId, epoch);
          if (!isCurrent()) return operation;
          if (kind !== "directions" && operation.result && operation.result.revision_id) {
            await openRevision(operation.result.revision_id, false, { projectId, epoch });
          }
          if (isCurrent()) await loadProjects();
          return operation;
        } catch (error) {
          watcher.controller = null;
          if (!isCurrent() || error.name === "AbortError") return null;
          if (latestOperation && !["queued", "running"].includes(latestOperation.status)) {
            setOperationActivity(latestOperation, projectId, epoch);
            setBanner("Saved operation; reconnecting to this study… " + error.message, true, true, epoch);
          } else {
            setActivity({
              key: operationKey,
              status: "reconnecting",
              title: "Reconnecting to saved operation",
              detail: error.message,
              createdAt: latestOperation && latestOperation.created_at,
            }, epoch);
            setBanner("Reconnecting to the saved operation… " + error.message, true, true, epoch);
          }
          await delay(1500);
        }
      }
    })().finally(() => {
      if (state.watchers.get(operationId) === watcher) state.watchers.delete(operationId);
    });
    state.watchers.set(operationId, watcher);
    return watcher.promise;
  }

  async function reloadProjectAfterOperation(projectId, epoch) {
    if (epoch !== state.viewEpoch || projectId !== state.projectId) return;
    const project = await api("/api/projects/" + encodeURIComponent(projectId));
    if (epoch !== state.viewEpoch || projectId !== state.projectId) return;
    state.project = project;
    showProject();
  }

  function renderSuggestions(revision) {
    const box = $("#next-steps");
    const list = $("#next-steps-list");
    const status = $("#next-steps-status");
    const retry = $("#next-steps-retry");
    const token = ++state.suggestToken;
    list.replaceChildren();
    status.replaceChildren();
    status.hidden = true;
    retry.hidden = true;
    box.hidden = false;
    if (revision.suggestions_status === "pending") {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.textContent = "Thinking of three next steps…";
      status.append(spinner, text);
      status.hidden = false;
      watchSuggestions(revision.id, token);
      return;
    }
    if (revision.suggestions_status === "ready" && revision.suggestions.length === 3) {
      list.replaceChildren(...revision.suggestions.map((suggestion) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "next-step";
        button.disabled = state.busy;
        const kind = document.createElement("span");
        kind.className = "next-step-kind";
        kind.textContent = suggestion.kind;
        const title = document.createElement("b");
        title.textContent = suggestion.title;
        const change = document.createElement("span");
        change.textContent = suggestion.change;
        button.append(kind, title, change);
        button.addEventListener("click", () => chooseSuggestion(suggestion));
        item.append(button);
        return item;
      }));
      return;
    }
    if (revision.suggestions_error) {
      status.textContent = "Next steps could not be made: " + revision.suggestions_error;
      status.hidden = false;
    }
    retry.hidden = false;
  }

  async function watchSuggestions(revisionId, token) {
    for (;;) {
      await delay(2000);
      if (token !== state.suggestToken) return;
      try {
        const revision = await api("/api/revisions/" + encodeURIComponent(revisionId));
        if (token !== state.suggestToken) return;
        if (revision.suggestions_status !== "pending") {
          state.revision = revision;
          renderSuggestions(revision);
          return;
        }
      } catch {
        // The studio may be restarting. The next poll tries again.
      }
    }
  }

  async function requestSuggestions() {
    if (!state.activeRevisionId) return;
    const revisionId = state.activeRevisionId;
    try {
      await api("/api/revisions/" + encodeURIComponent(revisionId) + "/suggestions", { method: "POST" });
      if (revisionId === state.activeRevisionId) renderSuggestions({ id: revisionId, suggestions_status: "pending", suggestions: [] });
    } catch (error) {
      showToast(error.message);
    }
  }

  function chooseSuggestion(suggestion) {
    if (state.busy) return;
    clearTargets();
    setField($("#refine-instruction"), suggestion.change);
    $("#refine-form").requestSubmit();
  }

  async function submitRefinement(event) {
    event.preventDefault();
    if (!state.activeRevisionId) return;
    const instruction = $("#refine-instruction").value.trim();
    if (!instruction) return;
    const projectId = state.projectId;
    const epoch = state.viewEpoch;
    const targets = state.targets.map((item) => ({ ...item }));
    const slot = projectId + ":refine:" + state.activeRevisionId + ":"
      + hashText(instruction + targets.map((item) => item.path).join("|"));
    const result = await startRequest({
      path: "/api/revisions/" + encodeURIComponent(state.activeRevisionId) + "/refine",
      body: { instruction, targets },
      slot,
      kind: "refine",
    });
    if (result && result.status === "succeeded" && epoch === state.viewEpoch && projectId === state.projectId
        && $("#refine-instruction").value.trim() === instruction) {
      setField($("#refine-instruction"), "");
      clearTargets();
    }
  }

  function showWelcome() {
    if (state.assetsUploading || state.creatingProject) {
      showToast("Wait for this setup to finish before changing studies.");
      return;
    }
    detachWatchers();
    const epoch = ++state.viewEpoch;
    setActivity(null, epoch);
    setBusy(false, epoch);
    setBanner("", false, false, epoch);
    state.project = null;
    state.projectId = null;
    state.activeRevisionId = null;
    state.revision = null;
    state.activeAlternativeId = null;
    state.showDirections = false;
    localStorage.removeItem("studio-project-id");
    syncView();
    $("#welcome-view").hidden = false;
    $("#project-view").hidden = true;
    drawHistory();
    loadProjects().catch(() => {});
    $("#project-brief").focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  const CUSTOM_MODEL = "__custom__";

  function presetById(id) {
    return (state.settings.presets || []).find((preset) => preset.id === id);
  }

  function selectedModel() {
    const choice = $("#provider-model").value;
    return choice === CUSTOM_MODEL ? $("#provider-model-custom").value.trim() : choice;
  }

  function syncCustomModel() {
    $("#provider-model-custom").hidden = $("#provider-model").value !== CUSTOM_MODEL;
  }

  function fillModelSuggestions(preset, current) {
    const live = preset && preset.id === "openrouter" ? state.popularModels : [];
    const models = [...new Set([...(preset ? preset.models : []), ...live])];
    $("#provider-model").replaceChildren(
      ...models.map((model) => new Option(model, model)),
      new Option("Custom model ID…", CUSTOM_MODEL),
    );
    const listed = models.includes(current);
    $("#provider-model").value = listed ? current : CUSTOM_MODEL;
    $("#provider-model-custom").value = listed ? "" : current;
    syncCustomModel();
  }

  async function loadPopularModels() {
    try {
      state.popularModels = (await api("/api/provider-models")).models;
    } catch {
      state.popularModels = [];
    }
    fillModelSuggestions(presetById($("#provider-preset").value), selectedModel());
  }

  function selectPreset() {
    const preset = presetById($("#provider-preset").value);
    if (preset && preset.url) $("#provider-url").value = preset.url;
    let current = selectedModel();
    if (preset && preset.models.length && !preset.models.includes(current)) current = preset.models[0];
    fillModelSuggestions(preset, current);
  }

  function fillSettingsForm() {
    const presets = state.settings.presets || [];
    $("#provider-preset").replaceChildren(...presets.map((preset) => new Option(preset.label, preset.id)));
    const match = presets.find((preset) => preset.url && preset.url === state.settings.provider_url);
    $("#provider-preset").value = match ? match.id : "custom";
    fillModelSuggestions(presetById($("#provider-preset").value), state.settings.model || "");
    $("#provider-url").value = state.settings.provider_url || "";
    $("#code-max-tokens").value = state.settings.code_max_tokens || 12000;
    $("#request-timeout").value = state.settings.request_timeout || 180;
    $("#reasoning-effort").value = state.settings.reasoning_effort || "";
    $("#provider-key").value = "";
    $("#key-state").textContent = state.settings.key_configured ? "Saved on this machine" : "Not configured";
    $("#vision-supported").checked = Boolean(state.settings.vision_supported);
    $("#auto-repair").checked = state.settings.auto_repair !== false;
    $("#settings-warning").textContent = state.settings.configuration_error || "";
    $("#settings-error").hidden = true;
    $("#clear-key-button").disabled = !state.settings.key_configured;
    state.clearKeyNext = false;
  }

  async function openSettings() {
    await loadSettings();
    fillSettingsForm();
    $("#settings-dialog").showModal();
    loadPopularModels();
  }

  async function saveSettings(event) {
    event.preventDefault();
    const body = {
      provider_url: $("#provider-url").value.trim(),
      model: selectedModel(),
      code_max_tokens: Number.parseInt($("#code-max-tokens").value, 10),
      request_timeout: Number.parseInt($("#request-timeout").value, 10),
      reasoning_effort: $("#reasoning-effort").value,
      vision_supported: $("#vision-supported").checked,
      auto_repair: $("#auto-repair").checked,
      clear_api_key: state.clearKeyNext,
    };
    const key = $("#provider-key").value;
    if (key) body.api_key = key;
    try {
      state.settings = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      fillSettingsForm();
      modeBadge();
      $("#settings-dialog").close();
      showToast("Provider settings saved on this machine.");
      if (state.project) showProject();
    } catch (error) {
      $("#settings-error").textContent = error.message;
      $("#settings-error").hidden = false;
    }
  }

  function switchView(view) {
    const preview = view === "preview";
    $("#preview-panel").hidden = !preview;
    $("#source-panel").hidden = preview;
    $("#preview-tab").classList.toggle("active", preview);
    $("#source-tab").classList.toggle("active", !preview);
    $("#preview-tab").setAttribute("aria-selected", String(preview));
    $("#source-tab").setAttribute("aria-selected", String(!preview));
  }

  function bindEvents() {
    $("#create-project-form").addEventListener("submit", createProject);
    $("#new-project-button").addEventListener("click", showWelcome);
    $("#explore-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const steer = $("#direction-steer").value.trim();
      const projectId = state.projectId;
      const epoch = state.viewEpoch;
      requestDirections(steer).then((result) => {
        if (result && result.status === "succeeded" && epoch === state.viewEpoch && projectId === state.projectId
            && $("#direction-steer").value.trim() === steer) {
          setField($("#direction-steer"), "");
        }
      });
    });
    $("#first-explore").addEventListener("click", () => requestDirections(""));
    $("#refine-form").addEventListener("submit", submitRefinement);
    $("#next-steps-retry").addEventListener("click", requestSuggestions);
    $("#open-settings").addEventListener("click", openSettings);
    $("#settings-form").addEventListener("submit", saveSettings);
    $("#provider-preset").addEventListener("change", selectPreset);
    $("#provider-model").addEventListener("change", syncCustomModel);
    $("#clear-key-button").addEventListener("click", () => {
      if (!state.settings.key_configured) return;
      state.clearKeyNext = true;
      $("#provider-key").value = "";
      $("#key-state").textContent = "Will be cleared when saved";
      $("#settings-warning").textContent = "Save settings to clear the stored key.";
    });
    $("#back-to-directions").addEventListener("click", () => {
      state.showDirections = true;
      syncView();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    $("#back-to-canvas").addEventListener("click", () => {
      state.showDirections = false;
      syncView();
    });
    const detailsDialog = $("#details-dialog");
    const openDetails = () => detailsDialog.showModal();
    $("#open-details").addEventListener("click", openDetails);
    $("#open-files").addEventListener("click", openDetails);
    detailsDialog.addEventListener("click", (event) => {
      if (event.target === detailsDialog) detailsDialog.close();
    });
    $("#setup-open-settings").addEventListener("click", openSettings);
    $("#fullscreen-button").addEventListener("click", () => {
      const shell = $("#preview-shell");
      if (document.fullscreenElement) document.exitFullscreen();
      else if (shell.requestFullscreen) shell.requestFullscreen().catch((error) => showToast(error.message));
    });
    $("#preview-tab").addEventListener("click", () => switchView("preview"));
    $("#source-tab").addEventListener("click", () => switchView("source"));
    $("#reference-image").addEventListener("change", () => {
      const file = $("#reference-image").files[0];
      $("#upload-name").textContent = file ? file.name : "＋ Reference image";
    });
    $("#initial-assets").addEventListener("change", () => {
      const files = Array.from($("#initial-assets").files || []);
      $("#initial-assets-name").textContent = files.length === 0 ? "＋ Files"
        : files.length === 1 ? files[0].name : files.length + " files selected";
    });
    $("#asset-upload-form").addEventListener("submit", uploadProjectAssets);
    window.addEventListener("message", (event) => {
      const frame = $("#prototype-frame");
      if (event.source !== frame.contentWindow || event.origin !== "null") return;
      const data = event.data || {};
      if (data.type === "possibility-studio-pick") addTarget(data);
      if (data.type === "possibility-studio-inspect-toggle") $("#inspect-toggle").click();
      if (data.type === "possibility-studio-inspect-exit" && $("#inspect-toggle").getAttribute("aria-pressed") === "true") {
        $("#inspect-toggle").click();
      }
      if (event.data && event.data.type === "possibility-studio-preview-error") {
        const message = typeof event.data.message === "string" ? event.data.message : "Runtime error";
        $("#preview-error").textContent = "Observed preview runtime error: " + message;
        $("#preview-error").hidden = false;
      }
    });
    $("#settings-dialog").addEventListener("click", (event) => {
      if (event.target === $("#settings-dialog")) $("#settings-dialog").close();
    });
  }

  const MAX_TARGETS = 5;

  function renderTargets() {
    const list = $("#target-list");
    list.replaceChildren(...state.targets.map((target, index) => {
      const chip = document.createElement("li");
      chip.className = "target-chip";
      const label = document.createElement("span");
      label.textContent = (target.name || target.tag) + (target.text ? ": " + shortText(target.text, 30) : "");
      label.title = target.path;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "Remove " + (target.name || target.tag));
      remove.addEventListener("click", () => {
        state.targets.splice(index, 1);
        renderTargets();
      });
      chip.append(label, remove);
      return chip;
    }));
    list.hidden = state.targets.length === 0;
  }

  function clearTargets() {
    state.targets = [];
    renderTargets();
  }

  function addTarget(pick) {
    if (typeof pick.path !== "string" || !pick.path) return;
    if (state.targets.some((item) => item.path === pick.path)) return;
    if (state.targets.length >= MAX_TARGETS) {
      showToast("Pick up to " + MAX_TARGETS + " parts. Remove one to add another.");
      return;
    }
    state.targets.push({
      name: String(pick.name || "").slice(0, 100),
      tag: String(pick.tag || "").slice(0, 30),
      path: pick.path.slice(0, 300),
      text: String(pick.text || "").slice(0, 120),
    });
    renderTargets();
    $("#refine-instruction").focus();
  }

  function setupInspector() {
    const toggle = $("#inspect-toggle");
    let inspecting = false;

    function tellFrame() {
      const frame = $("#prototype-frame");
      if (frame.contentWindow) frame.contentWindow.postMessage({ type: "possibility-studio-inspect", on: inspecting }, "*");
    }

    $("#prototype-frame").addEventListener("load", tellFrame);

    function setInspecting(on) {
      inspecting = on;
      tellFrame();
      toggle.setAttribute("aria-pressed", String(on));
    }

    toggle.addEventListener("click", () => setInspecting(!inspecting));
    document.addEventListener("keydown", (event) => {
      if (event.altKey && event.code === "KeyI") setInspecting(!inspecting);
      if (event.key === "Escape" && inspecting) setInspecting(false);
    });
  }

  function setupTheme() {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const toggle = $("#theme-toggle");
    const current = () => document.documentElement.dataset.theme || (query.matches ? "dark" : "light");
    const paint = () => {
      const dark = current() === "dark";
      toggle.setAttribute("aria-pressed", String(dark));
      toggle.textContent = "Theme: " + (dark ? "Dark" : "Light");
      document.querySelector('meta[name="theme-color"]').content = dark ? "#1b2130" : "#f2eee5";
    };
    toggle.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("studio-theme", next); } catch { /* the choice lasts for this visit */ }
      paint();
    });
    query.addEventListener("change", paint);
    paint();
  }

  function setupMenus() {
    const menus = Array.from(document.querySelectorAll("details.menu"));
    document.addEventListener("click", (event) => {
      for (const menu of menus) {
        if (!menu.open) continue;
        const inside = menu.contains(event.target);
        if (!inside || event.target.closest(".menu-panel button")) menu.open = false;
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") menus.forEach((menu) => { menu.open = false; });
    });
  }

  function setupDock() {
    const dock = $("#refine-section");
    const measure = () => document.body.style.setProperty("--dock-h", (dock.hidden ? 0 : dock.offsetHeight) + "px");
    if (window.ResizeObserver) new ResizeObserver(measure).observe(dock);
    new MutationObserver(measure).observe(dock, { attributes: true, attributeFilter: ["hidden"] });
    measure();
  }

  const FIELD_DRAFT_PREFIX = "studio-draft:";

  function draftKey(field) {
    return FIELD_DRAFT_PREFIX + field.id + (field.id === "project-brief" || field.id === "project-title" ? "" : ":" + (state.projectId || ""));
  }

  function fitField(field) {
    if (!field.classList.contains("grow")) return;
    field.style.height = "auto";
    const limit = Math.round(window.innerHeight * 0.4);
    field.style.height = Math.min(field.scrollHeight + 2, limit) + "px";
    field.style.overflowY = field.scrollHeight + 2 > limit ? "auto" : "hidden";
  }

  function setField(field, value) {
    field.value = value;
    field.dispatchEvent(new Event("input"));
  }

  function loadProjectDrafts() {
    for (const id of ["#refine-instruction", "#direction-steer"]) {
      const field = $(id);
      field.value = "";
      if (field.restoreDraft) field.restoreDraft();
    }
  }

  function setupFields() {
    document.querySelectorAll("textarea[maxlength], #project-title").forEach((field) => {
      const max = Number(field.getAttribute("maxlength"));
      const counter = document.createElement("small");
      counter.className = "field-counter";
      counter.hidden = true;
      field.insertAdjacentElement("afterend", counter);
      const update = () => {
        fitField(field);
        const near = field.value.length >= max * 0.8;
        counter.hidden = !near;
        counter.textContent = field.value.length + " / " + max;
      };
      field.addEventListener("input", () => {
        update();
        try {
          if (field.value) localStorage.setItem(draftKey(field), field.value);
          else localStorage.removeItem(draftKey(field));
        } catch { /* drafts are optional */ }
      });
      field.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey) || !field.form) return;
        event.preventDefault();
        field.form.requestSubmit();
      });
      field.restoreDraft = () => {
        try {
          const saved = localStorage.getItem(draftKey(field));
          if (saved && !field.value) field.value = saved;
        } catch { /* drafts are optional */ }
        update();
      };
      field.restoreDraft();
      if (field.form) field.form.addEventListener("reset", () => setTimeout(() => field.dispatchEvent(new Event("input")), 0));
    });
    window.addEventListener("resize", () => document.querySelectorAll("textarea.grow").forEach(fitField));
  }

  async function boot() {
    bindEvents();
    setupFields();
    setupInspector();
    setupBuildView();
    setupTheme();
    setupMenus();
    setupDock();
    await loadSettings();
    try {
      await loadProjects();
      const projectId = localStorage.getItem("studio-project-id");
      if (projectId) await openProject(projectId);
    } catch (error) {
      showToast(error.message);
    }
  }

  boot();
})();
