"""Frontend indexer — ALL areas.

One index entry per product AREA (= per core/AppRoutes/*Routes.tsx): its Jira-ish
modules, route path prefixes, the visible-text fingerprint (what a screenshot shows),
and the API surface that area calls (+ a best-effort backend). Static parse of the
clone under frontend_index/frontend-apps (gitignored).

Per area, code is gathered from the app/<Feature> dirs its routes lazy-import PLUS any
libs/** dir whose name matches a feature keyword (kebab/camel variants of the area,
feature dir, and route-prefix segments). Output: frontend_areas.jsonl.

Run: .venv/bin/python frontend_index/extract_frontend.py   (from repo root)
"""
import json, os, re
from pathlib import Path

FE = Path("frontend_index/frontend-apps")
APP = FE / "apps/enterprise/src/app"
ROUTES_DIR = FE / "apps/enterprise/src/core/AppRoutes"
LIBS = FE / "libs"
OUT = Path("frontend_index/frontend_areas.jsonl")
i18n = json.loads((FE / "libs/services/localization/src/locales/en_US.json").read_text(encoding="utf-8"))

# url prefix -> backend service (best-effort; the two backends are gst-enterprise-service
# and arap-auth-service). Refine against architecture.md for full accuracy.
BACKEND = {
    "reconciliation": "gst-enterprise-service", "gst-returns": "gst-enterprise-service",
    "gst": "gst-enterprise-service", "ims": "gst-enterprise-service", "import": "gst-enterprise-service",
    "reports": "gst-enterprise-service", "dashboard": "gst-enterprise-service",
    "notices": "gst-enterprise-service", "notices-orders": "gst-enterprise-service",
    "payment": "gst-enterprise-service", "configurations": "gst-enterprise-service",
    "einvoice": "gst-enterprise-service", "eway": "gst-enterprise-service",
    "vendor-followup": "arap-auth-service", "followup-agent": "arap-auth-service",
    "auth": "arap-auth-service", "users": "arap-auth-service", "user": "arap-auth-service",
    "organizations": "arap-auth-service", "organization": "arap-auth-service",
}
def backend_of(url):
    return BACKEND.get(url.strip("/").split("/", 1)[0], "gst-enterprise-service?")

HOOK = re.compile(r"(useGetDataApi|getDataApi|postDataApi|putDataApi|deleteDataApi|useGetDataApiLazy)\(\s*[`'\"]([^`'\"]+)")
def method_of(h):
    h = h.lower()
    return next((v for k, v in (("delete", "DELETE"), ("post", "POST"), ("put", "PUT"), ("get", "GET")) if k in h), "GET")

def kebab(s):
    toks = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", s)
    return "-".join(t.lower() for t in toks)

lib_dirs = []
for root, _, _ in os.walk(LIBS):
    if "node_modules" in root or ".spec" in root:
        continue
    if len(Path(root).relative_to(LIBS).parts) <= 4:
        lib_dirs.append(Path(root))

def scan(dirs):
    apis, labels, seen = set(), set(), set()
    for d in dirs:
        if not d.exists():
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if not fn.endswith((".ts", ".tsx")) or ".spec." in fn:
                    continue
                fp = Path(root) / fn
                if fp in seen:
                    continue
                seen.add(fp)
                t = fp.read_text(encoding="utf-8", errors="ignore")
                for h, url in HOOK.findall(t):
                    u = "/" + url.split("${")[0].split("?")[0].strip("/")
                    if len(u) > 1:
                        apis.add((method_of(h), u))
                for _id in re.findall(r"(?:id=\{?['\"]|id:\s*['\"])([\w.]+)", t):
                    if _id in i18n:
                        labels.add(i18n[_id])
                for lit in re.findall(r"(?:label|title):\s*'([^']{2,40})'", t):
                    labels.add(lit)
    return apis, labels

def leading(path):
    return re.split(r"\$\{|/:|:", path, maxsplit=1)[0].rstrip("/") or "/"

OUT.parent.mkdir(exist_ok=True)
rows = []
for rf in sorted(ROUTES_DIR.glob("*Routes.tsx")):
    if rf.name == "AppRoutes.tsx":
        continue
    txt = rf.read_text(encoding="utf-8", errors="ignore")
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)      # strip block comments
    txt = re.sub(r"(?<!:)//[^\n]*", "", txt)              # strip line comments (keep ://)
    area = rf.stem.replace("Routes", "")
    modules = sorted(set(re.findall(r"module:\s*APPModules\.(\w+)", txt)))
    prefixes = sorted({leading(p) for p in re.findall(r"path:\s*[`'\"]([^`'\"]+)", txt) if p.startswith("/")})
    feats = sorted({m.split("/")[0] for m in re.findall(r"import\(['\"][./]*app/([^'\"]+)['\"]\)", txt)})
    app_dirs = [APP / f for f in feats]
    # feature keywords: kebab/lower of the area + its app feature dirs (specific only —
    # broad/route-segment matching over-includes unrelated libs). A lib dir matches when
    # a keyword is a substring of its name. Areas that are separate Nx apps (TaxGPT,
    # MIProduct, Support) legitimately match nothing here — their code isn't in enterprise libs.
    kws = set()
    for src in [area] + feats:
        kws.add(src.lower()); kws.add(kebab(src))
    kws = {k for k in kws if k and (len(k) >= 4 or len(area) <= 4)}
    feat_lib_dirs = [d for d in lib_dirs if any(k in d.name.lower() for k in kws)]
    apis, labels = scan(app_dirs + feat_lib_dirs)
    api_list = sorted(f"{m} {u}" for m, u in apis)
    backends = sorted({backend_of(u) for _, u in apis})
    fp = prefixes + [re.sub(r"(?<!^)(?=[A-Z])", " ", m) for m in modules[:6]] + sorted(labels)[:20]
    rows.append({"area": area, "modules": modules, "route_prefixes": prefixes, "backends": backends,
                 "api_count": len(api_list), "fingerprint_sample": fp[:24], "api_calls": api_list,
                 "feature_dirs": [str(d.relative_to(FE)) for d in (app_dirs + feat_lib_dirs) if d.exists()]})

with OUT.open("w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"areas indexed: {len(rows)}  ->  {OUT}\n")
print(f"{'AREA':<20}{'#mod':>5}{'#route':>7}{'#api':>6}  backends / route-prefixes")
for r in rows:
    print(f"{r['area']:<20}{len(r['modules']):>5}{len(r['route_prefixes']):>7}{r['api_count']:>6}  "
          f"{','.join(r['backends']) or '-'}  {','.join(r['route_prefixes'][:3])}")
print(f"\ntotals: {sum(len(r['modules']) for r in rows)} modules, "
      f"{len(set(a for r in rows for a in r['api_calls']))} distinct API calls")
