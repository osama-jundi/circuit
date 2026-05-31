/* ============================================================
   app.js — Stage 3b
   ============================================================
   Sections:
     1. Setup            (constants, helpers)
     2. Boot             (fetch data, build cytoscape)
     3. Styling          (how nodes & edges look)
     4. Panel details    (right side panel, click handlers)
     5. Search           (filter & jump to a panel/paulos)
     6. Status editing   (POST to /api/edge/<sn>/status)
     7. Legend filter    (click legend to fade non-matching)
   ============================================================ */

cytoscape.use(cytoscapeDagre);

// -------- 1. Setup --------
let cy = null;                // the cytoscape instance
let STATUSES = [];            // ['Energized', 'Issued', 'Not Issued']
let COLORS = {};              // status -> color hex
let currentFileName = "";     // name of the active project (for the title block)

let CURRENT_FILE = null;      // id of the file (diagram) being viewed
let FILE_LIST = [];           // [{id, name, ...}] cached for the file tab bar
let lastRevision = -1;        // file revision we last rendered (for polling)
const CSRF = document.body.dataset.csrf || "";   // CSRF token for mutating calls

const $  = (id) => document.getElementById(id);
const el = (tag, props = {}, children = []) => {
  const e = document.createElement(tag);
  Object.assign(e, props);
  children.forEach(c => e.appendChild(typeof c === "string"
    ? document.createTextNode(c) : c));
  return e;
};

/** Show a brief message at the bottom of the screen. */
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove("show"), 1800);
}

// -------- 2. Boot --------
/** Fetch the graph data and (re)build the map. Safe to call repeatedly,
 *  e.g. after a new file is uploaded. */
function boot() {
  if (!CURRENT_FILE) { showEmptyState(true); $("loading").style.display = "none"; return; }
  fetch(`/api/files/${CURRENT_FILE}/graph`)
  .then(r => {
    if (r.status === 401) { window.location = "/login"; return Promise.reject("auth"); }
    return r.json();
  })
  .then(data => {
    $("loading").style.display = "none";

    // Project missing/unreadable -> show the empty-state prompt and stop.
    if (!data.loaded) {
      showEmptyState(true);
      return;
    }
    showEmptyState(false);
    lastRevision = data.revision;
    currentFileName = data.name || "";
    updateStatsText(data.stats);

    // Rebuild from scratch if a previous map exists (re-upload).
    if (cy) { cy.destroy(); cy = null; }

    STATUSES = data.statuses;
    COLORS   = data.colors;

    cy = cytoscape({
      container: $("cy"),
      elements: [...data.elements.nodes, ...data.elements.edges],
      style: graphStyle(),
      layout: { name: "preset" },   // positions computed in applySldLayout()
      minZoom: 0.1,
      maxZoom: 3,
    });

    buildSections(cy);                       // tag boards/leaves + edge direction
    applySldLayout(cy);                      // auto starting layout
    cy.nodes("[isBoard = 0]").ungrabify();   // only busbars are draggable; loads follow
    setupDragging(cy);                       // drag a bus -> its loads move with it
    restoreLayout(cy);                       // apply saved 2D arrangement if any
    setupLayoutControls(cy);                 // Reset button + drag hint in the legend
    cy.fit(undefined, 12);

    // ---- Wire up interactions ----
    cy.on("tap", "node[!isGroup]", e => openPanelDetails(e.target.id()));
    cy.on("tap", "edge", e => promptEdgeStatus(e.target));

    // Click on empty canvas = close panel + clear highlight
    cy.on("tap", e => {
      if (e.target === cy) {
        closeDetails();
        clearHighlight();
      }
    });

    setupSearch(data.elements.nodes, data.elements.edges);
    setupLegendFilter();
    setupDetailsClose();
    renderFindings(data.findings);
    renderStatusSummary(cy);
    renderTitleBlock(cy);

    console.log(
      `Map ready: ${cy.nodes().length} panels, ${cy.edges().length} feeders.`
    );
    window.cy = cy;
  })
  .catch(err => {
    $("loading").textContent = "Failed to load: " + err.message;
    console.error(err);
  });
}

/** Show a feeder count next to each status in the legend (a small title-block
 *  style summary). Recomputed each boot so it tracks the loaded file. */
function renderStatusSummary(cy) {
  const counts = {};
  cy.edges().forEach(e => {
    const s = e.data("status");
    counts[s] = (counts[s] || 0) + 1;
  });
  document.querySelectorAll(".legend-item").forEach(item => {
    const status = item.dataset.filter;
    let badge = item.querySelector(".legend-count");
    if (!badge) {
      badge = el("span", { className: "legend-count" });
      badge.style.cssText = "margin-left:auto;font-size:11px;color:#667085;font-weight:600";
      item.appendChild(badge);
    }
    badge.textContent = counts[status] || 0;
  });
}

const MY_ROLE = document.body.dataset.role || "";
const MY_NAME = document.body.dataset.username || "";
const IS_ADMIN = MY_ROLE === "admin";

function showEmptyState(show) {
  const es = $("empty-state");
  if (!es) return;
  es.style.display = show ? "block" : "none";
  if (show) {
    // Non-admins can't upload, so tailor the empty-state message/button.
    const h = es.querySelector("h2"), p = es.querySelector("p"), b = $("empty-upload-btn");
    if (!IS_ADMIN) {
      if (h) h.textContent = "No diagram available yet";
      if (p) p.innerHTML = "An administrator needs to upload the feeders file before it can be viewed.";
      if (b) b.style.display = "none";
    }
  }
}

function updateStatsText(stats) {
  const t = $("stats-text");
  if (t && stats) t.textContent = `${stats.panels} panels · ${stats.feeders} feeders`;
}

// -------- Upload handling (admin only) --------
function setupUpload() {
  const input = $("upload-input");
  if (!input) return;
  const trigger = () => input.click();
  const uploadBtn = $("upload-btn");           // absent for non-admins
  if (uploadBtn) uploadBtn.addEventListener("click", trigger);
  const emptyBtn = $("empty-upload-btn");
  if (emptyBtn && IS_ADMIN) emptyBtn.addEventListener("click", trigger);

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const suggested = file.name.replace(/\.[^.]+$/, "");
    const name = (prompt("Name this diagram:", suggested) || "").trim() || suggested;
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    toast("Uploading " + file.name + "…");
    fetch(`/api/projects/${PROJECT_ID}/files`, { method: "POST", body: form,
            headers: { "X-CSRFToken": CSRF } })
      .then(r => r.json().then(body => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) { toast(body.error || "Upload failed", true); return; }
        toast(`Added "${body.name}" — ${body.stats.panels} panels, ${body.stats.feeders} feeders`);
        loadFiles(body.id);   // refresh tabs and open the new file
      })
      .catch(err => toast("Upload error: " + err.message, true))
      .finally(() => { input.value = ""; });  // allow re-uploading same file
  });
}

// ============================================================
//  FILES within this project: switcher tabs + live polling
// ============================================================
const PROJECT_ID = parseInt(document.body.dataset.projectId, 10);
const CAN_MANAGE = document.body.dataset.canManage === "yes";
const LAST_FILE_KEY = `nhm_sld_last_file_${PROJECT_ID}`;

/** Fetch this project's files, (re)render the tab bar, and pick one to show.
 *  `preferId` forces a selection (e.g. a just-uploaded file). */
function loadFiles(preferId) {
  return fetch(`/api/projects/${PROJECT_ID}/files`)
    .then(r => { if (r.status === 401) { window.location = "/login"; return Promise.reject("auth"); } return r.json(); })
    .then(({ files }) => {
      FILE_LIST = files || [];
      renderFileTabs();
      if (!FILE_LIST.length) {
        CURRENT_FILE = null;
        if (cy) { cy.destroy(); cy = null; }
        showEmptyState(true);
        $("loading").style.display = "none";
        return;
      }
      const ids = FILE_LIST.map(f => f.id);
      let target = preferId;
      if (!ids.includes(target)) target = CURRENT_FILE;
      if (!ids.includes(target)) {
        const saved = parseInt(localStorage.getItem(LAST_FILE_KEY), 10);
        target = ids.includes(saved) ? saved : FILE_LIST[0].id;
      }
      if (target !== CURRENT_FILE) switchFile(target);
      else renderFileTabs();
    })
    .catch(() => {});
}

