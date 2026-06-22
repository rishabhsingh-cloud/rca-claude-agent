"""Load a `RepoGraph` from a graphify `graph.json`.

graphify (the /graphify skill) turns a repo into a knowledge graph and writes
`graphify-out/graph.json` — a NetworkX node-link export. For larger or
multi-language repos this is the production graph source; this adapter normalizes
it into the same `RepoGraph` the AST engine produces, so the RCA tools don't care
which built the graph.

Field mapping (verified against a real graphify run):
  node.source_file        -> file path        (NOT source_location)
  node.source_location     -> line, format "L25" / "L25-30" / "25"
  node.label               -> symbol name, e.g. ".compute_total()" / "build_invoice()"
  node.file_type           -> "code" | "rationale" (rationale = docstring, dropped)
  link.relation            -> edge kind ("calls"/"imports_from"/"method"/...)
  link.confidence          -> provenance (EXTRACTED/INFERRED/AMBIGUOUS)

Only call-like relations become call edges; structural relations (contains,
method) and docstring nodes are dropped — they aren't call-graph material.

Two caveats, both handled:
  - DIRECTION: graphify graphs are undirected unless built with `--directed`.
    Undirected -> we record each edge both ways and mark provenance "inferred".
  - PATHS: graphify records `source_file` relative to its scan root, which may
    carry a prefix (e.g. "fixtures/.../files/billing/invoice.py"). Pass
    `path_strip` to recover repo-relative paths the RCA tools expect.
"""

from __future__ import annotations

import re

from .graph import CallEdge, RepoGraph, SymbolNode

# graphify `relation` values that represent a call / use of one symbol by another.
_CALL_RELS = {"calls", "call", "invokes", "uses", "references", "instantiates", "constructs"}
# Module/file import relations -> the coarser import graph.
_IMPORT_RELS = {"imports_from", "imports", "import", "includes", "depends_on"}
# Structural / documentation relations that are NOT calls — dropped.
_SKIP_RELS = {"contains", "method", "member_of", "defines", "rationale_for", "rationale"}

_SOURCE_EXT = (".py", ".js", ".ts", ".go", ".rb", ".php", ".java", ".cs", ".rs", ".kt")


def _node_file(attrs: dict, path_strip: str) -> str | None:
    f = attrs.get("source_file") or attrs.get("file") or attrs.get("path")
    if not f:
        return None
    f = str(f).replace("\\", "/")
    if path_strip:
        ps = path_strip.replace("\\", "/").rstrip("/") + "/"
        if f.startswith(ps):
            f = f[len(ps):]
    return f


def _node_line(attrs: dict) -> int:
    loc = str(attrs.get("source_location") or attrs.get("line") or "")
    m = re.search(r"\d+", loc)  # "L25" / "L25-30" / "25" -> 25
    return int(m.group()) if m else 0


def _clean_name(label: str, nid: str) -> str:
    name = (label or nid.split("::")[-1]).strip()
    name = re.sub(r"\(\)$", "", name)  # drop trailing "()"
    return name.lstrip(".").strip()


def _infer_kind(label: str) -> str:
    if label.startswith("."):
        return "method"
    if label.endswith("()"):
        return "function"
    return "class"


def _is_symbol_node(attrs: dict, label: str) -> bool:
    if attrs.get("file_type") and attrs["file_type"] != "code":
        return False  # rationale / doc nodes
    # Drop file/module nodes (label is a filename like "invoice.py").
    return not label.lower().endswith(_SOURCE_EXT)


def from_graphify_json(data: dict, project: str = "", sha: str | None = None,
                       directed: bool | None = None, path_strip: str = "") -> RepoGraph:
    """Normalize a graphify node-link `graph.json` dict into a RepoGraph.

    `directed` defaults to the graph's own top-level `directed` flag.
    """
    if directed is None:
        directed = bool(data.get("directed", False))

    g = RepoGraph(project=project, sha=sha)
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("links", data.get("edges", []))

    # Path of EVERY node (incl. file/module nodes we don't keep as symbols) —
    # graphify models imports as file-node -> file-node, so we need these to
    # reconstruct the import graph even though file nodes aren't call-graph symbols.
    node_path: dict[str, str] = {}
    for n in raw_nodes:
        nid = str(n.get("id", n.get("label", "")))
        f = _node_file(n, path_strip)
        if nid and f:
            node_path[nid] = f

    for n in raw_nodes:
        nid = str(n.get("id", n.get("label", "")))
        label = str(n.get("label") or "")
        if not nid or not _is_symbol_node(n, label):
            continue
        file = node_path.get(nid)
        if not file:
            continue
        name = _clean_name(label, nid)
        g.nodes[nid] = SymbolNode(nid, name, name, _infer_kind(label), file, _node_line(n))

    for e in raw_edges:
        src, dst = str(e.get("source", "")), str(e.get("target", ""))
        rel = str(e.get("relation") or e.get("relationship") or e.get("type") or "").lower()

        if rel in _IMPORT_RELS:
            sf, df = node_path.get(src), node_path.get(dst)
            if sf and df and sf != df:
                g.imports.setdefault(sf, []).append(df)
            continue
        if rel in _SKIP_RELS:
            continue
        if rel and rel not in _CALL_RELS:
            continue
        if src not in g.nodes or dst not in g.nodes:
            continue

        prov = str(e.get("confidence") or e.get("provenance") or "inferred").lower()
        prov = prov if prov in {"extracted", "inferred", "ambiguous"} else "inferred"
        g.edges.append(CallEdge(src, dst, prov))
        if not directed:
            g.edges.append(CallEdge(dst, src, "inferred"))

    g._reindex()
    return g
