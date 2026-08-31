/* ================================================================
   Operations & Maintenance - single page app (vanilla JS)
   ================================================================ */
"use strict";

const State = {
  token: localStorage.getItem("ops_token") || null,
  user: null,
  modules: [],
};

const root = document.getElementById("root");

/* ---------- theme ---------- */
(function initTheme() {
  const saved = localStorage.getItem("ops_theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
})();
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : cur === "light" ? "dark"
    : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("ops_theme", next);
}

/* ---------- api ---------- */
async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (State.token) headers.Authorization = "Bearer " + State.token;
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch("/api" + path, { method, headers, body: payload });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && data.error) || res.statusText || "Request failed";
    if (res.status === 401 && State.token) { logout(true); }
    throw new Error(msg);
  }
  return data;
}

/* ---------- helpers ---------- */
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
};
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };
const badge = (value) => h("span", { class: "badge dot b-" + value }, String(value || "").replace(/_/g, " "));
const fmtDate = (s) => s ? new Date(s).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—";
const fmtWhen = (s) => s ? new Date(s).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
const esc = (s) => String(s == null ? "" : s);

function toast(msg, isErr = false) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  document.body.append(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, 2600);
}

function lightbox(src) {
  const lb = h("div", { class: "lightbox", onclick: () => lb.remove() }, h("img", { src }));
  document.body.append(lb);
}

function navigate(hash) { window.location.hash = hash; }

/* ---------- auth ---------- */
async function login(username, password) {
  const data = await api("/auth/login", { method: "POST", body: { username, password } });
  State.token = data.token;
  State.user = data.user;
  localStorage.setItem("ops_token", data.token);
}
async function logout(silent) {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  State.token = null; State.user = null;
  localStorage.removeItem("ops_token");
  if (!silent) navigate("#/login");
  else { window.location.hash = "#/login"; render(); }
}

/* ================================================================
   Chrome
   ================================================================ */
const MODULE_ROUTE = { assets: "#/assets", planning: "#/planning", dashboard: "#/dashboard" };
function topbar(active) {
  const navItems = State.modules.map((m) =>
    h("a", {
      href: MODULE_ROUTE[m.key] || "#/home",
      class: active === m.key ? "active" : "",
    }, m.name));
  return h("header", { class: "topbar" },
    h("div", { class: "brand", onclick: () => navigate("#/home"), style: "cursor:pointer" },
      h("span", { class: "spark" }), "Operations & Maintenance"),
    h("nav", {}, h("a", { href: "#/home", class: active === "home" ? "active" : "" }, "Home"), ...navItems),
    h("div", { class: "right" },
      h("button", { class: "icon-btn", title: "Toggle theme", onclick: () => { toggleTheme(); } }, "◐"),
      State.user && h("div", { class: "who" },
        h("b", {}, State.user.display_name),
        h("span", { class: "role" }, State.user.role)),
      h("button", { class: "btn sm ghost", onclick: () => logout() }, "Sign out")));
}

function shell(active, ...children) {
  return h("div", { class: "app" }, topbar(active), h("main", { class: "content" }, ...children));
}

function loading() { root.replaceChildren(h("div", { class: "spinner" }, "Loading…")); }

/* ================================================================
   Views
   ================================================================ */