function switchFile(fid) {
  if (fid === CURRENT_FILE && cy) return;
  CURRENT_FILE = fid;
  try { localStorage.setItem(LAST_FILE_KEY, String(fid)); } catch (e) {}
  renderFileTabs();
  if (cy) { cy.destroy(); cy = null; }
  $("loading").style.display = "block";
  $("loading").textContent = "Building map…";
  const nm = $("tb-name"); if (nm) nm.value = "";   // let the file name fill in
  boot();
}

function renderFileTabs() {
  const wrap = $("file-tabs");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!FILE_LIST.length) {
    wrap.appendChild(el("span", { className: "pb-empty",
      textContent: CAN_MANAGE ? "No files yet — click “➕ Add xlsx”." : "No files in this project yet." }));
    return;
  }
  FILE_LIST.forEach(f => {
    const tab = el("div", {
      className: "file-tab" + (f.id === CURRENT_FILE ? " active" : ""),
      title: `Uploaded by ${f.created_by || "?"}`,
    });
    tab.appendChild(el("span", { textContent: f.name }));
    tab.onclick = (ev) => { if (!ev.target.classList.contains("pt-act")) switchFile(f.id); };
    if (CAN_MANAGE) {
      const ren = el("span", { className: "pt-act", textContent: "✎", title: "Rename file" });
      ren.onclick = () => renameFile(f);
      const del = el("span", { className: "pt-act", textContent: "🗑", title: "Delete file" });
      del.onclick = () => deleteFile(f);
      tab.appendChild(ren); tab.appendChild(del);
    }
    wrap.appendChild(tab);
  });
}

function renameFile(f) {
  const name = (prompt("Rename file:", f.name) || "").trim();
  if (!name || name === f.name) return;
  apiJson(`/api/files/${f.id}`, "PATCH", { name })
    .then(() => { toast("Renamed to " + name); loadFiles(CURRENT_FILE); })
    .catch(err => toast(err, true));
}

function deleteFile(f) {
  if (!confirm(`Delete file "${f.name}"?\nThis removes its diagram, status edits and history. This cannot be undone.`)) return;
  apiJson(`/api/files/${f.id}`, "DELETE")
    .then(() => {
      toast(`Deleted "${f.name}"`);
      if (f.id === CURRENT_FILE) CURRENT_FILE = null;
      loadFiles();
    })
    .catch(err => toast(err, true));
}

// ---- Live polling: pick up other users' edits + new/renamed/deleted files ----
const POLL_MS = 5000;
function startPolling() { setInterval(pollTick, POLL_MS); }

function pollTick() {
  if (document.hidden) return;
  // 1) Keep the file list fresh.
  fetch(`/api/projects/${PROJECT_ID}/files`).then(r => r.ok ? r.json() : null).then(res => {
    if (!res) return;
    const fresh = res.files || [];
    if (JSON.stringify(fresh.map(f => [f.id, f.name])) !==
        JSON.stringify(FILE_LIST.map(f => [f.id, f.name]))) {
      FILE_LIST = fresh;
      const ids = fresh.map(f => f.id);
      if (CURRENT_FILE && !ids.includes(CURRENT_FILE)) {
        toast("This file was removed", true);
        CURRENT_FILE = null;
        loadFiles();
      } else {
        renderFileTabs();
      }
    }
  }).catch(() => {});

  // 2) Pull live status changes for the file we're viewing.
  if (!CURRENT_FILE || !cy) return;
  fetch(`/api/files/${CURRENT_FILE}/state`).then(r => r.ok ? r.json() : null).then(state => {
    if (!state || state.revision === lastRevision) return;
    lastRevision = state.revision;
    let changed = 0;
    Object.entries(state.statuses).forEach(([sn, status]) => {
      const edge = cy.edges(`[sn = ${parseInt(sn, 10)}]`);
      if (!edge.empty() && edge.data("status") !== status) {
        applyStatusToCanvas(parseInt(sn, 10), status, COLORS[status] || "#999");
        changed++;
      }
    });
    if (changed) {
      renderStatusSummary(cy);
      renderTitleBlock(cy);
      toast(`Updated — ${changed} change${changed > 1 ? "s" : ""} from others`);
    }
  }).catch(() => {});
}

setupUpload();
setupTitleBlock();
setupModals();
setupLegendToggle();
loadFiles();
startPolling();

/** On small screens the legend/key is a slide-in drawer toggled by a button. */
function setupLegendToggle() {
  const btn = $("legend-toggle"), legend = document.querySelector("aside.legend");
  if (!btn || !legend) return;
  btn.addEventListener("click", () => legend.classList.toggle("open"));
  // Tapping the map closes the drawer.
  const cyEl = $("cy");
  if (cyEl) cyEl.addEventListener("click", () => legend.classList.remove("open"));
}

// -------- History & Users modals --------
function openModal(title, builder) {
  $("modal-title").textContent = title;
  const body = $("modal-body");
  body.innerHTML = '<div class="modal-empty">Loading…</div>';
  $("modal-overlay").classList.add("open");
  builder(body);
}
function closeModal() { $("modal-overlay").classList.remove("open"); }

