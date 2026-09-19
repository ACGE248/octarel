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
      else if (BLOCKED_STATES.includes(t.state)) counts.blocked += 1;
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
    pairs.forEach(([label, value]) => {
      container.appendChild(el("div", { class: "k", text: label }));
      container.appendChild(el("div", { text: String(value) }));
    });
  }

  function isDesktop() {
    return window.matchMedia(DESKTOP_MQ).matches;
  }

  // --------------------------------------------------------------------- theme

  function applyTheme(value) {
    const root = document.documentElement;
    if (value === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", value);
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
        try {
          localStorage.setItem(THEME_KEY, select.value);
        } catch (err) {
          /* ignore */
        }
      });
    }
  }

  // --------------------------------------------------------------------- view switching

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
    closeSidebar();
    closeSystemMenu();
    closeAttention();
    applySearch(document.getElementById("global-search")?.value || "");
    if (viewId === "view-flow") requestAnimationFrame(() => drawFlowEdges());
    if (viewId === "view-terminal" && connectTerminalView) connectTerminalView();
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
    document.addEventListener("keydown", (evt) => {
      if ((evt.metaKey || evt.ctrlKey) && evt.key.toLowerCase() === "k") {
        evt.preventDefault();
        input.focus();
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

  function renderStatusRings(counts) {
    const total = Math.max(counts.total, 1);
    const map = [
      ["ring-running", "status-running", counts.running, "rgb(var(--ok-rgb))"],
      ["ring-queued", "status-queued", counts.queued, "rgb(var(--info-rgb))"],
      ["ring-completed", "status-completed", counts.completed, "rgb(var(--purple-rgb))"],
      ["ring-blocked", "status-blocked", counts.blocked, "rgb(var(--err-rgb))"],
    ];
    const colors = {
      "ring-running": "#12b76a",
      "ring-queued": "#2e90fa",
      "ring-completed": "#9b6bff",
      "ring-blocked": "#f04438",
    };
    map.forEach(([ringId, countId, value]) => {
      const countEl = document.getElementById(countId);
      if (countEl) countEl.textContent = String(value);
      const ring = document.getElementById(ringId);
      if (ring) ring.innerHTML = ringSVG(colors[ringId], value / total);
    });
  }

  function renderDonut(counts) {
    const root = document.getElementById("donut-chart");
    if (!root) return;
    const slices = [
      { key: "Running", value: counts.running, color: "#12b76a" },
      { key: "Queued", value: counts.queued, color: "#2e90fa" },
      { key: "Completed", value: counts.completed, color: "#9b6bff" },
      { key: "Blocked", value: counts.blocked, color: "#f04438" },
    ];
    const total = slices.reduce((sum, s) => sum + s.value, 0);
    const svg = svgEl("svg", { viewBox: "0 0 42 42", width: "140", height: "140", role: "img", "aria-label": "Task status donut" });
    const r = 15.5;
    const c = 2 * Math.PI * r;
    svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r, fill: "none", stroke: "rgba(148,163,184,0.18)", "stroke-width": "4" }));
    if (total === 0) {
      svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r, fill: "none", stroke: "rgba(148,163,184,0.28)", "stroke-width": "4" }));
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
    const task = workflow && workflow.task;
    const stages = (workflow && workflow.stages) || [];
    const mobile = window.matchMedia("(max-width: 640px)").matches;
    const desktop = window.matchMedia("(min-width: 1100px)").matches;
    root.className = `pipeline workflow-runtime ${mobile ? "workflow-mobile" : desktop ? "workflow-desktop" : "workflow-tablet"}`;
    root.style.setProperty("--workflow-stage-count", String(Math.max(1, stages.length)));
    root.innerHTML = "";
    if (!task) {
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
    function statusClass(value) {
      if (COMPLETED_STATES.includes(value)) return "complete";
      if (value === "RUNNING") return "running";
      if (["FAILED", "BLOCKED", "CANCELLED"].includes(value)) return "failed";
      return "pending";
    }
    function statusPill(value) {
      return el("span", { class: `workflow-status ${statusClass(value)}`, text: statusText(value) });
    }
    function progressBar(item) {
      const known = Number.isFinite(item.progress);
      const track = el("span", { class: `workflow-progress ${!known && item.state === "RUNNING" ? "indeterminate" : ""}` });
      if (known) track.appendChild(el("i", { style: `width:${Math.max(0, Math.min(100, item.progress))}%` }));
      return el("div", { class: "workflow-progress-row" }, [track, el("span", { text: known ? `${item.progress}%` : "—" })]);
    }
    function openDetail(item) {
      const sheet = document.getElementById("workflow-detail-sheet");
      const backdrop = document.getElementById("workflow-detail-backdrop");
      document.getElementById("workflow-detail-title").textContent = item.label || item.title || item.id;
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
      sheet.hidden = false;
      backdrop.hidden = false;
      document.getElementById("workflow-detail-close").focus();
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
        el("p", { class: "workflow-current", text: item.current_action || item.current_file || (item.state === "RUNNING" ? "Activity details not reported" : "Waiting for activity") }),
        progressBar(item),
        el("div", { class: "workflow-card-foot" }, [statusPill(item.state), item.subagents && item.subagents.length ? el("span", { text: `${item.subagents.length} sub-agent${item.subagents.length === 1 ? "" : "s"}` }) : null]),
      ]);
      card.addEventListener("click", () => openDetail(item));
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
      const signature = enabled.map((p) => `${p.project_id} ${projectLabel(p)}`).join("");
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
    if (!data.available) {
      renderKV("resources-body", [["psutil", data.note || "unavailable"]]);
      return;
    }
    renderKV("resources-body", [
      ["CPU", data.cpu_percent == null ? "—" : `${data.cpu_percent}%`],
      ["Memory", data.memory_percent == null ? "—" : `${data.memory_percent}%`],
      ["Disk", data.disk_percent == null ? "—" : `${data.disk_percent}%`],
      ["Load (1m)", data.load_average && data.load_average["1m"] != null ? data.load_average["1m"].toFixed(2) : "unavailable"],
    ]);
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

  function renderTaskCards(tasks) {
    const root = document.getElementById("tasks-cards");
    root.innerHTML = "";
    if (!tasks.length) {
      root.appendChild(el("p", { class: "hint", text: "No active tasks." }));
      return;
    }
    tasks.forEach((task) => {
      const card = el("article", {
        class: "entity-card",
        "data-searchable": "true",
        "data-search": `${task.id} ${task.task_ref} ${task.role} ${task.worker} ${task.state}`,
      });
      card.append(
        el("header", {}, [
          el("div", {}, [
            el("h3", { text: task.task_ref || task.id }),
            el("div", { class: "entity-meta", text: `${task.id} · ${task.role} · ${displayName(task.worker)} (${task.worker})` }),
          ]),
          el("span", { class: `status-pill ${statusClass(task.state)}`, text: task.state }),
        ]),
        el("div", { class: "entity-meta", text: relativeTime(task.updated_at || task.created_at) }),
        el("div", {
          class: "entity-meta",
          text: `Wave ${task.dependency_wave ?? "—"} · ${task.admission_state || "PENDING"}${task.admission_reason ? ` · ${task.admission_reason}` : ""}`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Alternatives: ${task.selection_alternatives?.length ? task.selection_alternatives.map(displayName).join(", ") : "none eligible/reported"}`,
        }),
        taskActions(task)
      );
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

  function renderProviderCards(providers) {
    const root = document.getElementById("providers-cards");
    root.innerHTML = "";
    providers.forEach((p) => {
      const card = el("article", {
        class: "entity-card",
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
      const titleRow = el("div", { class: "entity-title-row" }, [
        identityBadges(p.execution_system, p.provider),
        el("h3", { text: p.display_name || displayName(p.name) }),
      ]);
      const displayState = p.reason && p.reason !== "AVAILABLE" ? p.reason : (p.display_state || p.state || "UNKNOWN");
      card.append(
        el("header", {}, [
          el("div", {}, [
            titleRow,
            el("div", { class: "entity-meta", text: `${p.provider} · ${p.execution_system} · ${p.route_type}` }),
            el("div", { class: "entity-meta machine-id", text: `ID: ${p.name}` }),
          ]),
          el("span", { class: `status-pill ${statusClass(displayState)}`, text: displayState }),
        ]),
        el("div", {
          class: "entity-meta",
          text: `${p.description} Registered model: ${(p.models || []).join(", ") || "none"}. Capability: ${p.capability}.`,
        }),
        el("div", {
          class: "entity-meta",
          text: `Configured: ${p.configured ? "Yes" : "No"} · Local availability: ${displayState} · ${p.running_task_count || 0} running task(s) · Quota: ${p.quota_visibility} ${p.spend_safety}`,
        }),
        actions
      );
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

  function renderWorktreeCards(worktrees, checkpoints) {
    const managedRoot = document.getElementById("worktrees-managed");
    const discoveredRoot = document.getElementById("worktrees-discovered");
    if (!managedRoot || !discoveredRoot) return;
    managedRoot.innerHTML = "";
    discoveredRoot.innerHTML = "";
    worktrees.forEach((w) => {
      const card = el("article", { class: "entity-card worktree-card", "data-searchable": "true", "data-search": `${w.path} ${w.branch || ""} ${w.worker || ""} ${w.provider || ""}` });
      card.append(
        el("header", {}, [
          el("div", { class: "truncate-wrap" }, [el("h3", { text: w.display_name || w.branch || "UNKNOWN", title: w.branch || w.path }), el("div", { class: "entity-meta truncate", text: w.path, title: w.path })]),
          el("span", { class: `status-pill ${statusClass(w.classification)}`, text: w.classification || "UNKNOWN" }),
        ]),
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
        ])
      );
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
      card.appendChild(actions);
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
      ["Tester", option.proposed_tester || "Not configured"],
      ["Reviewer", option.proposed_reviewer || "Not configured"],
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

  function initOverviewContinueAction() {
    const btn = document.getElementById("overview-continue-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      if (overviewActiveRun) {
        showView("view-runs");
        document.querySelector(`[data-runbook-id="${overviewActiveRun.id}"]`)?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
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

  function renderRunbookCards(runbooks) {
    const root = document.getElementById("runbooks-cards");
    root.innerHTML = "";
    runbooks.forEach((r) => {
      const card = el("article", {
        class: "entity-card",
        "data-runbook-id": r.id,
        "data-searchable": "true",
        "data-search": `${r.name} ${r.preset} ${r.branch} ${r.status}`,
      });
      card.append(
        el("header", {}, [
          el("div", {}, [el("h3", { text: r.name }), el("div", { class: "entity-meta", text: `${r.preset} · ${r.branch}` })]),
          el("span", { class: `status-pill ${statusClass(r.status)}`, text: r.status }),
        ]),
        el("div", { class: "entity-meta", text: r.objective }),
        el("div", {
          class: "entity-meta",
          text: `worker ${displayName(r.parent_worker)} (${r.parent_worker}) · permission ${r.permission_profile} · ${
            r.status === "RUNNING" ? formatRemaining(r.remaining_seconds) : `budget ${r.max_duration_minutes}m`
          }`,
        })
      );
      if (r.phases && r.phases.length) {
        const phaseWrap = el("div", { class: "runbook-phases" });
        r.phases.forEach((phase) => phaseWrap.appendChild(el("span", { class: "phase-chip", text: phase })));
        card.appendChild(phaseWrap);
      }
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
      card.appendChild(actions);
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
    renderKV("tests-body", [
      ["Authority", data.authority],
      ["Status", gate.status === "IDLE" ? "IDLE" : gate.ready ? "PASS" : "NOT READY"],
      ["Reason", gate.reason || "-"],
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
  }

  async function refreshAttention() {
    const data = await getJSON("/api/attention");
    const runbooksNeedingAttention = data.runbooks || [];
    renderKV("attention-body", [
      ["Tasks needing attention", data.tasks.length],
      ["Providers needing attention", data.providers.length],
      ["Runbooks needing attention", runbooksNeedingAttention.length],
    ]);
    const list = document.getElementById("attention-list");
    list.innerHTML = "";
    data.tasks.forEach((t) => list.appendChild(el("li", { text: `Task ${t.id}: ${t.state} (${t.task_ref}/${t.role})` })));
    data.providers.forEach((p) => list.appendChild(el("li", { text: `Provider ${p.display_name || displayName(p.name)}: ${p.display_state || p.state}` })));
    runbooksNeedingAttention.forEach((r) => list.appendChild(el("li", { text: `Runbook ${r.name}: ${r.status}` })));
    const attentionCount = data.tasks.length + data.providers.length + runbooksNeedingAttention.length;
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
    document.getElementById("app-start").disabled = data.status === "RUNNING" || data.status === "UNKNOWN";
    document.getElementById("app-stop").disabled = data.status !== "RUNNING";
    document.getElementById("app-restart").disabled = data.status !== "RUNNING";
  }

  async function refreshRepositoryHealth() {
    const data = await getJSON("/api/repository-health");
    const main = data.main || {};
    renderKV("repository-health-body", [["Main", main.remote_head_sha ? main.remote_head_sha.slice(0, 8) : "UNKNOWN"], [main.remote_time_label || "Remote head", main.remote_head_commit_at || "Unavailable"], ["Last observed", data.observed_at || "—"], ["Open task branches", data.open_task_branches ?? "—"], ["Unpushed worktrees", data.unpushed_worktrees ?? "—"], ["Dirty worktrees", main.dirty_worktree_count ?? "—"], ["Active workers", main.active_worker_count ?? "—"]]);
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
        el("tr", {}, [el("td", { text: p.display_name || displayName(p.name) }), el("td", { text: p.execution_route }), el("td", { text: p.display_state || p.state })])
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

  async function refreshAll() {
    const jobs = [
      // ENG-CP-03: first, so the selector and topbar badge always agree with
      // the project the panels below are about to be populated from.
      refreshProjects,
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
      refreshRunbooks,
      refreshQuickStart,
      refreshFlow,
      refreshWorkflow,
      refreshTests,
      refreshAttention,
      refreshEvents,
      refreshRunEvidence,
      refreshRoadmap,
      refreshTelemetry,
      refreshUsageRouting,
      refreshCheckpointAge,
    ];
    for (const job of jobs) {
      try {
        await job();
      } catch (err) {
        console.warn(err);
      }
    }
    applySearch(document.getElementById("global-search")?.value || "");
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
    document.getElementById("max-writers-save").addEventListener("click", () => {
      const n = Number(document.getElementById("max-writers-input").value);
      if (!Number.isFinite(n)) return;
      postCommand("set_max_writers", { count: n }).then(refreshAll);
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
    };
    document.getElementById("workflow-detail-close").addEventListener("click", close);
    backdrop.addEventListener("click", close);
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
      postJSON("/api/steering/parse", { text }).then(({ body }) => {
        if (!body) return;
        if (body.status === "PARSED") showProposal(text, body);
        else {
          showUnrecognized(body);
          renderSteeringFeedEntry(text, body);
        }
      });
    });
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

  function initTerminal() {
    const host = document.getElementById("terminal");
    const statePill = document.getElementById("terminal-state");
    if (!host || !window.Terminal) return;
    const terminal = new window.Terminal({
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      fontSize: 13,
      fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
      theme: { background: "#090d18", foreground: "#e6edf7", cursor: "#9b8cff", selectionBackground: "#5b4fd966" },
    });
    terminal.open(host);
    let socket = null;
    function setState(label, css) {
      statePill.textContent = label;
      statePill.className = `status-pill ${css}`;
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
      socket = new WebSocket(`${scheme}://${location.host}/api/terminal/ws`);
      socket.addEventListener("open", resize);
      socket.addEventListener("message", (event) => {
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
      socket.addEventListener("close", () => setState("Disconnected", "st-paused"));
      socket.addEventListener("error", () => setState("Connection error", "st-blocked"));
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
  initNav();
  initProjectSwitcher();
  initMoreSheet();
  initMobileChrome();
  initSystemMenu();
  initAttentionBell();
  initStickyControlsPlacement();
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
  initOverviewContinueAction();
  initTerminal();
  document.getElementById("usage-refresh-btn")?.addEventListener("click", () => refreshUsage(true));
  refreshAll();
  refreshUsage();
  setInterval(refreshAll, POLL_MS);
})();
