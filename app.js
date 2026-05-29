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
fetch("/api/graph")
  .then(r => r.json())
  .then(data => {
    $("loading").style.display = "none";
    STATUSES = data.statuses;
    COLORS   = data.colors;

    cy = cytoscape({
      container: $("cy"),
      elements: [...data.elements.nodes, ...data.elements.edges],
      style: graphStyle(),
      layout: {
        name: "dagre",
        rankDir: "TB",
        nodeSep: 35,
        rankSep: 95,
        edgeSep: 14,
      },
      minZoom: 0.1,
      maxZoom: 3,
    });

    // Fit the graph once dagre finishes
    cy.on("layoutstop", () => {
      cy.fit(undefined, 40);
      setTimeout(() => cy.fit(undefined, 40), 200);
    });

    // ---- Wire up interactions ----
    cy.on("tap", "node", e => openPanelDetails(e.target.id()));
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

    console.log(
      `Map ready: ${cy.nodes().length} panels, ${cy.edges().length} feeders.`
    );
    window.cy = cy;
  })
  .catch(err => {
    $("loading").textContent = "Failed to load: " + err.message;
    console.error(err);
  });


// -------- 3. Styling --------
function graphStyle() {
  return [
    // ---- Nodes (panels) ----
    {
      selector: "node",
      style: {
        "shape": "round-rectangle",
        "background-color": "#ffffff",
        "border-color": "#1e3a5f",
        "border-width": 1.5,
        "label": "data(label)",
        "text-valign": "center",
        "text-halign": "center",
        "text-wrap": "wrap",
        "text-max-width": "130px",
        "font-size": "10px",
        "font-family": "-apple-system, Segoe UI, sans-serif",
        "color": "#1e3a5f",
        "width": "140px",
        "height": "36px",
        "padding": "6px",
      }
    },
    // Roots (transformers, main incomers) stand out
    {
      selector: "node[level = 0]",
      style: {
        "background-color": "#e7eef7",
        "border-color": "#1e3a5f",
        "border-width": 2.5,
        "font-weight": "bold",
        "height": "42px",
      }
    },
    // Transformers - blue tinge
    {
      selector: 'node[type = "TX"]',
      style: { "background-color": "#dbe9f7", "border-color": "#1e4f8a" }
    },
    // MDBs - light yellow
    {
      selector: 'node[type = "MDB"]',
      style: { "background-color": "#fff3d6" }
    },
    // SMDBs - light purple
    {
      selector: 'node[type = "SMDB"]',
      style: { "background-color": "#ede4f7" }
    },
    // Capacitor banks - pink
    {
      selector: 'node[type = "CAP"]',
      style: { "background-color": "#fde0e9" }
    },
    // End loads - light grey
    {
      selector: 'node[type = "LOAD"]',
      style: { "background-color": "#f0f1f4" }
    },

    // ---- Edges (feeders) ----
    {
      selector: "edge",
      style: {
        "width": 2.2,
        "line-color": "data(color)",
        "target-arrow-color": "data(color)",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.9,
        "curve-style": "bezier",
        "control-point-step-size": 40,    // separation for multi-feeders
      }
    },
    {
      selector: 'edge[status = "Not Issued"]',
      style: {
        "line-style": "dashed",
        "line-dash-pattern": [6, 4],
      }
    },

    // ---- Highlighted (from search or details popup) ----
    {
      selector: "node.highlighted",
      style: {
        "border-color": "#b42318",
        "border-width": 4,
        "background-blacken": -0.05,
      }
    },
    {
      selector: "edge.highlighted",
      style: { "width": 4 }
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
