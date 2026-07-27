"""PROTOTYPE frontend indexer — Reconcile area only.

Turns the React/Nx frontend into screen index entries: route + module + a
visible-text 'fingerprint' (what a screenshot would show) + the API surface that
area calls + the backend service that owns those APIs. Static parse only; reads the
clone under frontend_index/frontend-apps (gitignored). Output: reco_screens.jsonl.

Run: .venv/bin/python frontend_index/extract_reco.py   (from repo root)
"""
import json, os, re
from pathlib import Path

FE = Path("frontend_index/frontend-apps")
OUT = Path("frontend_index/reco_screens.jsonl")

const_txt = (FE / "libs/constants/src/db/reco/RecoConst.tsx").read_text(encoding="utf-8", errors="ignore")
routes_txt = (FE / "apps/enterprise/src/core/AppRoutes/ReconcileRoutes.tsx").read_text(encoding="utf-8", errors="ignore")
i18n = json.loads((FE / "libs/services/localization/src/locales/en_US.json").read_text(encoding="utf-8"))

RECO_DIRS = [FE / "libs/entp/src/lib/reconcile",
             FE / "libs/shared/src/lib/ReconcileComponents",
             FE / "apps/enterprise/src/app/Reconcile"]

def _block(name):
    m = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\n\}}", const_txt, re.S)
    return m.group(1) if m else ""

recon_types = dict(re.findall(r"(\w+):\s*'([^']+)'", _block("ReconTypes")))  # Reco2APr -> '2A-PR'

def _keyed_by_type(name):  # [ReconTypes.Reco2APr]: 'x' -> '2A-PR' -> 'x'
    out = {}
    for k, v in re.findall(r"\[ReconTypes\.(\w+)\]:\s*'([^']*)'", _block(name)):
        if k in recon_types:
            out[recon_types[k]] = v
    return out

prefix   = _keyed_by_type("ReconRoutesPrefix")   # '2A-PR' -> '2a'
accepted = _keyed_by_type("RecoAcceptedTypes")   # '2A-PR' -> 'GSTR-2A'
compare  = _keyed_by_type("RecoCompareLabel")    # '2A-PR' -> '2A vs Purchase'

# route -> module: for each <ReconcileView recoType={ReconTypes.X}> scan back for module
rlines = routes_txt.splitlines()
type2module = {}
for i, ln in enumerate(rlines):
    m = re.search(r"recoType=\{ReconTypes\.(\w+)\}", ln)
    if not m:
        continue
    for j in range(i, max(i - 10, -1), -1):
        mm = re.search(r"module:\s*APPModules\.(\w+)", rlines[j])
        if mm:
            type2module[m.group(1)] = mm.group(1)
            break

# API surface + visible-label pool from the reco code
HOOK = re.compile(r"(useGetDataApi|getDataApi|postDataApi|putDataApi|deleteDataApi|useGetDataApiLazy)\(\s*[`'\"]([^`'\"]+)")
def _method(h):
    h = h.lower()
    return next((v for k, v in (("delete", "DELETE"), ("post", "POST"), ("put", "PUT"), ("get", "GET")) if k in h), "GET")

apis, labels = set(), set()
for d in RECO_DIRS:
    for root, _, files in os.walk(d):
        for fn in files:
            if not fn.endswith((".ts", ".tsx")) or ".spec." in fn:
                continue
            t = (Path(root) / fn).read_text(encoding="utf-8", errors="ignore")
            for h, url in HOOK.findall(t):
                apis.add((_method(h), "/" + url.split("${")[0].split("?")[0].strip("/")))
            for _id in re.findall(r"(?:id=\{?['\"]|id:\s*['\"])([\w.]+)", t):
                if _id in i18n:
                    labels.add(i18n[_id])
            for lit in re.findall(r"(?:label|title):\s*'([^']{2,40})'", t):
                labels.add(lit)

api_list = sorted(f"{m} {u}" for m, u in apis)
def _backend(u):  # reco endpoints are owned by the enterprise backend
    return "gst-enterprise-service"

TAB_LABELS = ["Configuration", "Summary View", "Ledger View", "Document View",
              "Download Reconcile Report", "GST Reconciliation"]

OUT.parent.mkdir(exist_ok=True)
rows = []
for name, val in recon_types.items():           # name=Reco2BPr, val='2B-PR'
    if val not in prefix:
        continue
    fp = [x for x in (val, accepted.get(val, ""), compare.get(val, ""), prefix[val]) if x] + TAB_LABELS
    rows.append({
        "module": type2module.get(name, "Reconciliation"),
        "reco_type": val,
        "route": f"/reconciliation/{prefix[val]}/:tab",
        "backend": "gst-enterprise-service",
        "fingerprint": fp,
        "api_calls": api_list,   # shared reco API surface (one ReconcileView drives all types)
    })

with OUT.open("w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

# ---- report (code-derived, no PII) ----
print(f"reco screens indexed: {len(rows)}  ->  {OUT}\n")
for r in rows:
    print(f"[{r['reco_type']:<13}] route={r['route']:<28} module={r['module']}")
    print(f"    fingerprint: {r['fingerprint'][:6]}")
print(f"\nshared reco API surface: {len(api_list)} endpoints (backend=gst-enterprise-service). sample:")
for a in api_list[:20]:
    print("   ", a)
print(f"\nvisible-label pool harvested for fingerprints: {len(labels)} strings. sample:")
for s in sorted(labels)[:20]:
    print("   ", repr(s))