function viewLogin() {
  const form = h("form", { class: "login-card", onsubmit: async (e) => {
    e.preventDefault();
    errBox.textContent = "";
    btn.disabled = true; btn.textContent = "Signing in…";
    try {
      await login(u.value.trim(), p.value);
      await bootstrap();
      navigate("#/home");
    } catch (err) {
      errBox.textContent = err.message;
      btn.disabled = false; btn.textContent = "Sign in";
    }
  }});
  const u = h("input", { id: "u", autocomplete: "username", required: true, autofocus: true });
  const p = h("input", { id: "p", type: "password", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn primary", type: "submit" }, "Sign in");
  const errBox = h("div", { class: "form-error" });
  form.append(
    h("div", { class: "brand" }, h("span", { class: "spark" }), "Operations & Maintenance"),
    h("div", { class: "tagline" }, "Sign in to continue"),
    h("div", { class: "field" }, h("label", { for: "u" }, "Username"), u),
    h("div", { class: "field" }, h("label", { for: "p" }, "Password"), p),
    btn, errBox,
    h("div", { class: "seed-hint" },
      "Demo logins", h("br"),
      h("code", {}, "admin / admin123"), " — planner", h("br"),
      h("code", {}, "sclydesdale / tech123"), " — technician"));
  root.replaceChildren(h("div", { class: "login-wrap" }, form));
}

function viewHome() {
  const isAdmin = State.user.role === "ADMIN";
  const tiles = h("div", { class: "tiles" });
  tiles.append(
    h("button", { class: "tile", onclick: () => navigate("#/assets") },
      h("span", { class: "ico" }, "🔧"),
      h("h3", {}, "View asset information"),
      h("p", {}, "Browse the asset register, review full details and history, and log pending observations with photos.")));
  if (isAdmin) {
    tiles.append(
      h("button", { class: "tile", onclick: () => navigate("#/dashboard") },
        h("span", { class: "ico" }, "📊"),
        h("h3", {}, "Site dashboard"),
        h("p", {}, "Open pendings, the next service due dates, and retrofits still outstanding across the site.")),
      h("button", { class: "tile", onclick: () => navigate("#/planning") },
        h("span", { class: "ico" }, "📅"),
        h("h3", {}, "Planning"),
        h("p", {}, "Build the day's team plan — drag available technicians and tasks into 10 team rows.")));
  }
  root.replaceChildren(shell("home",
    h("div", { class: "page-head" },
      h("h1", {}, "Welcome, " + State.user.display_name.split(" ")[0]),
      h("p", { class: "sub" }, isAdmin
        ? "You have planner access. Choose an area to work in."
        : "Choose an area to work in.")),
    tiles));
}

/* ---------- site dashboard (admin) ---------- */
async function viewDashboard() {
  if (State.user.role !== "ADMIN") { navigate("#/home"); return; }
  loading();
  let d;
  try { d = await api("/dashboard"); }
  catch (e) { return renderError(e); }

  const daysAway = (iso) => Math.round((new Date(iso) - new Date()) / 86400000);

  const pendCard = h("div", { class: "card kpi" },
    h("div", { class: "kpi-num" }, d.open_pendings),
    h("div", { class: "kpi-label" }, "Open pending entries"),
    h("div", { class: "kpi-sub" },
      Object.entries(d.pendings_by_status || {})
        .map(([s, c]) => `${c} ${s.toLowerCase()}`).join(" · ") || "None"));

  const retroTotal = d.incomplete_retrofit_count;
  const retroCard = h("div", { class: "card kpi" },
    h("div", { class: "kpi-num" }, retroTotal),
    h("div", { class: "kpi-label" }, "Retrofit items not completed"),
    h("div", { class: "kpi-sub" }, `${(d.incomplete_retrofits || []).length} campaigns affected`));

  const svcCard = h("div", { class: "card kpi" },
    h("div", { class: "kpi-num" }, d.service_count),
    h("div", { class: "kpi-label" }, "Turbines with a next-service date"),
    h("div", { class: "kpi-sub" }, d.next_services[0]
      ? "Next: " + d.next_services[0].tag + " on " + fmtDate(d.next_services[0].due) : "—"));

  const svcList = h("div", { class: "card" }, h("h3", {}, "Next 10 service due dates"),
    h("p", { class: "hint", style: "margin:-.2rem 0 .6rem" }, "108-month service completion + 6 months."));
  if (!d.next_services.length) svcList.append(h("div", { class: "empty-state" }, "No service dates on record."));
  else {
    const list = h("div", { class: "rec-list" });
    for (const s of d.next_services) {
      const dd = daysAway(s.due);
      list.append(h("div", { class: "rec-row", style: "cursor:pointer", onclick: () => navigate("#/assets") },
        h("div", { class: "rec-name" }, s.tag,
          h("span", { class: "hint", style: "margin-left:.5rem" },
            dd < 0 ? `overdue ${-dd}d` : dd === 0 ? "today" : `in ${dd}d`)),
        h("span", { class: "rec-date " + (dd < 0 ? "out" : dd <= 30 ? "wip" : "") }, fmtDate(s.due))));
    }
    svcList.append(list);
  }

  const retroList = h("div", { class: "card" }, h("h3", {}, "Retrofits not completed"));
  if (!d.incomplete_retrofits.length) retroList.append(h("div", { class: "empty-state" }, "All retrofits complete."));
  else {
    const list = h("div", { class: "rec-list" });
    for (const g of d.incomplete_retrofits) {
      const out = g.outstanding || [], wip = g.in_progress || [];
      list.append(h("div", { class: "rec-row", style: "align-items:flex-start" },
        h("div", { class: "rec-name" }, g.name,
          h("div", { class: "hint", style: "font-weight:400;margin-top:.2rem" },
            (out.length ? out.length + " outstanding" : "")
            + (out.length && wip.length ? " · " : "")
            + (wip.length ? wip.length + " in progress" : ""))),
        h("span", { class: "rec-date out" }, out.length + wip.length)));
    }
    retroList.append(list);
  }

  root.replaceChildren(shell("dashboard",
    h("div", { class: "page-head" }, h("h1", {}, "Site dashboard"),
      h("p", { class: "sub" }, "Kilgallioch — live figures across all 96 turbines.")),
    h("div", { class: "kpi-row" }, pendCard, svcCard, retroCard),
    h("div", { class: "grid cols-2", style: "margin-top:1rem" }, svcList, retroList)));
}

/* ---------- assets list ---------- */
async function viewAssets() {
  loading();
  let assets;
  try { assets = (await api("/assets")).assets; }
  catch (e) { return renderError(e); }

  const search = h("input", { class: "search", placeholder: "Search turbine or location…", type: "search" });
  const locSel = h("select", {});
  const typeSel = h("select", {});
  locSel.append(h("option", { value: "" }, "All locations"),
    ...[...new Set(assets.map(a => a.location))].sort().map(l => h("option", { value: l }, l)));
  typeSel.append(h("option", { value: "" }, "All types"),
    ...[...new Set(assets.map(a => a.type))].sort().map(t => h("option", { value: t }, t)));

  const listEl = h("div", { class: "list" });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const rows = assets.filter(a =>
      (!q || (a.tag + " " + a.name + " " + a.location).toLowerCase().includes(q)) &&
      (!locSel.value || a.location === locSel.value) &&
      (!typeSel.value || a.type === typeSel.value));
    clear(listEl);
    if (!rows.length) { listEl.append(h("div", { class: "empty-state" }, "No assets match your filters.")); return; }
    for (const a of rows) {
      listEl.append(h("div", { class: "row-card", onclick: () => navigate("#/asset/" + a.id) },
        h("div", { class: "tag" }, a.tag),
        h("div", { class: "main" },
          h("div", { class: "name" }, a.name === a.tag ? a.type : a.name),
          h("div", { class: "meta" }, a.name === a.tag ? a.location : a.type + " · " + a.location)),
        h("div", { class: "aside" },
          a.open_pendings > 0
            ? h("span", { class: "count-pill", title: "Open pending entries" }, a.open_pendings + " pending")
            : h("span", { class: "meta", style: "color:var(--text-faint)" }, "No pendings"))));
    }
  };
  [search, locSel, typeSel].forEach(el => el.addEventListener("input", draw));

  root.replaceChildren(shell("assets",
    h("div", { class: "page-head" }, h("h1", {}, "Asset information"),
      h("p", { class: "sub" }, "Select an asset to view its full record and add pending entries.")),
    h("div", { class: "toolbar" }, search, locSel, typeSel),
    listEl));
  draw();
}

/* ---------- asset detail ---------- */
async function viewAsset(id) {
  loading();
  let detail, pendings;
  try {
    detail = await api("/assets/" + id);
    pendings = (await api("/assets/" + id + "/pendings")).pendings;
  } catch (e) { return renderError(e); }
  const a = detail.asset;
  const isAdmin = State.user.role === "ADMIN";

  const tabWrap = h("div", {});
  const tabs = ["Details", "Service dates", "HV history", "Stat history", "Retrofits", "Components", "History", "Pendings"];
  let activeTab = decodeURIComponent(location.hash.split("?tab=")[1] || "") || "Details";
  if (!tabs.includes(activeTab)) activeTab = "Details";

  const tabLabel = (t) => t === "Pendings" ? `Pendings (${pendings.length})` : t;
  const tabBar = h("div", { class: "tabs" }, ...tabs.map(t =>
    h("button", { "data-tab": t, class: t === activeTab ? "active" : "",
      onclick: () => { activeTab = t; drawTab(); } }, tabLabel(t))));

  const panes = {
    "Details": detailsPane,
    "Service dates": () => recordPane("service", detail.services, "No service dates recorded for this turbine."),
    "HV history": () => datePane("HV maintenance history",
      "Completion dates from the HV tab of the KGH Virtual Whiteboard.",
      detail.hv, "No HV maintenance recorded for this turbine."),
    "Stat history": () => datePane("Statutory inspection history",
      "Completion dates from the Stats tab of the KGH Virtual Whiteboard.",
      detail.stat, "No statutory inspections recorded for this turbine."),
    "Retrofits": () => recordPane("retrofit", detail.retrofits, "No retrofits recorded for this turbine."),
    "Components": componentsPane,
    "History": historyPane,
    "Pendings": pendingsPane,
  };

  function datePane(title, hint, records, emptyMsg) {
    records = (records || []).slice().sort((x, y) => String(y.date || "").localeCompare(String(x.date || "")));
    const done = records.filter(r => r.date).length;
    const card = h("div", { class: "card" }, h("h3", {}, title),
      h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
        records.length ? `${done} of ${records.length} completed. ${hint}` : hint));
    if (!records.length) { card.append(h("div", { class: "empty-state" }, emptyMsg)); return card; }
    const list = h("div", { class: "rec-list" });
    for (const r of records) {
      list.append(h("div", { class: "rec-row" },
        h("div", { class: "rec-name" }, r.name),
        r.date ? h("span", { class: "rec-date" }, fmtDate(r.date))
               : h("span", { class: "rec-date muted" }, r.detail || "Not completed")));
    }
    card.append(list);
    return card;
  }

  function drawTab() {
    [...tabBar.children].forEach((b) => b.classList.toggle("active", b.dataset.tab === activeTab));
    clear(tabWrap);
    tabWrap.append((panes[activeTab] || detailsPane)());
  }

  function detailsPane() {
    const dl = (pairs) => h("dl", { class: "dl" }, ...pairs.flatMap(([k, v]) =>
      [h("dt", {}, k), h("dd", {}, v || "—")]));
    const ns = detail.next_service || {};
    let dueNote = "No 108-month service completion on record.";
    let dueClass = "muted";
    if (ns.due) {
      const days = Math.round((new Date(ns.due) - new Date()) / 86400000);
      dueNote = days < 0 ? `Overdue by ${-days} days`
        : days === 0 ? "Due today"
        : `In ${days} day${days === 1 ? "" : "s"}`;
      dueClass = days < 0 ? "overdue" : days <= 30 ? "soon" : "ok";
    }
    return h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h3", {}, "Identification"),
        dl([["Turbine", a.tag], ...(a.name !== a.tag ? [["Name", a.name]] : []),
            ["Type", a.type], ["Location", a.location],
            ["Criticality", badge(a.criticality)]])),
      h("div", { class: "card" }, h("h3", {}, "Equipment"),
        dl([["Manufacturer", a.manufacturer], ["Model", a.model], ["Family", a.family],
            ["Serial number", a.serial], ["Installed", fmtDate(a.install_date)],
            ["Take-over cert.", fmtDate(a.toc)], ["Warranty expiry", fmtDate(a.warranty_expiry)]])),
      h("div", { class: "card next-svc " + dueClass, style: "grid-column:1/-1" },
        h("h3", {}, "Next service due"),
        h("div", { class: "next-svc-body" },
          h("div", { class: "next-svc-date" }, ns.due ? fmtDate(ns.due) : "—"),
          h("div", { class: "next-svc-note" }, dueNote)),
        h("p", { class: "hint", style: "margin:.6rem 0 0" },
          ns.base_108mo
            ? `108-month service completed ${fmtDate(ns.base_108mo)} + 6 months`
            : "Set once the 108-month major service is completed.")),
      smpCard());
  }

  function stateBadge(v) {
    const s = (v || "").trim();
    const cls = /alarm/i.test(s) ? "danger" : /warn/i.test(s) ? "warn"
      : /monitor/i.test(s) ? "info" : /normal/i.test(s) ? "ok" : "muted";
    return h("span", { class: "state-badge s-" + cls }, s || "—");
  }

  function smpCard() {
    const anyState = a.smp_gearbox || a.smp_generator || a.smp_main_bearing || a.smp_observations;
    return h("div", { class: "card", style: "grid-column:1/-1" },
      h("h3", {}, "Condition monitoring (SMP)"),
      h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
        anyState
          ? "From the KGH SMP Action Tracker" + (a.smp_data_date ? ` · last data ${fmtDate(a.smp_data_date)}` : "")
          : "No SMP data for this turbine."),
      anyState ? h("div", {},
        h("div", { class: "state-row" },
          h("div", { class: "state-cell" }, h("span", { class: "state-label" }, "Gearbox"), stateBadge(a.smp_gearbox)),
          h("div", { class: "state-cell" }, h("span", { class: "state-label" }, "Generator"), stateBadge(a.smp_generator)),
          h("div", { class: "state-cell" }, h("span", { class: "state-label" }, "Main bearing"), stateBadge(a.smp_main_bearing))),
        h("div", { class: "state-obs" },
          h("span", { class: "state-label" }, "Observations"),
          h("p", { style: "margin:.2rem 0 0;white-space:pre-line" },
            a.smp_observations || "None recorded."))) : null);
  }

  function recordPane(kind, records, emptyMsg) {
    records = records || [];
    const card = h("div", { class: "card" });
    if (kind === "service") {
      const done = records.filter(r => r.date).length;
      card.append(h("h3", {}, "Service completion dates"),
        h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
          `${done} of ${records.length} services completed. Dates from the KGH 2025 whiteboard.`));
    } else {
      const nc = records.filter(r => r.status && r.status !== "complete").length;
      card.append(h("h3", {}, "Retrofit records"),
        h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
          `${records.length} on record · ${nc} not completed. From the 25 KGH Retro whiteboard.`));
    }
    if (!records.length) { card.append(h("div", { class: "empty-state" }, emptyMsg)); return card; }
    const list = h("div", { class: "rec-list" });
    for (const r of records) {
      let right;
      if (r.date) right = h("span", { class: "rec-date" }, fmtDate(r.date));
      else if (r.status === "complete") right = h("span", { class: "rec-date" }, r.detail || "Completed");
      else if (r.status === "in_progress") right = h("span", { class: "rec-date wip" }, "In progress");
      else if (r.status === "outstanding") right = h("span", { class: "rec-date out" }, "Outstanding");
      else right = h("span", { class: "rec-date muted" }, r.detail || "Not recorded");
      list.append(h("div", { class: "rec-row" }, h("div", { class: "rec-name" }, r.name), right));
    }
    card.append(list);
    return card;
  }

  function componentsPane() {
    const comps = detail.components || [];
    const card = h("div", { class: "card" },
      h("h3", {}, "Component information"),
      h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
        comps.length ? "Traceability data from the Kilgallioch nacelle traceability record."
                     : ""));
    if (!comps.length) {
      card.append(h("div", { class: "empty-state" }, "No component information recorded for this turbine."));
      return card;
    }
    const list = h("div", { class: "rec-list" });
    for (const c of comps) {
      list.append(h("div", { class: "rec-row" },
        h("div", { class: "rec-name" }, c.name),
        c.date
          ? h("span", { class: "rec-date" }, fmtDate(c.date))
          : h("span", { class: "rec-val" }, c.detail || "—")));
    }
    card.append(list);
    return card;
  }

  function historyPane() {
    const wrap = h("div", {});
    const jobs = detail.jobs || [];
    if (jobs.length) {
      const jc = h("div", { class: "card" }, h("h3", {}, "Scheduled / open jobs"));
      for (const j of jobs) {
        jc.append(h("div", { class: "row-card", style: "cursor:default" },
          h("div", { class: "main" },
            h("div", { class: "name" }, j.title),
            h("div", { class: "meta" },
              (j.assignee_name ? "Assigned to " + j.assignee_name : "Unassigned") +
              (j.scheduled_date ? " · " + fmtDate(j.scheduled_date) : "") +
              (j.due_date ? " · due " + fmtDate(j.due_date) : ""))),
          h("div", { class: "aside" }, badge(j.priority), badge(j.status))));
      }
      wrap.append(jc);
    }

    const hist = detail.history || [];
    const card = h("div", { class: "card" }, h("h3", {}, "Work order history"));
    const types = [...new Set(hist.map(e => e.work_type).filter(Boolean))].sort();
    const filter = h("select", { style: "width:auto" },
      h("option", { value: "" }, `All work types (${hist.length})`),
      ...types.map(t => h("option", { value: t }, t)));
    const list = h("div", {});
    const draw = () => {
      clear(list);
      const rows = hist.filter(e => !filter.value || e.work_type === filter.value);
      if (!rows.length) { list.append(h("div", { class: "empty-state" }, "No work orders on record for this turbine.")); return; }
      for (const e of rows) {
        list.append(h("div", { class: "wo" },
          h("div", { class: "wo-head" },
            h("span", { class: "wo-date" }, fmtDate(e.date)),
            e.work_type ? h("span", { class: "wo-type" }, e.work_type) : null,
            e.service_order ? h("span", { class: "wo-so" }, "SO " + e.service_order) : null),
          h("div", { class: "wo-desc" }, esc(e.description)),
          e.technicians ? h("div", { class: "wo-techs" }, e.technicians) : null));
      }
    };
    filter.addEventListener("change", draw);
    card.append(
      h("p", { class: "hint", style: "margin:-.2rem 0 .7rem" },
        hist.length ? "From the Job Request “Scott & Stuart 2026” log." : ""),
      hist.length ? h("div", { class: "filter-row" }, filter) : null);
    card.append(list);
    if (!hist.length) card.append(h("div", { class: "empty-state" }, "No work order history for this turbine."));
    else draw();
    wrap.append(card);
    return wrap;
  }

  function pendingsPane() {
    const wrap = h("div", {});
    wrap.append(addPendingForm());
    if (!pendings.length) {
      wrap.append(h("div", { class: "empty-state" }, "No pending entries yet. Add the first one above."));
    }
    for (const p of pendings) {
      const actions = isAdmin ? h("div", { class: "btn-row", style: "margin-top:.7rem" },
        ...["SUBMITTED", "REVIEWED", "ACTIONED"].map(s =>
          h("button", {
            class: "btn sm" + (p.status === s ? " primary" : ""),
            onclick: async () => {
              try { await api("/pendings/" + p.id, { method: "PATCH", body: { status: s } });
                p.status = s; drawTab(); toast("Pending marked " + s.toLowerCase());
              } catch (e) { toast(e.message, true); }
            }
          }, s[0] + s.slice(1).toLowerCase()))) : null;

      wrap.append(h("div", { class: "pending" },
        h("div", { class: "head" },
          h("b", {}, p.author_name), "·", fmtWhen(p.created_at), badge(p.status)),
        h("div", {}, esc(p.note)),
        p.photos.length ? h("div", { class: "photos" }, ...p.photos.map(ph =>
          h("img", { src: ph.url, alt: ph.caption || "photo", loading: "lazy", onclick: () => lightbox(ph.url) }))) : null,
        actions));
    }
    return wrap;
  }

  function addPendingForm() {
    const note = h("textarea", { placeholder: "Describe what you found — condition, readings, actions taken…", required: true });
    const file = h("input", { type: "file", accept: "image/*", multiple: true, capture: "environment" });
    const preview = h("div", { class: "thumbs-preview" });
    const btn = h("button", { class: "btn primary", type: "submit" }, "Submit pending entry");
    const err = h("div", { class: "form-error" });
    file.addEventListener("change", () => {
      clear(preview);
      [...file.files].slice(0, 8).forEach(f => {
        const img = h("img", { alt: f.name });
        const r = new FileReader(); r.onload = () => img.src = r.result; r.readAsDataURL(f);
        preview.append(img);
      });
    });
    const form = h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault(); err.textContent = "";
      if (!note.value.trim()) { err.textContent = "A note is required."; return; }
      btn.disabled = true; btn.textContent = "Submitting…";
      const fd = new FormData();
      fd.append("note", note.value.trim());
      [...file.files].slice(0, 8).forEach(f => fd.append("photos", f));
      try {
        await api("/assets/" + id + "/pendings", { method: "POST", form: fd });
        pendings = (await api("/assets/" + id + "/pendings")).pendings;
        tabBar.querySelector('button[data-tab="Pendings"]').textContent = `Pendings (${pendings.length})`;
        drawTab();
        toast("Pending entry added");
      } catch (e2) {
        err.textContent = e2.message;
        btn.disabled = false; btn.textContent = "Submit pending entry";
      }
    }});
    form.append(
      h("h3", {}, "Add a pending entry"),
      h("div", { class: "field" }, h("label", {}, "Note"), note),
      h("div", { class: "field" }, h("label", {}, "Photos (optional)"), file,
        h("div", { class: "hint" }, "Up to 8 images. On a phone this opens the camera."), preview),
      btn, err);
    return form;
  }

  root.replaceChildren(shell("assets",
    h("div", { class: "crumb" }, h("a", { href: "#/assets" }, "← All assets")),
    h("div", { class: "page-head" },
      h("h1", {}, a.name === a.tag ? a.tag : a.tag + " — " + a.name),
      h("p", { class: "sub" }, a.type + " · " + a.location)),
    tabBar, tabWrap));
  drawTab();
}

