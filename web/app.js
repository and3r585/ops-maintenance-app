/* ================================================================
   Site Portal - single page app (vanilla JS)
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
const todayISO = () => new Date().toISOString().slice(0, 10);
const addDaysISO = (iso, days) => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};

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

/* ---------- shared pending-entry card + workflow ---------- */
const PENDING_STATUSES = ["SUBMITTED", "REVIEWED", "COMPLETED"];
const cap = (s) => s ? s[0] + s.slice(1).toLowerCase() : s;

function pendingItem(p, opts) {
  opts = opts || {};
  const isAdmin = State.user.role === "ADMIN";
  const reload = opts.onChange || (() => {});
  const notePhotos = (p.photos || []).filter(x => x.kind !== "evidence");
  const evidence = (p.photos || []).filter(x => x.kind === "evidence");

  const meta = [];
  if (opts.showTurbine && p.turbine) {
    meta.push(p.asset_id
      ? h("a", { href: "#/asset/" + p.asset_id + "?tab=Pendings", class: "wo-so" }, p.turbine)
      : h("span", { class: "wo-so" }, p.turbine));
  }
  if (p.wo_code) meta.push(h("span", { class: "wo-so" }, "WO " + p.wo_code));
  if (p.priority != null) meta.push(h("span", { class: "pri-tag p" + p.priority }, "Priority " + p.priority));
  if (p.system) meta.push(h("span", {}, p.system));

  const card = h("div", { class: "pending" },
    h("div", { class: "head" },
      h("b", {}, p.author_name || "—"), "·", fmtWhen(p.created_at), badge(p.status)),
    meta.length ? h("div", { class: "pending-meta" }, ...meta) : null,
    h("div", { style: "white-space:pre-line" }, esc(p.note)),
    notePhotos.length ? h("div", { class: "photos" }, ...notePhotos.map(ph =>
      h("img", { src: ph.url, alt: "photo", loading: "lazy", onclick: () => lightbox(ph.url) }))) : null);

  // parts reservation
  if ((p.parts && p.parts.length) || p.parts_service_order) {
    card.append(h("div", { class: "parts-box" },
      h("div", { class: "parts-title" }, "Parts reserved",
        p.parts_service_order ? h("span", { class: "wo-so" }, "SO " + p.parts_service_order) : null),
      h("div", { class: "parts-list" },
        ...(p.parts || []).map(pt => h("div", { class: "part-line" },
          h("span", { class: "pl-num" }, esc(pt.part_number)),
          h("span", { class: "pl-qty" }, "×" + esc(pt.quantity)))))));
  }

  // completion evidence
  if (p.status === "COMPLETED") {
    card.append(h("div", { class: "evidence-box" },
      h("div", { class: "parts-title" }, "Completed",
        h("span", { class: "hint" }, (p.completed_by_name || "") +
          (p.completed_at ? " · " + fmtWhen(p.completed_at) : ""))),
      p.completed_note ? h("div", { style: "white-space:pre-line" }, esc(p.completed_note)) : null,
      evidence.length ? h("div", { class: "photos" }, ...evidence.map(ph =>
        h("img", { src: ph.url, alt: "evidence", loading: "lazy", onclick: () => lightbox(ph.url) }))) : null));
  }

  // actions
  const acts = h("div", { class: "btn-row", style: "margin-top:.7rem" });
  if (p.status === "SUBMITTED" && isAdmin) {
    acts.append(h("button", { class: "btn sm primary", onclick: () => patchStatus("REVIEWED") }, "Mark reviewed"));
  }
  if (p.status === "REVIEWED") {
    if (isAdmin) {
      acts.append(h("button", { class: "btn sm", onclick: () => card.append(partsForm()) },
        (p.parts && p.parts.length ? "Edit parts reserved" : "Parts reserved")));
      acts.append(h("button", { class: "btn sm ghost", onclick: () => patchStatus("SUBMITTED") }, "Revert to submitted"));
    }
    acts.append(h("button", { class: "btn sm primary", onclick: () => card.append(completeForm()) }, "Complete"));
  }
  if (p.status === "COMPLETED" && isAdmin) {
    acts.append(h("button", { class: "btn sm ghost", onclick: () => patchStatus("REVIEWED") }, "Reopen"));
  }
  if (acts.children.length) card.append(acts);
  return card;

  async function patchStatus(status) {
    try {
      await api("/pendings/" + p.id, { method: "PATCH", body: { status } });
      toast("Marked " + cap(status)); reload();
    } catch (e) { toast(e.message, true); }
  }

  function partsForm() {
    const box = h("div", { class: "inline-form" });
    const rows = h("div", {});
    const soInput = h("input", { placeholder: "Service order number", value: p.parts_service_order || "" });
    const addRow = (pn = "", qty = "") => {
      const r = h("div", { class: "part-row" },
        h("input", { placeholder: "Part number", value: pn, class: "pn" }),
        h("input", { placeholder: "Qty", value: qty, class: "qty" }),
        h("button", { class: "icon-btn", type: "button", onclick: () => r.remove() }, "×"));
      rows.append(r);
    };
    (p.parts && p.parts.length ? p.parts : [{}, {}]).forEach(pt => addRow(pt.part_number || "", pt.quantity || ""));
    const err = h("div", { class: "form-error" });
    box.append(
      h("label", {}, "Parts to reserve"),
      rows,
      h("button", { class: "btn sm ghost", type: "button", onclick: () => addRow() }, "+ Add part"),
      h("div", { class: "field", style: "margin-top:.6rem" }, h("label", {}, "Service order"), soInput),
      err,
      h("div", { class: "btn-row" },
        h("button", { class: "btn sm primary", type: "button", onclick: save }, "Save reservation"),
        h("button", { class: "btn sm ghost", type: "button", onclick: () => box.remove() }, "Cancel")));
    return box;
    async function save() {
      const list = [...rows.querySelectorAll(".part-row")].map(r => ({
        part_number: r.querySelector(".pn").value.trim(),
        quantity: r.querySelector(".qty").value.trim(),
      })).filter(x => x.part_number);
      if (!list.length) { err.textContent = "Add at least one part number."; return; }
      try {
        await api("/pendings/" + p.id + "/parts", { method: "POST",
          body: { service_order: soInput.value.trim(), parts: list } });
        toast("Parts reserved"); reload();
      } catch (e) { err.textContent = e.message; }
    }
  }

  function completeForm() {
    const box = h("div", { class: "inline-form" });
    const comment = h("textarea", { placeholder: "What was done — evidence of completion…", required: true });
    const fileIn = h("input", { type: "file", accept: "image/*", multiple: true, capture: "environment" });
    const preview = h("div", { class: "thumbs-preview" });
    const err = h("div", { class: "form-error" });
    fileIn.addEventListener("change", () => {
      clear(preview);
      [...fileIn.files].slice(0, 8).forEach(f => {
        const img = h("img", {}); const rd = new FileReader();
        rd.onload = () => img.src = rd.result; rd.readAsDataURL(f); preview.append(img);
      });
    });
    box.append(
      h("label", {}, "Completion comment (required)"), comment,
      h("div", { class: "field", style: "margin-top:.5rem" },
        h("label", {}, "Evidence photo (required)"), fileIn,
        h("div", { class: "hint" }, "At least one photo. On a phone this opens the camera."), preview),
      err,
      h("div", { class: "btn-row" },
        h("button", { class: "btn sm primary", type: "button", onclick: submit }, "Mark completed"),
        h("button", { class: "btn sm ghost", type: "button", onclick: () => box.remove() }, "Cancel")));
    return box;
    async function submit() {
      if (!comment.value.trim()) { err.textContent = "A completion comment is required."; return; }
      if (!fileIn.files.length) { err.textContent = "At least one evidence photo is required."; return; }
      const fd = new FormData();
      fd.append("comment", comment.value.trim());
      [...fileIn.files].slice(0, 8).forEach(f => fd.append("photos", f));
      try {
        await api("/pendings/" + p.id + "/complete", { method: "POST", form: fd });
        toast("Pending completed"); reload();
      } catch (e) { err.textContent = e.message; }
    }
  }
}

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
const MODULE_ROUTE = { assets: "#/assets", planning: "#/planning", dashboard: "#/dashboard", explorer: "#/explorer" };
function topbar(active) {
  const navItems = State.modules.map((m) =>
    h("a", {
      href: MODULE_ROUTE[m.key] || "#/home",
      class: active === m.key ? "active" : "",
    }, m.name));
  return h("header", { class: "topbar" },
    h("div", { class: "brand", onclick: () => navigate("#/home"), style: "cursor:pointer" },
      h("span", { class: "spark" }), "Site Portal"),
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
    h("div", { class: "brand" }, h("span", { class: "spark" }), "Site Portal"),
    h("div", { class: "tagline" }, "Sign in to continue"),
    h("div", { class: "field" }, h("label", { for: "u" }, "Username"), u),
    h("div", { class: "field" }, h("label", { for: "p" }, "Password"), p),
    btn, errBox,
    h("div", { class: "seed-hint" },
      "Use the username and password issued to you."));
  root.replaceChildren(h("div", { class: "login-wrap" }, form));
}