function setupModals() {
  $("modal-close").addEventListener("click", closeModal);
  $("modal-overlay").addEventListener("click", (e) => {
    if (e.target === $("modal-overlay")) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  const hBtn = $("history-btn");
  if (hBtn) hBtn.addEventListener("click", () => openModal("Change history", renderHistory));
  const uBtn = $("users-btn");          // admin only
  if (uBtn) uBtn.addEventListener("click", () => openModal("Manage users", renderUsers));
  const dBtn = $("dashboard-btn");
  if (dBtn) dBtn.addEventListener("click",
    () => openModal("Energization progress", renderDashboard));
  const acc = $("account-link");
  if (acc) acc.addEventListener("click", () => openModal("My account", renderAccount));
}

/** Change-my-password form (any logged-in user). */
function renderAccount(body) {
  body.innerHTML = `
    <div style="max-width:360px">
      <p style="font-size:13px;color:#475467;margin:0 0 14px">
        Signed in as <strong>${escapeHtml(MY_NAME)}</strong>. Change your password below.</p>
      <label style="font-size:11px;color:#475467">Current password</label>
      <input id="ac-cur" type="password" autocomplete="current-password"
             style="width:100%;padding:8px 10px;margin:4px 0 12px;border:1px solid #d0d5dd;border-radius:6px">
      <label style="font-size:11px;color:#475467">New password (min 6 characters)</label>
      <input id="ac-new" type="password" autocomplete="new-password"
             style="width:100%;padding:8px 10px;margin:4px 0 12px;border:1px solid #d0d5dd;border-radius:6px">
      <label style="font-size:11px;color:#475467">Confirm new password</label>
      <input id="ac-new2" type="password" autocomplete="new-password"
             style="width:100%;padding:8px 10px;margin:4px 0 16px;border:1px solid #d0d5dd;border-radius:6px">
      <button class="mini-btn" id="ac-save">Change password</button>
    </div>`;
  $("ac-save").addEventListener("click", () => {
    const cur = $("ac-cur").value, nw = $("ac-new").value, nw2 = $("ac-new2").value;
    if (nw.length < 6) { toast("New password must be at least 6 characters", true); return; }
    if (nw !== nw2) { toast("New passwords don't match", true); return; }
    apiJson("/api/account/password", "POST", { current_password: cur, new_password: nw })
      .then(() => { toast("Password changed"); closeModal(); })
      .catch(err => toast(err, true));
  });
}

/** Progress dashboard for the current file, computed live from the diagram:
 *  overall status split + a per-switchboard breakdown. */
function renderDashboard(body) {
  if (!cy) { body.innerHTML = '<div class="modal-empty">Open a diagram first.</div>'; return; }

  const seg = (n, total, color, label) => {
    const pct = total ? (n / total * 100) : 0;
    return `<div class="dash-seg" style="width:${pct.toFixed(2)}%;background:${color}" title="${label}: ${n}"></div>`;
  };
  const bar = (counts, total) =>
    `<div class="dash-bar">${STATUSES.map(s => seg(counts[s] || 0, total, COLORS[s], s)).join("")}</div>`;

  // Overall counts across every feeder.
  const overall = {}; let total = 0;
  cy.edges().forEach(e => { const s = e.data("status"); overall[s] = (overall[s] || 0) + 1; total++; });
  const en = overall["Energized"] || 0;
  const pct = total ? Math.round(en / total * 100) : 0;

  const legend = STATUSES.map(s =>
    `<span class="dash-key"><i style="background:${COLORS[s]}"></i>${s}: <b>${overall[s] || 0}</b></span>`).join("");

  // Per-switchboard: count the feeders LEAVING each board, by status.
  const boards = cy.nodes("[isBoard = 1]");
  const rows = boards.map(b => {
    const out = b.outgoers("edge");
    const c = {}; let t = 0;
    out.forEach(e => { const s = e.data("status"); c[s] = (c[s] || 0) + 1; t++; });
    return { id: b.id(), c, t, en: c["Energized"] || 0 };
  }).filter(r => r.t > 0).sort((a, b) => (b.en / b.t || 0) - (a.en / a.t || 0));

  const boardRows = rows.map(r => `<tr>
      <td title="${escapeHtml(r.id)}">${escapeHtml(r.id)}</td>
      <td style="width:46%">${bar(r.c, r.t)}</td>
      <td style="text-align:right;white-space:nowrap">${Math.round(r.en / r.t * 100)}% · ${r.en}/${r.t}</td>
    </tr>`).join("");

  body.innerHTML = `
    <div class="dash-head">
      <div class="dash-pct">${pct}%</div>
      <div>
        <div class="dash-pct-label">energized — ${en} of ${total} feeders</div>
        ${bar(overall, total)}
        <div class="dash-legend">${legend}</div>
      </div>
    </div>
    <div class="dash-section">By switchboard (${rows.length})</div>
    <table class="grid dash-table"><tbody>${boardRows || '<tr><td>No boards</td></tr>'}</tbody></table>`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

/** Human-readable description of one audit entry. */
function describeEntry(e) {
  switch (e.action) {
    case "status_change":
      return { what: `Feeder SN ${e.sn}` + (e.paulos ? ` (${e.paulos})` : "") +
                     (e.source && e.target ? ` · ${e.source} → ${e.target}` : ""),
               detail: `<span class="change-old">${e.old_value || "?"}</span> → ` +
                       `<span class="change-new">${e.new_value || "?"}</span>` };
    case "upload":
      return { what: `Uploaded ${e.paulos || "workbook"}`, detail: e.new_value || "" };
    case "export":      return { what: "Exported spreadsheet", detail: "" };
    case "user_create": return { what: `Created user ${e.paulos}`, detail: `role: ${e.new_value}` };
    case "user_role":   return { what: `Changed role of ${e.paulos}`, detail: `→ ${e.new_value}` };
    case "user_password": return { what: `Reset password of ${e.paulos}`, detail: "" };
    case "user_delete": return { what: `Deleted user ${e.paulos}`, detail: "" };
    default:            return { what: e.action, detail: e.new_value || "" };
  }
}

function renderHistory(body) {
  if (!CURRENT_FILE) {
    body.innerHTML = '<div class="modal-empty">Open a project to see its history.</div>';
    return;
  }
  fetch(`/api/files/${CURRENT_FILE}/history`)
    .then(r => r.json())
    .then(({ entries }) => {
      if (!entries || !entries.length) {
        body.innerHTML = '<div class="modal-empty">No changes recorded yet.</div>';
        return;
      }
      const rows = entries.map(e => {
        const d = describeEntry(e);
        return `<tr>
          <td style="white-space:nowrap">${fmtTime(e.ts)}</td>
          <td><strong>${escapeHtml(e.username || "—")}</strong></td>
          <td>${escapeHtml(d.what)}</td>
          <td>${d.detail}</td>
        </tr>`;
      }).join("");
      body.innerHTML =
        `<table class="grid"><thead><tr>
           <th>When</th><th>Who</th><th>Change</th><th>Detail</th>
         </tr></thead><tbody>${rows}</tbody></table>`;
    })
    .catch(() => { body.innerHTML = '<div class="modal-empty">Could not load history.</div>'; });
}

function renderUsers(body) {
  fetch("/api/users")
    .then(r => r.json())
    .then(({ users }) => {
      const form = `
        <div class="form-row">
          <div><label>Username</label><input id="nu-name" autocomplete="off"></div>
          <div><label>Password</label><input id="nu-pass" type="password" autocomplete="new-password"></div>
          <div><label>Role</label>
            <select id="nu-role"><option value="user">user</option><option value="admin">admin</option></select>
          </div>
          <button id="nu-add">Add user</button>
        </div>`;
      const adminCount = (users || []).filter(u => u.role === "admin").length;
      const rows = (users || []).map(u => {
        const isMe = u.username === MY_NAME;
        // The last remaining admin can't be demoted or deleted (would lock
        // everyone out). The server enforces this too; this just hides it.
        const lastAdmin = u.role === "admin" && adminCount <= 1;
        const dis = " disabled style='opacity:.4;cursor:not-allowed'";
        const lockTip = lastAdmin ? ' title="The last admin can\'t be changed — create another admin first"' : "";
        return `<tr data-user="${escapeHtml(u.username)}">
          <td><strong>${escapeHtml(u.username)}</strong>${isMe ? " (you)" : ""}</td>
          <td><span class="pill ${u.role}">${u.role}</span></td>
          <td style="white-space:nowrap">${fmtTime(u.created_at)}</td>
          <td style="text-align:right;white-space:nowrap"${lockTip}>
            <button class="mini-btn ghost act-role"${lastAdmin ? dis : ""}>${u.role === "admin" ? "Make user" : "Make admin"}</button>
            <button class="mini-btn ghost act-pw">Reset password</button>
            <button class="mini-btn danger act-del"${(isMe || lastAdmin) ? dis : ""}>Delete</button>
          </td>
        </tr>`;
      }).join("");
      body.innerHTML = form +
        `<table class="grid"><thead><tr>
           <th>User</th><th>Role</th><th>Created</th><th></th>
         </tr></thead><tbody>${rows}</tbody></table>`;
      wireUserActions(body);
    })
    .catch(() => { body.innerHTML = '<div class="modal-empty">Could not load users.</div>'; });
}

function wireUserActions(body) {
  $("nu-add").addEventListener("click", () => {
    const username = $("nu-name").value.trim();
    const password = $("nu-pass").value;
    const role = $("nu-role").value;
    if (!username || !password) { toast("Username and password required", true); return; }
    apiJson("/api/users", "POST", { username, password, role })
      .then(() => { toast(`Added ${username}`); renderUsers(body); })
      .catch(err => toast(err, true));
  });

  body.querySelectorAll("tr[data-user]").forEach(tr => {
    const username = tr.dataset.user;
    const roleBtn = tr.querySelector(".act-role");
    if (roleBtn && !roleBtn.disabled) roleBtn.addEventListener("click", () => {
      const newRole = roleBtn.textContent === "Make admin" ? "admin" : "user";
      apiJson(`/api/users/${encodeURIComponent(username)}`, "PATCH", { role: newRole })
        .then(() => { toast(`${username} is now ${newRole}`); renderUsers(body); })
        .catch(err => toast(err, true));
    });
    const pwBtn = tr.querySelector(".act-pw");
    if (pwBtn) pwBtn.addEventListener("click", () => {
      const pw = prompt(`New password for ${username}:`);
      if (!pw) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "PATCH", { password: pw })
        .then(() => toast(`Password reset for ${username}`))
        .catch(err => toast(err, true));
    });
    const delBtn = tr.querySelector(".act-del");
    if (delBtn && !delBtn.disabled) delBtn.addEventListener("click", () => {
      if (!confirm(`Delete user ${username}? This cannot be undone.`)) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "DELETE")
        .then(() => { toast(`Deleted ${username}`); renderUsers(body); })
        .catch(err => toast(err, true));
    });
  });
}

/** fetch JSON, rejecting with the server's error message on non-2xx. */
function apiJson(url, method, payload) {
  const opts = { method, headers: { "X-CSRFToken": CSRF } };
  if (payload !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(payload);
  }
  return fetch(url, opts).then(r =>
    r.json().catch(() => ({})).then(body => {
      if (!r.ok) throw (body.error || `Request failed (${r.status})`);
      return body;
    }));
}

// -------- Title block (drawing-style corner panel) --------
function setupTitleBlock() {
  const toggle = $("tb-toggle");
  if (toggle) toggle.addEventListener("click",
    () => $("titleblock").classList.toggle("collapsed"));
}

/** Build the corner title block: project name, date, panel/feeder counts,
 *  a status breakdown and a transformer/main-board status table — the kind
 *  of summary that lives in the corner of a real SLD sheet. */
function renderTitleBlock(cy) {
  const tb = $("titleblock"), body = $("tb-body");
  if (!tb || !body) return;
  tb.classList.add("show");

  const nameInput = $("tb-name");
  if (nameInput && !nameInput.value)
    nameInput.value = currentFileName || "Single Line Diagram";

  // Feeder status counts
  const counts = {}; let total = 0;
  cy.edges().forEach(e => { const s = e.data("status"); counts[s] = (counts[s] || 0) + 1; total++; });

  const dot = c => {
    const d = el("span", { className: "tb-dot" });
    d.style.background = c; return d;
  };
  const statusRow = (label, val, color) => {
    const lab = el("div", { className: "tb-label" });
    if (color) lab.appendChild(dot(color));
    lab.appendChild(el("span", { className: "name", textContent: label }));
    return el("div", { className: "tb-row" }, [lab, el("span", { className: "tb-val", textContent: String(val) })]);
  };

  body.innerHTML = "";

  // Meta line: date + totals
  const meta = el("div", { className: "tb-meta" }, [
    el("span", { textContent: new Date().toLocaleDateString() }),
    el("span", { textContent: `${cy.nodes("[!isGroup]").length} panels · ${total} feeders` }),
  ]);
  body.appendChild(meta);

  // Status breakdown
  body.appendChild(el("div", { className: "tb-section", textContent: "Feeder status" }));
  STATUSES.forEach(s => body.appendChild(statusRow(s, counts[s] || 0, COLORS[s])));

  // Transformer / main-board energization table (like the TX list on a sheet)
  let mains = cy.nodes('[isBoard = 1]').filter(n => n.data("type") === "TX");
  let heading = "Transformers";
  if (mains.length === 0) {
    mains = cy.nodes('[isBoard = 1]').filter(n => n.incomers("node").length === 0);
    heading = "Main boards";
  }
  if (mains.length) {
    body.appendChild(el("div", { className: "tb-section", textContent: heading }));
    const arr = mains.map(n => n).sort((a, b) => a.id().localeCompare(b.id()));
    arr.slice(0, 8).forEach(n => {
      const inc = n.incomers("edge");
      const st = inc.length ? inc[0].data("status") : "—";
      const lab = el("div", { className: "tb-label" }, [
        dot(COLORS[st] || "#cbd2da"),
        el("span", { className: "name", textContent: n.id(), title: n.id() }),
      ]);
      body.appendChild(el("div", { className: "tb-row" }, [lab,
        el("span", { className: "tb-val", textContent: st, style: "font-size:10px;color:#667085" })]));
    });
    if (arr.length > 8)
      body.appendChild(el("div", { className: "tb-more", textContent: `…and ${arr.length - 8} more` }));
  }
}


// -------- 3. Styling --------
function graphStyle() {
  return [
    // ---- Busbar boards: thin colored rail, name below ----
    {
      selector: "node[isBoard = 1]",
      style: {
        "shape": "rectangle",
        "background-color": "data(bg)",
        "background-opacity": 1,
        "border-color": "#1f2d3a",
        "border-width": 1,
        "label": "data(label)",
        "font-size": "10px",
        "font-weight": "bold",
        "text-valign": "bottom",
        "text-margin-y": 4,
        "text-halign": "center",
        "color": "#10202e",
        "width": "data(barW)",
        "height": "data(barH)",
        "padding": "8px",
        "text-max-width": "700px",
        "text-wrap": "none",
      }
    },
    // ---- Leaf loads: box in a row above the bar, filled by feeder status ----
    {
      selector: "node[isBoard = 0]",
      style: {
        "shape": "round-rectangle",
        "background-color": "data(loadFill)",
        "border-color": "data(loadBorder)",
        "border-width": 2,
        "label": "data(label)",
        "font-size": "10px",
        "text-wrap": "wrap",
        "text-max-width": "96px",
        "text-valign": "center",
        "text-halign": "center",
        "color": "#1c2b38",
        "width": "104px",
        "height": "62px",
      }
    },
    // ---- Section groups: invisible (we color the rail itself) ----
    {
      selector: "node[?isGroup]",
      style: { "background-opacity": 0, "border-opacity": 0, "label": "" }
    },

    // ---- Feeder stubs to loads (short vertical drop, up to the load) ----
    {
      selector: "edge[tgtBoard = 0]",
      style: {
        "width": 1.4,
        "line-color": "#5b6b7b",
        "curve-style": "taxi",
        "taxi-direction": "upward",
        "taxi-turn": "50%",
        "target-arrow-shape": "none",
      }
    },
    {
      selector: 'edge[tgtBoard = 0][status = "Not Issued"]',
      style: { "line-style": "dashed", "line-dash-pattern": [5, 4] }
    },
    // ---- Inter-board cables (down) — colored by their SOURCE board ----
    {
      selector: "edge[tgtBoard = 1]",
      style: {
        "width": 2.6,
        "line-color": "data(cableColor)",
        "curve-style": "taxi",
        "taxi-direction": "downward",
        "taxi-turn": "20px",
        "target-arrow-shape": "triangle",
        "target-arrow-color": "data(cableColor)",
        "arrow-scale": 0.8,
        "target-endpoint": "0% -50%",
      }
    },
    {
      selector: 'edge[tgtBoard = 1][status = "Not Issued"]',
      style: { "line-style": "dashed", "line-dash-pattern": [6, 4] }
    },

    // ---- Faded (when filter or focus is on) ----
    {
      selector: ".faded",
      style: { "opacity": 0.15 }
    },
  ];
}


// -------- 4. Panel details (right sidebar) --------
let NOTES = {};          // sn -> {note, by, at} for the current file
let CURRENT_NODE = null; // id of the panel shown in the details sidebar

function openPanelDetails(nodeId) {
  CURRENT_NODE = nodeId;
  highlightNode(nodeId);
  Promise.all([
    fetch(`/api/files/${CURRENT_FILE}/node/` + encodeURIComponent(nodeId))
      .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status))),
    fetch(`/api/files/${CURRENT_FILE}/notes`).then(r => r.ok ? r.json() : { notes: {} })
      .catch(() => ({ notes: {} })),
  ]).then(([info, n]) => { NOTES = n.notes || {}; renderDetails(info); })
    .catch(() => toast("Couldn't load panel details", true));
}

