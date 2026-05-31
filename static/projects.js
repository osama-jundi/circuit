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
  const opts = { method, headers: {} };
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

function createProject() {
  const name = (prompt("Name the new project:") || "").trim();
  if (!name) return;
  apiJson("/api/projects", "POST", { name })
    .then(({ id }) => { toast("Project created"); window.location = `/project/${id}`; })
    .catch(err => toast(err, true));
}

function renameProject(p) {
  const name = (prompt("Rename project:", p.name) || "").trim();
  if (!name || name === p.name) return;
  apiJson(`/api/projects/${p.id}`, "PATCH", { name })
    .then(() => { toast("Renamed"); loadProjects(); })
    .catch(err => toast(err, true));
}

function deleteProject(p) {
  if (!confirm(`Delete project "${p.name}"?\nThis removes ALL its diagrams, status edits and history. This cannot be undone.`)) return;
  apiJson(`/api/projects/${p.id}`, "DELETE")
    .then(() => { toast("Deleted"); loadProjects(); })
    .catch(err => toast(err, true));
}

// -------- Users modal (admin) --------
function openModal() { $("modal-overlay").classList.add("open"); renderUsers(); }
function closeModal() { $("modal-overlay").classList.remove("open"); }

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
    if (pwBtn) pwBtn.onclick = () => {
      const pw = prompt(`New password for ${username}:`);
      if (!pw) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "PATCH", { password: pw })
        .then(() => toast(`Password reset for ${username}`)).catch(err => toast(err, true));
    };
    const delBtn = tr.querySelector(".act-del");
    if (delBtn && !delBtn.disabled) delBtn.onclick = () => {
      if (!confirm(`Delete user ${username}?`)) return;
      apiJson(`/api/users/${encodeURIComponent(username)}`, "DELETE")
        .then(() => { toast(`Deleted ${username}`); renderUsers(); }).catch(err => toast(err, true));
    };
  });
}

// -------- Wire up --------
const usersBtn = $("users-btn");
if (usersBtn) usersBtn.addEventListener("click", openModal);
$("modal-close").addEventListener("click", closeModal);
$("modal-overlay").addEventListener("click", (e) => { if (e.target === $("modal-overlay")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

loadProjects();
