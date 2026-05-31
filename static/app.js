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
let currentFileName = "";     // name of the last uploaded workbook (for the title block)

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
  fetch("/api/graph")
  .then(r => r.json())
  .then(data => {
    $("loading").style.display = "none";

    // Nothing uploaded yet -> show the empty-state prompt and stop.
    if (!data.loaded) {
      showEmptyState(true);
      return;
    }
    showEmptyState(false);
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

function showEmptyState(show) {
  const es = $("empty-state");
  if (es) es.style.display = show ? "block" : "none";
}

function updateStatsText(stats) {
  const t = $("stats-text");
  if (t && stats) t.textContent = `${stats.panels} panels · ${stats.feeders} feeders`;
}

// -------- Upload handling --------
function setupUpload() {
  const input = $("upload-input");
  const trigger = () => input.click();
  $("upload-btn").addEventListener("click", trigger);
  const emptyBtn = $("empty-upload-btn");
  if (emptyBtn) emptyBtn.addEventListener("click", trigger);

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    toast("Uploading " + file.name + "…");
    fetch("/api/upload", { method: "POST", body: form })
      .then(r => r.json().then(body => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) { toast(body.error || "Upload failed", true); return; }
        currentFileName = file.name.replace(/\.[^.]+$/, "");
        const nm = $("tb-name"); if (nm) nm.value = currentFileName;  // new file -> new title
        toast(`Loaded ${body.stats.panels} panels, ${body.stats.feeders} feeders`);
        $("loading").style.display = "block";
        $("loading").textContent = "Building map…";
        boot();
      })
      .catch(err => toast("Upload error: " + err.message, true))
      .finally(() => { input.value = ""; });  // allow re-uploading same file
  });
}

setupUpload();
setupTitleBlock();
boot();

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
function openPanelDetails(nodeId) {
  highlightNode(nodeId);

  fetch("/api/node/" + encodeURIComponent(nodeId))
    .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)))
    .then(info => renderDetails(info))
    .catch(err => toast("Couldn't load panel details", true));
}

function renderDetails(info) {
  const aside = $("details");
  const node  = cy.getElementById(info.id);
  const type  = node.data("type") || "OTHER";

  $("details-kind").textContent  = type;
  $("details-title").textContent = info.id;

  const body = $("details-body");
  body.innerHTML = "";   // clear previous

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
    info.outgoing.forEach(e => body.appendChild(feederRow(e, "to")));
  }

  const wasClosed = !aside.classList.contains("open");
  aside.classList.add("open");

  // The map canvas just shrank by 320px — tell cytoscape to re-measure
  // and re-center on the clicked node so it stays visible.
  if (wasClosed) {
    setTimeout(() => {
      cy.resize();
      cy.animate({ center: { eles: node }, duration: 250 });
    }, 50);
  }
}

/** Build one "feeder row" for the details panel. */
function feederRow(edgeData, direction) {
  // direction: "from" means this row describes a feeder coming IN (source side)
  //            "to"   means an outgoing feeder (target side)
  const otherPanel = direction === "from" ? edgeData.source : edgeData.target;
  const label      = direction === "from" ? "← " : "→ ";

  const row = el("div", {className: "feeder-row"});

  // Top line: label and panel name
  row.appendChild(el("div", {}, [
    el("span", {className: "arrow", textContent: label}),
    el("span", {className: "target", textContent: otherPanel}),
  ]));

  // Paulos & SN
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
        edgeData.status = newStatus;   // keep local copy in sync
        toast(`Updated SN ${edgeData.sn} → ${newStatus}`);
      }
    });
  };

  statusRow.appendChild(dot);
  statusRow.appendChild(sel);
  row.appendChild(statusRow);

  // Clicking the row jumps to that connection on the map
  row.onclick = (ev) => {
    if (ev.target.tagName === "SELECT") return;   // don't hijack the dropdown
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
  fetch(`/api/edge/${sn}/status`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({status: newStatus}),
  })
  .then(r => r.json().then(body => ({ok: r.ok, body})))
  .then(({ok, body}) => {
    if (!ok) {
      toast(body.error || "Update failed", true);
      callback(false);
      return;
    }
    // Update the edge on the canvas
    const edge = cy.edges(`[sn = ${sn}]`);
    edge.data("status", body.status);
    edge.data("color",  body.color);
    // Recolor a load box if its feeder status changed (matches the drawing)
    const tgt = edge.target();
    if (tgt.data("isBoard") === 0) {
      tgt.data("loadStatus", body.status);
      tgt.data("loadBorder", body.color);
      tgt.data("loadFill", lighten(body.color, 0.72));
    }
    callback(true);
  })
  .catch(err => {
    toast("Network error", true);
    callback(false);
  });
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