function renderDetails(info) {
  const aside = $("details");
  const node  = cy.getElementById(info.id);
  const type  = node.data("type") || "OTHER";

  $("details-kind").textContent  = type;
  $("details-title").textContent = info.id;

  const body = $("details-body");
  body.innerHTML = "";   // clear previous

  // --- Focus tools ---
  const tools = el("div", { className: "detail-tools" });
  const iso = el("button", { className: "tool-btn", textContent: "🔍 Isolate" });
  iso.onclick = () => isolateNode(info.id);
  const showAll = el("button", { className: "tool-btn", textContent: "⤢ Show all" });
  showAll.onclick = () => { if (cy) cy.elements().removeClass("faded"); };
  tools.appendChild(iso); tools.appendChild(showAll);
  body.appendChild(tools);

  // --- Incoming feeders ---
  body.appendChild(el("h3", {textContent: `Fed From (${info.incoming.length})`}));
  if (info.incoming.length === 0) {
    body.appendChild(el("div", {className: "empty",
      textContent: "Nothing — this is a root / source."}));
  } else {
    info.incoming.forEach(e => body.appendChild(feederRow(e, "from")));
  }

  // --- Outgoing feeders ---
  body.appendChild(el("h3", {textContent: `Feeds To (${info.outgoing.length})`}));
  if (info.outgoing.length === 0) {
    body.appendChild(el("div", {className: "empty",
      textContent: "Nothing — end-of-line panel."}));
  } else {
    if (info.outgoing.length > 1) body.appendChild(bulkBar(info.outgoing));
    info.outgoing.forEach(e => body.appendChild(feederRow(e, "to")));
  }

  const wasClosed = !aside.classList.contains("open");
  aside.classList.add("open");

  // The map canvas just shrank — tell cytoscape to re-measure and re-center.
  if (wasClosed) {
    setTimeout(() => {
      cy.resize();
      cy.animate({ center: { eles: node }, duration: 250 });
    }, 50);
  }
}

