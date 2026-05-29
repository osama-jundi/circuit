"""
graph.py - the data layer for the SLD app.

Same logic as stage2_hierarchy.py, but reshaped as functions
the Flask app can import and use.

Key function: load_and_build(xlsx_path) -> returns everything the
web frontend needs as a Python dict (nodes, edges, levels, findings).
"""

import pandas as pd
import networkx as nx

STATUS_COLORS = {
    "Energized":  "#00B050",
    "Issued":     "#FFC000",
    "Not Issued": "#A6A6A6",
}
DEFAULT_STATUS = "Not Issued"
VALID_STATUSES = list(STATUS_COLORS.keys())

# The 5 columns we expect. Tool will fail clearly if missing.
REQUIRED_COLS = ["SN", "Fed From", "Feed To", "Paulos", "Site status"]


def load_feeders(xlsx_path: str, sheet: str = "Energization") -> pd.DataFrame:
    """Read & clean the Energization sheet. Raises a clear error
    if the file doesn't have the right columns."""
    df = pd.read_excel(xlsx_path, sheet_name=sheet)

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"The sheet '{sheet}' is missing required columns: {missing}.\n"
            f"Found columns: {list(df.columns)}"
        )

    df = df[REQUIRED_COLS].dropna(subset=["Fed From", "Feed To"]).reset_index(drop=True)
    df["Fed From"] = df["Fed From"].astype(str).str.strip()
    df["Feed To"]  = df["Feed To"].astype(str).str.strip()
    df["Paulos"]   = df["Paulos"].astype(str).str.strip()
    df["Site status"] = (
        df["Site status"].astype(str).str.strip()
          .replace({"": DEFAULT_STATUS, "nan": DEFAULT_STATUS, "None": DEFAULT_STATUS})
          .fillna(DEFAULT_STATUS)
    )
    return df


def build_graph(df: pd.DataFrame) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for p in set(df["Fed From"]) | set(df["Feed To"]):
        G.add_node(p)
    for _, row in df.iterrows():
        G.add_edge(
            row["Fed From"], row["Feed To"],
            sn=int(row["SN"]),
            paulos=row["Paulos"],
            status=row["Site status"],
        )
    return G


def assign_levels(G: nx.MultiDiGraph) -> dict[str, int]:
    """Each panel gets a level number (0 = root, deeper = further down)."""
    roots = [n for n in G.nodes if G.in_degree(n) == 0]
    levels: dict[str, int] = {}
    for root in roots:
        for node, depth in nx.single_source_shortest_path_length(G, root).items():
            if node not in levels or depth < levels[node]:
                levels[node] = depth
    for node in G.nodes:
        if node not in levels:
            levels[node] = 0
    return levels


def _panel_type(name: str) -> str:
    """Guess the panel type from its name prefix or content.
    Used by the frontend to draw different visual styles per type."""
    n = name.upper().strip()
    if n.startswith("TX-"):           return "TX"      # Transformer
    if n.startswith("EMDB-"):         return "MDB"     # Emergency MDB - same look
    if n.startswith("MDB-"):          return "MDB"     # Main Distribution Board
    if n.startswith("SMDB-"):         return "SMDB"    # Sub-Main Distribution Board
    if n.startswith("DBL-"):          return "DB"      # Lighting DB
    if n.startswith("DB-FL-"):        return "DB"      # Floor DB
    if n.startswith("DB-"):           return "DB"      # Distribution Board
    if n.startswith("AHU"):           return "AHU"     # Air Handling Unit
    if "CAPACITOR" in n:              return "CAP"
    if "MOTOR" in n:                  return "MOTOR"
    if "FOOD TRUCK" in n or "BUGGY" in n or "PARKING" in n:
        return "LOAD"
    if "DSF-" in n:                   return "DB"      # Distribution Sub-Feeder (guess)
    return "OTHER"


def graph_to_cytoscape(G: nx.MultiDiGraph, levels: dict[str, int]) -> dict:
    """Turn our NetworkX graph into the JSON format Cytoscape.js expects.

    Cytoscape wants: {'nodes': [...], 'edges': [...]}
    Each node has data (id, label, level).
    Each edge has data (id, source, target, sn, paulos, status, color).
    """
    nodes = []
    for n in sorted(G.nodes):
        nodes.append({
            "data": {
                "id":    n,
                "label": n,
                "level": levels.get(n, 0),
                "type":  _panel_type(n),
                # How many feeders in/out - useful for the popup
                "in_degree":  G.in_degree(n),
                "out_degree": G.out_degree(n),
            }
        })

    edges = []
    # G.edges(keys=True, data=True) gives every edge separately, even duplicates
    for src, dst, key, edata in G.edges(keys=True, data=True):
        status = edata.get("status", DEFAULT_STATUS)
        edges.append({
            "data": {
                # Unique ID so multi-feeders don't collide on the canvas
                "id":     f"e{edata['sn']}",
                "source": src,
                "target": dst,
                "sn":     edata["sn"],
                "paulos": edata["paulos"],
                "status": status,
                "color":  STATUS_COLORS.get(status, "#999999"),
            }
        })

    return {"nodes": nodes, "edges": edges}


def find_findings(G: nx.MultiDiGraph, df: pd.DataFrame) -> dict:
    findings = {}
    simple = nx.DiGraph(G)
    if not nx.is_directed_acyclic_graph(simple):
        findings["cycles"] = list(nx.simple_cycles(simple))
    dupes = df[df.duplicated(subset=["Fed From", "Feed To"], keep=False)]
    if not dupes.empty:
        findings["multi_feeders"] = (
            dupes.groupby(["Fed From", "Feed To"]).size()
                 .reset_index(name="count").to_dict("records")
        )

    # Detect near-duplicate panel names (same name with extra spaces or case diff).
    # These are almost always typos that should be merged.
    panels = list(G.nodes)
    def _norm(s: str) -> str:
        return "".join(s.upper().split())   # remove ALL whitespace, uppercase
    groups: dict[str, list[str]] = {}
    for p in panels:
        groups.setdefault(_norm(p), []).append(p)
    near_dupes = [v for v in groups.values() if len(v) > 1]
    if near_dupes:
        findings["near_duplicate_names"] = near_dupes

    return findings


def load_and_build(xlsx_path: str, sheet: str = "Energization") -> dict:
    """One-shot: read the file and prepare everything the web app needs."""
    df = load_feeders(xlsx_path, sheet)
    G  = build_graph(df)
    levels = assign_levels(G)
    return {
        "cytoscape":  graph_to_cytoscape(G, levels),
        "findings":   find_findings(G, df),
        "stats": {
            "panels":  G.number_of_nodes(),
            "feeders": G.number_of_edges(),
            "depth":   max(levels.values()) + 1 if levels else 0,
        },
        # We hold the original DataFrame so Stage 3c can export with changes
        "_df": df,
    }