// -------- 8. Export the updated xlsx --------
// NOTE: this lives at the TOP LEVEL of the file (not inside any function).
document.getElementById('export-btn').addEventListener('click', () => {
  // This endpoint returns the file as a download; the page stays put.
  window.location = '/api/export';
});

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

  // BFS order so parents are placed above their child boards.
  const order = [], seen = new Set(), q = [...roots];
  while (q.length) {
    const b = q.shift();
    if (seen.has(b)) continue;
    seen.add(b); order.push(b);
    boardKids(b).forEach(c => q.push(c));
  }
  boards.forEach(b => { if (!seen.has(b)) { seen.add(b); order.push(b); } });

  // All bars share a vertical spine; the widest bar sets the centre line.
  const maxBar = Math.max(BAR_MIN, ...order.map(barWidth));
  const cx = LEFT + maxBar / 2;

  const pos = {};
  let curY = 40;
  order.forEach(id => {
    const lv = leaves(id), bw = barWidth(id), bh = barHeight(id);
    const cols = perRow(id), rows = rowCount(id), rw = rowWidth(id);
    cy.getElementById(id).data("barW", bw);
    cy.getElementById(id).data("barH", bh);

    // Loads: one or more centered rows sitting above the bar.
    if (lv.length) {
      const sx = cx - rw / 2 + LEAF_W / 2, sy = curY + LEAF_H / 2;
      lv.forEach((lf, i) => {
        const r = Math.floor(i / cols), c = i % cols;
        pos[lf] = { x: sx + c * (LEAF_W + HGAP), y: sy + r * (LEAF_H + HGAP) };
      });
    }

    const barY = curY + stackH(id) + bh / 2;
    pos[id] = { x: cx, y: barY };
    curY = barY + bh / 2 + NAME_H + ROW_GAP;
  });
  cy.nodes("[!isGroup]").forEach(n => { if (pos[n.id()]) n.position(pos[n.id()]); });

  // #2 (cont.): spread the inter-board cables along the SOURCE bus so they
  // leave it side by side rather than all from one point.
  boards.forEach(src => {
    const oe = cy.getElementById(src).outgoers("edge").filter(e => e.data("tgtBoard") === 1);
    const k = oe.length, bw = cy.getElementById(src).data("barW");
    oe.forEach((e, i) => e.style({ "source-endpoint": (bw * (i + 0.5) / k - bw / 2) + "px 50%" }));
  });
}

// ===================================================================
//  DRAGGABLE BUSBARS + SAVED 2D ARRANGEMENT
// ===================================================================
const LAYOUT_KEY = "nhm_sld_layout_v1";

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
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ ids, positions }));
  } catch (e) { /* storage may be unavailable; ignore */ }
}

function restoreLayout(cy) {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
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

function setupLayoutControls(cy) {
  const legend = document.querySelector("aside.legend");
  if (!legend || document.getElementById("reset-layout")) return;
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
  btn.onclick = () => {
    try { localStorage.removeItem(LAYOUT_KEY); } catch (e) {}
    applySldLayout(cy);
    cy.fit(undefined, 12);
  };
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