function viewHome() {
  const isAdmin = State.user.role === "ADMIN";
  const tiles = h("div", { class: "tiles" });
  tiles.append(
    h("button", { class: "tile", onclick: () => navigate("#/dashboard") },
      h("span", { class: "ico" }, "📊"),
      h("h3", {}, "Site dashboard"),
      h("p", {}, "Open pendings, the next service due dates, and retrofits still outstanding across the site.")),
    h("button", { class: "tile", onclick: () => navigate("#/assets") },
      h("span", { class: "ico" }, "🔧"),
      h("h3", {}, "View asset information"),
      h("p", {}, isAdmin
        ? "Browse the asset register, review full details and history, and edit records."
        : "Browse the asset register, review full details and history, and log pending observations with photos.")));
  if (isAdmin) {
    tiles.append(
      h("button", { class: "tile", onclick: () => navigate("#/planning") },
        h("span", { class: "ico" }, "📅"),
        h("h3", {}, "Planning"),
        h("p", {}, "Build the day's team plan — drag available technicians and tasks into 10 team rows.")),
      h("button", { class: "tile", onclick: () => navigate("#/explorer") },
        h("span", { class: "ico" }, "🗃️"),
        h("h3", {}, "Data Explorer"),
        h("p", {}, "View every asset tab as one editable table, push approved changes to the database, and export a dated change report.")));
  }
  root.replaceChildren(shell("home",
    h("div", { class: "page-head" },
      h("h1", {}, "Welcome, " + State.user.display_name.split(" ")[0]),
      h("p", { class: "sub" }, isAdmin
        ? "You have planner access. Choose an area to work in."
        : "Choose an area to work in.")),
    tiles));
}