/** Bulk-edit bar: set the same status on all of a board's outgoing feeders. */
function bulkBar(outgoing) {
  const bar = el("div", { className: "bulk-row" });
  bar.appendChild(el("span", { className: "bulk-label",
    textContent: `Set all ${outgoing.length} to` }));
  const sel = el("select");
  STATUSES.forEach(s => sel.appendChild(el("option", { value: s, textContent: s })));
  const btn = el("button", { className: "bulk-btn", textContent: "Apply" });
  btn.onclick = () => bulkSetStatus(outgoing.map(e => e.sn), sel.value);
  bar.appendChild(sel); bar.appendChild(btn);
  return bar;
}

function bulkSetStatus(sns, status) {
  apiJson(`/api/files/${CURRENT_FILE}/bulk-status`, "POST", { sns, status })
    .then(res => {
      if (typeof res.revision === "number") lastRevision = res.revision;
      sns.forEach(sn => applyStatusToCanvas(sn, status, COLORS[status]));
      renderStatusSummary(cy); renderTitleBlock(cy);
      toast(`Set ${res.changed} feeder${res.changed !== 1 ? "s" : ""} → ${status}`);
      if (CURRENT_NODE) openPanelDetails(CURRENT_NODE);   // refresh the panel
    })
    .catch(err => toast(err, true));
}

/** Fade everything except this board, its feeders and its direct neighbours. */
function isolateNode(id) {
  if (!cy) return;
  const n = cy.getElementById(id);
  const keep = n.union(n.connectedEdges()).union(n.connectedEdges().connectedNodes());
  cy.elements().addClass("faded");
  keep.removeClass("faded");
}

/** Build one "feeder row" for the details panel. */
function feederRow(edgeData, direction) {
  const otherPanel = direction === "from" ? edgeData.source : edgeData.target;
  const label      = direction === "from" ? "← " : "→ ";

  const row = el("div", {className: "feeder-row"});
  row.appendChild(el("div", {}, [
    el("span", {className: "arrow", textContent: label}),
    el("span", {className: "target", textContent: otherPanel}),
  ]));
  row.appendChild(el("div", {
    className: "sn",
    textContent: `SN ${edgeData.sn} · ${edgeData.paulos}`,
  }));

  // Status selector
  const statusRow = el("div", {className: "status-row"});
  const dot = el("span", {className: "status-dot"});
  dot.style.background = COLORS[edgeData.status] || "#999";
  const sel = el("select");
  STATUSES.forEach(s => {
    const opt = el("option", {value: s, textContent: s});
    if (s === edgeData.status) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.onchange = () => {
    const newStatus = sel.value;
    changeEdgeStatus(edgeData.sn, newStatus, ok => {
      if (ok) {
        dot.style.background = COLORS[newStatus];
        edgeData.status = newStatus;
        toast(`Updated SN ${edgeData.sn} → ${newStatus}`);
      }
    });
  };
  statusRow.appendChild(dot);
  statusRow.appendChild(sel);
  row.appendChild(statusRow);

  // Note: show existing note or an "add note" affordance; editable inline.
  const noteWrap = el("div", { className: "note-wrap" });
  const renderNote = () => {
    noteWrap.innerHTML = "";
    const n = NOTES[edgeData.sn];
    if (n && n.note) {
      noteWrap.appendChild(el("div", { className: "note-text", title: `by ${n.by || "?"}` },
        ["📝 " + n.note]));
      const ed = el("button", { className: "note-edit", textContent: "edit" });
      ed.onclick = editNote;
      noteWrap.appendChild(ed);
    } else {
      const add = el("button", { className: "note-edit", textContent: "📝 Add note" });
      add.onclick = editNote;
      noteWrap.appendChild(add);
    }
  };
  const editNote = () => {
    noteWrap.innerHTML = "";
    const ta = el("textarea", { className: "note-input", rows: 2 });
    ta.value = (NOTES[edgeData.sn] && NOTES[edgeData.sn].note) || "";
    const save = el("button", { className: "note-save", textContent: "Save" });
    save.onclick = () => {
      apiJson(`/api/files/${CURRENT_FILE}/edge/${edgeData.sn}/note`, "POST", { note: ta.value })
        .then(res => {
          if (res.note) NOTES[edgeData.sn] = { note: res.note, by: res.by };
          else delete NOTES[edgeData.sn];
          renderNote(); toast("Note saved");
        })
        .catch(err => toast(err, true));
    };
    noteWrap.appendChild(ta); noteWrap.appendChild(save);
  };
  renderNote();
  row.appendChild(noteWrap);

  // Clicking the row (but not its controls) jumps to that connection.
  row.onclick = (ev) => {
    const t = ev.target.tagName;
    if (t === "SELECT" || t === "BUTTON" || t === "TEXTAREA") return;
    if (ev.target.closest(".note-wrap")) return;
    highlightEdgeBySn(edgeData.sn);
  };

  return row;
}

function setupDetailsClose() {
  $("details-close").onclick = closeDetails;
}
function closeDetails() {
  const wasOpen = $("details").classList.contains("open");
  $("details").classList.remove("open");
  clearHighlight();
  if (wasOpen && cy) {
    setTimeout(() => cy.resize(), 50);
  }
}


// -------- 5. Search --------
function setupSearch(nodes, edges) {
  const input = $("search");
  const results = $("search-results");
  let highlightedIdx = -1;
  let currentMatches = [];

  // Pre-build a list of searchable items (panels + paulos labels)
  const items = [];
  nodes.forEach(n => items.push({
    type: "Panel",
    id:   n.data.id,
    text: n.data.label,
    action: () => focusNode(n.data.id),
  }));
  edges.forEach(e => items.push({
    type: "Paulos",
    id:   "e" + e.data.sn,
    text: e.data.paulos,
    action: () => focusEdgeBySn(e.data.sn),
  }));

  function render(matches) {
    results.innerHTML = "";
    currentMatches = matches;
    highlightedIdx = matches.length ? 0 : -1;
    if (!matches.length) {
      results.classList.remove("open");
      return;
    }
    matches.slice(0, 20).forEach((m, i) => {
      const item = el("div", {className: "item" + (i === 0 ? " highlighted" : "")},
        [m.text, el("span", {className: "kind", textContent: m.type})]);
      item.onmousedown = (ev) => { ev.preventDefault(); m.action(); closeResults(); };
      results.appendChild(item);
    });
    results.classList.add("open");
  }

  function closeResults() {
    results.classList.remove("open");
    input.blur();
  }

  input.oninput = () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { render([]); return; }
    const matches = items.filter(it => it.text.toLowerCase().includes(q));
    render(matches);
  };

  // Enter / Esc / arrow keys
  input.onkeydown = (ev) => {
    if (ev.key === "Escape") { closeResults(); return; }
    if (!currentMatches.length) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      highlightedIdx = Math.min(highlightedIdx + 1, currentMatches.length - 1, 19);
      updateHighlight();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      highlightedIdx = Math.max(highlightedIdx - 1, 0);
      updateHighlight();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      currentMatches[highlightedIdx]?.action();
      closeResults();
    }
  };

  function updateHighlight() {
    const items = results.querySelectorAll(".item");
    items.forEach((it, i) => it.classList.toggle("highlighted", i === highlightedIdx));
    items[highlightedIdx]?.scrollIntoView({block: "nearest"});
  }

  // Click outside to close
  document.addEventListener("mousedown", (ev) => {
    if (!results.contains(ev.target) && ev.target !== input) {
      results.classList.remove("open");
    }
  });
}

