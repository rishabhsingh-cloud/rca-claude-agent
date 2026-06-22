"""Symbol-level code graph for cross-file causal tracing.

The thing you lose by not cloning the repo: the ability to walk from a suspect
symbol to its callers (where bad input came from) and callees (its dependencies),
and to size the blast radius (what QA should retest). The crash site is rarely
the root cause; the graph traces outward from it.

This module is the deterministic, dependency-free engine: a directed
function/method/class call graph built from Python source with the stdlib `ast`
module, plus a file-level import graph. It preserves edge DIRECTION
(caller -> callee), which a plain undirected graph cannot. `graphify_adapter`
loads the same model from a graphify `graph.json` for multi-language / larger
repos.

Edges carry provenance (extracted | inferred | ambiguous) so the RCA agent can
honor the design's guardrail: if it claims A -> B but no edge exists, flag it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Max same-named candidates for which we'll still emit (ambiguous) call edges.
# Above this, a name (.get/.save/.create) collides across so many classes that
# edges to all of them are noise, not signal — so we skip rather than invent.
_AMBIGUOUS_FANOUT_CAP = 3


@dataclass(frozen=True)
class SymbolNode:
    id: str        # "<file>::<qualname>", e.g. "billing/invoice.py::Invoice.compute_total"
    name: str      # "compute_total"
    qualname: str  # "Invoice.compute_total"
    kind: str      # "function" | "method" | "class"
    file: str
    line: int

    def ref(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class CallEdge:
    src: str          # caller node id
    dst: str          # callee node id
    provenance: str   # "extracted" (unique resolution) | "ambiguous" (many) | "inferred"


@dataclass
class RepoGraph:
    project: str
    sha: str | None = None
    nodes: dict[str, SymbolNode] = field(default_factory=dict)
    edges: list[CallEdge] = field(default_factory=list)
    # file -> list of imported module strings (the coarser import graph)
    imports: dict[str, list[str]] = field(default_factory=dict)
    # cached name index
    _by_name: dict[str, list[str]] = field(default_factory=dict, repr=False)

    # --- indexing -------------------------------------------------------------
    def _reindex(self) -> None:
        self._by_name = {}
        for nid, n in self.nodes.items():
            self._by_name.setdefault(n.name, []).append(nid)
            if n.qualname != n.name:
                self._by_name.setdefault(n.qualname, []).append(nid)

    def resolve(self, symbol: str) -> list[SymbolNode]:
        """Resolve a node by id, qualname, or bare name (returns all matches)."""
        if symbol in self.nodes:
            return [self.nodes[symbol]]
        if not self._by_name:
            self._reindex()
        return [self.nodes[i] for i in self._by_name.get(symbol, [])]

    # --- queries (back the find_callers / find_dependents / get_subgraph tools)
    def callers_of(self, symbol: str) -> list[tuple[SymbolNode, str]]:
        """Direct callers: who calls `symbol`. Returns (node, provenance)."""
        targets = {n.id for n in self.resolve(symbol)}
        out = []
        for e in self.edges:
            if e.dst in targets and e.src in self.nodes:
                out.append((self.nodes[e.src], e.provenance))
        return out

    def callees_of(self, symbol: str) -> list[tuple[SymbolNode, str]]:
        """Direct callees: what `symbol` calls."""
        srcs = {n.id for n in self.resolve(symbol)}
        out = []
        for e in self.edges:
            if e.src in srcs and e.dst in self.nodes:
                out.append((self.nodes[e.dst], e.provenance))
        return out

    def transitive_callers(self, symbol: str, max_depth: int = 4,
                           include_ambiguous: bool = False) -> list[SymbolNode]:
        """Everything that (transitively) reaches `symbol` — the blast radius.

        Follows only high-confidence (`extracted`) edges by default; ambiguous
        name-collision edges are excluded so the blast radius stays trustworthy
        (set include_ambiguous=True to widen at the cost of precision)."""
        usable = [e for e in self.edges
                  if include_ambiguous or e.provenance != "ambiguous"]
        seen: set[str] = set()
        frontier = {n.id for n in self.resolve(symbol)}
        depth = 0
        while frontier and depth < max_depth:
            nxt: set[str] = set()
            for e in usable:
                if e.dst in frontier and e.src not in seen and e.src not in frontier:
                    nxt.add(e.src)
            seen |= nxt
            frontier = nxt
            depth += 1
        return [self.nodes[i] for i in seen if i in self.nodes]

    def get_subgraph(self, symbol: str, depth: int = 1) -> dict:
        """BFS both directions from `symbol` to `depth`. Returns nodes + edges."""
        roots = {n.id for n in self.resolve(symbol)}
        if not roots:
            return {"nodes": [], "edges": [], "note": f"symbol '{symbol}' not in graph"}
        keep = set(roots)
        frontier = set(roots)
        for _ in range(depth):
            nxt: set[str] = set()
            for e in self.edges:
                if e.src in frontier:
                    nxt.add(e.dst)
                if e.dst in frontier:
                    nxt.add(e.src)
            nxt -= keep
            keep |= nxt
            frontier = nxt
            if not frontier:
                break
        edges = [e for e in self.edges if e.src in keep and e.dst in keep]
        return {
            "nodes": [self.nodes[i].__dict__ for i in keep if i in self.nodes],
            "edges": [e.__dict__ for e in edges],
        }

    def search_symbols(self, terms: list[str], limit: int = 20) -> list[SymbolNode]:
        """Find symbols whose qualname/file matches any of `terms` (lowercased).

        The no-trace localizer: given words from a ticket (the failing action, a
        UI label, an error string), surface candidate functions to investigate.
        Ranked by number of matching terms; application code is preferred over
        tests so the real handler floats up."""
        terms = [t for t in terms if t]
        scored: list[tuple[float, SymbolNode]] = []
        for n in self.nodes.values():
            hay = f"{n.qualname} {n.file}".lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                if "/test" not in n.file.lower() and "test_" not in n.file.lower():
                    score += 0.5  # prefer real code over tests
                scored.append((score, n))
        scored.sort(key=lambda x: (-x[0], x[1].file, x[1].line))
        return [n for _, n in scored[:limit]]

    def has_edge(self, src_symbol: str, dst_symbol: str) -> bool:
        """For the guardrail: does a caller->callee edge actually exist?"""
        srcs = {n.id for n in self.resolve(src_symbol)}
        dsts = {n.id for n in self.resolve(dst_symbol)}
        return any(e.src in srcs and e.dst in dsts for e in self.edges)

    def dependents_of(self, module_or_file: str) -> list[str]:
        """Files that import `module_or_file`. Tolerates both module-dotted
        (`billing.invoice`, AST engine) and file-path (`billing/invoice.py`,
        graphify) import representations."""
        def norm(s: str) -> str:
            return s.replace("\\", "/").replace("/", ".").removesuffix(".py")

        key = norm(module_or_file)
        stem = key.split(".")[-1]
        out = []
        for f, mods in self.imports.items():
            for m in mods:
                mn = norm(m)
                if mn == key or mn.endswith("." + stem) or mn.split(".")[-1] == stem:
                    out.append(f)
                    break
        return out

    # --- serialization (the persisted "graph map" / provenance artifact) ------
    def to_json(self) -> dict:
        return {
            "project": self.project,
            "sha": self.sha,
            "nodes": [n.__dict__ for n in self.nodes.values()],
            "edges": [e.__dict__ for e in self.edges],
            "imports": self.imports,
        }

    @staticmethod
    def from_json(data: dict) -> "RepoGraph":
        g = RepoGraph(project=data.get("project", ""), sha=data.get("sha"))
        for n in data.get("nodes", []):
            g.nodes[n["id"]] = SymbolNode(**n)
        g.edges = [CallEdge(**e) for e in data.get("edges", [])]
        g.imports = data.get("imports", {})
        g._reindex()
        return g


# --- AST extraction ------------------------------------------------------------

class _FileVisitor(ast.NodeVisitor):
    """Collect defs, call sites, and imports from one Python file."""

    def __init__(self, file: str):
        self.file = file
        self._classes: list[str] = []
        self._funcs: list[str] = []  # stack of enclosing function node ids
        self.nodes: dict[str, SymbolNode] = {}
        # (caller_id | None, callee_simple_name, is_self_call)
        self.calls: list[tuple[str | None, str, bool]] = []
        self.imports: list[str] = []

    def _qual(self, name: str) -> str:
        return ".".join(self._classes + [name])

    def visit_ClassDef(self, node: ast.ClassDef):
        qual = self._qual(node.name)
        nid = f"{self.file}::{qual}"
        self.nodes[nid] = SymbolNode(nid, node.name, qual, "class", self.file, node.lineno)
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node)

    def _visit_func(self, node):
        qual = self._qual(node.name)
        nid = f"{self.file}::{qual}"
        kind = "method" if self._classes else "function"
        self.nodes[nid] = SymbolNode(nid, node.name, qual, kind, self.file, node.lineno)
        self._funcs.append(nid)
        self.generic_visit(node)
        self._funcs.pop()

    def visit_Call(self, node: ast.Call):
        caller = self._funcs[-1] if self._funcs else None
        f = node.func
        if isinstance(f, ast.Name):
            self.calls.append((caller, f.id, False))
        elif isinstance(f, ast.Attribute):
            is_self = isinstance(f.value, ast.Name) and f.value.id == "self"
            self.calls.append((caller, f.attr, is_self))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        self.imports.extend(a.name for a in node.names)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            self.imports.append(node.module)
        self.generic_visit(node)


def build_graph_from_sources(project: str, files: dict[str, str],
                             sha: str | None = None) -> RepoGraph:
    """Build a directed symbol graph from {path: source}. Python files only;
    non-Python files are ignored (use the graphify adapter for those)."""
    g = RepoGraph(project=project, sha=sha)
    raw_calls: list[tuple[str, str, str, bool]] = []  # (file, caller, name, is_self)

    for path, src in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue  # skip unparseable files; don't fail the whole build
        v = _FileVisitor(path)
        v.visit(tree)
        g.nodes.update(v.nodes)
        if v.imports:
            g.imports[path] = sorted(set(v.imports))
        for caller, name, is_self in v.calls:
            if caller:
                raw_calls.append((path, caller, name, is_self))

    g._reindex()

    # Resolve calls to intra-repo nodes. External/builtin calls (no match) drop.
    seen_edges: set[tuple[str, str]] = set()
    for file, caller, name, is_self in raw_calls:
        candidates: list[str]
        if is_self:
            # self.method() -> a method on the caller's own class, same file.
            cls = caller.split("::", 1)[1].rsplit(".", 1)[0] if "." in caller.split("::", 1)[1] else None
            candidates = [
                nid for nid, n in g.nodes.items()
                if n.file == file and n.kind == "method"
                and cls is not None and n.qualname == f"{cls}.{name}"
            ]
            provenance = "extracted" if len(candidates) == 1 else "inferred"
        else:
            candidates = [n.id for n in g.resolve(name)]
            candidates = [c for c in candidates if c != caller]
            if len(candidates) == 1:
                provenance = "extracted"
            elif 2 <= len(candidates) <= _AMBIGUOUS_FANOUT_CAP:
                provenance = "ambiguous"
            else:
                # Too many same-named candidates (e.g. .get/.save across hundreds
                # of Django classes) — emitting edges to all is pure noise. Skip.
                candidates = []
                provenance = ""
        for c in candidates:
            key = (caller, c)
            if key not in seen_edges:
                seen_edges.add(key)
                g.edges.append(CallEdge(caller, c, provenance or "inferred"))

    return g