/* ---------- site dashboard (all signed-in users) ---------- */
async function viewDashboard() {
  loading();
  let d;
  try { d = await api("/dashboard"); }
  catch (e) { return renderError(e); }

  const daysAway = (iso) => Math.round((new Date(iso) - new Date()) / 86400000);

  const kpi = (num, label, sub, route) => h("div", {
    class: "card kpi clickable", onclick: () => navigate(route),
    title: "Open full list",
  }, h("div", { class: "kpi-num" }, num),
     h("div", { class: "kpi-label" }, label),
     h("div", { class: "kpi-sub" }, sub),
     h("div", { class: "kpi-more" }, "View list →"));

  const pendCard = kpi(d.open_pendings, "Open pending entries",
    Object.entries(d.pendings_by_status || {})
      .map(([s, c]) => `${c} ${s.toLowerCase()}`).join(" · ") || "None", "#/pendings");
  const svcCard = kpi(d.service_count, "Turbines with a next-service date",
    d.next_services[0] ? "Next: " + d.next_services[0].tag + " on " + fmtDate(d.next_services[0].due) : "—",
    "#/services");
  const retroCard = kpi(d.incomplete_retrofit_count, "Retrofit items not completed",
    `${(d.incomplete_retrofits || []).length} campaigns affected`, "#/retrofits");

  const svcList = h("div", { class: "card" },
    h("h3", {}, "Next service due", h("a", { href: "#/services", class: "hint" }, "view all →")),
    h("p", { class: "hint", style: "margin:-.2rem 0 .6rem" }, "108-month service completion + 6 months."));
  if (!d.next_services.length) svcList.append(h("div", { class: "empty-state" }, "No service dates on record."));
  else svcList.append(serviceRows(d.next_services.slice(0, 10)));

  const retroList = h("div", { class: "card" },
    h("h3", {}, "Retrofits not completed", h("a", { href: "#/retrofits", class: "hint" }, "view all →")));
  if (!d.incomplete_retrofits.length) retroList.append(h("div", { class: "empty-state" }, "All retrofits complete."));
  else {
    const list = h("div", { class: "rec-list" });
    for (const g of d.incomplete_retrofits.slice(0, 8)) {
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
    h("div", { class: "page-head" },
      h("h1", {}, "Site dashboard"),
      h("p", { class: "sub" }, "Kilgallioch — live figures across all 96 turbines.")),
    h("div", { class: "kpi-row" }, pendCard, svcCard, retroCard),
    h("div", { class: "grid cols-2", style: "margin-top:1rem" }, svcList, retroList)));
}

function serviceRows(list) {
  const wrap = h("div", { class: "rec-list" });
  const daysAway = (iso) => Math.round((new Date(iso) - new Date()) / 86400000);
  for (const s of list) {
    const dd = daysAway(s.due);
    wrap.append(h("div", { class: "rec-row", style: "cursor:pointer",
      onclick: () => navigate("#/assets?q=" + s.tag) },
      h("div", { class: "rec-name" }, s.tag,
        h("span", { class: "hint", style: "margin-left:.5rem" },
          dd < 0 ? `overdue ${-dd}d` : dd === 0 ? "today" : `in ${dd}d`)),
      h("span", { class: "rec-date " + (dd < 0 ? "out" : dd <= 30 ? "wip" : "") }, fmtDate(s.due))));
  }
  return wrap;
}

