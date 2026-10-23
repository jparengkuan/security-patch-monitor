/* Security Patch Monitor – front end.
 *
 * Data flows one way: the API (or an embedded snapshot for static exports) fills `state`,
 * and `render()` derives the whole UI from it. Event handlers only change `state`.
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- constants
  const LOCALE = "en-GB";
  const POLL_MS = 5000;
  const SIDE_DAYS = 60;
  const MONTH_CHIPS = 3;

  const PRODUCTS = { drupal: "Drupal", php: "PHP", nextjs: "Next.js", node: "Node.js" };
  const KINDS = { upcoming: "Announced", expected: "Expected", eol: "End of support", advisory: "Advisory" };
  const SEVERITIES = ["critical", "high", "moderate", "low", "info"];
  const SECTIONS = [
    ["upcoming", "Announced security releases", "Officially pre-announced by Drupal, PHP or Node.js."],
    ["expected", "Expected (schedule / estimate)", "Fixed release windows and estimated release dates. Not confirmed."],
    ["eol", "End of security support", "After this date there are no more security patches for that version."],
    ["advisory", "Recently published advisories", "Security fixes that have already been released."],
  ];
  const FILTER_OPTIONS = {
    products: Object.entries(PRODUCTS),
    sevs: SEVERITIES.map((s) => [s, s]),
    kinds: Object.entries(KINDS),
  };
  const FILTER_LABELS = { products: "Product", sevs: "Severity", kinds: "Type" };

  // ---------------------------------------------------------------- state
  const STATIC = window.SPM_STATIC || null;
  const TOKEN = document.querySelector('meta[name="spm-token"]')?.content || "";

  const state = {
    items: [],
    errors: [],
    status: null,
    generated: null,
    // An empty set means "show everything" for that filter group.
    filters: { products: new Set(), sevs: new Set(), kinds: new Set() },
    affectsOnly: false,
    projectsConfigured: false,
    projects: [],
    canBrowse: false,
    pendingRemoval: null,
    editingProject: null, // id of the project in the form, null when adding
    view: "month",
    cursor: new Date(),
    selected: null,
    onlyNew: false,
  };

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
  const safeUrl = (url) => (/^https:\/\//i.test(url || "") ? url : "#");

  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const startOfWeek = (d) => addDays(startOfDay(d), -((d.getDay() + 6) % 7));
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const fromIso = (s) => new Date(`${s}T00:00:00`);
  const todayIso = () => iso(new Date());
  const daysFromToday = (s) => Math.round((fromIso(s) - fromIso(todayIso())) / 864e5);
  const fmtDate = (s, opts = { weekday: "short", day: "numeric", month: "short", year: "numeric" }) =>
    fromIso(s).toLocaleDateString(LOCALE, opts);
  const fmtTime = (s) => (s ? new Date(s).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "–");
  const relative = (n) =>
    n === 0 ? "today" : n === 1 ? "tomorrow" : n === -1 ? "yesterday" : n > 0 ? `in ${n} days` : `${-n} days ago`;

  const severityRank = (item) => {
    const rank = SEVERITIES.indexOf(item.severity);
    return rank < 0 ? SEVERITIES.length : rank;
  };
  const byUrgency = (a, b) => (a.kind !== "upcoming") - (b.kind !== "upcoming") || severityRank(a) - severityRank(b);
  const byDateAsc = (a, b) => (a.date || "").localeCompare(b.date || "");
  const byDateDesc = (a, b) => (b.date || "").localeCompare(a.date || "");

  // An impact is "active" unless the module is installed but not enabled in that project.
  const activeImpacts = (item) => (item.impacts || []).filter((i) => i.enabled.state !== "not_enabled");
  const affectsProjects = (item) => activeImpacts(item).length > 0;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // ---------------------------------------------------------------- selectors
  function visibleItems() {
    const { products, sevs, kinds } = state.filters;
    const allows = (set, value) => set.size === 0 || set.has(value);
    return state.items.filter((i) =>
      allows(products, i.product) && allows(sevs, i.severity) && allows(kinds, i.kind)
      && (!state.onlyNew || i.new) && (!state.affectsOnly || affectsProjects(i)));
  }

  function itemsByDate(items) {
    const map = new Map();
    for (const item of items) {
      if (!item.date) continue;
      if (!map.has(item.date)) map.set(item.date, []);
      map.get(item.date).push(item);
    }
    for (const list of map.values()) list.sort(byUrgency);
    return map;
  }

  // ---------------------------------------------------------------- templates
  function cardHtml(item) {
    const when = item.date
      ? `<span class="when">${esc(fmtDate(item.date))}</span> · ${relative(daysFromToday(item.date))} — `
      : "";
    return `<div class="card sev-${esc(item.severity)}" data-key="${esc(item.key)}">
      <div class="meta"><span class="prod">${esc(PRODUCTS[item.product])}</span>
        <span class="kind">${esc(KINDS[item.kind])}</span><span class="sev">${esc(item.severity)}</span>
        ${item.new ? '<span class="newtag">NEW</span>' : ""}</div>
      <a href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>
      <div class="detail">${when}${esc(item.detail)}</div>
      ${impactHtml(item)}
    </div>`;
  }

  function enabledHtml(impact) {
    const enabled = impact.enabled;
    const names = enabled.names.length ? ` (${enabled.names.map(esc).join(", ")})` : "";
    const where = enabled.source ? ` title="${esc(enabled.source)}"` : "";
    switch (enabled.state) {
      case "enabled":
        return `<span class="state enabled"${where}>Enabled${names}</span>`;
      case "split_only":
        return `<span class="state split_only"${where}>Enabled in config split: ${enabled.splits.map(esc).join(", ")}${names}</span>`;
      case "always":
        return impact.package === "drupal/core"
          ? '<span class="state enabled">Drupal core (always enabled)</span>'
          : `<span class="state enabled">Installed dependency</span>`;
      case "not_enabled":
        return `<span class="state not_enabled"${where}>Installed, not enabled</span>`;
      default:
        return '<span class="state unknown">Enabled state unknown (no core.extension.yml found)</span>';
    }
  }

  function impactHtml(item) {
    const impacts = item.impacts || [];
    if (impacts.length) {
      const rows = impacts.map((i) => {
        const fix = i.fixed_in ? ` → update to <b>${esc(i.fixed_in)}</b> or later` : " → see advisory for the fixed version";
        return `<li><strong>${esc(i.project)}</strong>: <code>${esc(i.package)}</code> ${esc(i.installed)}${fix}
          <div>${enabledHtml(i)}<span class="src">${esc(i.source)}</span></div></li>`;
      }).join("");
      const active = activeImpacts(item).length;
      const heading = active
        ? `⚠ Affects ${plural(active, "project")}`
        : `Installed in ${plural(impacts.length, "project")}, but not enabled`;
      return `<div class="impact${active ? "" : " dormant"}"><b>${heading}</b><ul>${rows}</ul></div>`;
    }
    if (item.checked) {
      return '<div class="impact clear"><b>✓</b> Not affected in your projects</div>';
    }
    return "";
  }

  function chipHtml(item) {
    const tooltip = `${PRODUCTS[item.product]} · ${KINDS[item.kind]}\n${item.title}`;
    const flags = `${item.new ? " isnew" : ""}${affectsProjects(item) ? " affects" : ""}`;
    return `<div class="ev ${esc(item.kind)} sev-${esc(item.severity)}${flags}"
      data-key="${esc(item.key)}" title="${esc(tooltip)}">${esc(PRODUCTS[item.product])} · ${esc(item.title)}</div>`;
  }

  // ---------------------------------------------------------------- renderers
  function renderFilters() {
    const counts = {};
    for (const item of state.items) counts[item.severity] = (counts[item.severity] || 0) + 1;
    for (const [group, options] of Object.entries(FILTER_OPTIONS)) {
      const active = state.filters[group];
      const buttons = options.map(([value, label]) => {
        const text = group === "sevs" ? `${label} ${counts[value] || 0}` : label;
        return `<button type="button" data-value="${esc(value)}" class="sev-${esc(value)}${active.has(value) ? " on" : ""}">${esc(text)}</button>`;
      });
      $(group).innerHTML = `<span class="glabel">${FILTER_LABELS[group]}</span>`
        + `<button type="button" data-all="1" class="${active.size ? "" : "on"}">All</button>${buttons.join("")}`;
    }
    for (const b of $("views").querySelectorAll("button")) b.classList.toggle("on", b.dataset.view === state.view);
    const affected = state.items.filter(affectsProjects).length;
    $("affectsOnly").hidden = !state.projectsConfigured;
    $("affectsOnly").setAttribute("aria-pressed", String(state.affectsOnly));
    $("affectsOnly").textContent = `⚠ Affects my projects ${affected}`;
  }

  function renderMonth(byDate) {
    const cursor = state.cursor;
    const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    const start = startOfWeek(first);
    const today = todayIso();
    $("calTitle").textContent = first.toLocaleDateString(LOCALE, { month: "long", year: "numeric" });

    const weekdays = Array.from({ length: 7 }, (_, n) =>
      `<div>${addDays(start, n).toLocaleDateString(LOCALE, { weekday: "short" })}</div>`).join("");
    const cells = Array.from({ length: 42 }, (_, n) => {
      const d = addDays(start, n);
      const key = iso(d);
      const items = byDate.get(key) || [];
      const classes = ["day",
        d.getMonth() !== cursor.getMonth() && "other", key === today && "today", key === state.selected && "sel"];
      const more = items.length > MONTH_CHIPS ? `<div class="more">+${items.length - MONTH_CHIPS} more</div>` : "";
      return `<div class="${classes.filter(Boolean).join(" ")}" data-date="${key}">
        <span class="num">${d.getDate()}</span>${items.slice(0, MONTH_CHIPS).map(chipHtml).join("")}${more}</div>`;
    }).join("");
    $("calbody").innerHTML = `<div class="dow">${weekdays}</div><div class="month">${cells}</div>`;
  }

  function renderWeek(byDate) {
    const start = startOfWeek(state.cursor);
    const end = addDays(start, 6);
    const today = todayIso();
    const startFmt = start.getMonth() === end.getMonth() ? { day: "numeric" } : { day: "numeric", month: "short" };
    $("calTitle").textContent = `${start.toLocaleDateString(LOCALE, startFmt)} – ${end.toLocaleDateString(LOCALE,
      { day: "numeric", month: "short", year: "numeric" })}`;

    const columns = Array.from({ length: 7 }, (_, n) => {
      const d = addDays(start, n);
      const key = iso(d);
      const items = byDate.get(key) || [];
      return `<div class="wcol${key === today ? " today" : ""}" data-date="${key}">
        <div class="whead" data-open-day="${key}" title="Open day view">
          <small>${d.toLocaleDateString(LOCALE, { weekday: "short" })}</small><b>${d.getDate()}</b></div>
        <div class="wlist">${items.map(chipHtml).join("")}</div></div>`;
    }).join("");
    $("calbody").innerHTML = `<div class="week">${columns}</div>`;
  }

  function renderDay(byDate) {
    const items = byDate.get(iso(state.cursor)) || [];
    $("calTitle").textContent = state.cursor.toLocaleDateString(LOCALE,
      { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    $("calbody").innerHTML = `<div class="dayview">${items.map(cardHtml).join("")
      || '<div class="empty">No items on this day.</div>'}</div>`;
  }

  const VIEW_RENDERERS = { month: renderMonth, week: renderWeek, day: renderDay };

  function renderSide(items) {
    let title;
    let shown;
    const dayPanel = state.selected && state.view !== "day";
    if (dayPanel) {
      title = fmtDate(state.selected);
      shown = items.filter((i) => i.date === state.selected).sort(byUrgency);
    } else if (state.onlyNew) {
      title = "New items";
      shown = [...items].sort(byDateDesc);
    } else {
      const today = todayIso();
      title = `Next ${SIDE_DAYS} days`;
      shown = items
        .filter((i) => i.date && i.date >= today && daysFromToday(i.date) <= SIDE_DAYS)
        .sort((a, b) => byDateAsc(a, b) || byUrgency(a, b));
    }
    const closeButton = dayPanel || state.onlyNew
      ? '<button type="button" class="btn" data-action="clear-selection" aria-label="Close">×</button>' : "";
    $("sideTitle").innerHTML = `<span>${esc(title)}</span>${closeButton}`;
    $("side").innerHTML = shown.map(cardHtml).join("") || '<div class="empty">No items.</div>';
  }

  function renderList(items) {
    const kinds = state.filters.kinds;
    $("list").innerHTML = SECTIONS
      .filter(([kind]) => kinds.size === 0 || kinds.has(kind))
      .map(([kind, title, subtitle]) => {
        const section = items.filter((i) => i.kind === kind).sort(kind === "advisory" ? byDateDesc : byDateAsc);
        return `<section><h2>${esc(title)} <small>${section.length}</small></h2><p class="sub">${esc(subtitle)}</p>
          <div class="cards">${section.map(cardHtml).join("") || '<div class="empty">Nothing found.</div>'}</div></section>`;
      }).join("");
  }

  function renderStatus() {
    const s = state.status;
    if (!s) return;
    $("status").classList.toggle("busy", s.checking);
    $("status").classList.remove("down");
    $("statusText").textContent = s.checking
      ? "Checking…"
      : `Last check ${fmtTime(s.last_check)} · next ${fmtTime(s.next_check)} · every ${s.settings.interval_minutes} min`
        + (s.settings.notifications ? "" : " · notifications off");
  }

  function renderBanners() {
    const count = state.items.filter((i) => i.new).length;
    $("newbar").hidden = count === 0;
    $("newCount").textContent = `${count} new item${count === 1 ? "" : "s"}`;
    $("errors").hidden = state.errors.length === 0;
    $("errors").innerHTML = state.errors.length
      ? `<b>Sources with errors:</b> ${state.errors.map(esc).join(" · ")}` : "";
  }

  function render() {
    const items = visibleItems();
    renderFilters();
    VIEW_RENDERERS[state.view](itemsByDate(items));
    renderSide(items);
    renderList(items);
    renderStatus();
    renderBanners();
  }

  // ---------------------------------------------------------------- API
  class ApiError extends Error {}

  async function api(path, body) {
    const init = { headers: { "X-SPM-Token": TOKEN } };
    if (body !== undefined) {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(data.error || `${path}: HTTP ${response.status}`);
    return data;
  }

  function applySnapshot(snapshot) {
    state.status = snapshot.status;
    if (snapshot.report) {
      state.items = snapshot.report.items;
      state.errors = snapshot.report.errors;
      state.generated = snapshot.report.generated;
      state.projectsConfigured = Boolean(snapshot.report.projects_configured);
      if (!state.projectsConfigured) state.affectsOnly = false;
    }
    render();
  }

  async function poll() {
    try {
      const status = await api("/api/status");
      state.status = status;
      if (status.generated !== state.generated) applySnapshot(await api("/api/report"));
      else renderStatus();
    } catch (err) {
      $("status").classList.add("down");
      $("statusText").textContent = "Lost connection to the app";
      console.error(err);
    }
  }

  // ---------------------------------------------------------------- projects
  function renderProjects() {
    $("browseLock").hidden = !state.canBrowse;
    if (!state.projects.length) {
      $("projectList").innerHTML = '<li class="empty">No projects yet. Add one to see which advisories affect it.</li>';
      return;
    }
    $("projectList").innerHTML = state.projects.map(projectHtml).join("");
  }

  function projectHtml(p) {
    const s = p.summary;
    const drupal = p.ecosystem === "composer";
    let meta = "";
    if (s && drupal) meta = `Drupal core ${esc(s.drupal_core || "not found")} · ${plural(s.drupal_projects, "contrib project")}`;
    else if (s) meta = `Next.js ${esc(s.next || "not found")} · ${plural(s.packages, "npm package")}`;
    let config = "";
    if (p.config) {
      config = `Config: ${esc(p.config.source)} · ${p.config.enabled} enabled`
        + (p.config.splits.length ? ` · splits: ${p.config.splits.map(esc).join(", ")}` : "");
    } else if (s && drupal) {
      config = "Config: no core.extension.yml found";
    }
    const source = p.source || (p.branch ? `origin/${p.branch}` : "Local files");
    const confirming = state.pendingRemoval === p.id;
    return `<li class="${state.editingProject === p.id ? "editing" : ""}">
      <div class="info"><strong>${esc(p.name)}</strong>
        <div class="path">${esc(p.lock_path)}</div>
        <div class="meta">Source: ${esc(source)}</div>
        ${meta ? `<div class="meta">${meta}</div>` : ""}
        ${config ? `<div class="meta">${config}</div>` : ""}
        ${p.warning ? `<div class="notice">⚠ ${esc(p.warning)}</div>` : ""}
        ${p.error ? `<div class="warn">⚠ ${esc(p.error)}</div>` : ""}</div>
      <div class="row-actions">
        <button type="button" class="btn" data-edit="${esc(p.id)}">Edit</button>
        <button type="button" class="btn danger${confirming ? " confirm" : ""}" data-remove="${esc(p.id)}">
          ${confirming ? "Confirm remove" : "Remove"}</button>
      </div>
    </li>`;
  }

  function applyProjects(data) {
    state.projects = data.projects;
    state.canBrowse = data.can_browse;
    state.pendingRemoval = null;
    renderProjects();
  }

  async function loadProjects() {
    try {
      applyProjects(await api("/api/projects"));
    } catch (err) {
      $("projectList").innerHTML = `<li class="warn">Cannot load projects: ${esc(err.message)}</li>`;
    }
  }

  async function refreshReport() {
    applySnapshot(await api("/api/report"));
  }

  const sourceRadio = (value) => document.querySelector(`input[name="projectSource"][value="${value}"]`);

  // One form serves both "add" (project = null) and "edit" (project = the project being edited).
  function openProjectForm(project = null) {
    state.editingProject = project ? project.id : null;
    $("projectFormTitle").textContent = project ? `Edit “${project.name}”` : "Add project";
    $("saveProject").textContent = project ? "Save changes" : "Add project";
    $("projectName").value = project ? project.name : "";
    $("projectPath").value = project ? project.lock_path : "";
    $("projectBranch").value = project?.branch || "develop";
    sourceRadio(project && !project.branch ? "local" : "git").checked = true;
    $("projectError").hidden = true;
    $("projectForm").hidden = false;
    $("addProject").hidden = true;
    renderProjects();
    $("projectForm").scrollIntoView({ block: "nearest" });
    $("projectName").focus();
  }

  function closeProjectForm() {
    state.editingProject = null;
    $("projectForm").hidden = true;
    $("addProject").hidden = false;
    $("projectError").hidden = true;
    renderProjects();
  }

  function showProjectError(message) {
    $("projectError").textContent = message;
    $("projectError").hidden = false;
  }

  async function saveProject() {
    const name = $("projectName").value.trim();
    const path = $("projectPath").value.trim();
    const useGit = sourceRadio("git").checked;
    const branch = useGit ? $("projectBranch").value.trim() : "";
    if (!name) return showProjectError("Enter a project name.");
    if (!path) return showProjectError("Choose a lock file (composer.lock, package-lock.json, yarn.lock or pnpm-lock.yaml).");
    if (useGit && !branch) return showProjectError("Enter a branch, or choose “Local files”.");

    const editing = state.editingProject;
    const button = $("saveProject");
    const idleLabel = button.textContent;
    button.disabled = true;
    button.textContent = useGit ? "Fetching…" : "Saving…";
    try {
      const data = editing
        ? await api("/api/projects/update", { id: editing, name, path, branch })
        : await api("/api/projects", { name, path, branch });
      applyProjects(data);
      closeProjectForm();
      await refreshReport();
    } catch (err) {
      showProjectError(err.message);
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }

  async function browseLock() {
    try {
      const { path } = await api("/api/browse", {});
      if (path) $("projectPath").value = path;
    } catch (err) {
      showProjectError(err.message);
    }
  }

  async function removeProject(id) {
    if (state.pendingRemoval !== id) {
      state.pendingRemoval = id; // first click asks for confirmation
      return renderProjects();
    }
    try {
      applyProjects(await api("/api/projects/remove", { id }));
      if (state.editingProject === id) closeProjectForm();
      await refreshReport();
    } catch (err) {
      state.pendingRemoval = null;
      renderProjects();
      showProjectError(err.message);
    }
  }

  // ---------------------------------------------------------------- navigation
  function setView(view, date) {
    state.view = view;
    if (date) state.cursor = fromIso(date);
    render();
  }

  function step(direction) {
    const c = state.cursor;
    if (state.view === "month") state.cursor = new Date(c.getFullYear(), c.getMonth() + direction, 1);
    else state.cursor = addDays(c, state.view === "week" ? 7 * direction : direction);
    render();
  }

  function goToday() {
    state.cursor = new Date();
    state.selected = null;
    render();
  }

  function setMode(mode) {
    for (const b of $("modes").querySelectorAll("button")) b.classList.toggle("on", b.dataset.mode === mode);
    document.body.classList.toggle("mode-cal", mode === "cal");
    document.body.classList.toggle("mode-list", mode === "list");
  }

  function revealInSide(key) {
    const card = [...$("side").querySelectorAll(".card")].find((c) => c.dataset.key === key);
    if (!card) return;
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    card.classList.remove("flash");
    void card.offsetWidth; // restart the animation
    card.classList.add("flash");
  }

  // Click shows only that value; further clicks add values; clicking an active value removes it.
  function toggleFilter(group, button) {
    const active = state.filters[group];
    if (button.dataset.all) active.clear();
    else if (active.has(button.dataset.value)) active.delete(button.dataset.value);
    else {
      active.add(button.dataset.value);
      if (active.size === FILTER_OPTIONS[group].length) active.clear();
    }
    render();
  }

  // ---------------------------------------------------------------- events
  function bindEvents() {
    $("modes").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) setMode(b.dataset.mode);
    });
    $("views").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) setView(b.dataset.view);
    });
    for (const group of Object.keys(FILTER_OPTIONS)) {
      $(group).addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (b) toggleFilter(group, b);
      });
    }

    $("calbody").addEventListener("click", (e) => {
      const head = e.target.closest("[data-open-day]");
      if (head) return setView("day", head.dataset.openDay);
      const cell = e.target.closest("[data-date]");
      if (!cell) return;
      const chip = e.target.closest(".ev");
      state.selected = !chip && state.selected === cell.dataset.date ? null : cell.dataset.date;
      state.onlyNew = false;
      render();
      if (chip) revealInSide(chip.dataset.key);
    });
    $("calbody").addEventListener("dblclick", (e) => {
      const cell = e.target.closest(".day");
      if (cell) setView("day", cell.dataset.date);
    });
    $("sideTitle").addEventListener("click", (e) => {
      if (!e.target.closest('[data-action="clear-selection"]')) return;
      state.selected = null;
      state.onlyNew = false;
      render();
    });

    $("prev").addEventListener("click", () => step(-1));
    $("next").addEventListener("click", () => step(1));
    $("todayBtn").addEventListener("click", goToday);

    $("showNew").addEventListener("click", () => {
      state.onlyNew = true;
      state.selected = null;
      setMode("cal");
      render();
    });
    $("dismissNew").addEventListener("click", async () => {
      await api("/api/seen", {});
      for (const item of state.items) item.new = false;
      state.onlyNew = false;
      render();
    });
    $("checkNow").addEventListener("click", async () => {
      state.status = { ...(await api("/api/check", {})), checking: true };
      renderStatus();
      setTimeout(poll, 1500);
    });

    $("openSettings").addEventListener("click", () => {
      const s = state.status?.settings || { interval_minutes: 30, notifications: true };
      $("interval").value = s.interval_minutes;
      $("notif").checked = s.notifications;
      closeProjectForm();
      $("settings").showModal();
      loadProjects();
    });
    $("testNotification").addEventListener("click", async (e) => {
      const button = e.currentTarget;
      button.disabled = true;
      button.textContent = "Sending…";
      try {
        // The first time on macOS this waits until the permission prompt is answered.
        await api("/api/test-notification", {});
        button.textContent = "Sent";
      } catch {
        button.textContent = "Failed";
      }
      setTimeout(() => {
        button.textContent = "Send test";
        button.disabled = false;
      }, 2000);
    });
    $("addProject").addEventListener("click", () => openProjectForm());
    $("cancelProject").addEventListener("click", closeProjectForm);
    $("saveProject").addEventListener("click", saveProject);
    $("browseLock").addEventListener("click", browseLock);
    $("projectList").addEventListener("click", (e) => {
      const remove = e.target.closest("[data-remove]");
      if (remove) return removeProject(remove.dataset.remove);
      const editButton = e.target.closest("[data-edit]");
      if (editButton) openProjectForm(state.projects.find((p) => p.id === editButton.dataset.edit));
    });
    $("projectBranch").addEventListener("focus", () => {
      document.querySelector('input[name="projectSource"][value="git"]').checked = true;
    });
    for (const id of ["projectName", "projectPath", "projectBranch"]) {
      // Enter would otherwise submit (and close) the settings dialog.
      $(id).addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          saveProject();
        }
      });
    }
    $("affectsOnly").addEventListener("click", () => {
      state.affectsOnly = !state.affectsOnly;
      render();
    });
    $("saveSettings").addEventListener("click", async (e) => {
      e.preventDefault();
      const minutes = Math.max(1, Math.min(1440, parseInt($("interval").value, 10) || 30));
      state.status = await api("/api/settings", { interval_minutes: minutes, notifications: $("notif").checked });
      $("settings").close();
      renderStatus();
    });

    const keyViews = { d: "day", w: "week", m: "month" };
    document.addEventListener("keydown", (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest("input, dialog")) return;
      if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else if (e.key === "t") goToday();
      else if (keyViews[e.key]) setView(keyViews[e.key]);
    });
  }

  // ---------------------------------------------------------------- boot
  bindEvents();
  if (STATIC) {
    document.body.classList.add("static");
    applySnapshot(STATIC);
  } else {
    render();
    poll();
    setInterval(poll, POLL_MS);
  }

  // Exposed for automated UI checks only.
  window.SPM = { state, render, visibleItems };
})();
