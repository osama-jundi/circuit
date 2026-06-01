/* ============================================================
   projects.js — the Projects landing page
   Lists projects as cards you open; admins create/rename/delete
   projects and manage users.
   ============================================================ */

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, children = []) => {
  const e = document.createElement(tag);
  Object.assign(e, props);
  children.forEach(c => e.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
  return e;
};
const CAN_MANAGE = document.body.dataset.canManage === "yes";
const MY_NAME = document.body.dataset.username || "";
const CSRF = document.body.dataset.csrf || "";
const STATUS_COLORS = { "Energized": "#00B050", "Issued": "#FFC000", "Not Issued": "#A6A6A6" };
const STATUS_ORDER = ["Energized", "Issued", "Not Issued"];

/** A small stacked progress bar + "% energized" line from a counts object. */
function progressBar(progress) {
  const wrap = el("div", { className: "prog-wrap" });
  const total = (progress && progress.total) || 0;
  const bar = el("div", { className: "prog" });
  if (total > 0) {
    STATUS_ORDER.forEach(s => {
      const n = progress[s] || 0;
      if (n <= 0) return;
      const seg = el("div", { className: "prog-seg", title: `${s}: ${n}` });
      seg.style.cssText = `width:${(n / total * 100).toFixed(2)}%;background:${STATUS_COLORS[s]}`;
      bar.appendChild(seg);
    });
  } else {
    bar.appendChild(el("div", { className: "prog-seg", style: "width:100%;background:#e6e9ee" }));
  }
  wrap.appendChild(bar);
  const en = (progress && progress["Energized"]) || 0;
  const pct = total ? Math.round(en / total * 100) : 0;
  wrap.appendChild(el("div", { className: "prog-label",
    textContent: total ? `${pct}% energized · ${en}/${total}` : "No diagrams yet" }));
  return wrap;
}

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1900);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString();
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
      if (r.status === 401) { window.location = "/login"; throw "Not logged in"; }
      if (!r.ok) throw (body.error || `Request failed (${r.status})`);
      return body;
    }));
}

// -------- Project cards --------
function loadProjects() {
  fetch("/api/projects")
    .then(r => { if (r.status === 401) { window.location = "/login"; return Promise.reject(); } return r.json(); })
    .then(({ projects }) => renderGrid(projects || []))
    .catch(() => { $("grid").innerHTML = '<div class="empty">Could not load projects.</div>'; });
}

function renderGrid(projects) {
  const grid = $("grid");
  grid.innerHTML = "";

  if (!projects.length && !CAN_MANAGE) {
    grid.innerHTML = '<div class="empty">No projects yet. An administrator needs to create one.</div>';
    return;
  }

  projects.forEach(p => {
    const card = el("div", { className: "card" });
    card.onclick = (ev) => { if (!ev.target.classList.contains("act")) window.location = `/project/${p.id}`; };
    card.appendChild(el("div", { className: "name", textContent: p.name }));
    const fc = p.file_count || 0;
    card.appendChild(el("span", { className: "files",
      textContent: `${fc} ${fc === 1 ? "diagram" : "diagrams"}` }));
    card.appendChild(progressBar(p.progress || {}));
    card.appendChild(el("div", { className: "meta",
      textContent: `Created by ${p.created_by || "?"} · ${fmtTime(p.created_at)}` }));
    if (CAN_MANAGE) {
      const acts = el("div", { className: "acts" });
      const ren = el("span", { className: "act", textContent: "✎", title: "Rename project" });
      ren.onclick = () => renameProject(p);
      const del = el("span", { className: "act", textContent: "🗑", title: "Delete project" });
      del.onclick = () => deleteProject(p);
      acts.appendChild(ren); acts.appendChild(del);
      card.appendChild(acts);
    }
    grid.appendChild(card);
  });

  if (CAN_MANAGE) {
    const add = el("div", { className: "card new", textContent: "➕ New project" });
    add.onclick = createProject;
    grid.appendChild(add);
  }
}

async function createProject() {
  const name = (await uiPrompt({ title: "New project", placeholder: "Project name" }) || "").trim();
  if (!name) return;
  apiJson("/api/projects", "POST", { name })
    .then(({ id }) => { toast("Project created"); window.location = `/project/${id}`; })
    .catch(err => toast(err, true));
}

async function renameProject(p) {
  const name = (await uiPrompt({ title: "Rename project", value: p.name }) || "").trim();
  if (!name || name === p.name) return;
  apiJson(`/api/projects/${p.id}`, "PATCH", { name })
    .then(() => { toast("Renamed"); loadProjects(); })
    .catch(err => toast(err, true));
}