/* ---------- dashboard drill-downs ---------- */
async function viewPendingsList() {
  loading();
  const want = decodeURIComponent((location.hash.split("?status=")[1] || "")).toUpperCase();
  let data;
  try { data = await api("/pendings" + (want ? "?status=" + want : "")); }
  catch (e) { return renderError(e); }

  const counts = data.counts || { SUBMITTED: 0, REVIEWED: 0, COMPLETED: 0 };
  const total = counts.SUBMITTED + counts.REVIEWED + counts.COMPLETED;

  const chip = (label, val) => h("button", {
    class: "filter-chip" + ((want || "") === val ? " active" : ""),
    onclick: () => navigate("#/pendings" + (val ? "?status=" + val : "")),
  }, label);

  const filterLabel = want ? want[0] + want.slice(1).toLowerCase() : "All";
  const exportBtn = h("button", { class: "btn sm",
    onclick: () => exportPendings(exportBtn, want) },
    `⭳ Export ${filterLabel.toLowerCase()} (CSV)`);

  const list = h("div", {});
  const reload = () => viewPendingsList();
  if (!data.pendings.length) list.append(h("div", { class: "empty-state" }, "No pending entries match."));
  for (const p of data.pendings) list.append(pendingItem(p, { showTurbine: true, onChange: reload }));

  root.replaceChildren(shell("dashboard",
    h("div", { class: "crumb" }, h("a", { href: "#/dashboard" }, "← Dashboard")),
    h("div", { class: "page-head dash-head" },
      h("div", {},
        h("h1", {}, "Pending entries"),
        h("p", { class: "sub" }, data.pendings.length + " shown"
          + (want ? " of " + total : ""))),
      exportBtn),
    h("div", { class: "filter-row" },
      chip(`All (${total})`, ""), chip(`Submitted (${counts.SUBMITTED})`, "SUBMITTED"),
      chip(`Reviewed (${counts.REVIEWED})`, "REVIEWED"),
      chip(`Completed (${counts.COMPLETED})`, "COMPLETED")),
    list));
}

async function viewServicesList() {
  loading();
  let d;
  try { d = await api("/dashboard"); }
  catch (e) { return renderError(e); }
  const card = h("div", { class: "card" },
    h("h3", {}, "All turbines — next service due"),
    h("p", { class: "hint", style: "margin:-.2rem 0 .6rem" },
      d.next_services.length + " turbines · 108-month completion + 6 months, soonest first."));
  card.append(d.next_services.length
    ? serviceRows(d.next_services)
    : h("div", { class: "empty-state" }, "No service dates on record."));
  root.replaceChildren(shell("dashboard",
    h("div", { class: "crumb" }, h("a", { href: "#/dashboard" }, "← Dashboard")),
    h("div", { class: "page-head" }, h("h1", {}, "Service due dates")),
    card));
}

async function viewRetrofitsList() {
  loading();
  let d;
  try { d = await api("/dashboard"); }
  catch (e) { return renderError(e); }
  const wrap = h("div", {});
  wrap.append(h("div", { class: "page-head" }, h("h1", {}, "Retrofits not completed"),
    h("p", { class: "sub" }, `${d.incomplete_retrofit_count} items across ${d.incomplete_retrofits.length} campaigns`)));
  for (const g of d.incomplete_retrofits) {
    const out = g.outstanding || [], wip = g.in_progress || [];
    const card = h("div", { class: "card" },
      h("h3", {}, g.name, h("span", { class: "rec-date out" }, out.length + wip.length)));
    if (out.length) card.append(h("div", { class: "retro-tags" },
      h("span", { class: "state-label" }, "Outstanding"),
      ...out.map(t => h("a", { href: "#/assets?q=" + t, class: "turb-tag" }, t))));
    if (wip.length) card.append(h("div", { class: "retro-tags" },
      h("span", { class: "state-label" }, "In progress"),
      ...wip.map(t => h("a", { href: "#/assets?q=" + t, class: "turb-tag wip" }, t))));
    wrap.append(card);
  }
  root.replaceChildren(shell("dashboard",
    h("div", { class: "crumb" }, h("a", { href: "#/dashboard" }, "← Dashboard")), wrap));
}