function focusNode(nodeId) {
  const n = cy.getElementById(nodeId);
  if (n.empty()) return;
  cy.animate({
    center: { eles: n },
    zoom: Math.max(cy.zoom(), 0.9),
    duration: 350,
  });
  highlightNode(nodeId);
  openPanelDetails(nodeId);
}

function focusEdgeBySn(sn) {
  const edge = cy.edges(`[sn = ${sn}]`);
  if (edge.empty()) return;
  cy.animate({
    fit: { eles: edge.union(edge.connectedNodes()), padding: 120 },
    duration: 350,
  });
  highlightEdgeBySn(sn);
}


// -------- 6. Status editing --------
function changeEdgeStatus(sn, newStatus, callback) {
  fetch(`/api/files/${CURRENT_FILE}/edge/${sn}/status`, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": CSRF},
    body: JSON.stringify({status: newStatus}),
  })
  .then(r => r.json().then(body => ({ok: r.ok, body})))
  .then(({ok, body}) => {
    if (!ok) {
      toast(body.error || "Update failed", true);
      callback(false);
      return;
    }
    // Our own change advances the revision; record it so the poller doesn't
    // mistake it for someone else's edit and re-toast it.
    if (typeof body.revision === "number") lastRevision = body.revision;
    applyStatusToCanvas(sn, body.status, body.color);
    callback(true);
  })
  .catch(err => {
    toast("Network error", true);
    callback(false);
  });
}

/** Paint a feeder's new status onto the diagram (edge + its load box). Used
 *  both for our own edits and for changes polled from other users. */
function applyStatusToCanvas(sn, status, color) {
  if (!cy) return;
  const edge = cy.edges(`[sn = ${sn}]`);
  if (edge.empty()) return;
  edge.data("status", status);
  edge.data("color",  color);
  const tgt = edge.target();
  if (tgt.data("isBoard") === 0) {
    tgt.data("loadStatus", status);
    tgt.data("loadBorder", color);
    tgt.data("loadFill", lighten(color, 0.72));
  }
}

/** Right-click-ish behavior: clicking an edge prompts with a tiny picker. */
function promptEdgeStatus(edge) {
  const sn = edge.data("sn");
  const current = edge.data("status");
  // Cycle through statuses on click - quick way to flip
  const next = STATUSES[(STATUSES.indexOf(current) + 1) % STATUSES.length];

  changeEdgeStatus(sn, next, ok => {
    if (ok) toast(`SN ${sn}: ${current} → ${next}`);
  });

  // Also highlight it briefly
  highlightEdgeBySn(sn);
}


// -------- Highlight helpers --------
function clearHighlight() {
  cy.elements().removeClass("highlighted faded");
}
function highlightNode(nodeId) {
  clearHighlight();
  cy.getElementById(nodeId).addClass("highlighted");
}
function highlightEdgeBySn(sn) {
  clearHighlight();
  const edge = cy.edges(`[sn = ${sn}]`);
  edge.addClass("highlighted");
  edge.connectedNodes().addClass("highlighted");
}


// -------- Render data findings banner --------
function renderFindings(findings) {
  if (!findings || Object.keys(findings).length === 0) return;
  const banner = $("findings");
  const summary = $("findings-summary");
  const list = $("findings-list");

  // Count total issues
  let totalCount = 0;
  for (const arr of Object.values(findings)) {
    totalCount += Array.isArray(arr) ? arr.length : 1;
  }
  summary.textContent = `${totalCount} data finding${totalCount > 1 ? "s" : ""} — click to see`;

  list.innerHTML = "";

  if (findings.multi_feeders) {
    findings.multi_feeders.forEach(item => {
      const li = el("li", {
        textContent: `${item.count} feeders between ${item["Fed From"]} and ${item["Feed To"]} ` +
                     `(legitimate if multiple cables; could be a copy-paste duplicate)`,
      });
      list.appendChild(li);
    });
  }
  if (findings.near_duplicate_names) {
    findings.near_duplicate_names.forEach(group => {
      const li = el("li", {
        textContent: `Possible typo — these names differ only by spaces/case: "${group.join('", "')}"`,
      });
      list.appendChild(li);
    });
  }
  if (findings.cycles) {
    findings.cycles.forEach(c => {
      const li = el("li", {
        textContent: `Cycle in power flow: ${c.join(" → ")} → ${c[0]}`,
      });
      list.appendChild(li);
    });
  }
  banner.classList.add("show");
}


// -------- 7. Legend filter --------
function setupLegendFilter() {
  let activeFilter = null;
  document.querySelectorAll(".legend-item").forEach(item => {
    item.onclick = () => {
      const status = item.dataset.filter;
      if (activeFilter === status) {
        // Click same one again -> clear filter
        cy.elements().removeClass("faded");
        activeFilter = null;
        item.style.fontWeight = "";
        return;
      }
      // Clear other active styling
      document.querySelectorAll(".legend-item")
        .forEach(li => li.style.fontWeight = "");
      item.style.fontWeight = "bold";
      activeFilter = status;

      // Fade everything except matching edges + their nodes
      const matchEdges = cy.edges(`[status = "${status}"]`);
      const matchNodes = matchEdges.connectedNodes();
      cy.elements().addClass("faded");
      matchEdges.removeClass("faded");
      matchNodes.removeClass("faded");
    };
  });
}


// -------- 8. Export: xlsx / png / pdf --------
// NOTE: this lives at the TOP LEVEL of the file (not inside any function).
(function setupExportMenu() {
  const btn  = $("export-btn");
  const menu = $("export-menu");
  if (!btn || !menu) return;

  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    menu.classList.toggle("open");
  });
  document.addEventListener("click", (ev) => {
    if (!menu.contains(ev.target) && ev.target !== btn) menu.classList.remove("open");
  });
  menu.querySelectorAll("button[data-export]").forEach(b => {
    b.addEventListener("click", () => {
      menu.classList.remove("open");
      const kind = b.dataset.export;
      if (kind === "xlsx")      window.location = `/api/files/${CURRENT_FILE}/export`;
      else if (kind === "png")  exportPng();
      else if (kind === "pdf")  exportPdf();
    });
  });
})();

/** A filename stem based on the title-block project name (or a default). */
function exportBaseName() {
  const nm = ($("tb-name") && $("tb-name").value.trim()) || currentFileName || "sld-diagram";
  return nm.replace(/[^\w.-]+/g, "_");
}