async function deleteProject(p) {
  const ok = await uiConfirm({ title: `Delete project “${p.name}”?`, danger: true, okLabel: "Delete",
    message: "This removes ALL its diagrams, status edits and history. This cannot be undone." });
  if (!ok) return;
  apiJson(`/api/projects/${p.id}`, "DELETE")
    .then(() => { toast("Deleted"); loadProjects(); })
    .catch(err => toast(err, true));
}

// -------- Modal --------
function openModal(title, builder) {
  $("modal-title").textContent = title;
  $("modal-overlay").classList.add("open");
  builder();
}
function closeModal() { $("modal-overlay").classList.remove("open"); }

// -------- My account: change own password --------
function renderAccount() {
  const body = $("modal-body");
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

// -------- Users modal (admin) --------
function renderUsers() {
  const body = $("modal-body");
  body.innerHTML = '<div class="empty">Loading…</div>';
  fetch("/api/users").then(r => r.json()).then(({ users }) => {
    const form = `
      <div class="form-row">
        <div><label>Username</label><input id="nu-name" autocomplete="off"></div>
        <div><label>Password</label><input id="nu-pass" type="password" autocomplete="new-password"></div>
        <div><label>Role</label>
          <select id="nu-role"><option value="user">user</option><option value="admin">admin</option></select></div>
        <button class="mini-btn" id="nu-add">Add user</button>
      </div>`;
    const adminCount = (users || []).filter(u => u.role === "admin").length;
    const rows = (users || []).map(u => {
      const isMe = u.username === MY_NAME;
      const lastAdmin = u.role === "admin" && adminCount <= 1;
      const dis = " disabled style='opacity:.4;cursor:not-allowed'";
      return `<tr data-user="${escapeHtml(u.username)}">
        <td><strong>${escapeHtml(u.username)}</strong>${isMe ? " (you)" : ""}</td>
        <td><span class="pill ${u.role}">${u.role}</span></td>
        <td style="white-space:nowrap">${fmtTime(u.created_at)}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="mini-btn ghost act-role"${lastAdmin ? dis : ""}>${u.role === "admin" ? "Make user" : "Make admin"}</button>
          <button class="mini-btn ghost act-pw">Reset password</button>
          <button class="mini-btn danger act-del"${(isMe || lastAdmin) ? dis : ""}>Delete</button>
        </td></tr>`;
    }).join("");
    body.innerHTML = form +
      `<table class="tbl"><thead><tr><th>User</th><th>Role</th><th>Created</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
    wireUsers(body);
  }).catch(() => { body.innerHTML = '<div class="empty">Could not load users.</div>'; });
}

function wireUsers(body) {
  $("nu-add").addEventListener("click", () => {
    const username = $("nu-name").value.trim(), password = $("nu-pass").value, role = $("nu-role").value;
    if (!username || !password) { toast("Username and password required", true); return; }
    apiJson("/api/users", "POST", { username, password, role })
      .then(() => { toast(`Added ${username}`); renderUsers(); }).catch(err => toast(err, true));
  });
  body.querySelectorAll("tr[data-user]").forEach(tr => {
    const username = tr.dataset.user;
    const roleBtn = tr.querySelector(".act-role");
    if (roleBtn && !roleBtn.disabled) roleBtn.onclick = () => {
      const newRole = roleBtn.textContent === "Make admin" ? "admin" : "user";
      apiJson(`/api/users/${encodeURIComponent(username)}`, "PATCH", { role: newRole })
        .then(() => { toast(`${username} is now ${newRole}`); renderUsers(); }).catch(err => toast(err, true));
    };
    const pwBtn = tr.querySelector(".act-pw");
    if (pwBtn) pwBtn.onclick = async () => {
      const pw = await uiPrompt({ title: `New password for ${username}`, placeholder: "New password" });
      if (!pw) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "PATCH", { password: pw })
        .then(() => toast(`Password reset for ${username}`)).catch(err => toast(err, true));
    };
    const delBtn = tr.querySelector(".act-del");
    if (delBtn && !delBtn.disabled) delBtn.onclick = async () => {
      if (!await uiConfirm({ title: `Delete user ${username}?`, danger: true, okLabel: "Delete" })) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "DELETE")
        .then(() => { toast(`Deleted ${username}`); renderUsers(); }).catch(err => toast(err, true));
    };
  });
}

// -------- Wire up --------
const usersBtn = $("users-btn");
if (usersBtn) usersBtn.addEventListener("click", () => openModal("Manage users", renderUsers));
const accountLink = $("account-link");
if (accountLink) accountLink.addEventListener("click", () => openModal("My account", renderAccount));
$("modal-close").addEventListener("click", closeModal);
$("modal-overlay").addEventListener("click", (e) => { if (e.target === $("modal-overlay")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

loadProjects();