/* ---------- planning board ---------- */
const REASON_LABEL = {
  "HOL in WD": "On holiday", "MED": "Medical appt", "SICK": "Off sick",
  "ABS": "Absent", "TRG": "Training", "PAT": "Paternity", "JURY": "Jury service",
};

async function viewPlanning() {
  if (State.user.role !== "ADMIN") { navigate("#/home"); return; }
  loading();
  let plan;
  let planDate = new Date().toISOString().slice(0, 10);

  async function load() { plan = await api("/plan?date=" + planDate); }
  async function mutate(body) {
    try { plan = await api("/plan", { method: "POST", body: { date: planDate, ...body } }); draw(); }
    catch (e) { toast(e.message, true); }
  }
  try { await load(); } catch (e) { return renderError(e); }

  const dateInput = h("input", { type: "date", value: planDate, style: "width:auto" });
  dateInput.addEventListener("change", async () => {
    planDate = dateInput.value || planDate;
    try { await load(); draw(); } catch (e) { toast(e.message, true); }
  });

  const rail = h("div", { class: "plan-rail" });
  const grid = h("div", { class: "team-table" });

  /* ---- unified pointer drag (job chips + tech chips) ---- */
  let dragging = false;
  function startDrag(e, payload, el) {
    if (e.button != null && e.button !== 0) return;
    const sx = e.clientX, sy = e.clientY;
    let ghost = null, zone = null;
    const move = (ev) => {
      if (!ghost && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 5) return;
      if (!ghost) {
        dragging = true; el.classList.add("dragging");
        ghost = el.cloneNode(true); ghost.classList.add("drag-ghost");
        ghost.style.width = el.offsetWidth + "px";
        document.body.append(ghost); document.body.style.cursor = "grabbing";
      }
      ghost.style.left = (ev.clientX + 8) + "px"; ghost.style.top = (ev.clientY + 8) + "px";
      ghost.style.display = "none";
      const under = document.elementFromPoint(ev.clientX, ev.clientY);
      ghost.style.display = "";
      const z = under && under.closest("[data-accept]");
      const ok = z && z.dataset.accept === payload.kind;
      if (z !== zone) {
        if (zone) zone.classList.remove("over", "reject");
        zone = z;
        if (zone) zone.classList.add(ok ? "over" : "reject");
      }
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
      if (ghost) ghost.remove();
      el.classList.remove("dragging");
      if (zone) {
        const accept = zone.dataset.accept, team = zone.dataset.team;
        zone.classList.remove("over", "reject");
        if (accept === payload.kind) {
          if (team === "rail") {
            if (payload.kind === "job" && payload.fromTeam) mutate({ op: "clear_job", team_no: +payload.fromTeam });
            if (payload.kind === "tech" && payload.fromTeam) mutate({ op: "remove_member", team_no: +payload.fromTeam, user_id: payload.id });
          } else if (payload.kind === "job") {
            mutate({ op: "set_job", team_no: +team, job_id: payload.id });
          } else if (payload.kind === "tech") {
            mutate({ op: "add_member", team_no: +team, user_id: payload.id });
          }
        }
      }
      setTimeout(() => { dragging = false; }, 0);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  }

  function techChip(t, fromTeam) {
    const off = t.available === false;
    const chip = h("div", { class: "tech-chip" + (off ? " off" : "") + (fromTeam ? " placed" : "") },
      h("span", { class: "tc-name" }, t.display_name),
      fromTeam
        ? h("button", { class: "chip-x", title: "Remove", onclick: () => mutate({ op: "remove_member", team_no: +fromTeam, user_id: t.id }) }, "×")
        : (off ? h("span", { class: "chip-reason" }, REASON_LABEL[t.reason] || t.reason) : null));
    if (!off) chip.addEventListener("pointerdown", (e) => startDrag(e, { kind: "tech", id: t.id, fromTeam }, chip));
    return chip;
  }

  function jobChip(j, fromTeam) {
    const card = h("div", { class: "task-chip" + (fromTeam ? " placed" : "") },
      h("div", { class: "jt" }, j.title),
      h("div", { class: "jm" }, badge(j.priority),
        j.asset_tag ? h("span", {}, j.asset_tag) : null,
        j.due_date ? h("span", {}, "due " + fmtDate(j.due_date)) : null,
        j.estimated_minutes ? h("span", {}, j.estimated_minutes + " min") : null),
      fromTeam ? h("button", { class: "chip-x", title: "Remove", onclick: () => mutate({ op: "clear_job", team_no: +fromTeam }) }, "×") : null);
    card.addEventListener("pointerdown", (e) => startDrag(e, { kind: "job", id: j.id, fromTeam }, card));
    return card;
  }

  function draw() {
    dateInput.value = planDate;
    clear(rail);
    const availWrap = h("div", { class: "chip-wrap", "data-accept": "tech", "data-team": "rail" },
      ...plan.available.map(t => techChip(t)));
    rail.append(
      h("label", { class: "rail-date" }, "Roster date", dateInput),
      h("h3", { class: "rail-head" },
        `Available technicians (${plan.available.length})`
        + (plan.assigned_count ? ` · ${plan.assigned_count} on teams` : "")),
      availWrap);
    if (plan.unavailable.length) {
      rail.append(
        h("h3", { class: "rail-head" }, `Unavailable (${plan.unavailable.length})`),
        h("div", { class: "chip-wrap" }, ...plan.unavailable.map(t => techChip(t))));
    }
    rail.append(
      h("h3", { class: "rail-head" }, `Tasks (${plan.backlog.length})`),
      h("div", { class: "chip-wrap", "data-accept": "job", "data-team": "rail" },
        plan.backlog.length ? null : h("span", { class: "slot-hint" }, "No outstanding tasks"),
        ...plan.backlog.map(j => jobChip(j))));

    clear(grid);
    grid.append(h("div", { class: "team-row team-head" },
      h("div", { class: "tcell tno" }, "Team"),
      h("div", { class: "tcell ttask" }, "Task"),
      h("div", { class: "tcell tmembers" }, "Technicians (min 2)")));
    for (const team of plan.teams) {
      const taskZone = h("div", { class: "tcell ttask dropzone2", "data-accept": "job", "data-team": team.team_no },
        team.job ? jobChip(team.job, team.team_no) : h("span", { class: "slot-hint" }, "Drop a task"));
      const memZone = h("div", { class: "tcell tmembers dropzone2", "data-accept": "tech", "data-team": team.team_no });
      team.members.forEach(m => memZone.append(techChip(m, team.team_no)));
      for (let i = team.members.length; i < 2; i++) memZone.append(h("span", { class: "slot-hint" }, "Drop a technician"));
      const needs = team.job && team.members.length < 2;
      grid.append(h("div", { class: "team-row" + (needs ? " needs" : "") },
        h("div", { class: "tcell tno" }, String(team.team_no),
          needs ? h("span", { class: "needs-tag" }, "needs 2") : null),
        taskZone, memZone));
    }
  }

  root.replaceChildren(shell("planning",
    h("div", { class: "page-head" }, h("h1", {}, "Planning"),
      h("p", { class: "sub" }, "Pick the date, then drag available technicians and tasks from the left into the 10 team rows. Every task needs at least two technicians.")),
    h("div", { class: "plan-layout" }, rail, h("div", { class: "team-wrap" }, grid))));
  draw();
}

/* ================================================================
   Router
   ================================================================ */
function renderError(e) {
  root.replaceChildren(shell("", h("div", { class: "empty-state" },
    h("h2", {}, "Something went wrong"), h("p", {}, e.message),
    h("button", { class: "btn", onclick: () => render() }, "Retry"))));
}

async function bootstrap() {
  if (!State.token) return false;
  try {
    const me = await api("/auth/me");
    State.user = me.user;
    State.modules = (await api("/modules")).modules;
    return true;
  } catch (_) {
    State.token = null; localStorage.removeItem("ops_token");
    return false;
  }
}

async function render() {
  const hash = location.hash || "#/home";
  const path = hash.replace(/^#/, "").split("?")[0];

  if (!State.user) {
    const ok = await bootstrap();
    if (!ok) { if (path !== "/login") { navigate("#/login"); return; } viewLogin(); return; }
  }
  if (path === "/login") { navigate("#/home"); return; }

  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "asset" && parts[1]) return viewAsset(parts[1]);
  switch (parts[0]) {
    case "assets": return viewAssets();
    case "planning": return viewPlanning();
    case "dashboard": return viewDashboard();
    case "home":
    default: return viewHome();
  }
}

window.addEventListener("hashchange", render);
render();
