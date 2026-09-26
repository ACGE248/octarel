/* OctAges Orchestrator Control Center — no build step, polls JSON endpoints.
 *
 * Every refresh below is a plain same-origin GET against this dashboard's own
 * FastAPI process; nothing here ever calls a provider/model API. Control
 * actions POST to /api/commands/<verb> or /api/steering/*, the same command
 * layer the CLI uses. Local notifications use the browser Notification API
 * only — no external notification service.
 */
(function () {
  "use strict";

  const POLL_MS = 2000;
  const THEME_KEY = "octages-orchestrator-theme";
  const NOTIF_KEY = "octages-orchestrator-notifications";
  const RAIL_KEY = "octages-orchestrator-rail";
  const SESSION_START_KEY = "octages-orchestrator-session-start";
  const COMPLETED_STATES = ["SUCCEEDED", "READY_LOCAL", "READY_BUT_UNMERGED"];
  const QUEUED_STATES = ["QUEUED", "PENDING", "PAUSED"];
  const BLOCKED_STATES = ["BLOCKED", "FAILED"];
  const ACTIVE_TASK_STATES = ["RUNNING"];
  const DESKTOP_MQ = "(min-width: 768px)";

  const state = {
    identity: { name: "Local Developer", role: "Developer", branch: "UNKNOWN", source: "fallback" },
    tasks: [],
    flow: { nodes: [], edges: [], concurrent_running: [] },
    workflow: { task: null, orchestrator: null, stages: [], worktrees: [], results: [] },
    providers: [],
    models: [],
    worktrees: [],
    events: [],
    telemetry: { providers: [], quota_windows: [], checkpoints: [] },
    attentionCount: 0,
    activityHours: 24,
    presets: [],
    runbooks: [],
    quickstart: [],
    usageRefreshedAt: null,
  };

  // Overview pipeline diagram: remembers each task node's state across polls
  // (keyed by task id) purely so a real state transition can get a one-shot
  // visual pulse instead of silently redrawing identical-looking boxes every
  // 2s. Replaced wholesale on each render — never grows unbounded.
  let pipelinePrevTaskStates = new Map();
  let connectTerminalView = null;

  // --------------------------------------------------------------------- helpers

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value);
    });
    (children || []).forEach((child) => {
      if (child) node.appendChild(child);
    });
    return node;
  }

  // OCTAREL-UI-03: <details> disclosures inside lists that re-render every poll
  // must not collapse under the operator; open state is remembered by key.
  const disclosureOpen = new Set();
  function rememberDisclosure(details, key) {
    details.open = disclosureOpen.has(key);
    details.addEventListener("toggle", () => {
      if (details.open) disclosureOpen.add(key);
      else disclosureOpen.delete(key);
    });
    return details;
  }

  function elapsedBetween(startIso, endIso) {
    if (!startIso) return null;
    const start = new Date(startIso).getTime();
    const end = endIso ? new Date(endIso).getTime() : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
    const secs = Math.round((end - start) / 1000);
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ${secs % 60}s`;
    const hrs = Math.floor(mins / 60);
    return hrs < 48 ? `${hrs}h ${mins % 60}m` : `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
  }

  // Set once the Overview pipeline renders; opens the Agent Activity viewer for a worker.
  let openWorkerActivity = null;

  // A status chip whose meaning is carried by text (and a glyph), never colour alone.
  const STATUS_GLYPHS = { running: "●", queued: "◷", blocked: "⏸", failed: "✕", complete: "✓", paused: "❚❚", other: "○" };
  function statusGlyphFor(stateName) {
    const s = String(stateName || "").toUpperCase();
    if (s === "RUNNING") return STATUS_GLYPHS.running;
    if (["QUEUED", "PENDING", "DRAFT"].includes(s)) return STATUS_GLYPHS.queued;
    if (["BLOCKED", "OWNER_ACTION_REQUIRED"].includes(s)) return STATUS_GLYPHS.blocked;
    if (["FAILED", "CANCELLED", "DEADLINE_REACHED"].includes(s)) return STATUS_GLYPHS.failed;
    if (["SUCCEEDED", "READY_LOCAL", "READY_BUT_UNMERGED"].includes(s)) return STATUS_GLYPHS.complete;
    if (["PAUSED", "STOPPING"].includes(s)) return STATUS_GLYPHS.paused;
    return STATUS_GLYPHS.other;
  }

  // Append only real nodes: Node.append(null) would insert the text "null".
  function appendAll(node, children) {
    children.filter(Boolean).forEach((child) => node.appendChild(child));
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  // Makes any node (e.g. a pipeline diagram box) keyboard- and
  // screen-reader-accessible as a real button without changing its visual
  // tag, per the repo's "every interactive control needs an accessible name
  // and known safety class rule in the maintained frontend/accessibility contracts.
  function wireActivate(node, handler, label) {
    node.classList.add("pipe-node-clickable");
    node.setAttribute("role", "button");
    node.setAttribute("tabindex", "0");
    if (label) node.setAttribute("aria-label", label);
    node.addEventListener("click", handler);
    node.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        handler(evt);
      }
    });
  }

  let agentActivityPollId = null;
  function stopAgentActivityPoll() {
    if (agentActivityPollId) {
      clearInterval(agentActivityPollId);
      agentActivityPollId = null;
    }
  }

  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }

  async function postJSON(path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    let body = null;
    try {
      body = await res.json();
    } catch (err) {
      /* no body */
    }
    return { ok: res.ok, status: res.status, body };
  }

  async function postCommand(verb, payload) {
    return postJSON(`/api/commands/${verb}`, payload);
  }

  function initials(name) {
    const parts = String(name || "LD")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (!parts.length) return "LD";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function greetingWord(date) {
    const h = date.getHours();
    if (h < 12) return "Good morning";
    if (h < 17) return "Good afternoon";
    return "Good evening";
  }

  function relativeTime(iso) {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return "UNKNOWN";
    const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (sec < 60) return `${sec}s ago`;
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.round(min / 60);
    if (hr < 48) return `${hr}h ago`;
    const day = Math.round(hr / 24);
    return `${day}d ago`;
  }

  function platformHint() {
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || "") || /Mac OS/.test(navigator.userAgent || "");
    return isMac ? "⌘K" : "Ctrl K";
  }

  function taskCounts(tasks) {
    const counts = { running: 0, queued: 0, paused: 0, completed: 0, blocked: 0, total: tasks.length };
    tasks.forEach((t) => {
      if (t.state === "RUNNING") counts.running += 1;
      else if (["QUEUED", "PENDING"].includes(t.state)) counts.queued += 1;
      else if (t.state === "PAUSED") counts.paused += 1;
      else if (COMPLETED_STATES.includes(t.state)) counts.completed += 1;
      else if (BLOCKED_STATES.includes(t.state) && !t.superseded) counts.blocked += 1;
    });
    return counts;
  }

  function statusClass(value) {
    return `st-${String(value || "unknown").toLowerCase()}`;
  }

  // ENG-AGENT-02-S7 (issue #97): truthful, specific unavailability reasons —
  // never collapse every cause into a bare "NOT_CONFIGURED" badge. Mirrors
  // registry.KNOWN_AVAILABILITY_REASONS exactly.
  const AVAILABILITY_REASON_LABELS = {
    AVAILABLE: "Available",
    DISABLED: "Disabled",
    CLI_MISSING: "CLI not installed",
    NOT_AUTHENTICATED: "Not authenticated — sign in with the CLI, then Probe",
    API_ONLY_NOT_AUTHORIZED: "API-only access not authorized",
    UNSUPPORTED: "Unsupported in this environment",
    CONFIGURATION_ERROR: "Configuration error",
    CATALOG_ONLY: "No adapter in this repository yet",
    LAUNCH_ENVIRONMENT_ERROR: "Launcher could not reach the CLI session — not a sign-out, then Probe",
  };

  function availabilityReasonLabel(reason) {
    return AVAILABILITY_REASON_LABELS[reason] || "Unknown";
  }

  // ENG-AGENT-02-S7 (issue #97): one shared provider-icon registry reused by
  // every surface (Providers, Agents, the overview pipeline, the workflow
  // graph) so a provider never has a different mark in two places. These are
  // deliberately original, brand-color-associated geometric glyphs — never a
  // reproduction of a real trademarked logo mark, since this repository has
  // no way to verify redistribution rights for one. Locally defined inline
  // SVG only; no external icon font, no CDN.
  const PROVIDER_ICONS = {
    // Original angular sunburst (rays + core), evoking Anthropic's public
    // brand identity abstractly -- never a traced/reproduced copy of the
    // actual mark -- and legible as a small badge instead of reading as a
    // bare letter.
    anthropic: {
      label: "Anthropic",
      bg: "#D97757",
      asset: "/static/assets/icons/anthropic.svg",
      svg:
        '<g fill="#fff">' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1"/>' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1" transform="rotate(60 12 12)"/>' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1" transform="rotate(120 12 12)"/>' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1" transform="rotate(180 12 12)"/>' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1" transform="rotate(240 12 12)"/>' +
        '<rect x="11" y="3" width="2" height="5.4" rx="1" transform="rotate(300 12 12)"/>' +
        '<circle cx="12" cy="12" r="3" />' +
        "</g>",
    },
    // Three connected nodes (a small "network"), standing in for an
    // original, non-trademarked OpenAI/Codex mark.
    openai: {
      label: "OpenAI",
      bg: "#0F1420",
      svg:
        '<g stroke="#fff" stroke-width="1.4" stroke-linecap="round" fill="#fff">' +
        '<path d="M12 9.6 8.6 13.4M12 9.6 15.4 13.4M9 15h6" fill="none"/>' +
        '<circle cx="12" cy="7.5" r="2.1" stroke="none"/>' +
        '<circle cx="7.5" cy="15" r="2.1" stroke="none"/>' +
        '<circle cx="16.5" cy="15" r="2.1" stroke="none"/>' +
        "</g>",
    },
    // Four-color pinwheel (Google's own brand palette) instead of a
    // sparkle/star silhouette that would read as a direct copy of the real
    // Gemini glyph. Rendered on a white badge so all four colors stay
    // legible in both themes.
    google: {
      label: "Google",
      bg: "#fff",
      asset: "/static/assets/icons/google.svg",
      svg:
        '<path d="M12 12 12 5A7 7 0 0 1 19 12Z" fill="#4285F4"/>' +
        '<path d="M12 12 19 12A7 7 0 0 1 12 19Z" fill="#34A853"/>' +
        '<path d="M12 12 12 19A7 7 0 0 1 5 12Z" fill="#FBBC05"/>' +
        '<path d="M12 12 5 12A7 7 0 0 1 12 5Z" fill="#EA4335"/>' +
        '<circle cx="12" cy="12" r="2.4" fill="#fff"/>',
    },
    // Three forward-motion chevrons -- deliberately not the literal X-mark
    // shape (too close to reading as the bare letter "X" on a small badge).
    xai: {
      label: "xAI",
      bg: "#0B0B0F",
      asset: "/static/assets/icons/xai.svg",
      svg:
        '<g fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M7 6 11 12 7 18"/>' +
        '<path d="M12 6 16 12 12 18"/>' +
        '<path d="M17 9 19 12 17 15"/>' +
        "</g>",
    },
    antigravity: {
      label: "Antigravity",
      bg: "#6D5CE7",
      svg: '<path d="M7 16 12 6l5 10M8.7 13h6.6" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="18" r="1.4" fill="#fff"/>',
    },
    opencode2: {
      label: "OpenCode2",
      bg: "#167D6B",
      svg: '<path d="m10 7-5 5 5 5M14 7l5 5-5 5" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    },
    deepseek: {
      label: "DeepSeek",
      bg: "#1656C9",
      svg: '<path d="M7 9.5c1.4-2 3.4-3 5-3s3.6 1 5 3c-1.4 2-3.4 3-5 3s-3.6-1-5-3Zm5 6.5c-2.2 0-3.8-.9-5-2.3" fill="none" stroke="#fff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="9.5" r="1.1" fill="#fff"/>',
    },
    zhipu: {
      label: "Zhipu",
      bg: "#6E56CF",
      svg: '<path d="M8 8h8l-8 8h8" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    },
    omniroute: {
      label: "OmniRoute",
      bg: "#0E9488",
      svg: '<circle cx="7" cy="12" r="1.6" fill="#fff"/><circle cx="12" cy="7" r="1.6" fill="#fff"/><circle cx="17" cy="12" r="1.6" fill="#fff"/><path d="M8.4 11 10.8 8.2M13.2 8.2 15.6 11" stroke="#fff" stroke-width="1.2"/>',
    },
    cheaperinference: {
      label: "Cheaper Inference",
      bg: "#2F855A",
      svg: '<path d="M7 12 12 7l5 0 0 5-5 5Z" fill="none" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/><circle cx="14.5" cy="9.5" r="1" fill="#fff"/>',
    },
  };

  function providerIconKey(providerOrSystem) {
    const p = String(providerOrSystem || "").toLowerCase();
    if (p.includes("antigravity")) return "antigravity";
    if (p.includes("opencode")) return "opencode2";
    if (p.includes("anthropic") || p.includes("claude")) return "anthropic";
    if (p.includes("google") || p.includes("gemini")) return "google";
    if (p.includes("openai") || p.includes("codex")) return "openai";
    if (p.includes("xai") || p.includes("grok")) return "xai";
    if (p.includes("deepseek")) return "deepseek";
    if (p.includes("zhipu") || p.includes("glm")) return "zhipu";
    if (p.includes("omniroute")) return "omniroute";
    if (p.includes("cheaper")) return "cheaperinference";
    return null;
  }

  function providerMark(provider) {
    const key = providerIconKey(provider);
    if (key) return { key, letter: provider.slice(0, 1).toUpperCase(), label: PROVIDER_ICONS[key].label };
    if (!provider) return null;
    return { key: null, letter: String(provider).slice(0, 1).toUpperCase(), label: provider };
  }

  // Builds one <span class="provider-badge"> with the shared icon (or a
  // neutral lettered fallback when the provider has no known glyph, e.g. a
  // catalog-only or not-yet-mapped provider — this always renders something,
  // never a blank space).
  function providerBadge(providerOrSystem, opts) {
    const size = (opts && opts.size) || 22;
    const key = providerIconKey(providerOrSystem);
    const icon = key ? PROVIDER_ICONS[key] : null;
    const label = icon ? icon.label : String(providerOrSystem || "?");
    // Real accessible name (role="img" + aria-label), not aria-hidden: a
    // provider badge is sometimes the only visible indicator (e.g. the
    // compact icon row on the Overview pipeline), so hiding it entirely
    // from assistive tech would leave no accessible text fallback at all.
    const badge = el("span", {
      class: `provider-badge provider-${key || "generic"} ${size <= 20 ? "provider-badge-compact" : size >= 28 ? "provider-badge-card" : ""}`,
      title: label,
      role: "img",
      "aria-label": label,
    });
    if (icon && icon.asset) {
      badge.appendChild(el("img", { src: icon.asset, alt: "", width: Math.round(size * 0.68), height: Math.round(size * 0.68) }));
    } else if (icon) {
      badge.innerHTML = `<svg viewBox="0 0 24 24" width="${Math.round(size * 0.64)}" height="${Math.round(
        size * 0.64
      )}" focusable="false">${icon.svg}</svg>`;
    } else {
      badge.textContent = label.slice(0, 1).toUpperCase();
    }
    return badge;
  }

  function identityBadges(executionSystem, provider, opts) {
    const wrap = el("span", { class: "identity-badges" });
    const seen = new Set();
    [executionSystem, provider].filter(Boolean).forEach((value) => {
      const key = providerIconKey(value) || String(value).toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        wrap.appendChild(providerBadge(value, opts));
      }
    });
    return wrap;
  }

  function displayName(machineId) {
    const model = state.models.find((item) => item.worker === machineId);
    if (model && model.display_name) return model.display_name;
    return String(machineId || "Unknown")
      .split("-")
      .filter(Boolean)
      .map((part) => part.length <= 3 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ");
  }

  function renderKV(containerId, pairs) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = "";
    pairs.forEach(([label, value, tone]) => {
      container.appendChild(el("div", { class: "k", text: label }));
      // A problem value carries a glyph and stays plain text; colour is only reinforcement.
      container.appendChild(el("div", { class: tone ? `kv-${tone}` : "" }, [
        tone ? el("span", { class: "kv-glyph", "aria-hidden": "true", text: tone === "err" ? "✕ " : "⚠ " }) : null,
        document.createTextNode(String(value)),
      ]));
    });
  }

  function isDesktop() {
    return window.matchMedia(DESKTOP_MQ).matches;
  }

  // --------------------------------------------------------------------- theme

  /* Surfaces that cannot read CSS custom properties themselves (the xterm
     canvas today) register here so a theme change repaints them too. CSS-driven
     surfaces need no hook, and the SVG charts repaint on the next poll. */
  const themeListeners = [];
  function onThemeChange(fn) { themeListeners.push(fn); }

  function applyTheme(value) {
    const root = document.documentElement;
    if (value === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", value);
    themeListeners.forEach((fn) => {
      try { fn(); } catch (err) { /* a repaint failure must never break theming */ }
    });
  }

  function initTheme() {
    const select = document.getElementById("theme-select");
    let saved = "dark";
    try {
      saved = localStorage.getItem(THEME_KEY) || "dark";
    } catch (err) {
      saved = "dark";
    }
    if (select) select.value = saved;
    applyTheme(saved);
    if (select) {
      select.addEventListener("change", () => {
        applyTheme(select.value);
        const fb = document.getElementById("theme-feedback");
        try {
          localStorage.setItem(THEME_KEY, select.value);
          if (fb) fb.textContent = "Saved";
        } catch (err) {
          if (fb) fb.textContent = "Applied (could not be saved in this browser)";
        }
      });
    }
  }

  // --------------------------------------------------- model & session usage

  /* OCTAREL-UI-06 (issue #25). Renders /api/usage-telemetry. Each metric
     arrives with the class that established it, and the class is shown next to
     the value — a figure Octarel cannot establish reads NOT_EXPOSED with its
     reason rather than appearing as a plausible number. */

  /* "Input" is the total input the CLI reported. It is deliberately not
     labelled "fresh input": without cache categories there is no way to know
     how much of it was fresh versus a cache read, and calling it fresh would
     assert something unmeasured. Fresh input and cache reads therefore appear
     as their own explicitly unavailable metrics. */
  const USAGE_METRIC_ORDER = [
    ["input_tokens", "Input"],
    ["output_tokens", "Output"],
    ["total_tokens", "Tokens processed"],
    ["fresh_input_tokens", "Fresh input"],
    ["cache_read_tokens", "Cache reads"],
    ["cache_hit_rate", "Cache hit rate"],
    ["context_used_percent", "Context used"],
    ["duration_seconds", "Duration"],
    ["cost_usd", "Cost"],
  ];

  const USAGE_ESTABLISHED = new Set(["MEASURED", "DERIVED"]);

  function usageMetricCell(label, cell) {
    /* The class decides, not the presence of a value. A cell classed
       NOT_EXPOSED/UNKNOWN/NOT_APPLICABLE must never render as a figure even if
       a value is somehow present -- that would be exactly the fabrication this
       panel exists to prevent. */
    const known =
      USAGE_ESTABLISHED.has(cell.class) && cell.value !== null && cell.value !== undefined;
    const display = known
      ? (cell.unit === "tokens"
          ? Number(cell.value).toLocaleString()
          : cell.unit === "seconds"
            ? `${Number(cell.value).toFixed(0)}s`
            : cell.unit === "usd"
              ? `$${Number(cell.value).toFixed(4)}`
              : cell.unit === "percent"
                ? `${Number(cell.value)}%`
                : String(cell.value))
      : cell.class;

    return el("div", { class: `usage-metric${known ? "" : " is-unavailable"}` }, [
      el("dt", { text: label }),
      el("dd", { text: display }),
      // The provenance travels with the number, never implied by its absence.
      el("span", {
        class: "usage-metric-class",
        text: known ? cell.class : "",
        title: cell.reason || cell.formula || cell.source || "",
      }),
      cell.reason && !known ? el("p", { class: "usage-metric-reason", text: cell.reason }) : null,
    ]);
  }

  function renderUsageTelemetry(body) {
    const root = document.getElementById("usage-telemetry-rows");
    if (!root) return;
    const rows = (body && body.rows) || [];
    root.innerHTML = "";

    const note = document.getElementById("usage-telemetry-note");
    if (note) note.textContent = body && body.note ? body.note : "";

    if (!rows.length) {
      root.appendChild(el("p", { class: "hint", text: "No run has recorded usage yet." }));
      return;
    }

    rows.forEach((row) => {
      const a = row.attribution || {};
      const route = row.route || {};
      // Attribution is per row; figures from different workers are never
      // merged into a single number.
      const identity = [a.worker, a.provider, a.model].filter(Boolean).join(" · ");
      root.appendChild(
        el("article", { class: "entity-card usage-row" }, [
          el("header", { class: "usage-row-head" }, [
            el("strong", { text: a.runbook_id || a.task_id || "run" }),
            el("span", { class: "usage-row-identity", text: identity || "worker UNKNOWN" }),
            // Billable vs subscription is a routing fact, not a run state, so
            // it does not borrow the running/idle status colours.
            el("span", {
              class: `status-pill usage-route-pill${route.billable ? " is-billable" : ""}`,
              text: route.execution_route || "UNKNOWN",
            }),
          ]),
          el(
            "dl",
            { class: "usage-metrics" },
            /* A metric missing from the payload is rendered as UNKNOWN rather
               than omitted: silently dropping it would hide from the operator
               that it was never established. */
            USAGE_METRIC_ORDER.map(([key, label]) =>
              usageMetricCell(
                label,
                (row.metrics && row.metrics[key]) || {
                  value: null,
                  class: "UNKNOWN",
                  reason: "not reported for this run",
                },
              ),
            ),
          ),
        ]),
      );
    });
  }

  async function refreshUsageTelemetry() {
    const root = document.getElementById("usage-telemetry-rows");
    if (!root || !isViewActive("view-providers")) return;
    try {
      renderUsageTelemetry(await getJSON("/api/usage-telemetry"));
    } catch (err) {
      root.innerHTML = "";
      const note = document.getElementById("usage-telemetry-note");
      if (note) note.textContent = "";
      root.appendChild(el("p", { class: "hint", text: "Usage could not be read; nothing is estimated in its place." }));
    }
  }

  // --------------------------------------------------------- command palette

  /* OCTAREL-UI-04 (issue #23). A real cross-entity palette, not a filter over
     the current view: it indexes the entities this dashboard already serves
     and jumps to any of them.

     Safety: the palette navigates and selects. It never executes a destructive
     verb. Choosing a command places it in the Manager input for review, so
     execution still goes through the existing parse/confirm path and the
     server's own destructiveness check — the palette cannot become a way to
     bypass a confirmation gate. */

  const PALETTE_VIEWS = [
    ["view-overview", "Overview"],
    ["view-runs", "Runs"],
    ["view-flow", "Flow"],
    ["view-priority", "Priority & Fallback Matrix"],
    ["view-tasks", "Tasks"],
    ["view-agents", "Agents"],
    ["view-providers", "Providers"],
    ["view-steering", "Manager"],
    ["view-history", "History"],
    ["view-worktrees", "Worktrees"],
    ["view-system", "System"],
    ["view-terminal", "Terminal"],
    ["view-settings", "Settings"],
    ["view-roadmap", "Roadmap"],
  ];

  /* The deterministic Manager grammar, mirrored from the help panel. The
     confirm flag marks a verb the server refuses without explicit
     confirmation; the palette says so up front rather than implying a
     one-keystroke action. */
  const PALETTE_COMMANDS = [
    ["/start [TASK_ID]", false],
    ["/pause TASK_ID", false],
    ["/resume TASK_ID", false],
    ["/stop TASK_ID", true],
    ["/stop-after-current", true],
    ["/stop-all", true],
    ["/enable PROVIDER", false],
    ["/disable PROVIDER", true],
    ["/drain PROVIDER", true],
    ["/probe PROVIDER", false],
    ["/cost-block PROVIDER [reason]", true],
    ["/cost-clear PROVIDER", false],
    ["/priority TASK_ID NUMBER", false],
    ["/defer TASK_ID", false],
    ["/set-max-writers NUMBER", false],
    ["/dry-run TASK_ID", false],
  ];

  let paletteEntries = [];
  let paletteMatches = [];
  let paletteActiveIndex = 0;
  let paletteLastFocus = null;

  async function buildPaletteIndex() {
    /* Read when the palette opens rather than mirrored into a second always-on
       cache: it is user-initiated and infrequent, and reading on open
       guarantees it never offers a stale entity. Every source is an endpoint
       the dashboard already polls, and allSettled means one failing endpoint
       costs only its own group. */
    const entries = [];

    PALETTE_VIEWS.forEach(([viewId, label]) => {
      entries.push({
        kind: "View",
        label,
        detail: "Go to view",
        haystack: `${label} ${viewId}`,
        run: () => showView(viewId),
      });
    });

    const [projects, tasks, runbooks, agents, providerRows, worktreeRows] = await Promise.allSettled([
      getJSON("/api/projects"),
      getJSON("/api/tasks"),
      getJSON("/api/runbooks"),
      getJSON("/api/models"),
      getJSON("/api/providers"),
      getJSON("/api/worktrees"),
    ]);
    const ok = (settled, fallback) => (settled.status === "fulfilled" ? settled.value : fallback);

    (ok(projects, {}).projects || []).forEach((project) => {
      entries.push({
        kind: "Project",
        label: project.display_name || project.project_id,
        detail: project.github_remote || project.project_id,
        haystack: `${project.display_name || ""} ${project.project_id} ${project.github_remote || ""}`,
        run: () => selectProject(project.project_id),
      });
    });

    (ok(tasks, []) || []).forEach((task) => {
      entries.push({
        kind: "Task",
        label: task.id,
        detail: [task.state, task.worker].filter(Boolean).join(" · "),
        haystack: `${task.id} ${task.task_ref || ""} ${task.state || ""} ${task.worker || ""} ${task.role || ""}`,
        run: () => revealInView("view-tasks", task.id),
      });
    });

    (ok(runbooks, []) || []).forEach((run) => {
      entries.push({
        kind: "Run",
        label: run.name || run.id,
        detail: [run.status, run.branch].filter(Boolean).join(" · "),
        haystack: `${run.id} ${run.name || ""} ${run.status || ""} ${run.branch || ""} ${run.preset || ""}`,
        // Runs have a real selection mechanism, so use it.
        run: () => focusRunbook(run.id),
      });
    });

    (ok(agents, []) || []).forEach((agent) => {
      entries.push({
        kind: "Agent",
        label: agent.display_name || agent.worker,
        detail: [agent.provider, agent.effective_model].filter(Boolean).join(" · "),
        haystack: `${agent.worker} ${agent.display_name || ""} ${agent.provider || ""} ${agent.effective_model || ""}`,
        run: () => revealInView("view-agents", agent.display_name || agent.worker),
      });
    });

    (ok(providerRows, []) || []).forEach((provider) => {
      entries.push({
        kind: "Provider",
        label: provider.display_name || provider.name,
        detail: [provider.provider, provider.display_state || provider.state].filter(Boolean).join(" · "),
        haystack: `${provider.name} ${provider.display_name || ""} ${provider.provider || ""} ${provider.state || ""}`,
        run: () => revealInView("view-providers", provider.display_name || provider.name),
      });
    });

    (ok(worktreeRows, []) || []).forEach((tree) => {
      const label = tree.display_name || tree.branch || tree.path;
      if (!label) return;
      entries.push({
        kind: "Worktree",
        label,
        detail: tree.branch && tree.branch !== label ? tree.branch : tree.path || "",
        haystack: `${tree.display_name || ""} ${tree.branch || ""} ${tree.path || ""}`,
        run: () => revealInView("view-worktrees", label),
      });
    });

    PALETTE_COMMANDS.forEach(([command, needsConfirm]) => {
      entries.push({
        kind: "Command",
        label: command,
        detail: needsConfirm
          ? "Opens in Manager · requires confirmation"
          : "Opens in Manager for review",
        haystack: command,
        run: () => {
          showView("view-steering");
          const input = document.getElementById("steering-input");
          if (input) {
            input.value = command;
            input.focus();
          }
        },
      });
    });

    return entries;
  }

  /* A palette row names a specific entity, so choosing it must actually
     surface that entity rather than dropping the operator on an unfiltered
     list. Runs have a real focus mechanism; everything else navigates and then
     applies the view filter to the entity's own identifier, which is the same
     filter the topbar field drives. */
  function revealInView(viewId, query) {
    showView(viewId);
    const field = document.getElementById("global-search");
    if (field) field.value = query;
    applySearch(query);
  }

  function renderPaletteResults(query) {
    const root = document.getElementById("palette-results");
    if (!root) return;
    const q = String(query || "").trim().toLowerCase();
    const matches = (q
      ? paletteEntries.filter((entry) => entry.haystack.toLowerCase().includes(q))
      : paletteEntries
    ).slice(0, 50);

    paletteMatches = matches;
    if (paletteActiveIndex > matches.length - 1) paletteActiveIndex = Math.max(matches.length - 1, 0);
    root.innerHTML = "";

    if (!matches.length) {
      root.appendChild(el("p", { class: "hint palette-empty", text: "Nothing matches that search." }));
      return;
    }

    matches.forEach((entry, index) => {
      const active = index === paletteActiveIndex;
      const row = el(
        "button",
        {
          type: "button",
          class: `palette-row${active ? " is-active" : ""}`,
          role: "option",
          "aria-selected": active ? "true" : "false",
        },
        [
          el("span", { class: "palette-kind", text: entry.kind }),
          el("span", { class: "palette-label", text: entry.label }),
          entry.detail ? el("span", { class: "palette-detail", text: entry.detail }) : null,
        ],
      );
      row.addEventListener("click", () => runPaletteEntry(entry));
      root.appendChild(row);
    });
  }

  function runPaletteEntry(entry) {
    if (!entry) return;
    closePalette();
    if (typeof entry.run === "function") entry.run();
  }

  function closePalette() {
    const palette = document.getElementById("palette");
    const backdrop = document.getElementById("palette-backdrop");
    if (palette) palette.hidden = true;
    if (backdrop) backdrop.hidden = true;
    // Focus restoration: this is a modal dialog.
    if (paletteLastFocus && document.contains(paletteLastFocus)) paletteLastFocus.focus();
    paletteLastFocus = null;
  }

  async function openPalette() {
    const palette = document.getElementById("palette");
    const backdrop = document.getElementById("palette-backdrop");
    const input = document.getElementById("palette-input");
    const root = document.getElementById("palette-results");
    if (!palette || !input) return;

    paletteLastFocus = document.activeElement;
    paletteActiveIndex = 0;
    input.value = "";
    if (backdrop) backdrop.hidden = false;
    palette.hidden = false;
    input.focus();

    if (root) {
      root.innerHTML = "";
      root.appendChild(el("p", { class: "hint palette-empty", text: "Loading…" }));
    }
    paletteEntries = await buildPaletteIndex();
    // The operator may have typed while the index loaded.
    renderPaletteResults(input.value);
  }

  function initPalette() {
    const input = document.getElementById("palette-input");
    const trigger = document.getElementById("palette-open");
    const backdrop = document.getElementById("palette-backdrop");
    const palette = document.getElementById("palette");
    if (!input || !palette) return;

    if (trigger) trigger.addEventListener("click", openPalette);
    const mobileTrigger = document.getElementById("palette-open-mobile");
    if (mobileTrigger) mobileTrigger.addEventListener("click", openPalette);
    if (backdrop) backdrop.addEventListener("click", closePalette);

    input.addEventListener("input", () => {
      paletteActiveIndex = 0;
      renderPaletteResults(input.value);
    });

    input.addEventListener("keydown", (evt) => {
      if (evt.key === "ArrowDown" || evt.key === "ArrowUp") {
        evt.preventDefault();
        if (!paletteMatches.length) return;
        const delta = evt.key === "ArrowDown" ? 1 : -1;
        paletteActiveIndex = (paletteActiveIndex + delta + paletteMatches.length) % paletteMatches.length;
        renderPaletteResults(input.value);
        const activeRow = document.querySelector(".palette-row.is-active");
        if (activeRow) activeRow.scrollIntoView({ block: "nearest" });
      } else if (evt.key === "Enter") {
        evt.preventDefault();
        runPaletteEntry(paletteMatches[paletteActiveIndex]);
      } else if (evt.key === "Escape") {
        evt.preventDefault();
        closePalette();
      }
    });

    // Keep focus inside the dialog while it is open.
    palette.addEventListener("keydown", (evt) => {
      if (evt.key !== "Tab") return;
      evt.preventDefault();
      input.focus();
    });
  }

  // -------------------------------------------------- priority & fallback matrix

  /* OCTAREL-UI-04 (issue #23). Renders /api/priority-matrix, which is a read
     model over the registry's configured routes. Nothing here re-derives or
     re-orders routing: priority is the position the server reports, and a
     candidate the router cannot use keeps its recorded reason rather than
     being dropped. There is deliberately no drag-to-reorder — no backend
     contract exists for mutating route order, and a draggable control that
     silently did nothing would be a lie. */

  /* These describe how a mode *would* allocate. The selector previews a mode;
     it does not set one. Octarel's live policy is not changed from this
     screen, and there is no endpoint that would do so — labelling these as
     live allocation would make a read-only control look like a policy switch. */
  const ROUTING_MODE_LABELS = {
    A: "Single primary — would send all traffic to the first routable candidate",
    B: "Even split — would weight every routable candidate equally",
    C: "Cost-weighted — would favour the cheapest cost class",
    D: "Capability-priority — share would decay by position in the route",
    E: "Failover chain — single primary, full ordered chain returned",
  };

  let priorityMode = "A";

  function priorityCandidateCard(candidate) {
    const active = candidate.share_percent != null;
    const classes = ["entity-card", "priority-card"];
    if (!candidate.routable) classes.push("is-excluded");
    if (active) classes.push("is-active");

    // Status text always accompanies the colour; never colour alone.
    // "Would receive" rather than "Active": this is the selected mode's
    // computed share, not observed live traffic.
    const stateText = active
      ? `Would receive ${candidate.share_percent}%`
      : candidate.routable
        ? "Eligible"
        : "Excluded";
    const stateClass = active ? "st-running" : candidate.routable ? "st-available" : "st-paused";

    /* A provider and an agent are distinct concepts and the design
       specification requires showing both, alongside the effective model. */
    const facts = [
      ["Provider", candidate.provider],
      ["Agent", candidate.execution_system],
      ["Model", candidate.model || "UNKNOWN"],
      ["Capability", candidate.capability],
      ["Cost class", candidate.cost_class],
      ["Provider state", candidate.provider_state],
    ]
      .filter(([, value]) => value != null && value !== "")
      .map(([label, value]) =>
        el("div", {}, [
          el("dt", { text: label }),
          el("dd", { text: String(value) }),
        ]),
      );

    return el("article", { class: classes.join(" ") }, [
      el("header", { class: "priority-card-head" }, [
        el("span", { class: "priority-tier", text: `P${candidate.priority}` }),
        el("strong", { class: "priority-agent", text: candidate.display_name }),
        el("span", { class: `status-pill ${stateClass}`, text: stateText }),
      ]),
      el("dl", { class: "priority-facts" }, facts),
      candidate.excluded_reason
        ? el("p", { class: "priority-reason", text: `Excluded: ${candidate.excluded_reason}` })
        : null,
    ]);
  }

  function renderPriorityMatrix(body) {
    const root = document.getElementById("priority-columns");
    if (!root) return;
    const roles = (body && body.roles) || [];
    root.innerHTML = "";

    if (!roles.length) {
      root.appendChild(el("p", { class: "hint", text: "No routes are configured for the selected project." }));
      return;
    }

    roles.forEach((role) => {
      root.appendChild(
        el("section", { class: "priority-column" }, [
          el("div", { class: "priority-column-head" }, [
            el("h3", { text: role.role }),
            el("span", {
              class: "hint",
              text: role.error
                ? "route could not be resolved"
                : `${role.routable_count} of ${role.candidate_count} routable`,
            }),
          ]),
          // A role whose route is misconfigured is shown as such, not omitted.
          role.error ? el("p", { class: "priority-reason", text: role.error }) : null,
          ...role.candidates.map(priorityCandidateCard),
        ]),
      );
    });
  }

  async function refreshPriorityMatrix() {
    const root = document.getElementById("priority-columns");
    if (!root || !isViewActive("view-priority")) return;
    try {
      const body = await getJSON(`/api/priority-matrix?mode=${encodeURIComponent(priorityMode)}`);
      renderPriorityMatrix(body);
      const note = document.getElementById("priority-mode-note");
      if (note) {
        // The selector already names the mode, so this line carries the
        // aggregate instead of repeating it: how much of the configured
        // fallback capacity is actually usable right now.
        const roles = body.roles || [];
        const candidates = roles.reduce((sum, role) => sum + role.candidate_count, 0);
        const routable = roles.reduce((sum, role) => sum + role.routable_count, 0);
        const starved = roles.filter((role) => role.routable_count === 0).length;
        note.textContent =
          `${roles.length} configured roles \u00b7 ${routable} of ${candidates} candidates routable` +
          (starved ? ` \u00b7 ${starved} with no routable candidate` : "");
        note.classList.toggle("priority-note-warn", starved > 0);
      }
    } catch (err) {
      root.innerHTML = "";
      // Clear the previous aggregate too: a stale "N of M routable" line would
      // otherwise still read as current.
      const note = document.getElementById("priority-mode-note");
      if (note) { note.textContent = ""; note.classList.remove("priority-note-warn"); }
      root.appendChild(
        el("p", {
          class: "hint",
          text: "Routing could not be read. The matrix is left empty rather than guessed.",
        }),
      );
    }
  }

  function initPriorityMatrix() {
    const select = document.getElementById("priority-mode");
    if (!select) return;
    Object.entries(ROUTING_MODE_LABELS).forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    select.value = priorityMode;
    select.addEventListener("change", () => {
      priorityMode = select.value;
      refreshPriorityMatrix();
    });
  }

  // ----------------------------------------------------------------- nav rail

  /* Collapsed/expanded state for the desktop navigation rail. Stored in this
     browser only, like the theme — it is a display preference, not
     orchestrator state, so it never round-trips to the server. */
  function applyRail(collapsed) {
    const root = document.documentElement;
    if (collapsed) root.setAttribute("data-rail", "collapsed");
    else root.removeAttribute("data-rail");
    const btn = document.getElementById("rail-toggle");
    if (btn) {
      btn.setAttribute("aria-pressed", collapsed ? "true" : "false");
      const label = btn.querySelector(".rail-toggle-label");
      // The accessible name has to describe the action in both states, and the
      // visible label is hidden while collapsed, so set both.
      const text = collapsed ? "Expand menu" : "Collapse menu";
      if (label) label.textContent = text;
      btn.setAttribute("aria-label", text);
    }
  }

  function initRail() {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(RAIL_KEY) === "collapsed";
    } catch (err) {
      collapsed = false;
    }
    applyRail(collapsed);
    const btn = document.getElementById("rail-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      collapsed = !collapsed;
      applyRail(collapsed);
      try {
        localStorage.setItem(RAIL_KEY, collapsed ? "collapsed" : "expanded");
      } catch (err) {
        /* A browser that refuses storage still gets the toggle, just not the
           memory of it; nothing else depends on the write succeeding. */
      }
    });
  }

  // --------------------------------------------------------------------- view switching

  /* A panel that costs real work to produce should not be produced while it is
     off-screen. /api/manager/route probes CLI presence on the filesystem and
     loads the model catalog; /api/priority-matrix walks every configured role;
     /api/usage-telemetry walks every durable usage record. Polling all three
     every 2s regardless of what the operator is looking at was measurable load
     for no benefit, so these refresh only while their view is visible and are
     fetched immediately on switching to it. */
  function isViewActive(viewId) {
    const view = document.getElementById(viewId);
    return !!view && !view.hidden;
  }

  const VIEW_SCOPED_REFRESH = {
    "view-priority": () => refreshPriorityMatrix(),
    "view-steering": () => refreshManagerRoute(),
    "view-providers": () => refreshUsageTelemetry(),
  };

  function showView(viewId) {
    document.querySelectorAll(".view").forEach((node) => {
      node.hidden = node.id !== viewId;
      node.classList.toggle("active", node.id === viewId);
    });
    document.querySelectorAll(".tab").forEach((btn) => {
      const active = btn.dataset.view === viewId;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".bn[data-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === viewId);
    });
    if (viewId !== "view-runs") state.focusRunbookId = null;
    closeSidebar();
    closeSystemMenu();
    closeAttention();
    applySearch(document.getElementById("global-search")?.value || "");
    if (viewId === "view-flow") requestAnimationFrame(() => drawFlowEdges());
    if (viewId === "view-terminal" && connectTerminalView) connectTerminalView();
    // Populate a view-scoped panel now rather than waiting for the next poll,
    // since it is skipped entirely while its view is hidden.
    const scoped = VIEW_SCOPED_REFRESH[viewId];
    if (scoped) {
      try { scoped(); } catch (err) { console.warn(err); }
    }
  }

  function initNav() {
    document.querySelectorAll("#tabbar .tab, #bottom-nav .bn[data-view], .more-item, .dropdown-item[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        showView(btn.dataset.view);
        if (btn.classList.contains("more-item")) closeMoreSheet();
      });
      btn.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          showView(btn.dataset.view);
          if (btn.classList.contains("more-item")) closeMoreSheet();
        }
      });
    });
  }

  function openMoreSheet() {
    document.getElementById("more-backdrop").hidden = false;
    document.getElementById("more-sheet").hidden = false;
    document.getElementById("more-tab").setAttribute("aria-expanded", "true");
  }

  function closeMoreSheet() {
    document.getElementById("more-backdrop").hidden = true;
    document.getElementById("more-sheet").hidden = true;
    document.getElementById("more-tab").setAttribute("aria-expanded", "false");
  }

  function initMoreSheet() {
    document.getElementById("more-tab").addEventListener("click", openMoreSheet);
    document.getElementById("more-close").addEventListener("click", closeMoreSheet);
    document.getElementById("more-backdrop").addEventListener("click", closeMoreSheet);
  }

  function openSidebar() {
    document.getElementById("sidebar").classList.add("open");
    document.getElementById("sidebar-backdrop").hidden = false;
    document.getElementById("menu-toggle").setAttribute("aria-expanded", "true");
  }

  function closeSidebar() {
    document.getElementById("sidebar").classList.remove("open");
    document.getElementById("sidebar-backdrop").hidden = true;
    document.getElementById("menu-toggle").setAttribute("aria-expanded", "false");
  }

  // OCTAREL-UI-03: on phones the bottom nav and the session bar are fixed. Publish
  // their *measured* heights (safe-area included) as CSS variables so page padding,
  // the terminal viewport and dialogs always clear them; nothing sits underneath.
  function syncChromeInsets() {
    const root = document.documentElement;
    const nav = document.getElementById("bottom-nav");
    const bar = document.getElementById("sticky-controls");
    const mobile = !window.matchMedia(DESKTOP_MQ).matches;
    const navH = mobile && nav ? Math.ceil(nav.getBoundingClientRect().height) : 0;
    const barShown = bar && !bar.classList.contains("desktop-inline") && getComputedStyle(bar).display !== "none";
    const barH = mobile && barShown ? Math.ceil(bar.getBoundingClientRect().height) : 0;
    root.style.setProperty("--bottom-nav-h", `${navH}px`);
    root.style.setProperty("--sticky-h", `${barH}px`);
  }

  function initChromeInsets() {
    syncChromeInsets();
    window.addEventListener("resize", syncChromeInsets);
    window.addEventListener("orientationchange", syncChromeInsets);
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(syncChromeInsets);
      ["bottom-nav", "sticky-controls"].forEach((id) => { const n = document.getElementById(id); if (n) ro.observe(n); });
    }
    const mq = window.matchMedia(DESKTOP_MQ);
    mq.addEventListener("change", syncChromeInsets);
  }

  function initMobileChrome() {
    document.getElementById("menu-toggle").addEventListener("click", () => {
      const open = document.getElementById("sidebar").classList.contains("open");
      if (open) closeSidebar();
      else openSidebar();
    });
    document.getElementById("sidebar-backdrop").addEventListener("click", closeSidebar);
  }

  function closeSystemMenu() {
    const menu = document.getElementById("system-menu");
    menu.hidden = true;
    document.getElementById("system-menu-btn").setAttribute("aria-expanded", "false");
  }

  function initSystemMenu() {
    const btn = document.getElementById("system-menu-btn");
    const menu = document.getElementById("system-menu");
    btn.addEventListener("click", () => {
      const next = menu.hidden;
      menu.hidden = !next;
      btn.setAttribute("aria-expanded", next ? "true" : "false");
    });
    document.addEventListener("click", (evt) => {
      if (!btn.contains(evt.target) && !menu.contains(evt.target)) closeSystemMenu();
    });
  }

  function closeAttention() {
    document.getElementById("attention-popover").hidden = true;
    document.getElementById("notif-bell").setAttribute("aria-expanded", "false");
  }

  function toggleAttention() {
    const pop = document.getElementById("attention-popover");
    const next = pop.hidden;
    pop.hidden = !next;
    document.getElementById("notif-bell").setAttribute("aria-expanded", next ? "true" : "false");
  }

  function initAttentionBell() {
    ["notif-bell", "notif-bell-mobile"].forEach((id) => {
      document.getElementById(id).addEventListener("click", (evt) => {
        evt.stopPropagation();
        toggleAttention();
      });
    });
    document.addEventListener("click", (evt) => {
      const pop = document.getElementById("attention-popover");
      if (pop.hidden) return;
      if (pop.contains(evt.target)) return;
      if (evt.target.closest(".bell-btn")) return;
      closeAttention();
    });
  }

  document.addEventListener("keydown", (evt) => {
    if (evt.key !== "Escape") return;
    closeMoreSheet();
    closeSidebar();
    closeSystemMenu();
    closeAttention();
    const sheet = document.getElementById("confirm-sheet");
    if (sheet && !sheet.hidden) {
      document.getElementById("confirm-cancel").click();
    }
  });

  // --------------------------------------------------------------------- sticky controls placement

  function initStickyControlsPlacement() {
    const mq = window.matchMedia(DESKTOP_MQ);
    const controls = document.getElementById("sticky-controls");
    const desktopSlot = document.getElementById("topbar-controls-slot");
    const originalParent = controls.parentElement;
    const originalNext = controls.nextSibling;

    function place() {
      if (mq.matches) {
        controls.classList.add("desktop-inline");
        desktopSlot.appendChild(controls);
      } else {
        controls.classList.remove("desktop-inline");
        originalParent.insertBefore(controls, originalNext);
      }
    }
    place();
    mq.addEventListener("change", place);
  }

  // --------------------------------------------------------------------- notifications

  const notifState = { lastAttentionCount: 0, seenTaskStates: {}, seenProviderStates: {} };

  function notifEnabled() {
    try {
      return localStorage.getItem(NOTIF_KEY) === "1";
    } catch (err) {
      return false;
    }
  }

  function notify(title, body) {
    if (!notifEnabled()) return;
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      new Notification(title, { body });
    } catch (err) {
      /* ignore */
    }
  }

  function initNotifications() {
    const btn = document.getElementById("notif-toggle");
    function reflect() {
      const on = notifEnabled() && typeof Notification !== "undefined" && Notification.permission === "granted";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "Notifications on" : "Enable notifications";
      const fb = document.getElementById("notif-feedback");
      if (fb) {
        fb.textContent = typeof Notification === "undefined" ? "Not supported by this browser"
          : Notification.permission === "denied" ? "Blocked in browser settings"
          : on ? "On" : "Off";
      }
    }
    reflect();
    btn.addEventListener("click", async () => {
      if (typeof Notification === "undefined") return;
      if (Notification.permission !== "granted") {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          reflect();
          return;
        }
      }
      const nowOn = !notifEnabled();
      try {
        localStorage.setItem(NOTIF_KEY, nowOn ? "1" : "0");
      } catch (err) {
        /* ignore */
      }
      reflect();
    });
  }

  // --------------------------------------------------------------------- clocks / search

  function formatElapsed(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const h = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
    const m = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
    const s = String(totalSeconds % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function tickClock() {
    const now = new Date();
    const dateEl = document.getElementById("clock-date");
    const timeEl = document.getElementById("clock-time");
    if (dateEl) {
      dateEl.textContent = now.toLocaleDateString(undefined, {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    }
    if (timeEl) {
      timeEl.textContent = now.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    }
    const greet = document.getElementById("greeting-title");
    if (greet) {
      const wave = now.getHours() < 17 ? "👋" : "✨";
      greet.textContent = `${greetingWord(now)}, ${state.identity.name} ${wave}`;
    }
  }

  function initSessionClock() {
    let start;
    try {
      start = Number(sessionStorage.getItem(SESSION_START_KEY));
      if (!start) {
        start = Date.now();
        sessionStorage.setItem(SESSION_START_KEY, String(start));
      }
    } catch (err) {
      start = Date.now();
    }
    tickClock();
    setInterval(() => {
      const label = document.getElementById("session-elapsed");
      if (label) label.textContent = formatElapsed(Date.now() - start);
      tickClock();
    }, 1000);
  }

  function applySearch(query) {
    const q = String(query || "").trim().toLowerCase();
    const view = document.querySelector(".view.active") || document.getElementById("view-overview");
    if (!view) return;
    view.querySelectorAll("[data-searchable]").forEach((node) => {
      if (!q) {
        node.hidden = false;
        return;
      }
      const hay = (node.getAttribute("data-search") || node.textContent || "").toLowerCase();
      node.hidden = !hay.includes(q);
    });
  }

  function initSearch() {
    const input = document.getElementById("global-search");
    const hint = document.getElementById("search-hint");
    if (hint) hint.textContent = platformHint();
    input.addEventListener("input", () => applySearch(input.value));
    // OCTAREL-UI-04: the accelerator opens the cross-entity command palette.
    // The field beside it keeps filtering the current view, which is a
    // different job and stays available.
    document.addEventListener("keydown", (evt) => {
      if ((evt.metaKey || evt.ctrlKey) && evt.key.toLowerCase() === "k") {
        evt.preventDefault();
        openPalette();
      }
    });
  }

  // --------------------------------------------------------------------- charts

  function ringSVG(toneColor, fraction) {
    const wrap = document.createElement("div");
    const svg = svgEl("svg", { viewBox: "0 0 36 36", role: "img" });
    const r = 15.5;
    const c = 2 * Math.PI * r;
    const f = Math.max(0, Math.min(1, fraction || 0));
    const bg = svgEl("circle", { class: "ring-bg", cx: 18, cy: 18, r, stroke: toneColor });
    const fg = svgEl("circle", {
      class: "ring-fg",
      cx: 18,
      cy: 18,
      r,
      stroke: toneColor,
      "stroke-dasharray": `${(f * c).toFixed(2)} ${c.toFixed(2)}`,
    });
    svg.append(bg, fg);
    wrap.appendChild(svg);
    return wrap.innerHTML;
  }

  /* SVG chart strokes are presentation attributes, so unlike a CSS `color`
     declaration they cannot resolve a custom property themselves. Read the
     themed tokens from the document once per render instead of pinning a
     palette here, so the rings and donut follow the active theme (and the
     design system's status colours) rather than drifting from it. */
  function themeColors() {
    const s = getComputedStyle(document.documentElement);
    const read = (name, fallback) => (s.getPropertyValue(name).trim() || fallback);
    return {
      ok: read("--ok", "#18a875"),
      info: read("--info", "#6d63d9"),
      completed: read("--purple", read("--info", "#6d63d9")),
      err: read("--err", "#d84d45"),
      track: read("--border-strong", "rgba(55,48,39,.17)"),
    };
  }

  function renderStatusRings(counts) {
    const total = Math.max(counts.total, 1);
    const tone = themeColors();
    const map = [
      ["ring-running", "status-running", counts.running, tone.ok],
      ["ring-queued", "status-queued", counts.queued, tone.info],
      ["ring-completed", "status-completed", counts.completed, tone.completed],
      ["ring-blocked", "status-blocked", counts.blocked, tone.err],
    ];
    map.forEach(([ringId, countId, value, color]) => {
      const countEl = document.getElementById(countId);
      if (countEl) countEl.textContent = String(value);
      const ring = document.getElementById(ringId);
      if (ring) ring.innerHTML = ringSVG(color, value / total);
    });
  }

  function renderDonut(counts) {
    const root = document.getElementById("donut-chart");
    if (!root) return;
    const tone = themeColors();
    const slices = [
      { key: "Running", value: counts.running, color: tone.ok },
      { key: "Queued", value: counts.queued, color: tone.info },
      { key: "Completed", value: counts.completed, color: tone.completed },
      { key: "Blocked", value: counts.blocked, color: tone.err },
    ];
    const total = slices.reduce((sum, s) => sum + s.value, 0);
    const svg = svgEl("svg", { viewBox: "0 0 42 42", width: "140", height: "140", role: "img", "aria-label": "Task status donut" });
    const r = 15.5;
    const c = 2 * Math.PI * r;
    svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r, fill: "none", stroke: tone.track, "stroke-width": "4" }));
    if (total === 0) {
      svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r, fill: "none", stroke: tone.track, "stroke-width": "4" }));
    } else {
      let offset = 0;
      slices.forEach((slice) => {
        if (!slice.value) return;
        const len = (slice.value / total) * c;
        const circ = svgEl("circle", {
          cx: 21,
          cy: 21,
          r,
          fill: "none",
          stroke: slice.color,
          "stroke-width": "4",
          "stroke-dasharray": `${len} ${c - len}`,
          "stroke-dashoffset": String(-offset),
          transform: "rotate(-90 21 21)",
        });
        svg.appendChild(circ);
        offset += len;
      });
    }
    const value = svgEl("text", { x: 21, y: 19.5, "text-anchor": "middle", class: "donut-center-value" });
    value.textContent = String(total);
    value.setAttribute("dominant-baseline", "middle");
    const label = svgEl("text", { x: 21, y: 26.5, "text-anchor": "middle", class: "donut-center-label" });
    label.textContent = "Total";
    svg.append(value, label);

    const legend = el("ul", { class: "donut-legend" });
    slices.forEach((slice) => {
      legend.appendChild(
        el("li", {}, [
          el("span", { class: "swatch", style: `background:${slice.color}` }),
          el("span", { text: `${slice.key}` }),
          el("strong", { text: String(slice.value) }),
        ])
      );
    });
    root.innerHTML = "";
    root.appendChild(el("div", { class: "donut-layout" }, [svg, legend]));
  }

  function parseTs(iso) {
    const t = new Date(iso).getTime();
    return Number.isFinite(t) ? t : null;
  }

  function renderActivity(events, hours) {
    const root = document.getElementById("activity-chart");
    const label = document.getElementById("activity-range-label");
    if (label) label.textContent = hours <= 1 ? "Past 1 hour" : hours <= 24 ? "Past 24 hours" : "Past 7 days";
    if (!root) return;
    const now = Date.now();
    const spanMs = hours * 3600 * 1000;
    const bucketCount = hours <= 1 ? 12 : hours <= 24 ? 12 : 14;
    const bucketMs = spanMs / bucketCount;
    const buckets = Array.from({ length: bucketCount }, () => 0);
    events.forEach((evt) => {
      const t = parseTs(evt.ts);
      if (t == null) return;
      const age = now - t;
      if (age < 0 || age > spanMs) return;
      const idx = Math.min(bucketCount - 1, Math.floor((spanMs - age) / bucketMs));
      buckets[idx] += 1;
    });
    const max = Math.max(1, ...buckets);
    const w = 320;
    const h = 160;
    const pad = { l: 8, r: 8, t: 10, b: 24 };
    const innerW = w - pad.l - pad.r;
    const innerH = h - pad.t - pad.b;
    const gap = 6;
    const barW = Math.max(4, (innerW - gap * (bucketCount - 1)) / bucketCount);
    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": "Activity over selected range" });
    const defs = svgEl("defs");
    const grad = svgEl("linearGradient", { id: "activity-bar-grad", x1: "0", y1: "1", x2: "0", y2: "0" });
    grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": "#2e90fa" }));
    grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": "#9b6bff" }));
    defs.appendChild(grad);
    svg.appendChild(defs);
    buckets.forEach((value, i) => {
      const bh = (value / max) * innerH;
      const x = pad.l + i * (barW + gap);
      const y = pad.t + innerH - bh;
      const rect = svgEl("rect", {
        class: "bar",
        x,
        y: value === 0 ? pad.t + innerH - 2 : y,
        width: barW,
        height: value === 0 ? 2 : bh,
        rx: 4,
        opacity: value === 0 ? 0.25 : 1,
      });
      svg.appendChild(rect);
    });
    const first = svgEl("text", { class: "axis", x: pad.l, y: h - 6 });
    first.textContent = hours <= 24 ? "start" : "older";
    const last = svgEl("text", { class: "axis", x: w - pad.r - 24, y: h - 6 });
    last.textContent = "now";
    svg.append(first, last);
    root.innerHTML = "";
    root.appendChild(svg);
  }

  // --------------------------------------------------------------------- pipeline

  function pipelineChain(node) {
    const parts = [
      node.worker,
      node.role,
      node.execution_system,
      [node.provider, node.model, node.intensity].filter(Boolean).join("/"),
      node.worktree,
    ].filter(Boolean);
    return parts.join(" → ");
  }

  // Same stages as pipelineChain(), but as separate labeled steps instead of
  // one dense arrow-joined string -- lets a task's progression (worker →
  // role → execution system → provider/model → worktree) actually be
  // scanned at a glance instead of read as a paragraph.
  function chainSteps(node) {
    return [
      node.worker && { label: "Worker", value: `${displayName(node.worker)} (${node.worker})` },
      node.role && { label: "Role", value: node.role },
      node.execution_system && { label: "System", value: node.execution_system },
      (node.provider || node.model) && {
        label: "Model",
        value: [node.provider, node.model, node.intensity].filter(Boolean).join("/"),
      },
      node.worktree && { label: "Worktree", value: node.worktree },
    ].filter(Boolean);
  }

  function pipelineStages(node) {
    return (node.stages || []).map((stage) => ({
      ...stage,
      display: `${stage.label}: ${stage.state}`,
    }));
  }

  function renderFlowLanes(data) {
    const container = document.getElementById("flow-graph");
    container.innerHTML = "";
    const concurrent = new Set(data.concurrent_running || []);
    const byState = {};
    data.nodes.forEach((node) => {
      (byState[node.state] = byState[node.state] || []).push(node);
    });
    const order = [
      "RUNNING",
      "QUEUED",
      "PENDING",
      "BLOCKED",
      "PAUSED",
      "FAILED",
      "SUCCEEDED",
      "READY_LOCAL",
      "READY_BUT_UNMERGED",
      "CANCELLED",
    ];
    Object.keys(byState)
      .sort((a, b) => {
        const ia = order.indexOf(a);
        const ib = order.indexOf(b);
        return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      })
      .forEach((st) => {
        const lane = el("div", { class: "flow-lane" });
        const laneTitle = el("div", { class: "flow-lane-title", text: `${st} (${byState[st].length})` });
        // A truthful signal straight from /api/flow's own concurrent_running
        // list -- never inferred client-side -- so "parallel workers" is
        // visible as a fact, not implied by proximity in a lane.
        if (st === "RUNNING" && concurrent.size > 1) {
          laneTitle.appendChild(
            el("span", { class: "parallel-badge", text: `${concurrent.size} running in parallel` })
          );
        }
        lane.appendChild(laneTitle);
        const cards = el("div", { class: "flow-cards" });
        byState[st].forEach((node) => {
          const steps = chainSteps(node);
          const isParallel = concurrent.has(node.id);
          const card = el("details", {
            class: `flow-node state-${st.toLowerCase()}${isParallel ? " is-parallel" : ""}`,
            "data-searchable": "true",
            "data-search": `${node.label} ${pipelineChain(node)}`,
            "data-task-id": node.id,
          });
          // Both the title row and the chain-step pills live INSIDE
          // <summary> so they stay visible while the card is collapsed --
          // only <summary>'s first child is exempt from a <details>
          // element's "hidden unless open" content model; everything else
          // appended straight to <details> (like the old .chain span) is
          // hidden until expanded, which silently broke this exact glance
          // view once the feature became a <details> card.
          const summary = el("summary", {});
          const titleRow = el("div", { class: "entity-title-row" });
          if (node.provider || node.execution_system) {
            titleRow.appendChild(identityBadges(node.execution_system, node.provider, { size: 18 }));
          }
          titleRow.appendChild(el("strong", { text: node.label }));
          if (isParallel) titleRow.appendChild(el("span", { class: "parallel-dot", title: "Running in parallel with other tasks", "aria-label": "Running in parallel" }));
          summary.appendChild(titleRow);
          const stepsRow = el("div", { class: "chain-steps" });
          steps.forEach((s) => stepsRow.appendChild(el("span", { class: "chain-step", text: s.value, title: s.label })));
          summary.appendChild(stepsRow);
          const stages = el("div", { class: "pipeline-stages", "aria-label": "Development pipeline stages" });
          pipelineStages(node).forEach((stage) => {
            stages.appendChild(
              el("span", {
                class: `pipeline-stage stage-${String(stage.state).toLowerCase().replace(/_/g, "-")}`,
                text: stage.display,
                title: `Source: ${stage.source}`,
              })
            );
          });
          summary.appendChild(stages);
          card.appendChild(summary);
          const detail = el("dl", { class: "kv flow-node-detail" });
          const detailRows = [
            ...steps.map((s) => [s.label, s.value]),
            node.pid !== null && node.pid !== undefined && ["PID", String(node.pid)],
            node.priority !== null && node.priority !== undefined && ["Priority", String(node.priority)],
            node.kind && ["Kind", node.kind],
            node.result && ["Result", typeof node.result === "string" ? node.result : JSON.stringify(node.result)],
          ].filter(Boolean);
          detailRows.forEach(([k, v]) => {
            detail.appendChild(el("dt", { text: k }));
            detail.appendChild(el("dd", { text: v }));
          });
          card.appendChild(detail);
          cards.appendChild(card);
        });
        lane.appendChild(cards);
        container.appendChild(lane);
      });
    if (data.nodes.length === 0) {
      container.appendChild(el("p", { class: "hint", text: "No tasks yet." }));
    }
    container.querySelectorAll(".flow-node").forEach((card) => {
      card.addEventListener("toggle", () => requestAnimationFrame(() => drawFlowEdges(data.edges || [])));
    });
    requestAnimationFrame(() => drawFlowEdges(data.edges || []));
  }

  // Draws a real dependency connector (from /api/flow's own `edges`,
  // task.dependencies -- never inferred) between two task cards wherever
  // both ends are currently rendered. Kanban lanes group by state, which
  // can't show *why* one task waits on another; this line can, without
  // adopting a graph-layout library for what is still a small, per-project
  // task count.
  let lastFlowEdges = [];
  function drawFlowEdges(edges) {
    if (edges) lastFlowEdges = edges;
    const wrap = document.querySelector(".flow-graph-wrap");
    const svg = document.getElementById("flow-edges-svg");
    if (!wrap || !svg || document.getElementById("view-flow")?.hidden) return;
    const box = wrap.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${Math.max(1, box.width)} ${Math.max(1, box.height)}`);
    svg.setAttribute("width", String(box.width));
    svg.setAttribute("height", String(box.height));
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    lastFlowEdges.forEach((edge) => {
      const from = wrap.querySelector(`[data-task-id="${edge.from}"] > summary`);
      const to = wrap.querySelector(`[data-task-id="${edge.to}"] > summary`);
      if (!from || !to) return; // dependency outside the currently loaded task list
      const fr = from.getBoundingClientRect();
      const tr = to.getBoundingClientRect();
      const start = { x: fr.right - box.left, y: fr.top - box.top + fr.height / 2 };
      const end = { x: tr.left - box.left, y: tr.top - box.top + tr.height / 2 };
      const path = svgEl("path", {
        d: `M ${start.x} ${start.y} C ${(start.x + end.x) / 2} ${start.y}, ${(start.x + end.x) / 2} ${end.y}, ${end.x} ${end.y}`,
        fill: "none",
        stroke: "#9b6bff",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-dasharray": "5 4",
      });
      svg.appendChild(path);
    });
  }

  function renderOverviewPipeline(workflow) {
    const root = document.getElementById("overview-pipeline");
    if (!root) return;
    openWorkerActivity = (item, taskRef) => openDetail(item, { workerCard: true, taskId: taskRef });
    const task = workflow && workflow.task;
    const stages = (workflow && workflow.stages) || [];
    const mobile = window.matchMedia("(max-width: 640px)").matches;
    // OCTAREL-UI-03: the one-row layout needs ~176px per column; otherwise reflow
    // into a wrapping grid (readable cards) instead of compressing or scrolling sideways.
    const available = root.clientWidth || Math.max(0, window.innerWidth - 320);
    const columns = 3 + Math.max(1, stages.length);
    const desktop = available >= columns * 176;
    root.className = `pipeline workflow-runtime ${mobile ? "workflow-mobile" : desktop ? "workflow-desktop" : "workflow-medium"}`;
    root.style.setProperty("--workflow-stage-count", String(Math.max(1, stages.length)));
    root.innerHTML = "";
    if (!task) {
      renderActiveWork(workflow);
      root.appendChild(el("div", { class: "workflow-empty" }, [
        el("span", { class: "workflow-empty-icon", text: "◇" }),
        el("strong", { text: "No workflow activity yet" }),
        el("span", { text: "Enqueue or start a task to see its live stages here." }),
      ]));
      return;
    }

    function statusText(value) {
      return String(value || "NOT_REPORTED").replaceAll("_", " ");
    }
    // Distinct, non-colour-only kinds: every kind has its own glyph and label.
    const STATE_KINDS = {
      complete: { glyph: "✓", label: "Completed" },
      running: { glyph: "●", label: "Running" },
      queued: { glyph: "◷", label: "Queued" },
      blocked: { glyph: "⏸", label: "Blocked" },
      failed: { glyph: "✕", label: "Failed" },
      skipped: { glyph: "⊘", label: "Skipped" },
      pending: { glyph: "○", label: "Not started" },
    };
    function statusClass(value) {
      if (COMPLETED_STATES.includes(value)) return "complete";
      if (value === "RUNNING") return "running";
      if (value === "BLOCKED") return "blocked";
      if (value === "FAILED") return "failed";
      if (["CANCELLED", "SKIPPED", "WAIVED"].includes(value)) return "skipped";
      if (["QUEUED", "PENDING", "PAUSED"].includes(value)) return "queued";
      return "pending";
    }
    function statusPill(value) {
      const kind = statusClass(value);
      return el("span", { class: `workflow-status ${kind}`, "data-kind": kind }, [
        el("span", { class: "workflow-status-glyph", "aria-hidden": "true", text: STATE_KINDS[kind].glyph }),
        document.createTextNode(statusText(value)),
      ]);
    }
    function progressBar(item) {
      const known = Number.isFinite(item.progress);
      const track = el("span", { class: `workflow-progress ${!known && item.state === "RUNNING" ? "indeterminate" : ""}` });
      if (known) track.appendChild(el("i", { style: `width:${Math.max(0, Math.min(100, item.progress))}%` }));
      return el("div", { class: "workflow-progress-row" }, [track, el("span", { text: known ? `${item.progress}%` : "—" })]);
    }
    function elapsedFrom(startedIso, endedIso) {
      if (!startedIso) return null;
      const start = new Date(startedIso).getTime();
      const end = endedIso ? new Date(endedIso).getTime() : Date.now();
      if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
      const secs = Math.round((end - start) / 1000);
      if (secs < 60) return `${secs}s`;
      const mins = Math.floor(secs / 60);
      if (mins < 60) return `${mins}m ${secs % 60}s`;
      const hrs = Math.floor(mins / 60);
      return `${hrs}h ${mins % 60}m`;
    }

    function agentActivityHeaderRow(label, value) {
      return el("div", { class: "agent-activity-fact" }, [
        el("span", { class: "agent-activity-fact-label", text: label }),
        el("span", { class: "agent-activity-fact-value", text: String(value) }),
      ]);
    }

    // OCTAREL-UI-01 Agent Activity viewer: read-only, reuses the existing
    // .agent-output/<task>/<worker>/<run_id>/ evidence tree via the new
    // /api/agent-activity endpoints (see agent_activity.py). Never a shell;
    // never invents output. Two attempts of the same role are distinguished
    // by run_id, per the acceptance criteria.
    async function renderAgentActivity(item, taskId) {
      const body = document.getElementById("workflow-detail-body");
      body.innerHTML = "";
      const worker = item.worker || item.id;
      const role = item.role || (item.id === "orchestrator" ? "Orchestrator" : displayName(worker));

      const factsHost = el("div", { class: "agent-activity-facts" });
      const attemptBar = el("div", { class: "agent-activity-attempts" });
      const tabBar = el("div", { class: "agent-activity-tabs", role: "tablist" });
      const panels = el("div", { class: "agent-activity-panels" });
      body.append(factsHost, attemptBar, tabBar, panels);

      let attempts = [];
      try {
        const resp = await getJSON(`/api/agent-activity/${encodeURIComponent(taskId)}/${encodeURIComponent(worker)}`);
        attempts = resp.attempts || [];
      } catch (err) {
        factsHost.appendChild(el("p", { class: "hint", text: "Could not load attempt history for this worker." }));
        return;
      }

      const headerRows = [
        ["Task/run", taskId], ["Stage", item.label || role], ["Role", role],
        ["Provider", item.provider], ["Model", item.model], ["State", statusText(item.state)],
        ["Elapsed", elapsedFrom(item.started_at, item.finished_at)], ["Worktree", item.worktree],
        ["Attempts", attempts.length ? `${attempts.length} recorded` : null],
      ].filter((row) => row[1]);
      const latest = attempts[0] || {};
      const runKind = !latest.result || latest.result === "RUNNING" || item.state === "RUNNING"
        ? "running" : (latest.result === "PASS" ? "complete" : "failed");
      const runMeta = { running: ["●", "Running"], complete: ["✓", "Completed"], failed: ["✕", "Failed"] }[runKind];
      factsHost.appendChild(el("div", { class: `agent-activity-banner ${runKind}`, role: "status", "data-run-state": runKind }, [
        el("span", { class: "agent-activity-banner-glyph", "aria-hidden": "true", text: runMeta[0] }),
        el("strong", { text: runMeta[1] }),
        el("span", { text: attempts.length ? `${attempts.length} attempt${attempts.length === 1 ? "" : "s"} recorded` : "no attempts recorded" }),
      ]));
      headerRows.forEach(([k, v]) => factsHost.appendChild(agentActivityHeaderRow(k, v)));

      if (!attempts.length) {
        panels.appendChild(el("p", { class: "hint", text: "No recorded runs yet for this worker on this task." }));
        return;
      }

      let activeRunId = attempts[0].run_id;
      let activeTab = "live";
      let followTail = true;
      let currentPayload = null;

      function attemptLabel(attempt, index) {
        const n = attempts.length - index;
        const when = attempt.started_at ? relativeTime(attempt.started_at) : attempt.run_id;
        return `Attempt ${n} · ${attempt.result || "UNKNOWN"} · ${when}`;
      }

      function renderAttemptBar() {
        attemptBar.innerHTML = "";
        if (attempts.length < 2) return;
        attempts.forEach((attempt, index) => {
          const btn = el("button", {
            type: "button",
            class: `agent-activity-attempt-pill ${attempt.run_id === activeRunId ? "active" : ""}`,
            text: attemptLabel(attempt, index),
          });
          btn.addEventListener("click", () => { activeRunId = attempt.run_id; loadAttempt(); });
          attemptBar.appendChild(btn);
        });
      }

      function renderTabs() {
        tabBar.innerHTML = "";
        [["live", "Live Output"], ["details", "Details"], ["evidence", "Evidence"]].forEach(([key, label]) => {
          const btn = el("button", {
            type: "button", role: "tab", "aria-selected": String(activeTab === key),
            class: `agent-activity-tab ${activeTab === key ? "active" : ""}`, text: label,
          });
          btn.addEventListener("click", () => { activeTab = key; renderTabs(); renderPanel(); });
          tabBar.appendChild(btn);
        });
      }

      // The recorded log (logs/run.log) is the worker's stdout and stderr concatenated
      // and redacted; no per-line stream is stored. So nothing below claims a stream:
      //  - "system" lines are only the wrapper's own pointer block (TASK:/STATUS:/... keys);
      //  - "error"/"warning" flags are keyword matches, labelled as such;
      //  - the raw text is never altered (copy always returns the exact recorded content).
      const SYSTEM_LINE = /^\s*(TASK|ROLE|STATUS|SYSTEM|PROVIDER|MODEL|EXIT|SUMMARY|MANIFEST|LOG):\s|^\s*\[(status|octarel|system)\]/i;
      function classifyLine(line) {
        const flag = /\b(error|exception|traceback|fatal|assertionerror)\b/i.test(line) ? "error"
          : /\bwarn(ing)?\b/i.test(line) ? "warn" : null;
        return { kind: SYSTEM_LINE.test(line) ? "sys" : "out", flag };
      }
      let logScrollTop = null;
      let logView = { highlights: true, flaggedOnly: false, wrap: false };

      function fillLog(codeEl, entries) {
        codeEl.textContent = "";
        const frag = document.createDocumentFragment();
        entries.forEach(({ line, n, info }) => {
          const classes = ["log-line", `log-${info.kind}`];
          const attrs = { "data-n": String(n) };
          if (info.flag) {
            classes.push(`log-flag-${info.flag}`);
            attrs["data-flag"] = info.flag === "error" ? "error keyword" : "warning keyword";
            attrs.title = "Matches an error/warning keyword. The log does not record which stream produced this line.";
          }
          if (info.kind === "sys") attrs["data-flag"] = attrs["data-flag"] || "wrapper";
          frag.appendChild(el("span", { class: classes.join(" "), ...attrs }, [document.createTextNode(`${line}\n`)]));
        });
        codeEl.appendChild(frag);
      }

      function formatBytes(n) {
        if (!Number.isFinite(n)) return "";
        return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
      }

      function renderLivePanel(payload) {
        const controls = el("div", { class: "agent-activity-output-controls" });
        const searchInput = el("input", { type: "search", placeholder: "Search output…", class: "agent-activity-search", "aria-label": "Search output" });
        const toggle = (label, checked) => {
          const wrap = el("label", { class: "agent-activity-toggle" });
          const box = el("input", { type: "checkbox" });
          box.checked = checked;
          wrap.append(box, ` ${label}`);
          return { wrap, box };
        };
        const wrapT = toggle("Wrap", logView.wrap);
        const hiT = toggle("Highlights", logView.highlights);
        const flagT = toggle("Flagged only", logView.flaggedOnly);
        const followBtn = el("button", { type: "button", class: `agent-activity-btn${followTail ? " active" : ""}`, "aria-pressed": String(followTail), text: followTail ? "Pause follow" : "Follow tail" });
        const jumpBtn = el("button", { type: "button", class: "agent-activity-btn", text: "Jump to bottom" });
        const copyBtn = el("button", { type: "button", class: "agent-activity-btn", text: "Copy output" });
        const meta = el("span", { class: "agent-activity-meta", role: "status", "aria-live": "polite" });
        controls.append(searchInput, wrapT.wrap, hiT.wrap, flagT.wrap, followBtn, jumpBtn, copyBtn, meta);

        const output = payload.output || {};
        const details = payload.details || {};
        const fullText = output.content || "";
        const allLines = fullText.replace(/\n$/, "").split("\n");
        const entries = allLines.map((line, i) => ({ line, n: i + 1, info: classifyLine(line) }));
        const flaggedCount = entries.filter((e) => e.info.flag).length;

        const note = el("p", { class: "agent-activity-note" }, [
          el("strong", { text: "Combined output. " }),
          document.createTextNode("stdout and stderr are recorded together, not separately. Line labels are keyword matches, not stream markers; the text is shown exactly as recorded."),
        ]);
        const pre = el("pre", { class: "agent-activity-log", tabindex: "0", "aria-label": "Worker output log" });
        const codeEl = el("code", {});
        pre.appendChild(codeEl);
        const hasLog = output.status !== "missing" && output.status !== "empty";
        if (!hasLog) {
          pre.classList.add("is-empty");
          codeEl.textContent = output.status === "missing"
            ? "No output recorded for this attempt (missing or pruned)."
            : "No output yet.";
        }
        if (output.truncated) controls.appendChild(el("span", { class: "hint", text: "Showing the most recent portion of a longer log." }));

        const facts = [
          `${allLines.length} line${allLines.length === 1 ? "" : "s"}`,
          output.size ? formatBytes(output.size) : null,
          flaggedCount ? `${flaggedCount} flagged` : null,
          details.exit_status !== null && details.exit_status !== undefined ? `exit ${details.exit_status}` : null,
          output.truncated ? "most recent portion" : null,
        ].filter(Boolean).join(" · ");

        function refill() {
          if (!hasLog) return;
          const q = searchInput.value.toLowerCase();
          const shown = entries.filter((e) => (!q || e.line.toLowerCase().includes(q)) && (!flagT.box.checked || e.info.flag));
          logView = { ...logView, highlights: hiT.box.checked, flaggedOnly: flagT.box.checked };
          pre.classList.toggle("plain", !hiT.box.checked);
          if (shown.length) fillLog(codeEl, shown);
          else codeEl.textContent = "(no matching lines)";
          meta.textContent = q || flagT.box.checked ? `${shown.length} of ${allLines.length} lines match` : facts;
        }
        refill();
        [searchInput, wrapT.box, hiT.box, flagT.box, jumpBtn, copyBtn].forEach((node) => { if (!hasLog) node.disabled = true; });
        searchInput.addEventListener("input", refill);
        hiT.box.addEventListener("change", refill);
        flagT.box.addEventListener("change", refill);
        wrapT.box.addEventListener("change", (e) => { logView.wrap = e.target.checked; pre.classList.toggle("wrap", e.target.checked); });
        pre.classList.toggle("wrap", logView.wrap);
        const syncFollow = () => {
          followBtn.textContent = followTail ? "Pause follow" : "Follow tail";
          followBtn.classList.toggle("active", followTail);
          followBtn.setAttribute("aria-pressed", String(followTail));
        };
        followBtn.addEventListener("click", () => {
          followTail = !followTail;
          syncFollow();
          if (followTail) pre.scrollTop = pre.scrollHeight;
        });
        // Scrolling up pauses follow; reaching the bottom again resumes it.
        pre.addEventListener("scroll", (event) => {
          logScrollTop = pre.scrollTop;
          if (!event.isTrusted) return; // programmatic scrolls never change the operator's follow choice
          const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
          if (atBottom !== followTail && hasLog) { followTail = atBottom; syncFollow(); }
        });
        jumpBtn.addEventListener("click", () => { pre.scrollTop = pre.scrollHeight; });
        copyBtn.addEventListener("click", () => {
          const done = () => { meta.textContent = "Copied to clipboard"; setTimeout(() => { meta.textContent = facts; }, 1800); };
          if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(fullText).then(done, () => { meta.textContent = "Copy unavailable"; });
          else meta.textContent = "Copy unavailable";
        });

        const panel = el("div", { class: "agent-activity-panel" }, [controls, note, pre]);
        requestAnimationFrame(() => {
          if (followTail || logScrollTop === null) pre.scrollTop = pre.scrollHeight;
          else pre.scrollTop = logScrollTop;
        });
        return panel;
      }

      function renderDetailsPanel(payload) {
        const d = payload.details;
        if (!d) return el("p", { class: "hint", text: "No details recorded for this attempt." });
        const attemptIndex = attempts.findIndex((a) => a.run_id === payload.run_id);
        const kv = (rows) => {
          const dl = el("dl", { class: "workflow-detail-list" });
          rows.filter((r) => r[1] !== null && r[1] !== undefined && r[1] !== "")
            .forEach(([k, v]) => dl.append(el("dt", { text: k }), el("dd", { text: String(v) })));
          return dl;
        };
        const route = (r) => [r && r.execution_system, r && r.provider, r && r.model].filter(Boolean).join(" / ");
        const planned = route(d.planned);
        const actual = route(d.actual);
        const section = (title, ...children) => el("section", { class: "agent-activity-section" }, [el("h4", { text: title }), ...children]);
        const resultKind = d.result === "PASS" ? "complete" : (d.result ? "failed" : "running");
        const panel = el("div", { class: "agent-activity-panel" }, [
          section("Outcome", el("p", { class: "agent-activity-outcome" }, [
            el("span", { class: `agent-activity-chip ${resultKind}`, text: `${{ complete: "✓", failed: "✕", running: "●" }[resultKind]} ${d.result || "RUNNING"}` }),
            d.exit_status !== null && d.exit_status !== undefined ? el("span", { class: "hint", text: `exit status ${d.exit_status}` }) : null,
          ])),
          section("Timing", kv([
            ["Started", d.started_at ? relativeTime(d.started_at) : null],
            ["Finished", d.finished_at ? relativeTime(d.finished_at) : null],
            ["Duration", elapsedFrom(d.started_at, d.finished_at)],
          ])),
          section("Route", kv([
            ["Planned", planned || null],
            ["Actual", actual && actual !== planned ? `${actual} (differs from plan)` : actual || null],
          ])),
          section("Attempt", kv([
            ["Run/session ID", payload.run_id],
            ["Attempt", attemptIndex === -1 ? null : `${attempts.length - attemptIndex} of ${attempts.length}`],
          ])),
        ]);

        /* OCTAREL-UI-07 (issue #26): recorded Graphify status. Graphify is
           derived, advisory repository intelligence that ranks below the source
           tree, managed-project policy and task contracts, so it is labelled as
           advisory here and never presented as authority. Every state is shown
           honestly, including the ones where no context was injected. */
        const graph = d.graph_context;
        if (graph) {
          const injected = graph.injected;
          const status = String(graph.status || "unknown").toLowerCase();
          /* Only a real failure is painted as failed, and only an injected
             context as complete. Every other recorded state (skipped, stale,
             unavailable, not-recorded) is neutral -- painting them "running"
             made a finished attempt look like work in progress. */
          const chipKind = injected
            ? "complete"
            : status === "failed-safe"
              ? "failed"
              : "neutral";
          panel.appendChild(
            el("section", { class: "agent-activity-section" }, [
              el("h4", { text: "Graphify context" }),
              el("p", { class: "agent-activity-outcome" }, [
                el("span", {
                  class: `agent-activity-chip ${chipKind}`,
                  text: `${injected ? "✓" : "○"} ${status.toUpperCase()}`,
                }),
                el("span", {
                  class: "hint",
                  text: injected ? "context was supplied to this attempt" : "no context supplied",
                }),
              ]),
              graph.reason ? el("p", { class: "hint", text: graph.reason }) : null,
              el("p", {
                class: "hint",
                text: graph.authoritative
                  ? "Recorded as authoritative."
                  : "Advisory only — ranks below the source tree, project policy and task contracts.",
              }),
            ]),
          );
        }
        return panel;
      }

      function renderEvidencePanel(payload) {
        const evd = payload.evidence;
        if (!evd) return el("p", { class: "hint", text: "No evidence recorded for this attempt." });
        const panel = el("div", { class: "agent-activity-panel" });
        const section = (title, ...children) => panel.appendChild(el("section", { class: "agent-activity-section" }, [el("h4", { text: title }), ...children]));
        if (evd.candidate_tree_sha) section("Candidate tree", el("code", { class: "agent-activity-sha", text: evd.candidate_tree_sha }));
        if (evd.files_changed && evd.files_changed.length) {
          section(`Files changed (${evd.files_changed.length})`, el("ul", { class: "agent-activity-list" }, evd.files_changed.map((f) => el("li", {}, [el("code", { text: f })]))));
        }
        if (evd.tests_or_checks && evd.tests_or_checks.length) {
          section("Tests / checks", el("ul", { class: "agent-activity-list" }, evd.tests_or_checks.map((t) => el("li", { text: t }))));
        }
        if (evd.summary) section("Summary", el("pre", { class: "agent-activity-summary" }, [el("code", { text: evd.summary })]));
        section("Sources", el("p", { class: "hint", text: `Manifest: ${evd.manifest_relpath || "n/a"}${evd.log_relpath ? " · Log: " + evd.log_relpath : ""}` }));
        return panel;
      }

      function renderPanel() {
        panels.innerHTML = "";
        if (!currentPayload) return;
        if (activeTab === "live") panels.appendChild(renderLivePanel(currentPayload));
        else if (activeTab === "details") panels.appendChild(renderDetailsPanel(currentPayload));
        else panels.appendChild(renderEvidencePanel(currentPayload));
      }

      async function loadAttempt() {
        renderAttemptBar();
        try {
          currentPayload = await getJSON(`/api/agent-activity/${encodeURIComponent(taskId)}/${encodeURIComponent(worker)}/${encodeURIComponent(activeRunId)}`);
        } catch (err) {
          currentPayload = { run_id: activeRunId, output: { status: "missing" }, details: null, evidence: null };
        }
        renderPanel();
      }

      renderTabs();
      await loadAttempt();

      stopAgentActivityPoll();
      const latestResult = attempts[0].result;
      const isRunning = (!latestResult || latestResult === "RUNNING" || item.state === "RUNNING") && activeRunId === attempts[0].run_id;
      if (isRunning) {
        agentActivityPollId = setInterval(async () => {
          if (activeTab !== "live") return;
          try {
            currentPayload = await getJSON(`/api/agent-activity/${encodeURIComponent(taskId)}/${encodeURIComponent(worker)}/${encodeURIComponent(activeRunId)}`);
            renderPanel();
          } catch (err) {
            // Transient fetch failure while the dialog is open: keep the last
            // known output rather than clearing it or fabricating new content.
          }
        }, 3000);
      }
    }

    function openDetail(item, opts) {
      const sheet = document.getElementById("workflow-detail-sheet");
      const backdrop = document.getElementById("workflow-detail-backdrop");
      document.getElementById("workflow-detail-title").textContent = item.label || item.title || item.id;
      sheet.hidden = false;
      backdrop.hidden = false;
      document.getElementById("workflow-detail-close").focus();

      if (opts && opts.workerCard) {
        stopAgentActivityPoll();
        renderAgentActivity(item, opts.taskId);
        return;
      }

      const facts = [
        ["Status", statusText(item.state)], ["Worker", item.worker ? displayName(item.worker) : null],
        ["Execution system", item.execution_system], ["Provider", item.provider], ["Model", item.model],
        ["Intensity", item.intensity], ["Current action", item.current_action], ["Current file", item.current_file],
        ["Worktree", item.worktree], ["Updated", item.updated_at ? relativeTime(item.updated_at) : null],
      ].filter((row) => row[1]);
      const body = document.getElementById("workflow-detail-body");
      body.innerHTML = "";
      const dl = el("dl", { class: "workflow-detail-list" });
      facts.forEach(([key, value]) => dl.append(el("dt", { text: key }), el("dd", { text: String(value) })));
      body.appendChild(dl);
      if (item.attempts && item.attempts.length) {
        body.appendChild(el("h4", { text: "Attempt history" }));
        item.attempts.forEach((attempt) => body.appendChild(el("p", { class: "workflow-attempt", text: attempt.summary })));
      }
      if (item.subagents && item.subagents.length) {
        body.appendChild(el("h4", { text: `Sub-agents (${item.subagents.length})` }));
        item.subagents.forEach((agent) => body.appendChild(el("p", { text: agent.label || agent.id })));
      }
    }
    // Pipeline phases: planner -> implementation -> test -> review -> integration.
    const PHASES = [
      { key: "plan", label: "Plan" },
      { key: "implement", label: "Implement" },
      { key: "test", label: "Test" },
      { key: "review", label: "Review" },
      { key: "integrate", label: "Integrate" },
    ];
    function phaseOf(item) {
      const cat = String(item.category || item.role || "").toLowerCase();
      if (/review|drift/.test(cat)) return "review";
      if (/test|qa/.test(cat)) return "test";
      if (/integrat|checkpoint|merge|pr-/.test(cat)) return "integrate";
      if (/implement|build|develop/.test(cat)) return "implement";
      if (/plan|orchestr/.test(cat)) return "plan";
      return null;
    }
    function phaseModel() {
      const byPhase = { plan: [{ state: (workflow.orchestrator || {}).state, item: workflow.orchestrator }], implement: [], test: [], review: [], integrate: [] };
      stages.forEach((item) => { const k = phaseOf(item); if (k && byPhase[k]) byPhase[k].push({ state: item.state, item }); });
      return PHASES.map((phase) => {
        const entries = byPhase[phase.key];
        const kinds = entries.map((e) => statusClass(e.state));
        let kind = "pending";
        if (!entries.length) kind = "pending";
        else if (kinds.includes("running")) kind = "running";
        else if (kinds.includes("failed")) kind = "failed";
        else if (kinds.includes("blocked")) kind = "blocked";
        else if (kinds.every((k) => k === "complete")) kind = "complete";
        else if (kinds.every((k) => k === "skipped")) kind = "skipped";
        else if (kinds.includes("queued")) kind = "queued";
        else if (kinds.includes("complete")) kind = "running";
        return { ...phase, kind, entries };
      });
    }
    function phaseTrack() {
      const list = el("ol", { class: "phase-track", "aria-label": "Pipeline phases" });
      phaseModel().forEach((phase) => {
        const meta = STATE_KINDS[phase.kind];
        const counts = phase.entries.length > 1 ? ` (${phase.entries.length})` : "";
        list.appendChild(el("li", { class: `phase-step ${phase.kind}`, "data-phase": phase.key, "data-kind": phase.kind }, [
          el("span", { class: "phase-glyph", "aria-hidden": "true", text: meta.glyph }),
          el("span", { class: "phase-name", text: `${phase.label}${counts}` }),
          el("span", { class: "phase-state", text: phase.kind === "pending" && !phase.entries.length ? "Not started" : meta.label }),
        ]));
      });
      return list;
    }

    // Active Work: current execution as the dominant Overview surface.
    function renderActiveWork(wf) {
      const body = document.getElementById("active-work-body");
      const stateEl = document.getElementById("active-work-state");
      if (!body) return;
      body.innerHTML = "";
      const t = wf && wf.task;
      const queued = (state.tasks || []).filter((x) => x.state === "QUEUED");
      const nextQueued = queued[0];
      // Active Work is only for work that is live. A finished task is history: say so
      // instead of presenting it as the current task.
      const LIVE = ["RUNNING", "QUEUED", "PENDING", "PAUSED", "BLOCKED"];
      const isLive = !!t && (LIVE.includes(t.state) || (wf.stages || []).some((x) => LIVE.includes(x.state)));
      if (!t || !isLive) {
        if (stateEl) stateEl.textContent = "Idle";
        const last = t ? `Last activity: ${t.title || t.id} — ${statusText(t.state).toLowerCase()}${t.updated_at ? ` ${relativeTime(t.updated_at)}` : ""}.` : null;
        body.appendChild(el("div", { class: "active-work-idle" }, [
          el("strong", { text: "Nothing is running" }),
          el("span", { text: nextQueued ? `Up next: ${nextQueued.task_ref} · ${displayName(nextQueued.role)} (${displayName(nextQueued.worker)})` : "Start a run from Quick Start to see live work here." }),
          last ? el("span", { class: "hint", text: last }) : null,
        ]));
        const open = el("button", { type: "button", class: "btn-secondary", text: "Open Runs" });
        open.addEventListener("click", () => showView("view-runs"));
        body.appendChild(open);
        return;
      }
      const running = (wf.stages || []).filter((x) => x.state === "RUNNING");
      const troubled = (wf.stages || []).filter((x) => ["BLOCKED", "FAILED"].includes(x.state));
      const phases = phaseModel();
      // The orchestrator (Plan) runs throughout; the working stage is the first non-Plan phase in motion.
      const working = phases.filter((ph) => ph.key !== "plan");
      const current = working.find((ph) => ph.kind === "running") || working.find((ph) => ["blocked", "failed"].includes(ph.kind))
        || phases.find((ph) => ph.kind === "running") || phases.find((ph) => ["blocked", "failed"].includes(ph.kind));
      const currentIndex = current ? phases.indexOf(current) : -1;
      const next = phases.slice(currentIndex + 1).find((ph) => !["complete", "skipped"].includes(ph.kind));
      const lead = running[0] || troubled[0];
      if (stateEl) stateEl.textContent = `${running.length} running${troubled.length ? ` · ${troubled.length} need attention` : ""}`;

      const head = el("div", { class: "active-work-head" }, [
        el("div", { class: "active-work-title" }, [
          el("span", { class: "active-work-id", text: t.id }),
          t.title && t.title !== t.id ? el("strong", { text: t.title }) : null,
        ]),
        statusPill(t.state),
      ]);
      const facts = el("dl", { class: "active-work-facts" });
      const addFact = (k, v) => { if (v) facts.appendChild(el("div", { class: "active-work-fact" }, [el("dt", { text: k }), el("dd", { text: String(v) })])); };
      addFact("Stage", current ? `${current.label} — ${STATE_KINDS[current.kind].label}` : "Between stages");
      if (lead) addFact("Agent", `${displayName(lead.worker)}${running.length > 1 ? ` +${running.length - 1} more` : ""}`);
      if (lead) addFact("Provider / model", [lead.execution_system, lead.provider, lead.model].filter(Boolean).join(" · "));
      const elapsed = elapsedFrom(t.started_at);
      addFact("Elapsed", elapsed);
      const tree = (wf.worktrees || [])[0];
      const leadTask = lead && (state.tasks || []).find((x) => x.id === lead.id);
      addFact("Branch / worktree", (tree && (tree.branch || tree.path)) || (leadTask && leadTask.worktree));
      addFact("Next expected", next ? `${next.label}${next.entries[0] && next.entries[0].item.worker ? ` (${displayName(next.entries[0].item.worker)})` : ""}` : (running.length ? "Completion" : null));
      body.append(head, facts);

      troubled.forEach((item) => {
        const task = (state.tasks || []).find((x) => x.id === item.id) || {};
        const why = task.failure_reason_sanitized || task.last_error || task.admission_reason
          || ((task.dependencies || []).length ? `Waiting on ${task.dependencies.join(", ")}` : "Reason not reported");
        body.appendChild(el("p", { class: `active-work-block ${statusClass(item.state)}`, role: "status" }, [
          el("span", { class: "active-work-block-glyph", "aria-hidden": "true", text: STATE_KINDS[statusClass(item.state)].glyph }),
          el("span", { text: `${item.label} ${statusText(item.state).toLowerCase()}: ${why}` }),
        ]));
      });

      if (running.length) {
        const agents = el("ul", { class: "active-work-agents", "aria-label": "Agents working now" });
        running.forEach((item) => {
          const row = el("button", { type: "button", class: "active-work-agent", "aria-label": `Open ${item.label} activity for ${displayName(item.worker)}` }, [
            item.provider ? providerBadge(item.provider, { size: 24 }) : null,
            el("span", { class: "active-work-agent-name", text: displayName(item.worker) }),
            el("span", { class: "active-work-agent-meta", text: [item.label, item.model].filter(Boolean).join(" · ") }),
            el("span", { class: "active-work-agent-time", text: elapsedFrom(item.started_at) || "" }),
          ]);
          row.addEventListener("click", () => openDetail(item, { workerCard: true, taskId: t.id }));
          agents.appendChild(el("li", {}, [row]));
        });
        body.appendChild(agents);
      }
    }

    function stageCard(item, extraClass) {
      const icon = item.provider ? providerBadge(item.provider, { size: 44 }) : el("span", { class: "workflow-glyph", text: "AI" });
      const meta = [item.execution_system, item.model, item.intensity].filter(Boolean).join(" · ") || displayName(item.worker);
      const card = el("button", {
        class: `workflow-stage-card ${statusClass(item.state)} ${extraClass || ""}`,
        type: "button", "data-stage-id": item.id,
        "aria-label": `Open ${item.label} details, ${statusText(item.state)}`,
      }, [
        el("div", { class: "workflow-stage-head" }, [icon, el("div", { class: "workflow-stage-title" }, [el("strong", { text: item.label }), el("span", { text: meta })])]),
        item.provider && item.worker ? el("div", { class: "workflow-stage-agent", text: `${displayName(item.worker)} · ${item.provider}` }) : null,
        el("p", { class: "workflow-current", text: item.current_action || item.current_file || (item.state === "RUNNING" ? "Activity details not reported" : "Waiting for activity") }),
        progressBar(item),
        el("div", { class: "workflow-card-foot" }, [statusPill(item.state), item.subagents && item.subagents.length ? el("span", { text: `${item.subagents.length} sub-agent${item.subagents.length === 1 ? "" : "s"}` }) : null]),
      ]);
      card.addEventListener("click", () => openDetail(item, { workerCard: true, taskId: task.id }));
      return card;
    }

    const summary = el("button", { class: "workflow-summary", type: "button", "data-workflow-task": task.id }, [
      el("span", { class: "workflow-glyph task", text: "▤" }),
      el("div", { class: "workflow-stage-title" }, [el("strong", { text: task.title || task.id }), el("span", { text: task.id })]),
      statusPill(task.state),
      progressBar(task),
    ]);
    summary.addEventListener("click", () => openDetail(task));
    const orch = stageCard({ ...workflow.orchestrator, id: "orchestrator", provider: null }, "orchestrator");
    const results = el("button", { class: "workflow-result-card", type: "button" }, [
      el("span", { class: "workflow-glyph result", text: "◇" }),
      el("div", { class: "workflow-stage-title" }, [el("strong", { text: "Results" }), el("span", { text: workflow.results.length ? `${workflow.results.length} reported item${workflow.results.length === 1 ? "" : "s"}` : "No artifacts reported" })]),
    ]);
    results.addEventListener("click", () => showView("view-history"));
    renderActiveWork(workflow);
    root.appendChild(phaseTrack());
    const layout = el("div", { class: "workflow-layout" });
    layout.appendChild(summary);
    layout.appendChild(orch);
    stages.forEach((item) => layout.appendChild(stageCard(item)));
    if (!mobile) {
      layout.appendChild(el("div", { class: "workflow-side" }, [
        el("button", { class: "workflow-result-card", type: "button", "data-open-worktrees": "true" }, [
          el("span", { class: "workflow-glyph worktree", text: "⑂" }),
          el("div", { class: "workflow-stage-title" }, [el("strong", { text: "Worktrees" }), el("span", { text: `${workflow.worktrees.length} active in this workflow` })]),
        ]), results,
      ]));
      layout.querySelector("[data-open-worktrees]").addEventListener("click", () => showView("view-worktrees"));
    } else layout.appendChild(results);
    root.appendChild(layout);
  }

  // --------------------------------------------------------------------- identity / chrome

  function applyIdentity(identity) {
    state.identity = identity;
    const name = identity.name || "Local Developer";
    const role = identity.role || "Developer";
    document.getElementById("identity-name").textContent = name;
    document.getElementById("identity-role").textContent = role;
    document.getElementById("identity-avatar").textContent = initials(name);
    document.getElementById("greeting-avatar").textContent = initials(name);

    // ENG-AGENT-02-S6 (issue #95): the server already cryptographically
    // verified the Cloudflare Access identity (if any) before this response
    // was built — this only reflects that decision, it never re-derives it.
    const isRemote = identity.access_mode === "remote";
    const remoteEmail = identity.remote_email || "";
    document.getElementById("remote-badge-sidebar").hidden = !isRemote;
    document.getElementById("remote-badge-topbar").hidden = !isRemote;
    document.getElementById("remote-badge-mobile").hidden = !isRemote;
    document.getElementById("remote-badge-email-sidebar").textContent = remoteEmail;
    document.getElementById("remote-badge-email-topbar").textContent = remoteEmail;

    document.getElementById("greeting-sub").textContent = isRemote
      ? `Connected remotely as ${remoteEmail} via Cloudflare Access.`
      : identity.source === "git"
        ? "Your local development orchestrator is ready."
        : "Ready to build — identity from the local environment.";
    tickClock();
  }

  function updateActiveTaskPill(tasks, identity) {
    const pill = document.getElementById("active-task-pill");
    const label = document.getElementById("active-task-label");
    const running = tasks.filter((t) => t.state === "RUNNING");
    if (running.length) {
      const t = running[0];
      label.textContent = `${t.task_ref} · ${t.role}`;
      pill.classList.remove("idle");
      pill.title = running.map((x) => `${x.id} ${x.state}`).join(", ");
    } else {
      label.textContent = identity.branch && identity.branch !== "UNKNOWN" ? `no active task · ${identity.branch}` : "no active task";
      pill.classList.add("idle");
    }
  }

  function updateTaskBadge(tasks) {
    const badge = document.getElementById("nav-tasks-badge");
    const n = tasks.filter((task) => (task.projection || (task.state === "RUNNING" ? "ACTIVE" : "")) === "ACTIVE").length;
    if (n > 0) {
      badge.hidden = false;
      badge.textContent = String(n);
    } else {
      badge.hidden = true;
    }
  }

  function updateBell(count) {
    document.querySelectorAll(".bell-count").forEach((node) => {
      if (count > 0) {
        node.hidden = false;
        node.textContent = String(count);
      } else {
        node.hidden = true;
      }
    });
  }

  // --------------------------------------------------------------------- refreshers

  async function refreshIdentity() {
    try {
      const data = await getJSON("/api/identity");
      applyIdentity(data);
    } catch (err) {
      applyIdentity({ name: "Local Developer", role: "Developer", branch: "UNKNOWN", source: "fallback" });
    }
  }

  // ------------------------------------------------------------------ projects
  // ENG-CP-03 (issue #165): the managed-project selector. The selected project
  // is shown in two always-visible places (sidebar selector + topbar badge) and
  // switching it re-runs the full refresh so no panel can keep rendering the
  // previous project's data.

  let currentProjects = [];
  let currentProjectId = null;
  // Set while a project switch is being persisted. The 2-second poll must not
  // snap the selector back to the previous project in that window, and must
  // not clobber the operator's choice with a response that was already stale
  // when it was issued.
  let pendingProjectId = null;
  // The last selection the server reported, so a steady-state poll can be
  // distinguished from a genuine (possibly other-tab) selection change.
  let lastRenderedSelection;
  let projectFetchSeq = 0;

  function projectLabel(project) {
    // "OctaScene / ACGE248/octages" -- display name plus repository identity,
    // falling back to the local folder name when no remote is configured.
    if (!project) return "No project";
    const remote = project.github_remote || project.local_repo_root || "";
    return remote ? `${project.display_name} / ${remote}` : project.display_name;
  }

  function renderProjects(data) {
    currentProjects = Array.isArray(data.projects) ? data.projects : [];
    // A switch that has been requested but not yet confirmed wins over a
    // concurrent poll response that still reports the previous project.
    currentProjectId = pendingProjectId || data.selected_project_id || null;
    const select = document.getElementById("project-select");
    const selected = currentProjects.find((p) => p.project_id === currentProjectId) || null;

    if (select) {
      const enabled = currentProjects.filter((p) => p.enabled);
      // Rebuild the option list only when it actually changed. This refresh
      // runs on the 2-second dashboard poll, and unconditionally replacing
      // innerHTML would close the dropdown under an operator mid-choice and
      // discard an in-flight selection. Signature-compare instead, and set
      // `.value` separately so a pure selection change never touches the DOM
      // structure.
      const signature = enabled.map((p) => `${p.project_id}\u0000${projectLabel(p)}`).join("\u0001");
      if (select.dataset.signature !== signature) {
        select.dataset.signature = signature;
        select.innerHTML = "";
        if (!enabled.length) {
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "No project registered";
          select.appendChild(opt);
        } else {
          for (const project of enabled) {
            const opt = document.createElement("option");
            opt.value = project.project_id;
            opt.textContent = projectLabel(project);
            select.appendChild(opt);
          }
        }
      }
      select.disabled = !enabled.length;
      // Only write `.value` when the *server-reported* selection actually
      // changed since the last render (or when the control holds no valid
      // value yet). Writing it on every 2-second poll reverted an operator's
      // in-progress choice before the `change` event could be handled, so the
      // switch silently never happened -- the selector must never fight the
      // person using it.
      const serverSelection = data.selected_project_id || null;
      const known = enabled.some((p) => p.project_id === select.value);
      if (currentProjectId && (serverSelection !== lastRenderedSelection || !known)) {
        select.value = currentProjectId;
      }
      lastRenderedSelection = serverSelection;
    }

    const remoteEl = document.getElementById("project-identity-remote");
    const rootEl = document.getElementById("project-identity-root");
    if (remoteEl) remoteEl.textContent = selected ? selected.github_remote || "no remote configured" : "—";
    if (rootEl) rootEl.textContent = selected ? selected.local_repo_root : "";

    const badge = document.getElementById("project-badge-label");
    if (badge) badge.textContent = selected ? projectLabel(selected) : "No project";

    // OCTAREL-UI-02: "what project am I controlling?" answered on Overview itself.
    const context = document.getElementById("overview-context");
    if (context) {
      context.innerHTML = "";
      if (selected) {
        const branch = state.identity && state.identity.branch && state.identity.branch !== "UNKNOWN" ? state.identity.branch : null;
        context.append(
          el("strong", { text: selected.display_name }),
          el("span", { text: selected.github_remote || selected.local_repo_root || "no remote configured" }),
          branch ? el("span", { class: "overview-context-branch", text: `branch ${branch}` }) : null
        );
        context.hidden = false;
      } else {
        context.hidden = true;
      }
    }
  }

  async function refreshProjects() {
    // Sequence the polls: /api/projects responses can land out of order (and a
    // response issued before a switch can arrive after it), which would
    // resurrect the previous project as the cached selection and make the next
    // explicit switch look like a no-op. Apply only the newest response.
    const ticket = ++projectFetchSeq;
    const data = await getJSON("/api/projects");
    if (ticket !== projectFetchSeq) return;
    renderProjects(data);
    await refreshProjectHealthNotice();
  }

  async function refreshProjectHealthNotice() {
    // Surface a broken checkout (moved/deleted repository) where the operator
    // is already looking, instead of letting every dependent panel fail quietly.
    const el = document.getElementById("project-problem");
    if (!el || !currentProjectId) return;
    try {
      const report = await getJSON(`/api/projects/${encodeURIComponent(currentProjectId)}/validate`);
      if (report.ok) {
        el.hidden = true;
        el.textContent = "";
      } else {
        el.hidden = false;
        el.textContent = report.problems.join("; ");
      }
    } catch (err) {
      el.hidden = true;
    }
  }

  async function selectProject(projectId) {
    // Deliberately no "already selected, skip" guard: this only ever runs from
    // an explicit operator action on the selector, selecting is idempotent
    // server-side, and comparing against the cached id meant a momentarily
    // stale cache silently swallowed a real switch.
    if (!projectId) return;
    // Invalidate any in-flight /api/projects poll so its older response cannot
    // land after this switch and undo it.
    projectFetchSeq += 1;
    pendingProjectId = projectId;
    let res;
    try {
      res = await postJSON("/api/projects/select", { project_id: projectId });
    } finally {
      pendingProjectId = null;
    }
    if (!res.ok) {
      const el = document.getElementById("project-problem");
      if (el) {
        el.hidden = false;
        el.textContent = res.body?.detail || "could not select project";
      }
      // Put the control back on the project that is actually selected, so the
      // selector never claims a project the Control Plane did not switch to.
      await refreshProjects();
      return;
    }
    renderProjects(res.body || {});
    // Clear every cached client-side projection before repopulating, so a slow
    // endpoint cannot leave the previous project's rows on screen.
    currentTasks = [];
    await refreshAll();
  }

  function initProjectSwitcher() {
    const select = document.getElementById("project-select");
    if (select) {
      select.addEventListener("change", (evt) => {
        selectProject(evt.target.value).catch((err) => console.warn(err));
      });
    }
    initAddProject();
  }

  function initAddProject() {
    const openBtn = document.getElementById("project-add-open");
    const backdrop = document.getElementById("project-add-backdrop");
    const sheet = document.getElementById("project-add-sheet");
    const pathInput = document.getElementById("project-add-path");
    const detectBtn = document.getElementById("project-add-detect");
    const detectedWrap = document.getElementById("project-add-detected");
    const detectedList = document.getElementById("project-detected-list");
    const errorEl = document.getElementById("project-add-error");
    const saveBtn = document.getElementById("project-add-save");
    const cancelBtn = document.getElementById("project-add-cancel");
    if (!openBtn || !sheet) return;

    let detected = null;

    function showError(message) {
      errorEl.hidden = !message;
      errorEl.textContent = message || "";
    }

    function close() {
      backdrop.hidden = true;
      sheet.hidden = true;
      detected = null;
      detectedWrap.hidden = true;
      saveBtn.disabled = true;
      pathInput.value = "";
      showError("");
    }

    function open() {
      // On mobile the selector lives inside the off-canvas nav drawer, which
      // sits above the sheet layer (.sidebar z-index 45 / .sidebar-backdrop 40
      // vs .sheet 31). Close the drawer so this modal is actually reachable
      // instead of opening underneath it.
      closeSidebar();
      backdrop.hidden = false;
      sheet.hidden = false;
      showError("");
      pathInput.focus();
    }

    function renderDetected(body) {
      detectedList.innerHTML = "";
      const rows = [
        ["Repository", body.local_repo_root],
        ["Git remote", body.github_remote || "none detected"],
        ["Default branch", body.default_branch],
        ["Policy files", (body.policy_entrypoints || []).join(", ") || "none detected"],
        ["Roadmap", (body.roadmap_paths || []).join(", ") || "none detected"],
        ["Task sources", (body.task_sources || []).join(", ") || "none detected"],
        ["Validation command", (body.validation_command || []).join(" ") || "none detected"],
      ];
      for (const [key, value] of rows) {
        const dt = document.createElement("dt");
        dt.textContent = key;
        const dd = document.createElement("dd");
        dd.textContent = value;
        detectedList.appendChild(dt);
        detectedList.appendChild(dd);
      }
      document.getElementById("project-add-id").value = body.suggested_project_id || "";
      document.getElementById("project-add-name").value = body.suggested_display_name || "";
      document.getElementById("project-add-remote").value = body.github_remote || "";
      document.getElementById("project-add-branch").value = body.default_branch || "main";
      detectedWrap.hidden = false;
      saveBtn.disabled = false;
    }

    openBtn.addEventListener("click", open);
    cancelBtn.addEventListener("click", close);
    backdrop.addEventListener("click", close);

    detectBtn.addEventListener("click", async () => {
      showError("");
      const path = (pathInput.value || "").trim();
      if (!path) {
        showError("Enter the path to a local Git repository.");
        return;
      }
      const res = await postJSON("/api/projects/detect", { path });
      if (!res.ok) {
        detectedWrap.hidden = true;
        saveBtn.disabled = true;
        showError(res.body?.detail || "could not inspect that folder");
        return;
      }
      detected = res.body;
      renderDetected(detected);
    });

    saveBtn.addEventListener("click", async () => {
      if (!detected) return;
      showError("");
      const payload = {
        project_id: document.getElementById("project-add-id").value.trim(),
        display_name: document.getElementById("project-add-name").value.trim(),
        local_repo_root: detected.local_repo_root,
        github_remote: document.getElementById("project-add-remote").value.trim(),
        default_branch: document.getElementById("project-add-branch").value.trim(),
        policy_entrypoints: detected.policy_entrypoints || [],
        roadmap_paths: detected.roadmap_paths || [],
        task_sources: detected.task_sources || [],
        validation_command: detected.validation_command || [],
      };
      const res = await postJSON("/api/projects", payload);
      if (!res.ok) {
        showError(res.body?.detail || "could not save project");
        return;
      }
      renderProjects(res.body || {});
      close();
    });
  }

  async function refreshOverview() {
    const data = await getJSON("/api/overview");
    renderKV("overview-body", [
      ["Active", data.task_count],
      ["Queued", data.task_counts?.queued ?? 0],
      ["Paused", data.task_counts?.paused ?? 0],
      ["Needs attention", data.task_counts?.needs_attention ?? 0],
      ["Providers", `${data.configured_provider_count}/${data.provider_count} configured`],
      ["Stop after current", data.stop_after_current ? "yes" : "no"],
    ]);
    const badge = document.getElementById("app-status");
    const status = data.octascene_app_status || "UNKNOWN";
    badge.textContent = `OctaScene app: ${status}`;
    badge.className = `badge badge-${String(status).toLowerCase()}`;
  }

  async function refreshWorkers() {
    const [data, dispatch] = await Promise.all([getJSON("/api/workers"), getJSON("/api/dispatch")]);
    const caps = dispatch.caps || {};
    renderKV("workers-body", [
      ["Global", caps.global ? `${caps.global.used}/${caps.global.limit}` : "UNKNOWN"],
      ["Write", `${data.write.used}/${data.write.limit}`],
      ["Read", `${data.read.used}/${data.read.limit}`],
      ["Heavy", `${data.heavy.used}/${data.heavy.limit}`],
      ["Provider load", Object.entries(caps.providers || {}).map(([name, used]) => `${displayName(name)} ${used}`).join(", ") || "none"],
    ]);
    const input = document.getElementById("max-writers-input");
    if (input && document.activeElement !== input) input.value = String(data.write.limit);
  }

  async function refreshResources() {
    const data = await getJSON("/api/resources");
    state.resources = data;
    if (!data.available) {
      renderKV("resources-body", [["psutil", data.note || "unavailable"]]);
      return;
    }
    const level = (value, warnAt) => (value != null && value >= warnAt ? "warn" : undefined);
    renderKV("resources-body", [
      ["CPU", data.cpu_percent == null ? "—" : `${data.cpu_percent}%`, level(data.cpu_percent, 90)],
      ["Memory", data.memory_percent == null ? "—" : `${data.memory_percent}%`, level(data.memory_percent, 85)],
      ["Disk", data.disk_percent == null ? "—" : `${data.disk_percent}%`, level(data.disk_percent, 90)],
      ["Load (1m)", data.load_average && data.load_average["1m"] != null ? data.load_average["1m"].toFixed(2) : "unavailable"],
    ]);
    renderSystemHealth();
  }

  function taskActions(task) {
    const actions = el("div", { class: "entity-actions" });
    if (["PENDING", "QUEUED", "BLOCKED", "RUNNING"].includes(task.state)) {
      const pauseBtn = el("button", { text: "Pause", "aria-label": `Pause ${task.id}` });
      pauseBtn.addEventListener("click", () => postCommand("pause", { task_id: task.id }).then(refreshAll));
      actions.appendChild(pauseBtn);
    }
    if (task.state === "PAUSED") {
      const resumeBtn = el("button", { text: "Resume", "aria-label": `Resume ${task.id}` });
      resumeBtn.addEventListener("click", () => postCommand("resume", { task_id: task.id }).then(refreshAll));
      actions.appendChild(resumeBtn);
    }
    if (ACTIVE_TASK_STATES.includes(task.state)) {
      const stopBtn = el("button", { text: "Stop", "aria-label": `Stop ${task.id}` });
      stopBtn.addEventListener("click", () =>
        confirmAndRun(`Stop task ${task.id}?`, () => postCommand("stop", { task_id: task.id }).then(refreshAll))
      );
      actions.appendChild(stopBtn);
    }
    return actions;
  }

  function taskAgentLabel(task) {
    const model = (state.models || []).find((m) => m.worker === task.worker);
    return model ? `${displayName(task.worker)} · ${model.provider}` : displayName(task.worker);
  }

  // Scannable: ID, title, state, stage, agent, elapsed. Scheduling internals sit behind one disclosure.
  function renderTaskCards(tasks) {
    const root = document.getElementById("tasks-cards");
    root.innerHTML = "";
    if (!tasks.length) {
      root.appendChild(el("p", { class: "hint", text: "No active tasks." }));
      return;
    }
    tasks.forEach((task) => {
      const card = el("article", {
        class: "entity-card task-card",
        "data-searchable": "true",
        "data-search": `${task.id} ${task.task_ref} ${task.role} ${task.worker} ${task.state}`,
        "data-task-state": task.state,
      });
      const elapsed = task.state === "RUNNING" ? elapsedBetween(task.started_at || task.created_at) : null;
      const primary = el("ul", { class: "entity-facts", "aria-label": `Summary for ${task.id}` });
      [
        ["Stage", displayName(task.role)],
        ["Agent", taskAgentLabel(task)],
        [elapsed ? "Elapsed" : "Updated", elapsed || relativeTime(task.updated_at || task.created_at)],
      ].forEach(([k, v]) => primary.appendChild(el("li", {}, [el("span", { class: "entity-fact-k", text: k }), el("span", { text: v })])));

      const reason = ["BLOCKED", "FAILED"].includes(task.state)
        ? (task.failure_reason_sanitized || task.last_error || task.admission_reason
          || ((task.dependencies || []).length ? `Waiting on ${task.dependencies.join(", ")}` : null))
        : null;

      const details = rememberDisclosure(el("details", { class: "entity-more" }, [
        el("summary", { text: "Scheduling details" }),
        el("div", { class: "entity-meta", text: `Wave ${task.dependency_wave ?? "—"} · ${task.admission_state || "PENDING"}${task.admission_reason ? ` · ${task.admission_reason}` : ""}` }),
        el("div", { class: "entity-meta", text: `Alternatives: ${task.selection_alternatives?.length ? task.selection_alternatives.map(displayName).join(", ") : "none eligible/reported"}` }),
        el("div", { class: "entity-meta", text: `${task.kind} · priority ${task.priority ?? 0} · ${task.worker}${task.worktree ? ` · ${task.worktree}` : ""}` }),
      ]), `task:${task.id}`);

      appendAll(card, [
        el("header", {}, [
          el("div", {}, [
            el("h3", { text: task.task_ref || task.id }),
            el("div", { class: "entity-id", text: task.id }),
          ]),
          el("span", { class: `status-pill ${statusClass(task.state)}`, text: task.state }),
        ]),
        primary,
        reason ? el("p", { class: "entity-reason", role: "status" }, [
          el("span", { class: "entity-reason-glyph", "aria-hidden": "true", text: statusGlyphFor(task.state) }),
          el("span", { text: reason }),
        ]) : null,
        taskActions(task),
        details,
      ]);
      root.appendChild(card);
    });
  }

  async function refreshTasks() {
    const tasks = await getJSON("/api/tasks");
    state.tasks = tasks;
    currentTasks = tasks;
    updateStickyControlsState();
    const activeTasks = tasks.filter((task) => task.projection === "ACTIVE");
    const tbody = document.querySelector("#tasks-table tbody");
    tbody.innerHTML = "";
    activeTasks.forEach((task) => {
      const actions = el("td", {});
      actions.appendChild(taskActions(task));
      tbody.appendChild(
        el("tr", { "data-searchable": "true", "data-search": `${task.id} ${task.task_ref} ${task.role} ${task.worker}` }, [
          el("td", { text: task.id }),
          el("td", { text: task.task_ref }),
          el("td", { text: task.role }),
          el("td", { text: displayName(task.worker) }),
          el("td", { text: task.kind }),
          el("td", { text: task.state }),
          el("td", { text: String(task.priority) }),
          actions,
        ])
      );

      const prevState = notifState.seenTaskStates[task.id];
      if (prevState && prevState !== task.state) {
        if (task.state === "FAILED") notify("Task failed", `${task.id} (${task.task_ref}/${task.role}) failed`);
        else if (task.state === "BLOCKED") notify("Task blocked", `${task.id} needs attention`);
        else if (COMPLETED_STATES.includes(task.state)) {
          notify("Task completed", `${task.id} (${task.task_ref}/${task.role}) reached ${task.state}`);
        }
      }
      notifState.seenTaskStates[task.id] = task.state;
    });

    renderTaskCards(activeTasks);
    const counts = taskCounts(tasks);
    document.getElementById("count-queued").textContent = String(counts.queued);
    document.getElementById("count-paused").textContent = String(counts.paused);
    document.getElementById("count-running").textContent = String(counts.running);
    document.getElementById("count-blocked").textContent = String(counts.blocked);
    document.getElementById("count-completed").textContent = String(counts.completed);
    renderStatusRings(counts);
    renderDonut(counts);
    updateTaskBadge(tasks);
    updateActiveTaskPill(tasks, state.identity);
  }

  async function refreshFlow() {
    const data = await getJSON("/api/flow");
    state.flow = data;
    renderFlowLanes(data);
  }

  async function refreshWorkflow() {
    const data = await getJSON("/api/workflow");
    state.workflow = data;
    renderOverviewPipeline(data);
  }

  function providerActionCell(p) {
    const actions = el("td", {});
    if (!p.configured) {
      actions.textContent = "not configured";
      return actions;
    }
    function add(label, verb, extra, destructive) {
      const btn = el("button", { text: label, "aria-label": `${label} ${p.name}` });
      btn.addEventListener("click", () => {
        const run = () => postCommand(verb, { name: p.name, ...(extra || {}) }).then(refreshAll);
        if (destructive) confirmAndRun(`${label} provider ${p.name}?`, run);
        else run();
      });
      actions.appendChild(btn);
    }
    add("Enable", "provider_enable");
    add("Disable", "provider_disable", null, true);
    add("Drain", "provider_drain", null, true);
    add("Probe", "probe");
    add("Cost-block", "provider_cost_block", { reason: "blocked from control center" }, true);
    add("Clear", "provider_cost_clear");
    return actions;
  }

  // Fallback position per role comes from /api/routing (the registry's own
  // route order). Cached for 30s so the 2-second poll does not refetch it.
  const routeOrders = { at: 0, byRole: {}, pending: false };
  function ensureRouteOrders(providers) {
    const roles = [...new Set(providers.flatMap((p) => p.roles || []))];
    if (routeOrders.pending || Date.now() - routeOrders.at < 30000 || !roles.length) return;
    routeOrders.pending = true;
    Promise.all(roles.map((role) =>
      getJSON(`/api/routing?role=${encodeURIComponent(role)}`).then((r) => [role, r.order || []]).catch(() => [role, null])
    )).then((pairs) => {
      pairs.forEach(([role, order]) => { if (order) routeOrders.byRole[role] = order; });
      routeOrders.at = Date.now();
      routeOrders.pending = false;
      renderProviderCards(state.providers || []);
    });
  }

  function providerFallbackText(p) {
    const parts = (p.roles || []).map((role) => {
      const order = routeOrders.byRole[role];
      if (!order) return null;
      const index = order.indexOf(p.name);
      if (index === -1) return null;
      return `${displayName(role)}: ${index === 0 ? "primary" : `fallback #${index}`}`;
    }).filter(Boolean);
    return parts.length ? parts.join(" · ") : "—";
  }

  function providerAuthText(p) {
    const model = (state.models || []).find((m) => m.worker === p.name);
    if (p.reason === "NOT_AUTHENTICATED") return { text: "Sign-in required", tone: "warn" };
    if (p.reason === "API_ONLY_NOT_AUTHORIZED") return { text: "API-only, not authorized", tone: "warn" };
    if (p.reason === "CLI_MISSING") return { text: "CLI not installed", tone: "warn" };
    if (!p.configured) return { text: "Not configured", tone: "muted" };
    const mode = model && model.auth_mode ? String(model.auth_mode).replace(/[-_]+/g, " ") : null;
    return { text: mode ? mode.charAt(0).toUpperCase() + mode.slice(1) : "Ready", tone: "ok" };
  }

  function providerWhy(p, displayState) {
    if (p.state === "COST_BLOCKED") return "Cost-blocked by an operator; spend stays blocked until cleared.";
    if (p.state === "QUOTA_EXHAUSTED" || p.state === "RATE_LIMITED") return `${displayState.replaceAll("_", " ").toLowerCase()} — waiting for the provider to reset.${p.last_error ? ` ${p.last_error}` : ""}`;
    if (p.state === "DISABLED") return "Disabled in the worker registry.";
    if (displayState !== "AVAILABLE" && displayState !== "BUSY") return availabilityReasonLabel(p.reason) || p.last_error || null;
    return null;
  }

  function renderProviderCards(providers) {
    const root = document.getElementById("providers-cards");
    ensureRouteOrders(providers);
    // Rebuild only when something visible changed: the 2-second poll must not
    // tear down the list (and collapse any open Details/Actions disclosure).
    const shown = providers.map((p) => [
      p.name, p.display_name, p.execution_system, p.provider, p.state, p.display_state, p.reason, p.configured,
      p.model, p.intensity, p.roles, p.running_task_count, p.route_type, p.cost_class, p.capability, p.description,
      p.models, p.quota_visibility, p.spend_safety, p.last_error,
    ]);
    const signature = JSON.stringify([shown, routeOrders.byRole, (state.models || []).map((m) => [m.worker, m.auth_mode])]);
    if (root.dataset.signature === signature) return;
    const openDisclosures = new Set(
      [...root.querySelectorAll(".provider-row")].flatMap((card) => {
        const worker = card.dataset.worker;
        if (!worker) return [];
        return [...card.querySelectorAll("details[open]")].map((details) =>
          `${worker}:${details.classList.contains("entity-actions-details") ? "actions" : "details"}`
        );
      })
    );
    root.dataset.signature = signature;
    root.innerHTML = "";
    // At-a-glance summary: problems are counted up front rather than reordering the list.
    const ready = providers.filter((p) => p.configured && ["AVAILABLE", "BUSY"].includes(p.state) && (!p.reason || p.reason === "AVAILABLE")).length;
    const problems = providers.filter((p) => p.configured && !(["AVAILABLE", "BUSY"].includes(p.state) && (!p.reason || p.reason === "AVAILABLE"))).length;
    const unconfigured = providers.filter((p) => !p.configured).length;
    root.appendChild(el("p", { class: "provider-summary", role: "status" }, [
      el("span", { class: "provider-summary-item ok", text: `✓ ${ready} ready` }),
      el("span", { class: `provider-summary-item ${problems ? "warn" : ""}`, text: `${problems ? "⚠ " : ""}${problems} unavailable` }),
      el("span", { class: "provider-summary-item muted", text: `○ ${unconfigured} not configured` }),
    ]));
    providers.forEach((p) => {
      const card = el("article", {
        class: "entity-card provider-row",
        "data-worker": p.name,
        "data-searchable": "true",
        "data-search": `${p.name} ${p.provider} ${p.state}`,
      });
      // ENG-AGENT-02-S7 (issue #97): six actions per card by default made the
      // mobile Providers view an extremely long stack; collapsed behind one
      // "Actions" disclosure, actions stay fully reachable without forcing
      // every card to always show all six.
      const actionButtons = el("div", { class: "entity-actions" });
      [...providerActionCell(p).childNodes].forEach((node) => actionButtons.appendChild(node));
      const actions = p.configured
        ? el("details", { class: "entity-actions-details" }, [
            // A unique accessible name per card (Grok Build review, issue
            // #97): an identical "Actions" name on every card in a list is
            // a real screen-reader ambiguity, not just a cosmetic nit.
            el("summary", { text: `Actions for ${p.display_name || displayName(p.name)}` }),
            actionButtons,
          ])
        : actionButtons;
      if (actions.tagName === "DETAILS" && openDisclosures.has(`${p.name}:actions`)) actions.open = true;
      const titleRow = el("div", { class: "entity-title-row" }, [
        identityBadges(p.execution_system, p.provider),
        el("h3", { text: p.display_name || displayName(p.name) }),
      ]);
      const displayState = p.reason && p.reason !== "AVAILABLE" ? p.reason : (p.display_state || p.state || "UNKNOWN");
      const auth = providerAuthText(p);
      const why = providerWhy(p, displayState);
      const kind = statusClass(displayState);
      const facts = el("dl", { class: "provider-facts" });
      const fact = (label, value, tone) => facts.appendChild(el("div", { class: `provider-fact${tone ? ` tone-${tone}` : ""}` }, [
        el("dt", { text: label }), el("dd", { text: value }),
      ]));
      fact("Authentication", auth.text, auth.tone);
      fact("Model", [p.model, p.intensity].filter(Boolean).join(" · ") || "—");
      fact("Role", (p.roles || []).map((r) => displayName(r)).join(", ") || p.capability || "—");
      fact("Usage", `${p.running_task_count || 0} running task(s) · ${p.route_type || p.cost_class}`);
      fact("Fallback", providerFallbackText(p));
      const foot = el("div", { class: "provider-foot" }, [
        el("details", { class: "provider-more" }, [
          el("summary", { text: "Details" }),
          el("div", { class: "entity-meta", text: `${p.provider} · ${p.execution_system} · ${p.route_type}` }),
          el("div", {
            class: "entity-meta",
            text: `${p.description} Registered model: ${(p.models || []).join(", ") || "none"}. Capability: ${p.capability}.`,
          }),
          el("div", {
            class: "entity-meta",
            text: `Configured: ${p.configured ? "Yes" : "No"} · Local availability: ${displayState} · Quota: ${p.quota_visibility} ${p.spend_safety}`,
          }),
        ]),
        actions,
      ]);
      const more = foot.querySelector(".provider-more");
      if (more && openDisclosures.has(`${p.name}:details`)) more.open = true;
      [
        el("header", {}, [
          el("div", {}, [
            titleRow,
            el("div", { class: "entity-meta machine-id", text: `ID: ${p.name}` }),
          ]),
          el("span", { class: `status-pill ${kind}`, "data-availability": displayState, text: displayState.replaceAll("_", " ") }),
        ]),
        why ? el("p", { class: "provider-why", role: "status" }, [
          el("span", { class: "provider-why-glyph", "aria-hidden": "true", text: "ⓘ" }),
          el("span", { text: why }),
        ]) : null,
        facts,
        foot,
      ].filter(Boolean).forEach((node) => card.appendChild(node));
      root.appendChild(card);
    });
  }

  async function refreshProviders() {
    const providers = await getJSON("/api/providers");
    state.providers = providers;
    const tbody = document.querySelector("#providers-table tbody");
    tbody.innerHTML = "";
    providers.forEach((p) => {
      tbody.appendChild(
        el("tr", { "data-searchable": "true", "data-search": `${p.name} ${p.provider} ${p.state}` }, [
          el("td", { text: p.display_name || displayName(p.name) }),
          el("td", { text: p.execution_system }),
          el("td", { text: p.provider }),
          el("td", { text: p.cost_class }),
          el("td", { text: p.execution_route || "—" }),
          el("td", { text: p.reason && p.reason !== "AVAILABLE" ? `${p.state} — ${availabilityReasonLabel(p.reason)}` : p.state }),
          el("td", { text: p.configured ? "yes" : "no" }),
          providerActionCell(p),
        ])
      );
      const prevState = notifState.seenProviderStates[p.name];
      if (prevState && prevState !== p.state) {
        if (p.state === "QUOTA_EXHAUSTED") notify("Provider quota exhausted", p.name);
        else if (p.state === "COST_BLOCKED") notify("Provider cost-blocked", p.name);
        else if (prevState === "QUOTA_EXHAUSTED" && p.state === "AVAILABLE") {
          notify("Provider quota reset", `${p.name} is available again`);
        }
      }
      notifState.seenProviderStates[p.name] = p.state;
    });
    renderProviderCards(providers);
  }

  const USAGE_FACT_LABELS = {
    subscription_type: "Subscription",
    auth_method: "Auth method",
    organization: "Organization",
    usage_window: "Usage window (session/5h/weekly)",
    session_usage: "Session usage",
    rolling_window_usage: "Rolling-window usage",
    weekly_usage: "Weekly usage",
    reset_time: "Reset time",
    rpm: "RPM",
    rpd: "RPD",
    tpm: "TPM",
    balance: "Balance",
    provider_spend: "Provider spend",
    configured_spend_ceiling: "Configured spend ceiling",
    local_running_tasks: "Local running tasks",
  };

  function renderUsage(facts) {
    const root = document.getElementById("usage-body");
    if (!root) return;
    root.innerHTML = "";
    const byWorker = {};
    facts.forEach((f) => {
      (byWorker[f.worker] = byWorker[f.worker] || []).push(f);
    });
    Object.entries(byWorker).forEach(([worker, workerFacts]) => {
      const worker0 = state.models.find((m) => m.worker === worker);
      const group = el("article", { class: "entity-card usage-card" });
      group.appendChild(
        el("header", {}, [
          el("div", { class: "entity-title-row" }, [
            identityBadges(worker0 ? worker0.execution_system : worker, worker0 ? worker0.provider : null),
            el("h3", { text: displayName(worker) }),
          ]),
        ])
      );
      const list = el("dl", { class: "usage-fact-list" });
      workerFacts.forEach((f) => {
        list.appendChild(el("dt", { text: USAGE_FACT_LABELS[f.label] || f.label }));
        list.appendChild(
          el("dd", {
            class: f.source === "NOT_EXPOSED" ? "muted" : "",
            text:
              f.source === "NOT_EXPOSED"
                ? `${f.note || "Not exposed by provider"} (${f.source})`
                : `${f.value} (${f.source})${f.note ? ` — ${f.note}` : ""}`,
          })
        );
      });
      group.appendChild(list);
      root.appendChild(group);
    });
    if (!facts.length) root.appendChild(el("p", { class: "hint", text: "No workers registered." }));
  }

  // Compact, Overview-visible counterpart to the full "Usage & limits" panel
  // above -- one pill per worker with its most useful single fact, fed by
  // the exact same /api/usage facts (never a second usage resolver).
  function renderOverviewUsageSnapshot(facts) {
    const root = document.getElementById("overview-usage-snapshot");
    if (!root) return;
    root.innerHTML = "";
    const byWorker = {};
    facts.forEach((f) => {
      (byWorker[f.worker] = byWorker[f.worker] || []).push(f);
    });
    // Overview redesign: one dead "Not exposed by provider" tile per worker
    // was the single biggest source of visual noise on this card (commonly
    // 10+ tiles saying the same thing). Reported workers still get their own
    // pill; everything else collapses into one summary chip with the same
    // underlying /api/usage facts, one click away on the Providers view.
    const reportedPills = [];
    let unreportedCount = 0;
    Object.entries(byWorker).forEach(([worker, workerFacts]) => {
      const worker0 = state.models.find((m) => m.worker === worker);
      const reported = workerFacts.find((f) => f.source !== "NOT_EXPOSED" && f.label === "subscription_type");
      if (!reported) {
        unreportedCount += 1;
        return;
      }
      reportedPills.push(
        el("div", { class: "usage-pill" }, [
          identityBadges(worker0 ? worker0.execution_system : worker, worker0 ? worker0.provider : null, { size: 20 }),
          el("div", { class: "usage-pill-copy" }, [
            el("strong", { text: displayName(worker) }),
            el("span", { text: String(reported.value) }),
          ]),
        ])
      );
    });
    reportedPills.forEach((pill) => root.appendChild(pill));
    if (unreportedCount > 0) {
      const more = el("button", {
        type: "button",
        class: "usage-pill-more",
        // Fixed wording (no singular/plural branch) so this control's
        // accessible name is a single stable string for coverage-manifest
        // tracking regardless of how many providers are collapsed into it.
        text: `+ ${unreportedCount} more · not exposed by provider`,
      });
      more.addEventListener("click", () => showView("view-providers"));
      root.appendChild(more);
    }
    if (!facts.length) root.appendChild(el("p", { class: "hint", text: "No workers registered." }));
  }

  async function refreshUsage(force) {
    const data = await getJSON(`/api/usage${force ? "?refresh=true" : ""}`);
    state.usageRefreshedAt = data.refreshed_at || null;
    renderUsage(data.facts || []);
    renderOverviewUsageSnapshot(data.facts || []);
    const refreshed = document.getElementById("usage-last-refresh");
    if (refreshed) refreshed.textContent = data.refreshed_at ? `Last refresh ${relativeTime(data.refreshed_at)}` : "";
  }

  async function refreshUsageRouting() {
    const data = await getJSON("/api/usage-routing");
    const root = document.getElementById("usage-routing-body");
    if (!root) return;
    root.innerHTML = "";
    (data.records || []).forEach((record) => {
      const policy = (record.context_manifest || {}).policy_manifest || {};
      const card = el("article", { class: "entity-card", "data-searchable": "true", "data-search": `${record.runbook_id} ${record.classification} ${record.codex_policy}` }, [
        el("header", {}, [
          el("strong", { text: `${record.runbook_id} · ${record.classification}` }),
          el("span", { class: `status-pill ${record.codex_policy === "conserve" ? "st-available" : "st-paused"}`, text: record.codex_policy }),
        ]),
        el("div", { class: "entity-meta", text: `Codex ${record.codex_invocations}/${record.max_codex_invocations} · auto ${record.codex_auto_eligible ? "eligible" : "blocked"} · tokens ${record.telemetry_quality}` }),
        el("div", { class: "entity-meta", text: `Escalation: ${record.escalation_state}${record.escalation_reason ? ` — ${record.escalation_reason}` : ""}` }),
        el("div", { class: "entity-meta", text: `Policy: ${policy.role || "unrecorded"} · ${policy.workflow || "no workflow"} · ${policy.provider_policy || "provider policy unrecorded"}` }),
        el("div", { class: "entity-meta", text: `Actual: ${policy.actual_worker || policy.worker || "unrecorded"} · ${policy.actual_provider || policy.provider || "unrecorded"} · ${policy.actual_model || "model unrecorded"}` }),
        el("div", { class: "entity-meta", text: `Access: ${policy.read_write_mode || "unrecorded"} · fallback ${policy.fallback_reason || "none"} · context ${policy.approximate_context_characters ?? "unknown"} chars` }),
      ]);
      const override = el("button", { type: "button", class: "danger", text: "Premium override" });
      override.addEventListener("click", () => confirmAndRun(
        `Allow premium Codex routing for ${record.runbook_id}? This is audited.`,
        () => postCommand("usage_override", { runbook_id: record.runbook_id, reason: "Control Center operator override", confirm: true }).then(refreshUsageRouting)
      ));
      card.appendChild(override);
      root.appendChild(card);
    });
    if (!(data.records || []).length) root.appendChild(el("p", { class: "hint", text: data.policy_note }));
  }

  function renderAgentCards(models) {
    const root = document.getElementById("agents-cards");
    root.innerHTML = "";
    models.forEach((m) => {
      const card = el("article", {
        class: "entity-card",
        "data-searchable": "true",
        "data-search": `${m.display_name} ${m.worker} ${m.provider} ${m.default_model}`,
      });
      card.append(
        el("header", {}, [
          el("div", {}, [
            el("div", { class: "entity-title-row" }, [
              identityBadges(m.execution_system, m.provider),
              el("h3", { text: m.display_name || displayName(m.worker) }),
            ]),
            el("div", { class: "entity-meta", text: `${m.execution_system} · ${m.provider}` }),
            el("div", { class: "entity-meta machine-id", text: `ID: ${m.worker}` }),
          ]),
          el("span", { class: `status-pill ${m.enabled ? "st-available" : "st-paused"}`, text: m.enabled ? "enabled" : "disabled" }),
        ]),
        el("div", {
          class: "entity-meta",
          text: `${m.description} Best for: ${(m.best_for || []).join(", ") || "Not specified"}.`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Model ${m.default_model || "UNKNOWN"} · intensity ${m.default_intensity || "UNKNOWN"} · ${m.capability} · availability ${m.availability || "UNKNOWN"} · CLI ${m.cli_available ? "installed" : "missing"}`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Policy ${m.provider_policy || "missing"} · roles ${(m.allowed_policy_roles || []).join(", ") || "none"} · auth ${m.auth_mode || "unknown"} · API billing ${m.api_billing_enabled ? "enabled" : "blocked"}`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Repository data ${m.repository_data_authorization || "unrecorded"} · isolated worktree ${m.requires_isolated_worktree ? "required" : "not required"}`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Current task: ${(m.current_tasks || []).join(", ") || "None"} · worktree: ${(m.worktrees || []).join(", ") || "Not active"}`,
        })
      );
      root.appendChild(card);
    });
  }

  async function refreshModels() {
    const models = await getJSON("/api/models");
    state.models = models;
    const tbody = document.querySelector("#models-table tbody");
    tbody.innerHTML = "";
    models.forEach((m) => {
      tbody.appendChild(
        el("tr", { "data-searchable": "true", "data-search": `${m.worker} ${m.default_model}` }, [
          el("td", { text: m.display_name || displayName(m.worker) }),
          el("td", { text: m.execution_system }),
          el("td", { text: m.default_model }),
          el("td", { text: m.default_intensity }),
          el("td", { text: m.capability }),
          el("td", { text: m.enabled ? "yes" : "no" }),
          el("td", { text: m.cli_available ? "installed" : "missing" }),
        ])
      );
    });
    renderAgentCards(models);
  }

  // Condition: one plain-language word for "what state is this checkout in".
  function worktreeCondition(w) {
    const cls = w.classification || "UNKNOWN";
    if (w.stale_lock || cls === "STALE_RECOVERABLE") return { label: "Stale", kind: "stale", attention: true, glyph: "↻" };
    if (cls === "ACTIVE") return { label: "Active", kind: "active", attention: false, glyph: "●" };
    if (cls === "QUEUED" || cls === "PAUSED") return { label: cls === "PAUSED" ? "Paused" : "Queued", kind: "idle", attention: false, glyph: "◷" };
    if (cls === "FINISHED_DIRTY") return { label: "Finished, uncommitted changes", kind: "stale", attention: true, glyph: "!" };
    if (cls === "FINISHED_CLEAN") return { label: "Finished", kind: "done", attention: false, glyph: "✓" };
    if (cls === "UNRELATED_MANUAL") return { label: "Not managed", kind: "idle", attention: false, glyph: "○" };
    return { label: "Unknown", kind: "unknown", attention: false, glyph: "?" };
  }

  function renderWorktreeCards(worktrees, checkpoints) {
    const managedRoot = document.getElementById("worktrees-managed");
    const discoveredRoot = document.getElementById("worktrees-discovered");
    if (!managedRoot || !discoveredRoot) return;
    managedRoot.innerHTML = "";
    discoveredRoot.innerHTML = "";
    const flagged = (w) => {
      const c = worktreeCondition(w);
      return c.attention || w.dirty || (w.ahead > 0 && w.behind > 0) ? 0 : 1;
    };
    [...worktrees].sort((x, y) => flagged(x) - flagged(y)).forEach((w) => {
      const cond = worktreeCondition(w);
      const diverged = w.ahead > 0 && w.behind > 0;
      const card = el("article", {
        class: `entity-card worktree-card${cond.attention || w.dirty || diverged ? " needs-attention" : ""}`,
        "data-searchable": "true",
        "data-search": `${w.path} ${w.branch || ""} ${w.worker || ""} ${w.provider || ""}`,
        "data-condition": cond.kind,
      });
      // Scan line: branch, condition, owner. Everything else is one disclosure away.
      const flags = el("ul", { class: "chip-row", "aria-label": "Worktree conditions" });
      [
        [`${cond.glyph} ${cond.label}`, cond.attention ? "warn" : ""],
        [w.dirty ? "● Uncommitted changes" : "✓ Clean", w.dirty ? "warn" : ""],
        diverged ? [`⇅ Diverged (ahead ${w.ahead}, behind ${w.behind})`, "warn"]
          : w.ahead > 0 ? [`↑ Ahead ${w.ahead}`, ""] : w.behind > 0 ? [`↓ Behind ${w.behind}`, ""] : null,
        w.stale_lock ? [`Stale lock (${w.stale_lock_holder || "unknown"})`, "warn"] : w.locked ? [`Locked by ${w.lock_holder || "writer"}`, ""] : null,
      ].filter(Boolean).forEach(([text, tone]) => flags.appendChild(el("li", { class: `chip${tone ? ` chip-${tone}` : ""}`, text })));

      const owner = w.worker ? `${displayName(w.worker)} · ${w.task || w.task_id}` : "No assigned agent";
      const primary = el("ul", { class: "entity-facts", "aria-label": "Worktree summary" }, [
        el("li", {}, [el("span", { class: "entity-fact-k", text: "Owner" }), el("span", { text: owner })]),
        el("li", {}, [el("span", { class: "entity-fact-k", text: "Writer" }), el("span", { text: w.locked ? w.lock_holder || "locked" : "free" })]),
        w.protected_reason && !w.cleanup_eligible ? el("li", {}, [el("span", { class: "entity-fact-k", text: "Protected" }), el("span", { text: w.protected_reason })]) : null,
      ].filter(Boolean));

      const details = rememberDisclosure(el("details", { class: "entity-more" }, [
        el("summary", { text: "Git details" }),
        el("div", { class: "worktree-facts" }, [
          el("span", { text: `${w.head_short || "UNKNOWN"} · ${w.commit_subject || "No commit subject"}` }),
          el("span", { text: `${w.dirty ? "Dirty" : "Clean"} · ahead ${w.ahead ?? "?"} / behind ${w.behind ?? "?"}` }),
          el("span", { text: `Merged: ${w.merged == null ? "UNKNOWN" : w.merged ? "yes" : "no"} · cleanup ${w.cleanup_eligible ? "eligible" : `protected: ${w.protected_reason || "not eligible"}`}` }),
          el("span", { text: `Upstream: ${w.upstream || "None"}` }),
          el("span", { text: `${w.remote_head_label}: ${w.remote_head_commit_at || "Unavailable"}` }),
          el("span", { text: `Writer: ${w.locked ? w.lock_holder || "locked" : "free"} · PID ${w.pid || "—"}` }),
          w.stale_lock && el("span", { text: `Stale lock reconciled: ${w.stale_lock_holder || "UNKNOWN"}` }),
          el("span", { text: w.worker ? `${w.task || w.task_id} · ${w.execution_system} / ${w.provider} / ${w.model} / ${w.intensity}` : "No assigned worker" }),
          el("span", { text: `PR ${w.pr} · authoritative local gate ${w.local_gate}` }),
          // ENG-AGENT-14 (issue #140): an auto-provisioned exact-head review
          // checkout's origin and reclaim-safety are surfaced honestly here
          // rather than only in the generic PR/merged fields above.
          w.review_pr != null && el("span", {
            class: "worktree-review-origin",
            text: `Review checkout: ${w.review_repository || "unknown repo"} PR #${w.review_pr} @ ${
              (w.review_head_sha || "").slice(0, 8) || "unknown"
            }${w.review_head_matches_current === false ? " (checkout has moved off the reviewed commit)" : ""}`,
          }),
        ].filter(Boolean)),
      ]), `wt:${w.path}`);

      const actions = el("div", { class: "card-actions worktree-actions" });
      if (w.management === "DISCOVERED") {
        const adopt = el("button", { type: "button", text: "Adopt / Register", title: `Adopt ${w.path}` });
        adopt.addEventListener("click", () => confirmAndRun(`Register ${w.path} as orchestrator-managed?`, () => postCommand("worktree_adopt", { path: w.path }).then(refreshWorktrees)));
        actions.appendChild(adopt);
      }
      ["fetch", "pull", "push", "prepare_merge"].forEach((action) => {
        const button = el("button", { type: "button", text: action.replace("_", " "), title: `${action} ${w.branch || w.path}` });
        button.disabled = action === "push" && (!w.ahead || w.dirty);
        button.addEventListener("click", () => {
          const needsConfirm = action === "pull" || action === "push";
          const run = (confirm) => postCommand("git_operation", { action, path: w.path, confirm }).then(() => Promise.all([refreshWorktrees(), refreshOperations()]));
          if (needsConfirm) run(false).then(() => confirmAndRun(`${action} ${w.branch}? The preview is recorded in Operation progress.`, () => run(true)));
          else run(false);
        });
        actions.appendChild(button);
      });

      appendAll(card, [
        el("header", {}, [
          el("div", { class: "truncate-wrap" }, [el("h3", { text: w.display_name || w.branch || "UNKNOWN", title: w.branch || w.path }), el("div", { class: "entity-id truncate", text: w.path, title: w.path })]),
          el("span", { class: `status-pill ${statusClass(w.classification)}`, text: w.classification || "UNKNOWN" }),
        ]),
        flags,
        primary,
        actions,
        details,
      ]);
      (w.management === "MANAGED" ? managedRoot : discoveredRoot).appendChild(card);
    });
    if (!worktrees.some((w) => w.management === "MANAGED")) managedRoot.appendChild(el("p", { class: "hint", text: "No orchestrator-managed worktrees." }));
    if (!worktrees.some((w) => w.management === "DISCOVERED")) discoveredRoot.appendChild(el("p", { class: "hint", text: "No unregistered Git worktrees discovered." }));
  }

  async function refreshWorktrees() {
    const worktrees = await getJSON("/api/worktrees");
    state.worktrees = worktrees;
    renderWorktreeCards(worktrees, state.telemetry.checkpoints);
  }

  async function previewWorktreeCleanup(confirm) {
    const response = await postCommand("worktree_cleanup", confirm ? { confirm: true } : {});
    const data = response.body?.data || {};
    const root = document.getElementById("worktrees-cleanup-result");
    if (root) root.textContent = `${data.mode || "PREVIEW"}: ${(data.eligible || []).length} eligible, ${(data.protected || []).length} protected, ${(data.removed || []).length} removed.`;
    await Promise.all([refreshWorktrees(), refreshOperations(), refreshOverview(), refreshTasks()]);
  }

  async function refreshOperations() {
    const rows = await getJSON("/api/operations?limit=50");
    const root = document.getElementById("git-operations");
    if (!root) return;
    root.innerHTML = "";
    rows.forEach((op) => {
      const card = el("article", { class: "entity-card" }, [
        el("header", {}, [el("strong", { text: `${op.action} · ${op.stage}` }), el("span", { class: `status-pill st-${String(op.state).toLowerCase()}`, text: op.state })]),
        el("div", { class: "entity-meta truncate", text: op.target, title: op.target }),
        el("div", { text: op.message }),
      ]);
      if (op.action === "prepare_merge" && op.state === "SUCCEEDED") {
        const merge = el("button", { type: "button", class: "danger", text: "Merge prepared PR" });
        merge.addEventListener("click", () => confirmAndRun("Merge the prepared PR through GitHub policy checks?", () => postCommand("git_operation", { action: "merge", path: op.target, prepare_id: op.id, confirm: true }).then(refreshOperations)));
        card.appendChild(merge);
      }
      root.appendChild(card);
    });
    if (!rows.length) root.appendChild(el("p", { class: "hint", text: "No Git operations recorded yet." }));
  }

  // --------------------------------------------------------------------- runbooks (ENG-AGENT-02-S5)

  const RUNBOOK_DURATION_LABELS = { 120: "2 hours", 240: "4 hours", 360: "6 hours", 480: "8 hours" };
  let runbookFormInitialized = false;
  let requestedRunbookPreset = null;

  // --------------------------------------------------------------- quick start

  const STOP_CONDITION_LABELS = {
    LOCALLY_REVIEW_READY: "Locally review-ready",
    QUEUE_EXHAUSTED: "Queue exhausted",
    DEADLINE_REACHED: "Stop deadline reached",
    OWNER_DECISION_REQUIRED: "Owner decision required",
    PROVIDER_QUOTA_EXHAUSTED: "Provider/session quota exhausted",
  };

  function formatStopConditions(list) {
    return (list || []).map((c) => STOP_CONDITION_LABELS[c] || c).join(", ") || "—";
  }

  // Shared by the Quick Start card flow and the NL steering preview, so a
  // resolved Prepared Run always looks identical no matter how it was
  // reached (ENG-AGENT-02-S7, issue #97).
  function preparedRunSummaryEl(option) {
    const wrap = el("div", { class: "kv prepared-run-kv" });
    const rows = [
      ["Program", option.program || "OctaScene"],
      ["Task", [option.task_id, option.task_title].filter(Boolean).join(" — ") || option.source_ref || "—"],
      ["Issue / reference", option.issue_reference || option.source_ref || "—"],
      ["Why next", option.why_next || "—"],
      ["Dependencies", option.dependency_state || "—"],
      ["Objective", option.objective || "—"],
      ["Preferred implementer", option.parent_worker],
      ["Tester", option.proposed_tester_unavailable_reason ? `${option.proposed_tester} — ${option.proposed_tester_unavailable_reason}` : option.proposed_tester || "Not configured"],
      ["Reviewer", option.proposed_reviewer_unavailable_reason ? `${option.proposed_reviewer} — ${option.proposed_reviewer_unavailable_reason}` : option.proposed_reviewer || "Not configured"],
      ["Duration", RUNBOOK_DURATION_LABELS[option.duration_minutes] || `${option.duration_minutes} minutes`],
      ["Permission profile", option.permission_profile],
      [
        "Automatic fallback",
        option.codex_auto_eligible
          ? `Same-role policy selection at start · Codex ${option.codex_policy} · maximum ${option.max_codex_invocations} invocation`
          : "Not enabled for this prepared run",
      ],
      ["Branch / worktree mode", option.branch_worktree_mode || "automatic"],
      ["Expected focused checks", (option.expected_checks || []).join("; ") || "Task-specific checks"],
      ["Checkpoint / PR", option.checkpoint_pr_behavior || "Never auto-merge"],
      ["Stop conditions", formatStopConditions(option.stop_conditions)],
      ["Branch", option.branch || "—"],
      [
        "Worktree",
        option.branch && option.worktree
          ? option.continues_existing_worktree
            ? `${option.worktree} (existing)`
            : `${option.worktree} (created automatically on start)`
          : "—",
      ],
    ];
    rows.forEach(([k, v]) => {
      wrap.appendChild(el("div", { class: "k", text: k }));
      wrap.appendChild(el("div", { text: String(v) }));
    });
    if (!option.ready) {
      const warn = el("div", { class: "hint quickstart-unavailable" });
      warn.appendChild(el("strong", { text: "Not ready: " }));
      warn.appendChild(document.createTextNode(option.unavailable_reason || "unavailable"));
      wrap.appendChild(el("div", { class: "wide-field" }, [warn]));
    }
    return wrap;
  }

  function showPreparedRun(option) {
    const card = document.getElementById("card-prepared-run");
    document.getElementById("prepared-run-title").textContent = option.title;
    const body = document.getElementById("prepared-run-body");
    body.innerHTML = "";
    body.appendChild(preparedRunSummaryEl(option));
    const startBtn = document.getElementById("prepared-run-start");
    startBtn.disabled = !option.ready;
    startBtn.dataset.key = option.key;
    // A disabled Start needs its reason exposed to assistive tech, not just
    // visible in the card body (Grok Build review, issue #97).
    startBtn.setAttribute("aria-describedby", "prepared-run-feedback");
    document.getElementById("prepared-run-feedback").textContent = option.ready ? "" : option.unavailable_reason || "";
    card.hidden = false;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function hidePreparedRun() {
    document.getElementById("card-prepared-run").hidden = true;
  }

  function renderQuickStartRow(options) {
    const row = document.getElementById("quickstart-row");
    if (!row) return;
    row.innerHTML = "";
    options.forEach((option) => {
      const btn = el("button", { type: "button", class: "quickstart-btn" });
      btn.appendChild(el("strong", { text: option.title }));
      btn.appendChild(
        el("span", {
          class: "quickstart-summary",
          text: option.action === "advanced" ? "Open manual settings" : `${option.program || "OctaScene"} · ${option.source_ref || "Current state"}`,
        })
      );
      btn.appendChild(
        el("span", {
          class: `status-pill ${option.ready ? "st-available" : "st-blocked"}`,
          text: option.ready ? "Ready" : "Needs setup",
        })
      );
      // Say why it cannot start (e.g. the task is already accepted) right on the card.
      if (!option.ready && option.unavailable_reason) {
        btn.appendChild(el("span", { class: "quickstart-reason", text: option.unavailable_reason }));
      }
      btn.addEventListener("click", () => {
        if (option.action === "advanced") {
          openAdvancedSettings();
        } else {
          showPreparedRun(option);
        }
      });
      row.appendChild(btn);
    });
    if (!options.length) row.appendChild(el("p", { class: "hint", text: "No Quick Start options available." }));
  }

  // ENG-AGENT-02-S7 follow-up (dogfooding, dev.octascene.com): the primary
  // "resume the maintained Video Editor ledger task" workflow needs to be
  // visible the instant Overview loads, not only after navigating to Runs
  // and reading the Quick Start row -- fed from the exact same
  // /api/quickstart options as that row, never a second resolver.
  let overviewContinueOption = null;
  let overviewActiveRun = null;

  function renderOverviewContinue(options) {
    const card = document.getElementById("overview-continue-card");
    const title = document.getElementById("overview-continue-title");
    const detail = document.getElementById("overview-continue-detail");
    const btn = document.getElementById("overview-continue-btn");
    if (!card || !title || !detail || !btn) return;
    const target =
      options.find((o) => o.key === "continue-video-editor") || options.find((o) => o.ready) || options[0] || null;
    // ENG-AGENT-13 (issue #138): a runbook whose implementation succeeded but
    // whose acceptance pipeline (Test/Review/Checkpoint/PR readiness) is not
    // finished yet is still the current/latest task -- keep it primary here
    // rather than letting it fall back to a stale "continue" suggestion.
    const active =
      state.runbooks.find((r) =>
        ["RUNNING", "PAUSED", "STOPPING", "IMPLEMENTATION_COMPLETE", "ACCEPTANCE_PENDING"].includes(r.status)
      ) || null;
    overviewActiveRun = active;
    overviewContinueOption = target;
    if (active) {
      card.dataset.state = "active";
      title.textContent = "View Active Run";
      detail.textContent = `${active.name} — ${active.status}${active.source_ref ? ` · ${active.source_ref}` : ""}`;
      btn.textContent = "View Active Run";
      btn.disabled = false;
      return;
    }
    btn.textContent = "Start Development";
    if (!target) {
      card.dataset.state = "empty";
      title.textContent = "Nothing to continue right now";
      detail.textContent = "No Quick Start option is available yet — see Runs for details.";
      btn.disabled = true;
      return;
    }
    card.dataset.state = target.ready ? "ready" : "blocked";
    title.textContent = target.title;
    detail.textContent = target.ready
      ? `${target.source_ref || "Current task"} — ready to resume`
      : `${target.source_ref || "Current task"} — ${target.unavailable_reason || "needs setup"}`;
    btn.disabled = !target.ready;
  }

  // Go straight to a run: open Runs, select it, expand its details, scroll and focus it.
  function focusRunbook(runbookId) {
    state.focusRunbookId = runbookId;
    disclosureOpen.add(`run:${runbookId}`);
    showView("view-runs");
    state.focusRunbookId = runbookId; // showView clears focus when leaving Runs; re-assert for this navigation
    renderRunbookCards(state.runbooks);
    const card = document.querySelector(`[data-runbook-id="${runbookId}"]`);
    if (!card) return;
    card.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    card.focus({ preventScroll: true });
    const live = document.getElementById("runs-focus-status");
    if (live) live.textContent = `Showing run ${card.querySelector("h3")?.textContent || runbookId}`;
  }

  function initOverviewContinueAction() {
    const btn = document.getElementById("overview-continue-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      if (overviewActiveRun) {
        focusRunbook(overviewActiveRun.id);
        return;
      }
      if (!overviewContinueOption) return;
      showView("view-runs");
      showPreparedRun(overviewContinueOption);
    });
  }

  async function refreshQuickStart() {
    const options = await getJSON("/api/quickstart");
    state.quickstart = options;
    renderQuickStartRow(options);
    renderOverviewContinue(options);
  }

  function initPreparedRunActions() {
    document.getElementById("prepared-run-cancel").addEventListener("click", hidePreparedRun);
    document.getElementById("prepared-run-start").addEventListener("click", async (evt) => {
      const key = evt.currentTarget.dataset.key;
      const feedback = document.getElementById("prepared-run-feedback");
      evt.currentTarget.disabled = true;
      feedback.textContent = "Starting…";
      const result = await postCommand("quickstart_start", { key });
      if (result.ok && result.body && result.body.ok) {
        feedback.textContent = result.body.message || "Started.";
        hidePreparedRun();
        showView("view-runs");
        await Promise.all([refreshQuickStart(), refreshRunbooks()]);
      } else {
        feedback.textContent = `Error: ${(result.body && (result.body.detail || result.body.message)) || "could not start"}`;
        evt.currentTarget.disabled = false;
      }
    });
  }

  function openAdvancedSettings() {
    hidePreparedRun();
    showView("view-runs");
    const details = document.getElementById("card-runbook-create");
    if (details) {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    document.getElementById("rb-objective")?.focus();
  }

  function formatRemaining(seconds) {
    if (seconds === null || seconds === undefined) return "n/a";
    if (seconds <= 0) return "0m";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h > 0 ? `${h}h ${m}m remaining` : `${m}m remaining`;
  }

  function fillRunbookFormFromPreset(presetKey) {
    const preset = state.presets.find((p) => p.key === presetKey);
    if (!preset) return;
    const objectiveEl = document.getElementById("rb-objective");
    const durationEl = document.getElementById("rb-duration");
    const permissionEl = document.getElementById("rb-permission");
    const nameEl = document.getElementById("rb-name");
    if (!nameEl.value) nameEl.value = preset.name;
    objectiveEl.value = preset.objective_template;
    objectiveEl.placeholder = preset.objective_template;
    if (RUNBOOK_DURATION_LABELS[preset.default_duration_minutes]) durationEl.value = String(preset.default_duration_minutes);
    permissionEl.value = preset.permission_profile;
    const workerSelect = document.getElementById("rb-parent-worker");
    if (workerSelect && [...workerSelect.options].some((o) => o.value === preset.default_parent_worker)) {
      workerSelect.value = preset.default_parent_worker;
    }
  }

  // ENG-AGENT-02-S7: `initRunbookForm` used to be a single one-shot function
  // guarded by `runbookFormInitialized`, set unconditionally at the end. If the
  // very first call ever saw an empty `presets`/`worktrees`/`models` array (a
  // transient fetch race, or a browser/edge cache serving a stale bundle before
  // the S5 preset endpoint existed), the guard latched permanently and the
  // dropdown stayed empty ("No Options") for the rest of the session, even once
  // the real API later returned real data — a real defect found in live use.
  // The one-time structural pieces (stop-condition checkboxes, the static safety
  // list) still run exactly once; the API-populated `<select>`s now repopulate
  // on every call where they are still empty, so a transient miss self-heals on
  // the next ~2s poll instead of wedging the form forever.
  function populateSelectOnce(selectEl, items, toOption) {
    if (!selectEl || selectEl.options.length > 0 || !items.length) return;
    items.forEach((item) => selectEl.appendChild(toOption(item)));
  }

  function initRunbookForm(presets, worktrees, models) {
    const presetSelect = document.getElementById("rb-preset");
    const presetWasEmpty = presetSelect && presetSelect.options.length === 0;
    populateSelectOnce(presetSelect, presets, (p) => el("option", { value: p.key, text: p.name }));
    if (presetSelect && !presetSelect.dataset.wired) {
      presetSelect.addEventListener("change", () => fillRunbookFormFromPreset(presetSelect.value));
      presetSelect.dataset.wired = "1";
    }
    if (
      requestedRunbookPreset &&
      presetSelect &&
      [...presetSelect.options].some((option) => option.value === requestedRunbookPreset)
    ) {
      presetSelect.value = requestedRunbookPreset;
      fillRunbookFormFromPreset(requestedRunbookPreset);
      requestedRunbookPreset = null;
    } else if (presetWasEmpty && presets.length) {
      presetSelect.value = presets[0].key;
      fillRunbookFormFromPreset(presets[0].key);
    }

    const worktreeSelect = document.getElementById("rb-worktree");
    populateSelectOnce(worktreeSelect, worktrees, (w) =>
      el("option", { value: w.path, text: `${w.branch || "UNKNOWN"} — ${w.path}` })
    );

    const workerSelect = document.getElementById("rb-parent-worker");
    populateSelectOnce(
      workerSelect,
      models.filter((m) => m.enabled),
      (m) => el("option", { value: m.worker, text: `${m.display_name || displayName(m.worker)} (${m.execution_system})` })
    );

    if (runbookFormInitialized) return;
    const stopFieldset = document.getElementById("rb-stop-conditions");
    const STOP_CONDITIONS = [
      ["LOCALLY_REVIEW_READY", "Locally review-ready"],
      ["QUEUE_EXHAUSTED", "Queue exhausted"],
      ["DEADLINE_REACHED", "Stop deadline reached"],
      ["OWNER_DECISION_REQUIRED", "Owner decision required"],
      ["PROVIDER_QUOTA_EXHAUSTED", "Provider/session quota exhausted"],
    ];
    STOP_CONDITIONS.forEach(([value, label]) => {
      const row = el("label", { class: "stop-condition-row" });
      const cb = el("input", { type: "checkbox", value, checked: "checked" });
      row.append(cb, el("span", { text: label }));
      stopFieldset.appendChild(row);
    });

    const safetyList = document.getElementById("rb-safety-list");
    [
      "No automatic PR merge / auto-merge",
      "No force push",
      "No git reset --hard",
      "No destructive git clean",
      "No deleting branches/worktrees",
      "No secret/credential exposure",
      "No production-system/data mutation",
      "No unauthorized billable provider calls",
      "No paid CI / premium runners",
      "No weakening or skipping legitimate tests",
    ].forEach((line) => safetyList.appendChild(el("li", { text: line })));

    runbookFormInitialized = true;
  }

  // OCTAREL-OPS-02: Completed -> Advancing -> Next task selected / Blocked / No eligible task.
  const ADVANCEMENT_LABELS = {
    COMPLETED: "Completed",
    ADVANCING: "Advancing...",
    NEXT_SELECTED: "Next task selected",
    BLOCKED: "Blocked",
    NO_ELIGIBLE_TASK: "No eligible task",
    OWNER_DECISION_REQUIRED: "Owner decision required",
  };

  function renderAdvancement(a) {
    const wrap = el("div", {
      class: "runbook-advancement",
      role: "status",
      "data-advancement-state": a.state,
      "aria-label": "Task advancement",
    });
    const steps = ["Completed"];
    if (a.state !== "COMPLETED") steps.push(ADVANCEMENT_LABELS[a.state] || a.state);
    wrap.appendChild(
      el("div", { class: "runbook-phases" }, steps.map((label, i) =>
        el("span", { class: `phase-chip ${i ? statusClass(a.state) : ""}`, text: i ? `\u2193 ${label}` : label })
      ))
    );
    const next = a.next_task;
    if (next && (a.state === "NEXT_SELECTED" || a.state === "ADVANCING")) {
      wrap.appendChild(el("div", { class: "entity-meta", text: `${next.task_id} \u2014 ${next.title}` }));
      if (a.selection_reason) wrap.appendChild(el("div", { class: "entity-meta", text: `Why: ${a.selection_reason}` }));
      const deps = (a.dependency_status || []).map((d) => `${d.task_id}: ${d.status}`).join(", ");
      wrap.appendChild(el("div", { class: "entity-meta", text: `Dependencies: ${deps || "none declared"}` }));
      if (a.intended_worker) {
        wrap.appendChild(
          el("div", { class: "entity-meta", text: `Worker: ${displayName(a.intended_worker)}${a.intended_provider ? ` (${a.intended_provider})` : ""}` })
        );
      }
      if (a.started_runbook_id) wrap.appendChild(el("div", { class: "entity-meta", text: `Started as ${a.started_runbook_id}` }));
    } else if (a.reason) {
      wrap.appendChild(el("div", { class: "entity-meta", text: `reason: ${a.reason}` }));
    }
    return wrap;
  }

  function renderRunbookCards(runbooks) {
    const root = document.getElementById("runbooks-cards");
    root.innerHTML = "";
    runbooks.forEach((r) => {
      const card = el("article", {
        class: "entity-card run-card",
        tabindex: "-1",
        "data-runbook-id": r.id,
        "data-searchable": "true",
        "data-search": `${r.name} ${r.preset} ${r.branch} ${r.status}`,
      });
      // Scannable primary row: ID, title, state, current stage, agent, elapsed/progress.
      const linkedTask = (state.tasks || []).find((t) => t.id === r.task_id) || null;
      const agentModel = (state.models || []).find((m) => m.worker === r.parent_worker);
      const stageText = ({
        DRAFT: "Not started", QUEUED: "Queued", RUNNING: "Implementing",
        PAUSED: "Paused", STOPPING: "Stopping", SUCCEEDED: "Accepted", CANCELLED: "Cancelled",
        FAILED: "Failed", DEADLINE_REACHED: "Deadline reached",
      })[r.status] || (r.acceptance_stage && !["PENDING", "DONE"].includes(r.acceptance_stage)
        ? `Acceptance: ${String(r.acceptance_stage).replace(/_/g, " ")}` : String(r.status).replace(/_/g, " ").toLowerCase());
      const runElapsed = r.started_at ? elapsedBetween(r.started_at, ["RUNNING", "PAUSED", "STOPPING"].includes(r.status) ? null : r.ended_at) : null;
      const primary = el("ul", { class: "entity-facts", "aria-label": `Summary for ${r.name}` });
      [
        ["Stage", stageText],
        ["Agent", agentModel ? `${displayName(r.parent_worker)} · ${agentModel.provider}` : displayName(r.parent_worker)],
        runElapsed ? [r.status === "RUNNING" ? "Elapsed" : "Duration", runElapsed] : null,
        r.status === "RUNNING" && r.remaining_seconds != null ? ["Remaining", formatRemaining(r.remaining_seconds)] : null,
      ].filter(Boolean).forEach(([k, v]) => primary.appendChild(el("li", {}, [el("span", { class: "entity-fact-k", text: k }), el("span", { text: v })])));
      appendAll(card, [
        el("header", {}, [
          el("div", {}, [el("h3", { text: r.name }), el("div", { class: "entity-id", text: `${r.id} · ${r.branch}` })]),
          el("span", { class: `status-pill ${statusClass(r.status)}`, text: r.status }),
        ]),
        primary,
      ]);
      // ENG-AGENT-13 (issue #138): surface truthful per-stage acceptance
      // evidence -- PASS/FAIL/NOT_APPLICABLE/NOT_REPORTED, never silently
      // collapsed into the overall status pill above.
      if (r.acceptance_evidence && Object.keys(r.acceptance_evidence).length) {
        const acceptanceWrap = el("div", { class: "runbook-phases", "aria-label": `Acceptance evidence for ${r.name}` });
        Object.entries(r.acceptance_evidence).forEach(([stageName, record]) => {
          acceptanceWrap.appendChild(
            el("span", {
              class: `phase-chip ${statusClass(record.status || "NOT_REPORTED")}`,
              text: `${stageName}: ${record.status || "NOT_REPORTED"}`,
              title: record.reason || "",
            })
          );
        });
        card.appendChild(acceptanceWrap);
      }
      if (r.advancement) card.appendChild(renderAdvancement(r.advancement));
      if (r.recovery_note) card.appendChild(el("div", { class: "entity-meta", text: `Note: ${r.recovery_note}` }));
      if (r.failure_reason) {
        const failure = el("div", { class: "runbook-failure", role: "alert" });
        const facts = r.failure || {};
        const source = `${facts.execution_system || "UNKNOWN"} / ${facts.provider || "UNKNOWN"}`;
        failure.append(
          el("strong", { text: `${source} ${String(facts.category || "failure").replace(/_/g, " ").toLowerCase()}` }),
          el("p", { text: r.failure_reason }),
          el("div", { class: "entity-meta", text: `Failed worker ${facts.worker_id || "UNKNOWN"} · model ${facts.model || "UNKNOWN"}` })
        );
        if (facts.reset) {
          failure.appendChild(
            el("div", {
              class: "entity-meta",
              text: `Reset ${facts.reset}${facts.reset_source ? ` · source ${facts.reset_source}` : ""}`,
            })
          );
        }
        (r.recovery_actions || []).forEach((action) =>
          failure.appendChild(el("div", { class: "entity-meta", text: `Recovery: ${action}` }))
        );
        card.appendChild(failure);
      }
      if ((r.attempt_history || []).length) {
        const chain = el("div", {
          class: "runbook-fallback-chain",
          role: "status",
          "aria-label": `Worker attempt history for ${r.name}`,
        });
        r.attempt_history.forEach((attempt) => {
          if (attempt.automatic && attempt.from_worker) {
            chain.appendChild(
              el("div", {
                class: "entity-meta fallback-transition",
                text: `Automatically falling back to ${attempt.worker_name}`,
              })
            );
          }
          const failedAttempt = ["FAILED", "UNAVAILABLE", "BLOCKED"].includes(attempt.status);
          const category = failedAttempt && attempt.failure_category
            ? ` (${String(attempt.failure_category).replace(/_/g, " ")})`
            : "";
          chain.appendChild(
            el("div", {
              class: `entity-meta fallback-attempt ${statusClass(attempt.status)}`,
              text: `${attempt.worker_name} — ${attempt.status}${category}`,
            })
          );
        });
        if (r.fallback_transition?.status === "BLOCKED") {
          chain.appendChild(
            el("div", {
              class: "entity-meta fallback-blocked",
              text: r.fallback_transition.reason || "Automatic fallback blocked; manual recovery is required.",
            })
          );
        }
        card.appendChild(chain);
      }

      const actions = el("div", { class: "runbook-actions" });
      if (r.status === "DRAFT") {
        const startBtn = el("button", { type: "button", text: "Start" });
        startBtn.addEventListener("click", () => postCommand("runbook_start", { runbook_id: r.id }).then(refreshRunbooks));
        actions.appendChild(startBtn);
      }
      if (r.status === "RUNNING" || r.status === "PAUSED") {
        if (r.status === "RUNNING") {
          const pauseBtn = el("button", { type: "button", text: "Pause" });
          pauseBtn.addEventListener("click", () => postCommand("runbook_pause", { runbook_id: r.id }).then(refreshRunbooks));
          actions.appendChild(pauseBtn);
        } else {
          const resumeBtn = el("button", { type: "button", text: "Resume" });
          resumeBtn.addEventListener("click", () => postCommand("runbook_resume", { runbook_id: r.id }).then(refreshRunbooks));
          actions.appendChild(resumeBtn);
        }
        const stopBtn = el("button", { type: "button", class: "ctl-danger", text: "Stop" });
        stopBtn.addEventListener("click", () =>
          confirmAndRun(`Stop runbook "${r.name}"? A live worker subprocess is not force-killed; it is asked to finish gracefully.`, () =>
            postCommand("runbook_stop", { runbook_id: r.id, confirm: true }).then(refreshRunbooks)
          )
        );
        const stopAfterBtn = el("button", { type: "button", text: "Stop after current" });
        stopAfterBtn.addEventListener("click", () =>
          confirmAndRun(`Record stop-after-current for "${r.name}"?`, () =>
            postCommand("runbook_stop_after_current", { runbook_id: r.id, confirm: true }).then(refreshRunbooks)
          )
        );
        actions.append(stopBtn, stopAfterBtn);
      }
      if (["FAILED", "CANCELLED"].includes(r.status) && (r.eligible_retry_workers || []).length) {
        const retrySelect = el("select", { "aria-label": `Retry worker for ${r.name}` });
        r.eligible_retry_workers.forEach((worker) => {
          retrySelect.appendChild(el("option", {
            value: worker.name,
            text: `${worker.display_name} · ${worker.provider} · ${worker.model}`,
          }));
        });
        const retryBtn = el("button", { type: "button", class: "btn-primary", text: "Retry with selected worker" });
        retryBtn.addEventListener("click", () =>
          confirmAndRun(
            `Retry this same runbook and worktree with ${retrySelect.options[retrySelect.selectedIndex].text}?`,
            () => postCommand("runbook_retry", { runbook_id: r.id, worker: retrySelect.value }).then(refreshRunbooks)
          )
        );
        actions.append(retrySelect, retryBtn);
      }
      if (["OWNER_ACTION_REQUIRED", "BLOCKED"].includes(r.status)) {
        // ENG-AGENT-13 (issue #138): resume the halted acceptance stage only
        // -- this never relaunches the (already-succeeded) implementation
        // worker, unlike "Retry with selected worker" above.
        const resumeBtn = el("button", { type: "button", class: "btn-primary", text: "Resume acceptance" });
        resumeBtn.addEventListener("click", () =>
          confirmAndRun(
            `Resume the ${r.acceptance_stage || "halted"} acceptance stage for "${r.name}"? The implementation worker is not rerun.`,
            () => postCommand("runbook_retry_acceptance", { runbook_id: r.id, confirm: true }).then(refreshRunbooks)
          )
        );
        actions.appendChild(resumeBtn);
      }
      if (r.has_report) {
        const reportBtn = el("button", { type: "button", text: "View report" });
        reportBtn.addEventListener("click", () => openRunbookReport(r.id));
        actions.appendChild(reportBtn);
      }
      if (linkedTask) {
        const liveBtn = el("button", { type: "button", text: "Live output", "aria-label": `Open live output for ${r.name}` });
        liveBtn.addEventListener("click", () => openWorkerActivity && openWorkerActivity({
          id: linkedTask.id, worker: linkedTask.worker, label: r.name, state: linkedTask.state,
          provider: agentModel && agentModel.provider, model: agentModel && agentModel.default_model,
          started_at: r.started_at,
        }, linkedTask.task_ref));
        actions.appendChild(liveBtn);
      }
      card.appendChild(actions);
      // Secondary implementation detail, one disclosure away (kept open across polls).
      card.appendChild(rememberDisclosure(el("details", { class: "entity-more" }, [
        el("summary", { text: "Run details" }),
        el("div", { class: "entity-meta", text: r.objective }),
        el("div", {
          class: "entity-meta",
          text: `${r.preset} · worker ${displayName(r.parent_worker)} (${r.parent_worker}) · permission ${r.permission_profile} · budget ${r.max_duration_minutes}m`,
        }),
        el("div", { class: "entity-meta", text: `Branch ${r.branch}${r.worktree ? ` · ${r.worktree}` : ""}` }),
        (r.phases && r.phases.length)
          ? el("div", { class: "runbook-phases" }, r.phases.map((phase) => el("span", { class: "phase-chip", text: phase })))
          : null,
      ]), `run:${r.id}`));
      if (state.focusRunbookId === r.id) {
        card.classList.add("is-focused");
        card.setAttribute("aria-current", "true");
      }
      root.appendChild(card);
    });
    if (!runbooks.length) root.appendChild(el("p", { class: "hint", text: "No runbooks yet. Create one above or use Run Overnight." }));
  }

  async function openRunbookReport(runbookId) {
    const data = await getJSON(`/api/runbooks/${runbookId}/report`);
    document.getElementById("report-body").textContent = data.report_markdown || "(no report yet)";
    document.getElementById("report-backdrop").hidden = false;
    document.getElementById("report-sheet").hidden = false;
  }

  function initReportSheet() {
    function close() {
      document.getElementById("report-backdrop").hidden = true;
      document.getElementById("report-sheet").hidden = true;
    }
    document.getElementById("report-close").addEventListener("click", close);
    document.getElementById("report-backdrop").addEventListener("click", close);
  }

  // ENG-AO-05: durable overnight session read model + controls (the daemon advances it).
  function overnightRemaining(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  }

  function renderOvernight(payload) {
    const session = payload && payload.current;
    const body = document.getElementById("overnight-body");
    body.innerHTML = "";
    const live = !!session && ["ACTIVE", "PAUSED", "STOPPING"].includes(session.state);
    document.getElementById("overnight-state").textContent = session ? session.state : "No session";
    document.getElementById("overnight-form").hidden = live || !!(session && session.state === "STOPPED" && session.resumable);
    document.getElementById("overnight-controls").hidden = !live;
    document.getElementById("overnight-pause").hidden = !(session && session.state === "ACTIVE");
    document.getElementById("overnight-resume").hidden = !(
      session && (session.state === "PAUSED" || (session.state === "STOPPED" && session.resumable))
    );
    document.getElementById("overnight-stop-after").hidden = !live || session.state === "STOPPING";
    if (session && session.state === "STOPPED" && session.resumable) {
      document.getElementById("overnight-controls").hidden = false;
      document.getElementById("overnight-pause").hidden = true;
      document.getElementById("overnight-stop-after").hidden = true;
      document.getElementById("overnight-stop").hidden = true;
    } else {
      document.getElementById("overnight-stop").hidden = !live;
    }
    if (!session) {
      body.appendChild(el("div", { class: "hint", text: "No overnight session for this project." }));
      return;
    }
    const rb = session.current_runbook;
    const last = session.last_accepted;
    const rows = [
      ["Project", session.project_id],
      ["State", session.state],
      ["Started", session.started_at || "—"],
      ["Deadline", session.deadline_at || "—"],
      ["Time remaining", live ? overnightRemaining(session.time_remaining_seconds) : "—"],
      ["Current task", session.current_task ? `${session.current_task.task_id} — ${session.current_task.title || ""}` : "—"],
      ["Current runbook / stage", rb ? `${rb.id} · ${rb.status}${rb.stage ? " · " + rb.stage : ""}` : "—"],
      ["Current provider", rb ? [rb.worker, rb.provider].filter(Boolean).join(" / ") || "—" : "—"],
      ["Accepted tasks", String(session.accepted_count)],
      ["Maximum tasks", session.max_tasks ? String(session.max_tasks) : "no limit"],
      ["Last accepted task", last ? `${last.task_id || last.runbook_id}${last.merged ? " (merged)" : " (not merged)"}` : "—"],
      ["Merge authorization", session.merge_authorized ? "authorized for this session" : "owner merges (not authorized)"],
      ["Stop reason", session.stop_reason ? `${session.stop_kind}: ${session.stop_reason}` : "—"],
      ["Next advancement", session.next_advancement || "—"],
    ];
    rows.forEach(([k, v]) => {
      body.appendChild(el("div", { class: "k", text: k }));
      body.appendChild(el("div", { text: String(v) }));
    });
  }

  async function refreshOvernight() {
    if (!document.getElementById("card-overnight")) return;
    try {
      renderOvernight(await getJSON("/api/overnight"));
    } catch (err) {
      document.getElementById("overnight-feedback").textContent = `Could not load overnight session: ${err.message}`;
    }
  }

  async function overnightCommand(verb, payload, working) {
    const feedback = document.getElementById("overnight-feedback");
    feedback.textContent = working;
    const result = await postCommand(verb, payload);
    feedback.textContent =
      result.ok && result.body && result.body.ok
        ? result.body.message
        : `Error: ${(result.body && (result.body.detail || result.body.message)) || "request failed"}`;
    await refreshOvernight();
  }

  function initOvernightActions() {
    document.getElementById("overnight-form").addEventListener("submit", (evt) => {
      evt.preventDefault();
      const payload = { duration: `${Number(document.getElementById("overnight-duration").value)}h` };
      const max = document.getElementById("overnight-max-tasks").value;
      if (max !== "") payload.max_tasks = Number(max);
      overnightCommand("overnight_start", payload, "Starting…");
    });
    document.getElementById("overnight-pause").addEventListener("click", () => overnightCommand("overnight_pause", {}, "Pausing…"));
    document.getElementById("overnight-resume").addEventListener("click", () => overnightCommand("overnight_resume", {}, "Resuming…"));
    document
      .getElementById("overnight-stop-after")
      .addEventListener("click", () => overnightCommand("overnight_stop_after_current", {}, "Requesting stop after current…"));
    document
      .getElementById("overnight-stop")
      .addEventListener("click", () =>
        confirmAndRun("Stop the overnight session now and cancel its current run through safe process termination?", () =>
          overnightCommand("overnight_stop", { confirm: true }, "Stopping…")
        )
      );
  }

  async function refreshRunbooks() {
    const [presets, worktrees, models, runbooks] = await Promise.all([
      state.presets.length ? Promise.resolve(state.presets) : getJSON("/api/runbooks/presets"),
      getJSON("/api/worktrees"),
      getJSON("/api/models"),
      getJSON("/api/runbooks"),
    ]);
    state.presets = presets;
    state.runbooks = runbooks;
    initRunbookForm(presets, worktrees, models);
    renderRunbookCards(runbooks);
    const active = runbooks.filter((r) => r.status === "RUNNING" || r.status === "PAUSED").length;
    const badge = document.getElementById("nav-runs-badge");
    badge.textContent = String(active);
    badge.hidden = active === 0;
    renderOverviewContinue(state.quickstart);
    await refreshOvernight();
  }

  function initRunbookFormSubmit() {
    document.getElementById("runbook-form").addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const feedback = document.getElementById("runbook-form-feedback");
      feedback.textContent = "Creating…";
      const stopConditions = [...document.querySelectorAll('#rb-stop-conditions input[type="checkbox"]:checked')].map(
        (cb) => cb.value
      );
      const payload = {
        name: document.getElementById("rb-name").value,
        preset: document.getElementById("rb-preset").value,
        objective: document.getElementById("rb-objective").value,
        source_ref: document.getElementById("rb-source-ref").value,
        branch: document.getElementById("rb-branch").value,
        worktree: document.getElementById("rb-worktree").value,
        duration_minutes: Number(document.getElementById("rb-duration").value),
        parent_worker: document.getElementById("rb-parent-worker").value,
        permission_profile: document.getElementById("rb-permission").value,
        stop_conditions: stopConditions,
      };
      const result = await postCommand("runbook_create", payload);
      if (result.ok) {
        feedback.textContent = `Created ${result.body?.data?.runbook_id || ""} (DRAFT). Review then Start below.`;
        document.getElementById("rb-name").value = "";
        document.getElementById("rb-source-ref").value = "";
        document.getElementById("rb-branch").value = "";
        await refreshRunbooks();
      } else {
        feedback.textContent = `Error: ${result.body?.detail || "could not create runbook"}`;
      }
    });
  }

  function initRunOvernightButton() {
    document.getElementById("run-overnight-btn").addEventListener("click", () => {
      showView("view-runs");
      // Preserve this intent if the initial preset fetch is still in flight.
      // initRunbookForm applies it as soon as the option exists instead of
      // allowing the default preset to overwrite the quick action.
      requestedRunbookPreset = "overnight-development";
      // This quick action goes straight to the manual form (a specific preset
      // is already decided), so Advanced Settings must open automatically —
      // otherwise the field it just populated would be invisible.
      const details = document.getElementById("card-runbook-create");
      if (details) details.open = true;
      const presetSelect = document.getElementById("rb-preset");
      if (presetSelect && [...presetSelect.options].some((option) => option.value === requestedRunbookPreset)) {
        presetSelect.value = "overnight-development";
        fillRunbookFormFromPreset("overnight-development");
        requestedRunbookPreset = null;
      }
      document.getElementById("rb-objective")?.focus();
    });
  }

  async function refreshTests() {
    const [data, performance] = await Promise.all([
      getJSON("/api/tests"),
      getJSON("/api/development-throughput"),
    ]);
    const gate = data.local_gate || {};
    const evidence = gate.evidence || {};
    const current = performance.current || {};
    const impact = current.test_impact || {};
    const throughput = current.throughput || {};
    const reuse = current.evidence_reuse || {};
    state.gate = gate;
    renderKV("tests-body", [
      ["Status", gate.status === "IDLE" ? "IDLE" : gate.ready ? "PASS" : "NOT READY", gate.status === "IDLE" || gate.ready ? undefined : "warn"],
      ["Reason", gate.reason || "-"],
      ["Authority", data.authority],
      ["Exact commit", evidence.current_head_sha || evidence.head_sha || "-"],
      ["Exact tree", evidence.tree_sha || "-"],
      ["Risk", evidence.classification || "-"],
      ["Test tier", (evidence.selection || {}).test_tier || "-"],
      ["Review level", (evidence.selection || {}).review_level || "-"],
      ["UI audit scope", (evidence.selection || {}).ui_audit_scope || "none"],
      ["Preflight", (evidence.preflight || {}).result || "not recorded"],
      ["Worktree readiness", (current.environment || {}).status || "UNKNOWN"],
      ["Concrete selection", (impact.python_selectors || []).concat(impact.frontend_selectors || []).join(", ") || (impact.python_full || impact.frontend_full ? "complete applicable suite" : "none")],
      ["Phase DAG", (current.phase_dag || []).map((p) => `${p.name}${p.dependencies?.length ? ` <- ${p.dependencies.join("+")}` : ""}`).join(", ") || "UNKNOWN"],
      ["Gate elapsed", throughput.gate_duration_seconds == null ? "UNKNOWN" : `${throughput.gate_duration_seconds}s`],
      ["Estimate", current.estimate_label === "ESTIMATED" ? Object.entries(current.estimated_phase_seconds || {}).map(([name, value]) => `${name}:${value ?? "UNKNOWN"}s`).join(", ") : "UNKNOWN"],
      ["Ports", Object.entries(current.ports || {}).map(([name, value]) => `${name}:${value}`).join(", ") || "none"],
      ["Evidence reused", (reuse.reused_phases || []).join(", ") || "none"],
      ["Evidence invalidated", (reuse.invalidated || []).map((item) => `${item.phase}: ${item.reason}`).join(", ") || "none"],
      ["Rerun", `${throughput.rerun_count ?? "UNKNOWN"} · ${throughput.rerun_reason || "UNKNOWN"}`],
      ["Premium active / elapsed", throughput.premium_model_active_ratio || "UNKNOWN"],
      ["Recent gate trend", (performance.recent || []).map((item) => `${item.test_tier || "?"}:${item.duration_seconds ?? "UNKNOWN"}s`).join(", ") || "UNKNOWN"],
      ["Selected checks", (evidence.checks || []).map((c) => `${c.name}:${c.result}`).join(", ") || "-"],
      ["Documentation", evidence.documentation_reconciled ? "reconciled" : "not reconciled"],
      ["Independent review", evidence.independent_review_provider || "not required/recorded"],
      ["Evidence", evidence.evidence_path || "-"],
      ["Logs", (evidence.checks || []).map((c) => c.log).join(", ") || "-"],
    ]);
    // Status, reason and authority stay up front; the rest is evidence detail.
    collapseKV("tests-body", 3, "Gate evidence and history");
    renderSystemHealth();
  }

  // Keep the first `keep` label/value pairs visible; move the rest into one disclosure.
  function collapseKV(containerId, keep, label) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const nodes = [...container.children];
    if (nodes.length <= keep * 2) return;
    const rest = el("div", { class: "kv kv-rest" });
    nodes.slice(keep * 2).forEach((node) => rest.appendChild(node));
    container.appendChild(rememberDisclosure(el("details", { class: "kv-more" }, [el("summary", { text: label }), rest]), `kv:${containerId}`));
  }

  // Warnings only: healthy values stay quiet, problems are named with a glyph and words.
  function renderSystemHealth() {
    const box = document.getElementById("system-summary");
    if (!box) return;
    const problems = [];
    const r = state.resources || {};
    if (r.available) {
      if (r.cpu_percent >= 90) problems.push(["Runtime", `CPU is at ${r.cpu_percent}%`]);
      if (r.memory_percent >= 85) problems.push(["Runtime", `Memory is at ${r.memory_percent}%`]);
      if (r.disk_percent >= 90) problems.push(["Runtime", `Disk is at ${r.disk_percent}%`]);
    }
    const repo = state.repoHealth || {};
    const dirty = (repo.main || {}).dirty_worktree_count;
    if (dirty > 0) problems.push(["Repository", `${dirty} worktree${dirty === 1 ? " has" : "s have"} uncommitted changes`]);
    if (repo.unpushed_worktrees > 0) problems.push(["Repository", `${repo.unpushed_worktrees} worktree${repo.unpushed_worktrees === 1 ? " has" : "s have"} unpushed commits`]);
    const app = state.appStatus || {};
    if (app.last_exit_code != null && app.last_exit_code !== 0) problems.push(["Services", `Development app last exited with code ${app.last_exit_code}`]);
    const gate = state.gate;
    if (gate && !gate.ready && gate.status !== "IDLE") problems.push(["Diagnostics", `Local gate not ready: ${gate.reason || "no reason recorded"}`]);
    const errors = (state.events || []).filter((e) => e.level === "error");
    if (errors.length) problems.push(["Diagnostics", `${errors.length} recent error event${errors.length === 1 ? "" : "s"}`]);

    box.innerHTML = "";
    box.dataset.problems = String(problems.length);
    if (!problems.length) {
      box.appendChild(el("p", { class: "system-ok" }, [el("span", { "aria-hidden": "true", text: "✓ " }), document.createTextNode("No warnings — resources, repository, services and diagnostics look healthy.")]));
    } else {
      box.appendChild(el("p", { class: "system-problems-title" }, [el("span", { "aria-hidden": "true", text: "⚠ " }), document.createTextNode(`${problems.length} warning${problems.length === 1 ? "" : "s"}`)]));
      const list = el("ul", { class: "system-problem-list" });
      problems.forEach(([group, text]) => list.appendChild(el("li", {}, [el("span", { class: "chip chip-warn", text: group }), el("span", { text })])));
      box.appendChild(list);
    }
    const recent = document.getElementById("system-problems");
    if (recent) {
      recent.innerHTML = "";
      const items = (state.events || []).filter((e) => ["error", "warning"].includes(e.level)).slice(0, 6);
      renderEventItems(recent, items);
      if (!items.length) recent.appendChild(el("li", { class: "attn-clear" }, [el("span", { class: "attn-icon ok", "aria-hidden": "true", text: "✓" }), el("span", { text: "No recent errors or warnings." })]));
    }
  }

  // OCTAREL-UI-02: one visual language for everything that needs an operator.
  // Each kind carries a glyph AND a text label (never colour alone), a tone,
  // and the view where the operator can act. The same rows render in the bell
  // popover and on Overview; this is a presentation of /api/attention plus the
  // advancement records already on /api/runbooks, not a second notification system.
  const ATTENTION_KINDS = {
    owner: { label: "Owner decision required", glyph: "?", tone: "warn", view: "view-runs" },
    auth: { label: "Authentication required", glyph: "🔑", tone: "warn", view: "view-providers" },
    provider: { label: "Provider unavailable", glyph: "⊘", tone: "warn", view: "view-providers" },
    blocked: { label: "Task blocked", glyph: "⏸", tone: "warn", view: "view-tasks" },
    test: { label: "Failed test", glyph: "✕", tone: "err", view: "view-tasks" },
    review: { label: "Failed review", glyph: "✕", tone: "err", view: "view-tasks" },
    quota: { label: "Spend / quota issue", glyph: "$", tone: "err", view: "view-providers" },
    stale: { label: "Stale repository state", glyph: "↻", tone: "warn", view: "view-runs" },
    failed: { label: "Task failed", glyph: "✕", tone: "err", view: "view-tasks" },
    run: { label: "Run needs attention", glyph: "!", tone: "warn", view: "view-runs" },
  };

  function attentionItems(data) {
    const items = [];
    const add = (kind, title, detail, extra) => items.push({ kind, title, detail: detail || "", ...(extra || {}) });
    (data.tasks || []).forEach((t) => {
      const title = `Task ${t.id}: ${t.state} (${t.task_ref}/${t.role})`;
      const category = String(t.failure_category || "");
      const role = String(t.role || "");
      let kind = "blocked";
      if (t.state === "FAILED") {
        if (["QUOTA", "RATE_LIMIT"].includes(category)) kind = "quota";
        else if (category === "AUTH_CLI") kind = "auth";
        else if (/test/i.test(role)) kind = "test";
        else if (/review|drift/i.test(role)) kind = "review";
        else kind = "failed";
      }
      const waiting = (t.dependencies || []).length ? `Waiting on ${t.dependencies.join(", ")}` : "";
      add(kind, title, t.failure_reason_sanitized || t.last_error || t.admission_reason || waiting);
    });
    (data.providers || []).forEach((p) => {
      const name = p.display_name || displayName(p.name);
      const stateName = p.display_state || p.state;
      let kind = "provider";
      if (["QUOTA_EXHAUSTED", "RATE_LIMITED", "COST_BLOCKED"].includes(p.state)) kind = "quota";
      else if (p.reason === "NOT_AUTHENTICATED") kind = "auth";
      add(kind, `Provider ${name}: ${stateName}`, p.last_error || availabilityReasonLabel(p.reason));
    });
    (data.runbooks || []).forEach((r) => {
      const kind = r.status === "OWNER_ACTION_REQUIRED" ? "owner" : "run";
      add(kind, `Runbook ${r.name}: ${r.status}`, r.recovery_note || "");
    });
    // Advancement (OCTAREL-OPS-02) stops that are not already a troubled runbook.
    (state.runbooks || []).forEach((r) => {
      const a = r.advancement;
      if (!a) return;
      if (a.state === "OWNER_DECISION_REQUIRED") add("owner", `Next task for ${r.name}: owner decision`, a.reason);
      else if (a.state === "BLOCKED") {
        const kind = a.stop_kind === "stale_repository_state" ? "stale"
          : a.stop_kind === "provider_unavailable" ? "quota" : "blocked";
        add(kind, `Next task for ${r.name}: blocked`, a.reason);
      }
    });
    return items;
  }

  function attentionRow(item, opts) {
    const spec = ATTENTION_KINDS[item.kind] || ATTENTION_KINDS.run;
    const row = el("li", { class: `attn attn-${spec.tone}`, "data-attention-kind": item.kind }, [
      el("span", { class: "attn-icon", "aria-hidden": "true", text: spec.glyph }),
      el("div", { class: "attn-copy" }, [
        el("span", { class: "attn-kind", text: spec.label }),
        el("span", { class: "attn-title", text: item.title }),
        item.detail ? el("span", { class: "attn-detail", text: item.detail }) : null,
      ]),
    ]);
    if (opts && opts.action) {
      const go = el("button", { type: "button", class: "attn-action", text: "Open", "aria-label": `Open ${spec.view.replace("view-", "")} for ${spec.label}` });
      go.addEventListener("click", () => { closeAttention(); showView(spec.view); });
      row.appendChild(go);
    }
    return row;
  }

  async function refreshAttention() {
    const data = await getJSON("/api/attention");
    const runbooksNeedingAttention = data.runbooks || [];
    renderKV("attention-body", [
      ["Tasks needing attention", data.tasks.length],
      ["Providers needing attention", data.providers.length],
      ["Runbooks needing attention", runbooksNeedingAttention.length],
    ]);
    const items = attentionItems(data);
    const list = document.getElementById("attention-list");
    const attentionSignature = JSON.stringify([data, items]);
    if (list.dataset.signature === attentionSignature) {
      state.attentionCount = data.tasks.length + data.providers.length + runbooksNeedingAttention.length;
      return;
    }
    list.dataset.signature = attentionSignature;
    list.innerHTML = "";
    // The popover keeps one row per /api/attention entry; advancement-derived
    // rows appear on the Overview card only.
    const base = data.tasks.length + data.providers.length + runbooksNeedingAttention.length;
    items.slice(0, base).forEach((item) => list.appendChild(attentionRow(item, { action: true })));
    const card = document.getElementById("overview-attention-list");
    if (card) {
      card.innerHTML = "";
      items.forEach((item) => card.appendChild(attentionRow(item, { action: true })));
      if (!items.length) {
        card.appendChild(el("li", { class: "attn-clear" }, [
          el("span", { class: "attn-icon ok", "aria-hidden": "true", text: "✓" }),
          el("span", { text: "Nothing needs your attention." }),
        ]));
      }
      const count = document.getElementById("overview-attention-count");
      if (count) count.textContent = items.length ? `${items.length} item${items.length === 1 ? "" : "s"}` : "All clear";
      document.getElementById("overview-attention")?.setAttribute("data-count", String(items.length));
    }
    const attentionCount = base;
    state.attentionCount = attentionCount;
    updateBell(attentionCount);
    if (attentionCount > notifState.lastAttentionCount) {
      notify("Attention needed", `${attentionCount} item(s) now need attention`);
    }
    notifState.lastAttentionCount = attentionCount;
  }

  function eventIcon(evt) {
    const cat = String(evt.category || "");
    if (cat === "steering") return "S";
    if (cat === "command") return "C";
    if (evt.level === "error") return "!";
    return "•";
  }

  function renderEventItems(listEl, events) {
    listEl.innerHTML = "";
    events.forEach((e) => {
      const task = state.tasks.find((item) => item.id === e.task_id);
      const model = state.models.find((item) => item.worker === (task && task.worker));
      const attribution = model ? `${model.execution_system} · ${model.provider} · ${model.default_model} · ${model.default_intensity}` : e.provider || "Local control plane";
      const title = e.message || `${e.category}`;
      const short = title.length > 90 ? `${title.slice(0, 87)}…` : title;
      listEl.appendChild(
        el("li", { class: `level-${e.level}`, "data-searchable": "true", "data-search": title }, [
          el("span", { class: "evt-icon", text: eventIcon(e) }),
          el("div", {}, [el("strong", { text: short }), el("div", { class: "entity-meta", text: `${e.category} · ${e.task_id || "system"} · ${attribution}` })]),
          el("span", { class: "evt-when", text: relativeTime(e.ts) }),
        ])
      );
    });
  }

  async function refreshEvents() {
    const events = await getJSON("/api/events?limit=100");
    state.events = events;
    const hist = document.getElementById("events-list");
    const term = (document.getElementById("history-search")?.value || "").toLowerCase();
    const category = document.getElementById("history-category")?.value || "";
    renderEventItems(hist, events.filter((e) => (!category || e.category === category) && (!term || JSON.stringify(e).toLowerCase().includes(term))));
    const recent = document.getElementById("recent-events");
    renderEventItems(recent, events.slice(0, 8));
    renderActivity(events, state.activityHours);
    renderSystemHealth();
  }

  async function refreshRunEvidence() {
    const rows = await getJSON("/api/run-evidence?limit=100");
    const root = document.getElementById("run-evidence");
    if (!root) return;
    root.innerHTML = "";
    rows.forEach((run) => {
      const actual = run.actual || {};
      const planned = run.planned || {};
      const details = el("details", { class: "entity-card evidence-card", "data-searchable": "true", "data-search": JSON.stringify(run) });
      details.append(el("summary", {}, [el("strong", { text: `${run.role || "Run"} · ${run.task || "Unknown task"}` }), el("span", { class: `status-pill st-${String(run.result || "unknown").toLowerCase()}`, text: run.result || "UNKNOWN" })]), el("div", { class: "worktree-facts" }, [
        el("span", { text: `Actual: ${actual.execution_system || "UNKNOWN"} / ${actual.provider || "UNKNOWN"} / ${actual.model || "UNKNOWN"} / ${actual.intensity || "UNKNOWN"}` }),
        el("span", { text: `Planned: ${planned.execution_system || "UNKNOWN"} / ${planned.provider || "UNKNOWN"} / ${planned.model || "UNKNOWN"} / ${planned.intensity || "UNKNOWN"}` }),
        el("span", { text: `Why: ${run.why_this_model || "Not recorded"}` }),
        el("span", { text: `Duration ${run.duration_seconds ?? "UNKNOWN"}s · exit ${run.exit_status ?? "UNKNOWN"} · files ${run.files_changed.length}` }),
        el("span", { text: `Checks: ${run.tests_or_checks.join(", ") || "None recorded"}` }),
        el("code", { text: (run.requested_command || []).join(" ") }),
        el("span", { text: `Evidence: ${run.manifest_path}` }),
      ]));
      root.appendChild(details);
    });
  }

  async function refreshRoadmap() {
    const rows = await getJSON("/api/roadmap");
    const root = document.getElementById("roadmap-cards");
    const summary = document.getElementById("roadmap-summary");
    root.innerHTML = "";
    const computable = rows.filter((row) => row.total !== null);
    const total = computable.reduce((sum, row) => sum + row.total, 0);
    const complete = computable.reduce((sum, row) => sum + row.completed, 0);
    const pct = total ? Math.round((complete * 1000) / total) / 10 : null;
    summary.innerHTML = "";
    summary.append(el("strong", { text: pct === null ? "Tracked completion not computable" : `${complete}/${total} · ${pct}% DERIVED` }), el("div", { class: "progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": pct ?? "0" }, [el("span", { style: `width:${pct || 0}%` })]));
    rows.forEach((row) => {
      const card = el("article", { class: "entity-card roadmap-card" });
      card.append(el("header", {}, [el("h3", { text: row.program }), el("span", { class: "status-pill st-paused", text: row.version })]), el("strong", { text: row.percentage_label }));
      if (row.total !== null) card.appendChild(el("div", { class: "progress-track", role: "progressbar", "aria-label": `${row.program} completion`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": row.percentage }, [el("span", { style: `width:${row.percentage}%` })]));
      card.append(el("div", { class: "entity-meta", text: row.total === null ? "No deterministic denominator in the maintained ledger." : `${row.completed} complete · ${row.active} active · ${row.pending} pending · ${row.blocked} blocked` }), el("div", { class: "entity-meta", text: `Source: ${row.canonical_source || row.status_source}` }));
      root.appendChild(card);
    });
  }

  async function refreshAppLifecycle() {
    const data = await getJSON("/api/app-status");
    renderKV("app-lifecycle-body", [["State", data.status], ["PID", data.pid || "—"], ["Port", data.port], ["Uptime", data.uptime_seconds == null ? "—" : `${data.uptime_seconds}s`], ["Started", data.started_at || "—"], ["Launch", data.launch_source], ["Last exit", data.last_exit_code ?? "—"], ["Observation", data.note || data.port_probe]]);
    state.appStatus = data;
    renderSystemHealth();
    document.getElementById("app-start").disabled = data.status === "RUNNING" || data.status === "UNKNOWN";
    document.getElementById("app-stop").disabled = data.status !== "RUNNING";
    document.getElementById("app-restart").disabled = data.status !== "RUNNING";
  }

  async function refreshRepositoryHealth() {
    const data = await getJSON("/api/repository-health");
    const main = data.main || {};
    state.repoHealth = data;
    renderKV("repository-health-body", [["Main", main.remote_head_sha ? main.remote_head_sha.slice(0, 8) : "UNKNOWN"], [main.remote_time_label || "Remote head", main.remote_head_commit_at || "Unavailable"], ["Last observed", data.observed_at || "—"], ["Open task branches", data.open_task_branches ?? "—"], ["Unpushed worktrees", data.unpushed_worktrees ?? "—", data.unpushed_worktrees > 0 ? "warn" : undefined], ["Dirty worktrees", main.dirty_worktree_count ?? "—", main.dirty_worktree_count > 0 ? "warn" : undefined], ["Active workers", main.active_worker_count ?? "—"]]);
    renderSystemHealth();
    const root = document.getElementById("recent-merges");
    if (!root) return;
    root.innerHTML = "";
    (data.recent_merges || []).forEach((merge) => root.appendChild(el("div", { class: "entity-meta", text: `${merge.sha.slice(0, 8)} · ${merge.merged_at} · ${merge.subject}` })));
  }

  async function refreshTerminalHistory() {
    const limit = document.getElementById("terminal-history-limit")?.value || "50";
    const rows = await getJSON(`/api/terminal/history?limit=${encodeURIComponent(limit)}`);
    const query = (document.getElementById("terminal-history-search")?.value || "").toLowerCase();
    const root = document.getElementById("terminal-history");
    if (!root) return;
    root.innerHTML = "";
    rows.filter((row) => !query || JSON.stringify(row).toLowerCase().includes(query)).forEach((row) => root.appendChild(el("article", { class: "entity-card" }, [el("code", { text: row.command }), el("div", { class: "entity-meta", text: `${row.ts} · ${row.actor} · ${row.branch || "detached"} · ${row.session_id} · exit ${row.exit_code ?? "UNKNOWN"}` }), el("div", { class: "entity-meta truncate", text: row.cwd, title: row.cwd })])));
    if (!rows.length) root.appendChild(el("p", { class: "hint", text: "No Control Center terminal commands recorded." }));
  }

  async function refreshTelemetry() {
    const data = await getJSON("/api/telemetry");
    state.telemetry = data;
    const tbody = document.querySelector("#telemetry-table tbody");
    tbody.innerHTML = "";
    data.providers.forEach((p) => {
      tbody.appendChild(
        el("tr", { role: "row" }, [
          el("td", { role: "cell", "data-label": "Provider", text: p.display_name || displayName(p.name) }),
          el("td", { role: "cell", "data-label": "Route", text: p.execution_route }),
          el("td", { role: "cell", "data-label": "State", text: p.display_state || p.state }),
        ])
      );
    });
    const windows = data.quota_windows || [];
    renderKV(
      "quota-body",
      windows.map((w) => [w.name || w.label || w.window || "quota", w.status || w.state || "UNKNOWN"])
    );
    renderWorktreeCards(state.worktrees, data.checkpoints);
  }

  async function refreshCheckpointAge() {
    try {
      const data = state.telemetry && state.telemetry.checkpoints ? state.telemetry : await getJSON("/api/telemetry");
      const checkpoints = data.checkpoints || [];
      if (!checkpoints.length) {
        document.getElementById("checkpoint-age").textContent = "UNKNOWN";
        return;
      }
      const times = checkpoints
        .map((c) => c.last_commit_at || c.last_commit_time || c.commit_time)
        .filter(Boolean)
        .map((t) => new Date(t).getTime())
        .filter((n) => Number.isFinite(n));
      if (!times.length) {
        document.getElementById("checkpoint-age").textContent = "UNKNOWN";
        return;
      }
      const newest = Math.max(...times);
      const ageMinutes = Math.max(0, Math.round((Date.now() - newest) / 60000));
      document.getElementById("checkpoint-age").textContent = `${ageMinutes}m`;
    } catch (err) {
      document.getElementById("checkpoint-age").textContent = "UNKNOWN";
    }
  }

  let currentTasks = [];
  let refreshInFlight = null;

  function refreshAll() {
    // A slow endpoint must not let the 2-second timer stack another complete
    // refresh over the active one. Besides wasting work, overlapping renders
    // can replace freshly opened controls with an older response.
    if (refreshInFlight) return refreshInFlight;
    document.body.setAttribute("aria-busy", "true");
    refreshInFlight = (async () => {
      // Resolve project selection first. The remaining reads run in two bounded
      // dependency waves; serially awaiting ~25 endpoints made a normal poll
      // take their summed latency (often 3-8 seconds).
      try {
        await refreshProjects();
      } catch (err) {
        console.warn(err);
      }
      const foundationJobs = [
        refreshIdentity,
        refreshOverview,
        refreshWorkers,
        refreshResources,
        refreshTasks,
        refreshProviders,
        refreshModels,
        refreshWorktrees,
        refreshOperations,
        refreshAppLifecycle,
        refreshRepositoryHealth,
        refreshTerminalHistory,
      ];
      let results = await Promise.allSettled(foundationJobs.map((job) => job()));
      results.filter((result) => result.status === "rejected").forEach((result) => console.warn(result.reason));

      // These projections consume foundational model/worktree/task state. They
      // form a second concurrent wave so controls are never rendered against a
      // half-refreshed worker list while still avoiding serial endpoint latency.
      const projectionJobs = [
        refreshRunbooks,
        refreshQuickStart,
        refreshFlow,
        refreshWorkflow,
        refreshTests,
        refreshAttention,
        refreshEvents,
        refreshRunEvidence,
        refreshRoadmap,
        refreshPriorityMatrix,
        refreshManagerRoute,
        refreshUsageTelemetry,
        refreshTelemetry,
        refreshUsageRouting,
      ];
      results = await Promise.allSettled(projectionJobs.map((job) => job()));
      results.filter((result) => result.status === "rejected").forEach((result) => console.warn(result.reason));
      await refreshCheckpointAge();
      applySearch(document.getElementById("global-search")?.value || "");
    })().finally(() => {
      document.body.removeAttribute("aria-busy");
      document.documentElement.dataset.initialRefreshComplete = "true";
      refreshInFlight = null;
    });
    return refreshInFlight;
  }

  // --------------------------------------------------------------------- confirmation sheet

  function confirmAndRun(message, action) {
    const backdrop = document.getElementById("confirm-backdrop");
    const sheet = document.getElementById("confirm-sheet");
    document.getElementById("confirm-body").textContent = message;
    backdrop.hidden = false;
    sheet.hidden = false;

    function cleanup() {
      backdrop.hidden = true;
      sheet.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      backdrop.removeEventListener("click", onCancel);
    }
    function onOk() {
      cleanup();
      action();
    }
    function onCancel() {
      cleanup();
    }
    const okBtn = document.getElementById("confirm-ok");
    const cancelBtn = document.getElementById("confirm-cancel");
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    backdrop.addEventListener("click", onCancel);
    okBtn.focus();
  }

  // --------------------------------------------------------------------- sticky start/pause/resume/stop

  // ENG-AGENT-02-S7 (issue #97): Start/Pause/Resume/Stop used to always show
  // as four equally-prominent buttons regardless of real task state.
  // State-aware instead: IDLE shows only Start, RUNNING shows Pause+Stop,
  // PAUSED shows Resume+Stop — matching the actual verbs that can do
  // anything right now. Stop stays visually differentiated (.ctl-danger) in
  // every state it appears.
  function updateStickyControlsState() {
    const hasRunning = currentTasks.some((t) => t.state === "RUNNING");
    const hasPaused = currentTasks.some((t) => t.state === "PAUSED");
    const startBtn = document.getElementById("ctl-start");
    const pauseBtn = document.getElementById("ctl-pause");
    const resumeBtn = document.getElementById("ctl-resume");
    const stopBtn = document.getElementById("ctl-stop");
    if (!startBtn || !pauseBtn || !resumeBtn || !stopBtn) return;
    // Independent, not mutually exclusive (Grok Build review, issue #97):
    // RUNNING and PAUSED tasks can coexist, and hiding Resume whenever
    // anything was RUNNING made a mixed state's paused work unreachable
    // from this bar (per-task Resume on the Tasks view still worked, but
    // this bar exists precisely so that isn't the only path).
    startBtn.hidden = hasRunning || hasPaused;
    pauseBtn.hidden = !hasRunning;
    resumeBtn.hidden = !hasPaused;
    stopBtn.hidden = !(hasRunning || hasPaused);
    syncChromeInsets();
  }

  function initStickyControls() {
    document.getElementById("ctl-start").addEventListener("click", () => {
      const runnable = currentTasks.find((t) => t.state === "PENDING" || t.state === "QUEUED");
      if (!runnable) {
        postCommand("start", {}).then(refreshAll);
        return;
      }
      postCommand("start", { task_id: runnable.id }).then(refreshAll);
    });
    document.getElementById("ctl-pause").addEventListener("click", () => {
      const running = currentTasks.filter((t) => t.state === "RUNNING" || t.state === "QUEUED");
      Promise.all(running.map((t) => postCommand("pause", { task_id: t.id }))).then(refreshAll);
    });
    document.getElementById("ctl-resume").addEventListener("click", () => {
      const paused = currentTasks.filter((t) => t.state === "PAUSED");
      Promise.all(paused.map((t) => postCommand("resume", { task_id: t.id }))).then(refreshAll);
    });
    document.getElementById("ctl-stop").addEventListener("click", () => {
      confirmAndRun("Stop scheduling new tasks after current work finishes?", () =>
        postCommand("stop_after_current", {}).then(refreshAll)
      );
    });
  }

  function initMaxWriters() {
    const input = document.getElementById("max-writers-input");
    const feedback = document.getElementById("max-writers-feedback");
    document.getElementById("max-writers-save").addEventListener("click", async () => {
      const n = Number(input.value);
      const min = Number(input.min || 1);
      const max = Number(input.max || 8);
      if (!Number.isInteger(n) || n < min || n > max) {
        feedback.textContent = `Enter a whole number from ${min} to ${max}.`;
        input.setAttribute("aria-invalid", "true");
        return;
      }
      input.removeAttribute("aria-invalid");
      feedback.textContent = "Applying…";
      const result = await postCommand("set_max_writers", { count: n });
      const ok = result && result.ok && (!result.body || result.body.ok !== false);
      feedback.textContent = ok ? `Applied — up to ${n} write worker${n === 1 ? "" : "s"}` : `Not applied${result && result.body && result.body.detail ? `: ${result.body.detail}` : ""}`;
      refreshAll();
    });
  }

  function initRanges() {
    const sel = document.getElementById("workflow-range");
    sel.addEventListener("change", () => {
      state.activityHours = Number(sel.value) || 24;
      renderActivity(state.events, state.activityHours);
    });
    window.addEventListener("resize", () => {
      renderOverviewPipeline(state.workflow);
      drawFlowEdges();
    });
  }

  function initWorkflowDetail() {
    const sheet = document.getElementById("workflow-detail-sheet");
    const backdrop = document.getElementById("workflow-detail-backdrop");
    const close = () => {
      sheet.hidden = true;
      backdrop.hidden = true;
      stopAgentActivityPoll();
    };
    document.getElementById("workflow-detail-close").addEventListener("click", close);
    backdrop.addEventListener("click", close);
    // OCTAREL-UI-01: keyboard accessible -- Escape closes the Agent Activity
    // viewer (and any other content this shared sheet is showing) exactly
    // like the Close button, only while the sheet is actually open.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !sheet.hidden) close();
    });
  }

  function initOperationsControls() {
    document.getElementById("worktrees-cleanup-preview")?.addEventListener("click", () => previewWorktreeCleanup(false));
    document.getElementById("worktrees-cleanup")?.addEventListener("click", () =>
      confirmAndRun("Remove only the worktrees listed as FINISHED_CLEAN in the current preview? Git worktree removal will be used; dirty, active, manual, unknown, and main checkouts stay protected.", () => previewWorktreeCleanup(true))
    );
    document.getElementById("worktrees-refresh")?.addEventListener("click", () => {
      const path = state.worktrees[0]?.path;
      if (!path) return refreshWorktrees();
      return postCommand("git_operation", { action: "refresh", path }).then(() => Promise.all([refreshWorktrees(), refreshOperations(), refreshRepositoryHealth()]));
    });
    document.getElementById("history-search")?.addEventListener("input", refreshEvents);
    document.getElementById("history-category")?.addEventListener("change", refreshEvents);
    document.getElementById("terminal-history-limit")?.addEventListener("change", refreshTerminalHistory);
    document.getElementById("terminal-history-search")?.addEventListener("input", refreshTerminalHistory);
    document.getElementById("terminal-history-clear")?.addEventListener("click", () => confirmAndRun("Clear local Control Center terminal command metadata?", () => postCommand("terminal_history_clear", { confirm: true }).then(refreshTerminalHistory)));
    document.getElementById("app-start")?.addEventListener("click", () => postJSON("/api/app-lifecycle/start", {}).then(refreshAppLifecycle));
    document.getElementById("app-stop")?.addEventListener("click", () => confirmAndRun("Stop only the OctaScene development process started by Control Center?", () => postJSON("/api/app-lifecycle/stop", { confirm: true }).then(refreshAppLifecycle)));
    document.getElementById("app-restart")?.addEventListener("click", () => confirmAndRun("Restart the managed local OctaScene development process?", () => postJSON("/api/app-lifecycle/restart", { confirm: true }).then(refreshAppLifecycle)));
  }

  // --------------------------------------------------------------------- steering

  function renderSteeringFeedEntry(text, proposal) {
    const feed = document.getElementById("steering-feed");
    const li = el("li", { class: proposal.status === "PARSED" ? "level-info" : "level-warning" });
    li.textContent = `[${proposal.ts || new Date().toISOString()}] "${text}" → ${
      proposal.status === "PARSED" ? `${proposal.verb}(${JSON.stringify(proposal.args)})` : "unrecognized"
    }`;
    feed.prepend(li);
  }

  function bindSteeringForm(form, input, previewBox) {
    function clearPreview() {
      previewBox.hidden = true;
      previewBox.innerHTML = "";
      previewBox.classList.remove("destructive");
    }

    function showUnrecognized(proposal) {
      previewBox.hidden = false;
      previewBox.classList.add("destructive");
      const ai = proposal.ai_escalation || {};
      previewBox.innerHTML = "";
      previewBox.appendChild(el("strong", { text: "Not recognized." }));
      previewBox.appendChild(
        el("p", {
          text: ai.available
            ? `An AI-assisted parse via ${ai.worker} is available but was not used automatically.`
            : `No AI-assisted parse route is currently configured/authorized (${ai.reason || "unavailable"}).`,
        })
      );
    }

    function showProposal(text, proposal) {
      previewBox.hidden = false;
      previewBox.classList.toggle("destructive", !!proposal.destructive);
      previewBox.innerHTML = "";
      // ENG-AGENT-02-S7 (issue #97): a development-intent phrase must show the
      // real resolved run, not just a bare verb name, before anything starts.
      if (proposal.verb === "quickstart_start" && proposal.quickstart_option) {
        previewBox.appendChild(el("strong", { text: proposal.preview }));
        previewBox.appendChild(preparedRunSummaryEl(proposal.quickstart_option));
        const actions = el("div", { class: "actions" });
        const option = proposal.quickstart_option;
        const runBtn = el("button", { text: "Start Development" });
        runBtn.disabled = !option.ready;
        runBtn.addEventListener("click", () => executeProposal(text, proposal, input, clearPreview));
        actions.appendChild(runBtn);
        if (!option.ready) actions.appendChild(el("span", { class: "hint", text: option.unavailable_reason }));
        previewBox.appendChild(actions);
        return;
      }
      previewBox.appendChild(el("strong", { text: proposal.preview }));
      const actions = el("div", { class: "actions" });
      const runBtn = el("button", { text: proposal.destructive ? "Review & confirm" : "Run" });
      runBtn.addEventListener("click", () => {
        if (proposal.destructive) {
          confirmAndRun(proposal.preview, () => executeProposal(text, proposal, input, clearPreview));
        } else {
          executeProposal(text, proposal, input, clearPreview);
        }
      });
      actions.appendChild(runBtn);
      previewBox.appendChild(actions);
    }

    form.addEventListener("submit", (evt) => {
      evt.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      /* OCTAREL-UI-05 (issue #24): the Manager endpoint tries the deterministic
         parser first, so a slash command still costs zero AI calls, and only
         routes genuinely unrecognized text to an eligible interpreter. It
         always returns a proposal — execution still goes through
         /api/steering/execute and its server-enforced confirmation gate. */
      appendManagerMessage("you", text);
      postJSON("/api/manager/message", { text })
        .then(({ ok, status, body }) => {
          /* postJSON does not throw on an HTTP error, so without this a 4xx/5xx
             body would be rendered as "not recognized" -- telling the operator
             their phrasing was the problem when the request actually failed. */
          if (!ok || !body) {
            appendManagerError(`The Manager could not be reached (HTTP ${status}).`);
            return;
          }
          appendManagerResponse(text, body);
          if (body.status === "PARSED") showProposal(text, body);
          else {
            showUnrecognized(body);
            renderSteeringFeedEntry(text, body);
          }
        })
        .catch(() => appendManagerError("The Manager could not be reached."));
    });
  }

  // ------------------------------------------------------------- manager chat

  /* The conversation thread. Each Manager turn is an execution card that names
     the route that produced it — deterministic, or the actual
     worker/provider/model that interpreted the sentence — so a fallback is
     never an unexplained change of behaviour. */

  function routeLabel(route) {
    if (!route) return "unknown route";
    if (route.kind === "deterministic") return "deterministic · no model call";
    const parts = [route.worker, route.provider, route.model].filter(Boolean);
    return parts.length ? parts.join(" · ") : "no eligible interpreter";
  }

  function appendManagerMessage(who, text) {
    const thread = document.getElementById("manager-thread");
    if (!thread) return;
    thread.appendChild(
      el("li", { class: `manager-msg manager-msg-${who}` }, [
        el("span", { class: "manager-who", text: who === "you" ? "You" : "Manager" }),
        el("p", { class: "manager-text", text }),
      ]),
    );
    thread.scrollTop = thread.scrollHeight;
  }

  function appendManagerError(message) {
    const thread = document.getElementById("manager-thread");
    if (!thread) return;
    thread.appendChild(
      el("li", { class: "manager-msg manager-msg-manager is-error" }, [
        el("span", { class: "manager-who", text: "Manager" }),
        el("p", { class: "manager-text", text: message }),
        el("p", { class: "manager-route-line", text: "no interpretation was attempted" }),
      ]),
    );
    thread.scrollTop = thread.scrollHeight;
  }

  function appendManagerResponse(text, body) {
    const thread = document.getElementById("manager-thread");
    if (!thread) return;
    const route = body.route || {};
    const parsed = body.status === "PARSED";

    const children = [
      el("span", { class: "manager-who", text: "Manager" }),
      el("p", { class: "manager-text", text: body.preview || body.reason || "No action matched." }),
    ];

    if (parsed) {
      children.push(
        el("p", { class: "manager-plan" }, [
          el("span", { class: "manager-verb", text: body.verb }),
          el("code", { text: JSON.stringify(body.args || {}) }),
        ]),
      );
    }
    if (body.destructive) {
      // Stated in the thread as well as the preview: natural language can
      // propose a destructive action, never pre-authorize one.
      children.push(
        el("p", { class: "manager-destructive", text: "Destructive — requires explicit confirmation before it runs." }),
      );
    }
    children.push(el("p", { class: "manager-route-line", text: `via ${routeLabel(route)}` }));

    thread.appendChild(
      el("li", { class: `manager-msg manager-msg-manager${parsed ? "" : " is-unmatched"}` }, children),
    );
    thread.scrollTop = thread.scrollHeight;
  }

  async function refreshManagerRoute() {
    const label = document.getElementById("manager-route");
    if (!label || !isViewActive("view-steering")) return;
    try {
      const route = await getJSON("/api/manager/route");
      if (route.eligible) {
        label.textContent = `Interpreter: ${routeLabel(route)}`;
        label.classList.remove("is-unavailable");
      } else {
        // Honest about being unavailable rather than silently deterministic-only.
        label.textContent = `No interpreter available — ${route.reason || "unknown reason"}`;
        label.classList.add("is-unavailable");
      }
    } catch (err) {
      label.textContent = "Interpreter route could not be read";
      label.classList.add("is-unavailable");
    }
  }

  function executeProposal(text, proposal, input, clearPreview) {
    postJSON("/api/steering/execute", {
      verb: proposal.verb,
      args: proposal.args,
      confirm: true,
      raw_text: text,
    }).then(() => {
      renderSteeringFeedEntry(text, proposal);
      clearPreview();
      input.value = "";
      refreshAll();
    });
  }

  /* xterm paints its own canvas and cannot read CSS custom properties, so the
     themed console tokens are resolved here and handed to it. Keeps the
     terminal on the same warm near-black material as the surrounding shell
     instead of the cool slate it used to hard-code. */
  function terminalTheme() {
    const s = getComputedStyle(document.documentElement);
    const read = (name, fallback) => (s.getPropertyValue(name).trim() || fallback);
    const accent = read("--accent", "#ed7a12");
    return {
      background: read("--console-bg", "#131210"),
      foreground: read("--console-text", "#f0ebe1"),
      cursor: accent,
      selectionBackground: "rgba(237, 122, 18, 0.32)",
    };
  }

  function initTerminal() {
    const host = document.getElementById("terminal");
    const statePill = document.getElementById("terminal-state");
    if (!host || !window.Terminal) return;
    const terminal = new window.Terminal({
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      fontSize: 13,
      fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace',
      theme: terminalTheme(),
    });
    terminal.open(host);
    onThemeChange(() => { terminal.options.theme = terminalTheme(); });
    let socket = null;
    const frame = document.getElementById("terminal-frame");
    const overlayTitle = document.getElementById("terminal-overlay-title");
    const overlayText = document.getElementById("terminal-overlay-text");
    const stateDetail = document.getElementById("terminal-state-detail");
    const reconnectBtn = document.getElementById("terminal-reconnect");
    function setState(label, css) {
      statePill.textContent = label;
      statePill.className = `status-pill ${css}`;
      const kind = label === "Connected" ? "connected" : label === "Connecting" ? "connecting" : label === "Connection error" ? "error" : "disconnected";
      if (frame) frame.dataset.connection = kind;
      if (overlayTitle) overlayTitle.textContent = { connecting: "Connecting…", error: "Connection error", disconnected: "Terminal disconnected" }[kind] || "";
      if (overlayText) overlayText.textContent = { connecting: "Opening a shell in the project worktree.", error: "The terminal could not be reached. Check that the Control Center is running, then reconnect.", disconnected: "Reconnect to open a new shell session." }[kind] || "";
      if (stateDetail) stateDetail.textContent = kind === "connected" ? `since ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : "";
      // The primary action is the one that fixes the current problem.
      if (reconnectBtn) reconnectBtn.classList.toggle("btn-primary", kind !== "connected");
    }
    function resize() {
      const cols = Math.max(20, Math.floor(host.clientWidth / 8.2));
      const rows = Math.max(8, Math.floor(host.clientHeight / 18));
      terminal.resize(cols, rows);
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows }));
    }
    function connect() {
      if (socket) socket.close();
      setState("Connecting", "st-running");
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const sock = new WebSocket(`${scheme}://${location.host}/api/terminal/ws`);
      socket = sock;
      // A replaced socket must never overwrite the state of the current one.
      const current = () => sock === socket;
      sock.addEventListener("open", () => { if (current()) resize(); });
      sock.addEventListener("message", (event) => {
        if (!current()) return;
        const message = JSON.parse(event.data);
        if (message.type === "output") terminal.write(message.data);
        if (message.type === "ready") {
          setState("Connected", "st-available");
          const meta = document.getElementById("terminal-meta");
          meta.innerHTML = "";
          [["Repository", message.repository], ["Branch", message.branch], ["Virtual environment", message.virtual_environment], ["Shell", message.shell]].forEach(([key, value]) => {
            meta.appendChild(el("div", {}, [el("dt", { text: key }), el("dd", { text: value || "Unavailable" })]));
          });
          terminal.focus();
        }
      });
      sock.addEventListener("close", () => { if (current()) setState("Disconnected", "st-paused"); });
      sock.addEventListener("error", () => { if (current()) setState("Connection error", "st-blocked"); });
    }
    terminal.onData((data) => {
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data }));
    });
    new ResizeObserver(resize).observe(host);
    connectTerminalView = connect;
    document.getElementById("terminal-reconnect")?.addEventListener("click", connect);
    document.getElementById("terminal-clear")?.addEventListener("click", () => terminal.clear());
    document.getElementById("terminal-copy")?.addEventListener("click", async () => {
      const selected = terminal.getSelection();
      if (selected) await navigator.clipboard.writeText(selected);
    });
    getJSON("/api/terminal/info").then((message) => {
      const meta = document.getElementById("terminal-meta");
      meta.innerHTML = "";
      [["Repository", message.repository], ["Branch", message.branch], ["Virtual environment", message.virtual_environment], ["Shell", message.shell]].forEach(([key, value]) => {
        meta.appendChild(el("div", {}, [el("dt", { text: key }), el("dd", { text: value || "Unavailable" })]));
      });
    });
  }

  function initSteering() {
    bindSteeringForm(
      document.getElementById("steering-form"),
      document.getElementById("steering-input"),
      document.getElementById("steering-preview")
    );
    bindSteeringForm(
      document.getElementById("overview-command-form"),
      document.getElementById("overview-command-input"),
      document.getElementById("overview-command-preview")
    );
  }

  // --------------------------------------------------------------------- boot

  initTheme();
  initRail();
  initPriorityMatrix();
  initPalette();
  initNav();
  initProjectSwitcher();
  initMoreSheet();
  initMobileChrome();
  initSystemMenu();
  initAttentionBell();
  initStickyControlsPlacement();
  initChromeInsets();
  initStickyControls();
  updateStickyControlsState();
  initNotifications();
  initSessionClock();
  initSearch();
  initSteering();
  initMaxWriters();
  initRanges();
  initWorkflowDetail();
  initOperationsControls();
  initReportSheet();
  initRunbookFormSubmit();
  initRunOvernightButton();
  initPreparedRunActions();
  initOvernightActions();
  initOverviewContinueAction();
  initTerminal();
  document.getElementById("usage-refresh-btn")?.addEventListener("click", () => refreshUsage(true));
  refreshAll();
  refreshUsage();
  setInterval(refreshAll, POLL_MS);
})();