/* fetch a file endpoint with auth and hand it to the browser as a download */
async function downloadFile(path, btn, okMsg, fallbackName) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Preparing…";
  try {
    const res = await fetch("/api" + path, { headers: { Authorization: "Bearer " + State.token } });
    if (!res.ok) {
      let msg = "Download failed (" + res.status + ")";
      try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : fallbackName;
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (okMsg) toast(okMsg);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

function exportPendings(btn, status) {
  return downloadFile("/pendings/export" + (status ? "?status=" + status : ""),
    btn, "Pendings exported", "pendings.csv");
}

/* ---------- data explorer (admin) ---------- */
async function viewExplorer() {
  if (State.user.role !== "ADMIN") { navigate("#/home"); return; }
  loading();
  let cats;
  try { cats = (await api("/explorer/categories")).categories; }
  catch (e) { return renderError(e); }

  let activeCat = decodeURIComponent((location.hash.split("?cat=")[1] || "").split("&")[0])
    || cats[0].key;
  if (!cats.some(c => c.key === activeCat)) activeCat = cats[0].key;

  const edits = new Map();           // record id -> { id, tag, name, field, oldValue, newValue }
  const gridWrap = h("div", { class: "explorer-grid" });
  const footer = h("div", { class: "explorer-bar", style: "display:none" });

  const catTabs = h("div", { class: "filter-row" }, ...cats.map(c =>
    h("button", {
      class: "filter-chip" + (c.key === activeCat ? " active" : ""),
      onclick: () => {
        if (edits.size && !confirm("Discard " + edits.size + " unsaved change(s) and switch table?")) return;
        activeCat = c.key; edits.clear();
        window.location.hash = "#/explorer?cat=" + c.key;
        loadGrid();
      },
    }, c.label)));

  function refreshFooter() {
    if (!edits.size) { footer.style.display = "none"; clear(footer); return; }
    clear(footer);
    footer.style.display = "flex";
    footer.append(
      h("span", { class: "explorer-bar-count" },
        edits.size + " unsaved change" + (edits.size === 1 ? "" : "s")),
      h("button", { class: "btn sm ghost", onclick: () => { edits.clear(); loadGrid(); } }, "Discard"),
      h("button", { class: "btn sm primary", onclick: reviewChanges }, "Review & save"));
  }

  function reviewChanges() {
    const list = [...edits.values()];
    const rows = list.map(e => h("tr", {},
      h("td", {}, e.tag),
      h("td", {}, e.name),
      h("td", { class: "old" }, e.oldValue || "—"),
      h("td", {}, "→"),
      h("td", { class: "new" }, e.newValue || "(cleared)")));
    const table = h("table", { class: "review-table" },
      h("thead", {}, h("tr", {},
        h("th", {}, "Turbine"), h("th", {}, "Record"),
        h("th", {}, "Was"), h("th", {}, ""), h("th", {}, "New"))),
      h("tbody", {}, ...rows));
    const saveBtn = h("button", { class: "btn primary" }, "Approve & save " + list.length);
    const backdrop = h("div", { class: "modal-backdrop" });
    const close = () => backdrop.remove();
    saveBtn.onclick = async () => {
      saveBtn.disabled = true; saveBtn.textContent = "Saving…";
      try {
        const r = await api("/explorer/apply", { method: "POST",
          body: { changes: list.map(e => ({ id: e.id, value: e.newValue })) } });
        close();
        if (r.errors && r.errors.length)
          toast(r.applied + " saved · " + r.errors.length + " rejected", true);
        else toast(r.applied + " change" + (r.applied === 1 ? "" : "s") + " saved to the database");
        edits.clear();
        loadGrid();
      } catch (e) {
        saveBtn.disabled = false; saveBtn.textContent = "Approve & save " + list.length;
        toast(e.message, true);
      }
    };
    backdrop.append(h("div", { class: "modal" },
      h("h3", {}, "Review changes"),
      h("p", { class: "hint" }, "These edits will be written to the SQLite database and recorded in the change log."),
      h("div", { class: "review-scroll" }, table),
      h("div", { class: "btn-row", style: "justify-content:flex-end;margin-top:1rem" },
        h("button", { class: "btn ghost", onclick: close }, "Cancel"), saveBtn)));
    backdrop.addEventListener("click", (ev) => { if (ev.target === backdrop) close(); });
    document.body.append(backdrop);
  }

  async function loadGrid() {
    clear(gridWrap);
    gridWrap.append(h("div", { class: "spinner" }, "Loading…"));
    let data;
    try { data = await api("/explorer/matrix?category=" + activeCat); }
    catch (e) { clear(gridWrap); gridWrap.append(h("div", { class: "empty-state" }, e.message)); return; }
    [...catTabs.children].forEach(b => b.classList.toggle("active", b.textContent === data.label));

    const isDate = data.value_field === "occurred_on";
    const table = h("table", { class: "explorer-table" });
    table.append(h("thead", {}, h("tr", {},
      h("th", { class: "sticky-col" }, "Turbine"),
      ...data.columns.map(c => h("th", {}, c)))));
    const tb = h("tbody", {});
    for (const row of data.rows) {
      const tr = h("tr", {});
      tr.append(h("td", { class: "sticky-col" }, row.tag));
      row.cells.forEach((cell, i) => {
        const td = h("td", {});
        // no record row for this turbine/column -> leave the cell blank and non-editable
        if (!cell || !cell.id) { tr.append(td); return; }
        const name = data.columns[i];
        const inp = h("input", { class: "explorer-cell" });
        inp.value = cell.value || "";     // blank in SQLite -> blank cell
        if (isDate) {
          // An empty <input type="date"> renders inconsistently across browsers
          // (dd/mm/yyyy in some, *today's date* in others). Keep empty cells as a
          // plain text box that shows nothing, and swap in the real date picker
          // only while the cell is focused.
          inp.type = inp.value ? "date" : "text";
          inp.autocomplete = "off";
          inp.addEventListener("focus", () => {
            if (inp.type !== "date") inp.type = "date";
            try { if (inp.showPicker) inp.showPicker(); } catch (_) {}
          });
          inp.addEventListener("blur", () => { if (!inp.value) inp.type = "text"; });
        }
        inp.addEventListener("change", () => {
          const nv = inp.value.trim();
          const ov = cell.value || "";
          if (nv === ov) { edits.delete(cell.id); td.classList.remove("cell-dirty"); }
          else {
            edits.set(cell.id, { id: cell.id, tag: row.tag, name,
              oldValue: ov, newValue: nv });
            td.classList.add("cell-dirty");
          }
          refreshFooter();
        });
        td.append(inp);
        tr.append(td);
      });
      tb.append(tr);
    }
    table.append(tb);
    clear(gridWrap);
    gridWrap.append(table);
    refreshFooter();
  }

  // --- change report panel ---
  const to = h("input", { type: "date", value: todayISO() });
  const from = h("input", { type: "date", value: addDaysISO(todayISO(), -30) });
  const reportBtn = h("button", { class: "btn" }, "⭳ Download change report (XLSX)");
  reportBtn.onclick = () => {
    if (!from.value || !to.value) { toast("Pick a from and to date", true); return; }
    downloadFile("/explorer/changes?from=" + from.value + "&to=" + to.value,
      reportBtn, "Change report downloaded", "change-report.xlsx");
  };
  const exportBtn = h("button", { class: "btn sm" }, "⭳ Export this table (CSV)");
  exportBtn.onclick = () => downloadFile("/explorer/export?category=" + activeCat,
    exportBtn, "Table exported", "explorer.csv");

  root.replaceChildren(shell("explorer",
    h("div", { class: "page-head" },
      h("h1", {}, "Data Explorer"),
      h("p", { class: "sub" }, "Every asset tab as one table for all 96 turbines. Edit cells, review, then push approved changes to the database.")),
    h("div", { class: "card" },
      h("h3", {}, "Change report",
        h("span", { class: "hint" }, "one worksheet per tab that changed in the window")),
      h("p", { class: "hint", style: "margin:-.2rem 0 .7rem" },
        "Queries the change log for every edit made between the two dates and returns an Excel workbook with a tab for each asset tab that was updated."),
      h("div", { class: "toolbar" },
        h("label", { style: "display:flex;align-items:center;gap:.4rem;font-weight:400" }, "From", from),
        h("label", { style: "display:flex;align-items:center;gap:.4rem;font-weight:400" }, "To", to),
        reportBtn)),
    h("div", { class: "explorer-head" }, catTabs, exportBtn),
    gridWrap,
    footer));
  loadGrid();
}

/* ---------- assets list ---------- */
async function viewAssets() {
  loading();
  let assets;
  try { assets = (await api("/assets")).assets; }
  catch (e) { return renderError(e); }

  const q0 = decodeURIComponent((location.hash.split("?q=")[1] || "").split("&")[0]);
  const search = h("input", { class: "search", placeholder: "Search turbine or location…", type: "search", value: q0 });
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
  const tabs = ["Details", "Service dates", "HV history", "Stat history", "Retrofits", "Blades", "Components", "History", "Pendings"];
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
      "Completion dates from the HV tab of the KGH Virtual Whiteboard."
      + (isAdmin ? " The 2026/27 and 2027/28 campaigns have no source date yet — add one to record completion." : ""),
      detail.hv, "No HV maintenance recorded for this turbine.", isAdmin),
    "Stat history": () => datePane("Statutory inspection history",
      "Completion dates from the Stats tab of the KGH Virtual Whiteboard.",
      detail.stat, "No statutory inspections recorded for this turbine."),
    "Retrofits": () => recordPane("retrofit", detail.retrofits, "No retrofits recorded for this turbine."),
    "Blades": bladesPane,
    "Components": componentsPane,
    "History": historyPane,
    "Pendings": pendingsPane,
  };

  /* date widget. Admins get an inline editor (PATCH /records/:id -> SQLite);
     everyone else sees the value read-only, and a blank stays blank. */
  function dateCell(rec, reload) {
    if (!isAdmin || !rec.id) {
      return h("span", { class: "rec-date" + (rec.date ? "" : " muted") },
        rec.date ? fmtDate(rec.date) : "");
    }
    const wrap = h("span", { class: "rec-date-edit" });
    const render = () => {
      clear(wrap);
      if (rec.date) {
        wrap.append(
          h("span", { class: "rec-date" }, fmtDate(rec.date)),
          h("button", { class: "date-edit-link", title: "Change date", onclick: openInput }, "edit"));
      } else {
        wrap.append(h("button", { class: "btn sm", onclick: openInput }, "＋ Add date"));
      }
    };
    const openInput = () => {
      clear(wrap);
      const inp = h("input", { type: "date", value: rec.date || "", style: "width:auto" });
      const save = async (val) => {
        try {
          const r = await api("/records/" + rec.id, { method: "PATCH", body: { occurred_on: val } });
          rec.date = r.occurred_on;
          if (r.status) rec.status = r.status;
          toast(val ? "Date saved" : "Date cleared");
          if (reload) reload(); else render();
        } catch (e) { toast(e.message, true); render(); }
      };
      inp.addEventListener("change", () => save(inp.value));
      wrap.append(inp, h("button", { class: "btn sm ghost", onclick: () => render() }, "Cancel"));
      if (rec.date) wrap.append(h("button", { class: "btn sm ghost", onclick: () => save("") }, "Clear"));
      inp.focus();
    };
    render();
    return wrap;
  }

  function bladesPane() {
    const wrap = h("div", {});
    const blades = detail.blades || [];
    const card = h("div", { class: "card" }, h("h3", {}, "Blade inspection"),
      h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
        "Drone inspection date from the KGH 2025 database."
        + (isAdmin ? " Add a date to record a completed inspection." : "")));
    if (!blades.length) card.append(h("div", { class: "empty-state" }, "No blade records for this turbine."));
    else {
      const list = h("div", { class: "rec-list" });
      for (const b of blades) {
        list.append(h("div", { class: "rec-row" },
          h("div", { class: "rec-name" }, b.name),
          dateCell(b, drawTab)));
      }
      card.append(list);
    }
    wrap.append(card);

    // read-only blade configuration from the traceability record
    const comps = (detail.components || []).filter(c =>
      /^Blade (type|[ABC] S\/N|bearing)/.test(c.name));
    if (comps.length) {
      const cfg = h("div", { class: "card" }, h("h3", {}, "Blade configuration"),
        h("div", { class: "rec-list" }, ...comps.map(c => h("div", { class: "rec-row" },
          h("div", { class: "rec-name" }, c.name),
          h("span", { class: "rec-val" }, c.detail || "—")))));
      wrap.append(cfg);
    }
    return wrap;
  }

  function datePane(title, hint, records, emptyMsg, editable) {
    records = (records || []).slice().sort((x, y) => String(y.date || "").localeCompare(String(x.date || "")));
    const done = records.filter(r => r.date).length;
    const card = h("div", { class: "card" }, h("h3", {}, title),
      h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
        records.length ? `${done} of ${records.length} completed. ${hint}` : hint));
    if (!records.length) { card.append(h("div", { class: "empty-state" }, emptyMsg)); return card; }
    const list = h("div", { class: "rec-list" });
    for (const r of records) {
      let right;
      if (editable && r.id) right = dateCell(r, drawTab);
      else if (r.date) right = h("span", { class: "rec-date" }, fmtDate(r.date));
      else right = h("span", { class: "rec-date muted" }, r.detail || "");
      list.append(h("div", { class: "rec-row" }, h("div", { class: "rec-name" }, r.name), right));
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
            ["Type", a.type], ["Location", a.location]])),
      h("div", { class: "card" }, h("h3", {}, "Equipment"),
        dl([["Manufacturer", a.manufacturer], ["Model", a.model], ["Family", a.family],
            ["Serial number", a.serial], ["Installed", fmtDate(a.install_date)],
            ["Take-over cert.", fmtDate(a.toc)], ["Warranty expiry", fmtDate(a.warranty_expiry)]])),
      h("div", { class: "card defect-card" + (a.defect ? " flagged" : ""), style: "grid-column:1/-1" },
        h("h3", {}, "Defect / operational issue"),
        h("p", { style: "margin:0;" + (a.defect ? "" : "color:var(--text-soft)") },
          a.defect || "No defects or issues affecting work or operation.")),
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
          `${done} of ${records.length} services completed. Dates from the KGH 2025 database`
          + (isAdmin ? " — add a date to record a completed service." : ".")));
    } else {
      const nc = records.filter(r => !r.date).length;
      card.append(h("h3", {}, "Retrofit records"),
        h("p", { class: "hint", style: "margin:-.2rem 0 .8rem" },
          `${records.length} on record · ${nc} not completed. From the 25 KGH Retro whiteboard.`));
    }
    if (!records.length) { card.append(h("div", { class: "empty-state" }, emptyMsg)); return card; }
    const list = h("div", { class: "rec-list" });
    for (const r of records) {
      let right;
      if (kind === "service") {
        right = dateCell(r, drawTab);
      } else if (r.id) {
        // retrofit: status badge (when not already implied by a date) + the date/add-date control
        const badge = !r.date && r.status === "in_progress" ? h("span", { class: "rec-date wip" }, "In progress")
          : !r.date && r.status === "outstanding" ? h("span", { class: "rec-date out" }, "Outstanding")
          : null;
        right = h("span", { class: "rec-date-edit" }, badge, dateCell(r, drawTab));
      } else right = h("span", { class: "rec-date muted" }, r.detail || "");
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

  async function reloadPendings() {
    pendings = (await api("/assets/" + id + "/pendings")).pendings;
    const btn = tabBar.querySelector('button[data-tab="Pendings"]');
    if (btn) btn.textContent = `Pendings (${pendings.length})`;
    drawTab();
  }

  function pendingsPane() {
    const wrap = h("div", {});
    wrap.append(addPendingForm());
    const open = pendings.filter(p => p.status !== "COMPLETED");
    const done = pendings.filter(p => p.status === "COMPLETED");
    if (!pendings.length) {
      wrap.append(h("div", { class: "empty-state" }, "No pending entries yet. Add the first one above."));
    }
    for (const p of open) wrap.append(pendingItem(p, { onChange: reloadPendings }));
    if (done.length) {
      wrap.append(h("h3", { style: "margin:1.4rem 0 .6rem;color:var(--text-soft)" }, `Completed (${done.length})`));
      for (const p of done) wrap.append(pendingItem(p, { onChange: reloadPendings }));
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
        await reloadPendings();
        toast("Pending entry added — status Submitted");
      } catch (e2) {
        err.textContent = e2.message;
        btn.disabled = false; btn.textContent = "Submit pending entry";
      }
    }});
    form.append(
      h("h3", {}, "Add a pending entry"),
      h("p", { class: "hint", style: "margin:-.2rem 0 .7rem" },
        "New entries start as Submitted. An admin reviews them; a technician then completes with a photo and comment."),
      h("div", { class: "field" }, h("label", {}, "Note"), note),
      h("div", { class: "field" }, h("label", {}, "Photos (optional)"), file,
        h("div", { class: "hint" }, "Up to 8 images. On a phone this opens the camera."), preview),
      btn, err);
    return form;
  }

  const goto = (target) => navigate("#/asset/" + target.id + "?tab=" + encodeURIComponent(activeTab));
  const navBtns = h("div", { class: "asset-nav" },
    h("button", { class: "icon-btn", title: "Previous: " + detail.prev.tag,
      onclick: () => goto(detail.prev) }, "‹"),
    h("button", { class: "icon-btn", title: "Next: " + detail.next.tag,
      onclick: () => goto(detail.next) }, "›"));

  root.replaceChildren(shell("assets",
    h("div", { class: "crumb" }, h("a", { href: "#/assets" }, "← All assets")),
    h("div", { class: "page-head asset-head" },
      h("div", {},
        h("h1", {}, a.name === a.tag ? a.tag : a.tag + " — " + a.name),
        h("p", { class: "sub" }, a.type + " · " + a.location)),
      navBtns),
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
            mutate({ op: "set_job", team_no: +team, job_id: payload.id });   // job_id = pending entry id
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
      h("div", { class: "jm" },
        j.priority != null ? h("span", { class: "pri-tag p" + j.priority }, "Priority " + j.priority) : null,
        j.asset_tag ? h("span", {}, j.asset_tag) : null,
        j.wo_code ? h("span", {}, "WO " + j.wo_code) : null),
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
    const shown = plan.backlog.length;
    const total = plan.backlog_total != null ? plan.backlog_total : shown;
    rail.append(
      h("h3", { class: "rail-head" }, `Tasks (${shown}${total > shown ? " of " + total : ""})`),
      total > shown ? h("p", { class: "slot-hint", style: "margin:.1rem 0 .4rem" },
        "Reviewed pending entries, highest priority first. Review more in Site Dashboard → pendings.") : null,
      h("div", { class: "chip-wrap", "data-accept": "job", "data-team": "rail" },
        shown ? null : h("span", { class: "slot-hint" }, "No reviewed pending entries to schedule"),
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
    case "pendings": return viewPendingsList();
    case "services": return viewServicesList();
    case "retrofits": return viewRetrofitsList();
    case "explorer": return viewExplorer();
    case "home":
    default: return viewHome();
  }
}

window.addEventListener("hashchange", render);
render();