/** Render the whole diagram to a high-res PNG and download it. */
function exportPng() {
  if (!cy) { toast("Nothing to export yet", true); return; }
  try {
    const png = cy.png({ full: true, scale: 2, bg: "#ffffff" });
    const a = el("a", { href: png, download: exportBaseName() + ".png" });
    document.body.appendChild(a); a.click(); a.remove();
    toast("Exported PNG");
  } catch (e) {
    toast("PNG export failed: " + e.message, true);
  }
}

/** Export a PDF by opening a print window containing the diagram image plus a
 *  title strip, then invoking the browser's "Save as PDF". No external libs. */
function exportPdf() {
  if (!cy) { toast("Nothing to export yet", true); return; }
  let dataUrl;
  try {
    dataUrl = cy.png({ full: true, scale: 2, bg: "#ffffff" });
  } catch (e) {
    toast("PDF export failed: " + e.message, true);
    return;
  }
  const title = (($("tb-name") && $("tb-name").value.trim()) || "Single Line Diagram");
  const date  = new Date().toLocaleDateString();
  const counts = {};
  cy.edges().forEach(e => { const s = e.data("status"); counts[s] = (counts[s] || 0) + 1; });
  const summary = STATUSES.map(s => `${s}: ${counts[s] || 0}`).join(" · ");

  const w = window.open("", "_blank");
  if (!w) { toast("Allow pop-ups to export PDF", true); return; }
  w.document.write(`<!DOCTYPE html><html><head><title>${escapeHtml(title)}</title>
    <style>
      @page { size: A3 landscape; margin: 10mm; }
      * { box-sizing: border-box; }
      body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; color: #1c2b38; }
      .sheet-head { display: flex; justify-content: space-between; align-items: flex-end;
                    border-bottom: 2px solid #1e3a5f; padding-bottom: 6px; margin-bottom: 8px; }
      .sheet-head h1 { font-size: 18px; margin: 0; color: #1e3a5f; }
      .sheet-head .meta { font-size: 11px; color: #667085; text-align: right; }
      img { width: 100%; height: auto; border: 1px solid #d0d5dd; }
    </style></head><body>
      <div class="sheet-head">
        <h1>${escapeHtml(title)}</h1>
        <div class="meta">${escapeHtml(date)}<br>${escapeHtml(summary)}</div>
      </div>
      <img src="${dataUrl}">
    </body></html>`);
  w.document.close();
  // Wait for the image to decode before triggering the print dialog.
  const img = w.document.querySelector("img");
  const go = () => { w.focus(); w.print(); };
  if (img && !img.complete) { img.onload = go; img.onerror = go; }
  else setTimeout(go, 300);
  toast("Opening print dialog — choose “Save as PDF”");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ===================================================================
//  SECTIONS + BUSBAR LAYOUT  (the "look like the PDF" stage)
// ===================================================================
const SECTION_PALETTE = [
  "#F8CCE0","#BFE6EC","#C9E8C2","#FCE3B4","#D6CCEF","#F7C9C2","#CDE7F0",
  "#E6D7B8","#D9EAD3","#F4CFE7","#C2E0DA","#EAD1B0","#D0D9F0","#F0D0C0"
];

function isBoardNode(node) { return node.outgoers("node").length > 0; }

/** Blend a #rrggbb hex toward white. t=0 keeps the color, t=1 is white. */
function lighten(hex, t) {
  const h = (hex || "#999999").replace("#", "");
  const r = parseInt(h.substr(0, 2), 16),
        g = parseInt(h.substr(2, 2), 16),
        b = parseInt(h.substr(4, 2), 16);
  const m = c => Math.round(c + (255 - c) * t);
  return `rgb(${m(r)},${m(g)},${m(b)})`;
}

function buildSections(cy) {
  // tag boards (feed others) vs leaves; color each board rail. No compound groups
  // so busbars drag freely in 2D.
  cy.nodes().forEach(n => n.data("isBoard", isBoardNode(n) ? 1 : 0));
  cy.nodes().filter(isBoardNode).forEach((b, i) =>
    b.data("bg", SECTION_PALETTE[i % SECTION_PALETTE.length]));
  cy.edges().forEach(e => e.data("tgtBoard", e.target().data("isBoard") === 1 ? 1 : 0));

  // ---- Color each LOAD box by the status of the feeder that feeds it ----
  // (green = energized, amber = issued, grey = not issued — like the drawing)
  cy.nodes("[isBoard = 0]").forEach(n => {
    const inc = n.incomers("edge");
    const status = inc.length ? inc[0].data("status") : null;
    const color = (status && COLORS[status]) || "#9aa5b1";
    n.data("loadStatus", status || "");
    n.data("loadBorder", color);
    n.data("loadFill", lighten(color, 0.72));   // soft tint so the label reads
  });

  // ---- Color each inter-board cable by its SOURCE board's rail color ----
  // so you can trace a cable back to the switchboard it leaves from.
  cy.edges("[tgtBoard = 1]").forEach(e => {
    e.data("cableColor", e.source().data("bg") || "#7c8a99");
  });
}

function applySldLayout(cy) {
  // ---- sizing constants (tuned to read like the reference drawing) ----
  const LEAF_W = 104, LEAF_H = 62, HGAP = 14, STUB = 44;
  const BASE_BAR = 18, MAX_ROW = 14;     // wrap a board's loads after this many
  const MAX_BAR = 1600, CABLE_SLOT = 64, BAR_MIN = 70;
  // How much taller the bar gets per feeder, and its cap. This makes the
  // rectangle grow in BOTH dimensions (not just longer) as more leave it.
  const BAR_PER_CONN_H = 3.2, BAR_H_MAX = 84;
  const NAME_H = 22, ROW_GAP = 150, LEFT = 80;

  const kids       = id => cy.getElementById(id).outgoers("node").map(n => n.id());
  const isBd       = id => cy.getElementById(id).data("isBoard") === 1;
  const leaves     = id => kids(id).filter(k => !isBd(k));
  const boardKids  = id => kids(id).filter(isBd);
  const outConns   = id => cy.getElementById(id).outgoers("edge").length;

  // Loads sit in a horizontal row above the bar (wrapping to more rows if a
  // board has a lot of them, like the wide boards in the drawing).
  const rowCount   = id => { const n = leaves(id).length;
                             return n ? Math.ceil(n / MAX_ROW) : 0; };
  const perRow     = id => { const n = leaves(id).length;
                             return n ? Math.ceil(n / rowCount(id)) : 0; };
  const rowWidth   = id => { const c = perRow(id);
                             return c ? c * LEAF_W + (c - 1) * HGAP : 0; };

  // #1: the bar is EXACTLY as wide as its parts — the row of loads (incl. the
  // gaps between them), with no extra slack. Boards that only feed other boards
  // (no loads) get just enough width for their down-cables to sit side by side.
  const barWidth  = id => {
    const lw = rowWidth(id);                         // exact span of the loads
    const cw = boardKids(id).length * CABLE_SLOT;    // room for inter-board cables
    return Math.min(MAX_BAR, Math.max(BAR_MIN, lw, cw));
  };
  // #2: the bar also gets THICKER (taller) the more feeders leave it, so the
  // rectangle becomes bigger overall instead of just longer.
  const barHeight = id => Math.min(BAR_H_MAX, BASE_BAR + outConns(id) * BAR_PER_CONN_H);
  // Vertical room a board's stack of loads needs above the bar.
  const stackH    = id => { const r = rowCount(id);
                            return r ? r * LEAF_H + (r - 1) * HGAP + STUB : 0; };

  const boards = cy.nodes("[isBoard = 1]").map(n => n.id());
  const roots  = boards.filter(b => cy.getElementById(b).incomers("node").length === 0);

  // ---- Lay boards out as a TOP-DOWN TREE that spreads horizontally ----
  // (instead of stacking every board on one vertical line). Power still flows
  // downward — each board sits below its parent — but sibling sub-trees fan
  // out left/right, so a fresh diagram opens balanced and roughly square
  // rather than as a tall vertical strip.
  const HSPACE = 70;                 // horizontal gap between sibling sub-trees

  // Depth (rank) of each board: distance from a root, via BFS over boards.
  const depth = {}, seenD = new Set(), dq = roots.map(r => [r, 0]);
  while (dq.length) {
    const [b, d] = dq.shift();
    if (seenD.has(b)) continue;
    seenD.add(b); depth[b] = d;
    boardKids(b).forEach(c => dq.push([c, d + 1]));
  }
  // Orphans (unreachable, e.g. inside a cycle) become their own roots at depth 0.
  const rootList = [...roots];
  boards.forEach(b => { if (depth[b] === undefined) { depth[b] = 0; rootList.push(b); } });

  // Width a whole sub-tree needs = max(this bar, the span of its children).
  const slotCache = {};
  const slotW = (id, guard = new Set()) => {
    if (slotCache[id] !== undefined) return slotCache[id];
    if (guard.has(id)) return barWidth(id);     // cycle guard
    guard.add(id);
    const ch = boardKids(id);
    const span = ch.length
      ? ch.reduce((s, c) => s + slotW(c, guard), 0) + HSPACE * (ch.length - 1) : 0;
    return (slotCache[id] = Math.max(BAR_MIN, barWidth(id), span));
  };

  // Place each board centered in its own horizontal slot; children centered
  // as a group beneath it.
  const posX = {}, placed = new Set();
  const placeX = (id, slotLeft) => {
    if (placed.has(id)) return;
    placed.add(id);
    const w = slotW(id);
    posX[id] = slotLeft + w / 2;
    const ch = boardKids(id).filter(c => !placed.has(c));
    if (ch.length) {
      const span = ch.reduce((s, c) => s + slotW(c), 0) + HSPACE * (ch.length - 1);
      let cur = slotLeft + (w - span) / 2;
      ch.forEach(c => { placeX(c, cur); cur += slotW(c) + HSPACE; });
    }
  };
  let cursorX = LEFT;
  rootList.forEach(r => { placeX(r, cursorX); cursorX += slotW(r) + HSPACE * 2; });

  // Vertical position by depth: each rank is a row tall enough for its loads
  // + bar, with a gap below for the down-cables.
  const rowH = {};
  boards.forEach(id => {
    rowH[depth[id]] = Math.max(rowH[depth[id]] || 0, stackH(id) + barHeight(id) + NAME_H);
  });
  const maxDepth = boards.reduce((m, id) => Math.max(m, depth[id]), 0);
  const rowTop = {}; let yCur = 40;
  for (let d = 0; d <= maxDepth; d++) { rowTop[d] = yCur; yCur += (rowH[d] || 0) + ROW_GAP; }

  const pos = {};
  boards.forEach(id => {
    const bw = barWidth(id), bh = barHeight(id), d = depth[id];
    cy.getElementById(id).data("barW", bw);
    cy.getElementById(id).data("barH", bh);

    // Loads: one or more rows centered above this board's bar.
    const lv = leaves(id), cols = perRow(id), rw = rowWidth(id);
    if (lv.length) {
      const sx = posX[id] - rw / 2 + LEAF_W / 2, sy = rowTop[d] + LEAF_H / 2;
      lv.forEach((lf, i) => {
        const r = Math.floor(i / cols), c = i % cols;
        pos[lf] = { x: sx + c * (LEAF_W + HGAP), y: sy + r * (LEAF_H + HGAP) };
      });
    }
    pos[id] = { x: posX[id], y: rowTop[d] + stackH(id) + bh / 2 };
  });
  cy.nodes("[!isGroup]").forEach(n => { if (pos[n.id()]) n.position(pos[n.id()]); });

  // Spread the inter-board cables along the SOURCE bus so they leave it side
  // by side rather than all from one point.
  boards.forEach(src => {
    const oe = cy.getElementById(src).outgoers("edge").filter(e => e.data("tgtBoard") === 1);
    const k = oe.length, bw = cy.getElementById(src).data("barW");
    oe.forEach((e, i) => e.style({ "source-endpoint": (bw * (i + 0.5) / k - bw / 2) + "px 50%" }));
  });
}

// ===================================================================
//  DRAGGABLE BUSBARS + SAVED 2D ARRANGEMENT
// ===================================================================
const layoutKey = () => `nhm_sld_layout_v1_file_${CURRENT_FILE || "none"}`;

function setupDragging(cy) {
  cy.on("grab", "node[isBoard = 1]", e => {
    const b = e.target, bp = b.position();
    // remember each load's offset relative to the bus at grab time
    const offs = b.outgoers("node").filter(n => n.data("isBoard") === 0)
      .map(n => ({ node: n, dx: n.position().x - bp.x, dy: n.position().y - bp.y }));
    b.scratch("_offs", offs);
  });
  cy.on("drag", "node[isBoard = 1]", e => {
    const b = e.target, bp = b.position(), offs = b.scratch("_offs");
    if (offs) offs.forEach(o => o.node.position({ x: bp.x + o.dx, y: bp.y + o.dy }));
  });
  cy.on("dragfree", "node[isBoard = 1]", () => saveLayout(cy));
}

function saveLayout(cy) {
  try {
    const ids = cy.nodes("[!isGroup]").map(n => n.id()).sort();
    const positions = {};
    cy.nodes("[!isGroup]").forEach(n => positions[n.id()] = n.position());
    localStorage.setItem(layoutKey(), JSON.stringify({ ids, positions }));
  } catch (e) { /* storage may be unavailable; ignore */ }
}

function restoreLayout(cy) {
  try {
    const raw = localStorage.getItem(layoutKey());
    if (!raw) return false;
    const saved = JSON.parse(raw);
    const cur = cy.nodes("[!isGroup]").map(n => n.id()).sort();
    if (JSON.stringify(saved.ids) !== JSON.stringify(cur)) return false;   // different file
    cy.nodes("[!isGroup]").forEach(n => {
      const p = saved.positions[n.id()];
      if (p) n.position(p);
    });
    return true;
  } catch (e) { return false; }
}

/** "Reset layout" returns the diagram to the freshly-uploaded auto-layout.
 *  We always (re)bind the button to the CURRENT cy — after switching files
 *  the old cy is destroyed, so a stale closure would reset nothing (the
 *  cause of the "collapses to a vertical line" bug). */
function resetLayout(cy) {
  try { localStorage.removeItem(layoutKey()); } catch (e) {}
  buildSections(cy);        // recompute board/leaf tagging + colors
  applySldLayout(cy);       // exactly what a fresh upload shows
  cy.fit(undefined, 12);
}

function setupLayoutControls(cy) {
  const legend = document.querySelector("aside.legend");
  if (!legend) return;
  const existing = document.getElementById("reset-layout");
  if (existing) { existing.onclick = () => resetLayout(cy); return; }
  const wrap = document.createElement("div");
  wrap.style.marginTop = "18px";
  wrap.innerHTML =
    '<h3>LAYOUT</h3>' +
    '<div style="font-size:12px;color:#5b6b7b;line-height:1.4;margin-bottom:8px">' +
    'Drag a busbar to move that board (its loads follow). Your arrangement saves automatically.</div>';
  const btn = document.createElement("button");
  btn.id = "reset-layout";
  btn.textContent = "Reset layout";
  btn.style.cssText =
    "font-size:12px;padding:6px 10px;border:1px solid #c3ccd6;border-radius:5px;background:#fff;cursor:pointer";
  btn.onclick = () => resetLayout(cy);
  wrap.appendChild(btn);

  // #5: a bit more in the legend - what the line styles / bus color mean
  const notes = document.createElement("div");
  notes.style.cssText = "margin-top:16px;font-size:12px;color:#5b6b7b;line-height:1.5";
  notes.innerHTML =
    '<h3>NOTES</h3>' +
    'Solid line = energized / issued<br>' +
    'Dashed line = not issued<br>' +
    'Colored bar = a switchboard (MDB/SMDB)<br>' +
    'White box = a load / feeder';
  legend.appendChild(wrap);
  legend.appendChild(notes);
}