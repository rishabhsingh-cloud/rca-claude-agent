"""Read-only, PII-masked access to the application's MongoDB (business documents).

Postgres (`app_db.py`) holds users/orgs; MongoDB holds the actual business
documents — GSTR-3B returns, e-invoice/e-way-bill docs, import jobs, etc. This is
the GROUND TRUTH for confirming a `data`-cause hypothesis (e.g. "the autofill
snapshot doc is missing", "a required field is null on this return").

Safety mirrors app_db.py (org rule: customer PII must NEVER enter prompt/response):

  1. READ-ONLY: the tool only ever calls `.find()` — there is no write path in the
     code at all. Forbidden operators that can execute server-side JS or write
     ($where/$function/$out/$merge/...) are rejected before the query runs. Point
     the URI at a READ-ONLY Mongo user as defense in depth.
  2. PII-MASKED output: documents are masked recursively, reusing app_db's field-
     name and value-pattern rules — so a customer value nested deep in a document
     is still redacted. You see field presence/null-ness/shape, not raw values.
  3. BOUNDED: a hard document cap + server-selection/connect timeouts.

Required env (point at a READ REPLICA / read-only user):
  APP_MONGO_URI   e.g. "mongodb://ro_user:pass@10.x.x.x:27017/?authSource=admin"
  APP_MONGO_DB    default database name (optional if the URI already names one)
"""

from __future__ import annotations

import json
import os

from .app_db import _is_pii_column, _mask_value

_DOC_CAP = 50
_TIMEOUT_MS = 8000

# Operators that can run server-side JS or WRITE — never allowed in a filter.
_FORBIDDEN_OPS = {"$where", "$function", "$accumulator", "$out", "$merge", "$expr"}


def _not_configured() -> dict:
    return {"error": "MongoDB not configured — set APP_MONGO_URI in .env "
                     "(point it at a READ-ONLY user)"}


def _check_filter(obj) -> None:
    """Reject server-side-JS / write operators anywhere in the filter."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_OPS:
                raise ValueError(f"operator {k} is not allowed")
            _check_filter(v)
    elif isinstance(obj, list):
        for x in obj:
            _check_filter(x)


def _mask_doc(obj):
    """Recursively mask a Mongo document: PII fields -> presence flag, embedded
    PII patterns in free-text scrubbed, structure otherwise preserved."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_pii_column(k):
                if v is None:
                    out[k] = None
                elif v in ("", {}, [], ()):
                    out[k] = "<empty>"
                else:
                    out[k] = "<present>"
            else:
                out[k] = _mask_doc(v)
        return out
    if isinstance(obj, list):
        return [_mask_doc(x) for x in obj]
    if isinstance(obj, str):
        return _mask_value(obj)
    return obj


def query_mongo(collection: str, filter_str: str = "{}", projection_str: str = "",
                limit: int = 20, database: str = "") -> dict:
    """Run a read-only, capped `find` and return PII-masked documents.

    Returns {"collection": ..., "count": n, "documents": [...]} or {"error": ...}.
    """
    uri = os.getenv("APP_MONGO_URI", "").strip()
    if not uri:
        return _not_configured()

    try:
        filt = json.loads(filter_str) if filter_str.strip() else {}
        proj = json.loads(projection_str) if projection_str.strip() else None
    except json.JSONDecodeError as e:
        return {"error": f"invalid JSON in filter/projection: {e}"}
    if not isinstance(filt, dict):
        return {"error": "filter must be a JSON object"}
    try:
        _check_filter(filt)
    except ValueError as e:
        return {"error": f"rejected: {e}"}

    try:
        import pymongo
    except ImportError:
        return {"error": "pymongo not installed (pip install pymongo)"}

    db_name = (database or os.getenv("APP_MONGO_DB", "")).strip()
    n = max(1, min(int(limit or 20), _DOC_CAP))
    client = None
    try:
        client = pymongo.MongoClient(
            uri, serverSelectionTimeoutMS=_TIMEOUT_MS, connectTimeoutMS=_TIMEOUT_MS)
        db = client[db_name] if db_name else client.get_default_database()
        if db is None:
            return {"error": "no database specified — set APP_MONGO_DB or pass database"}
        docs = [_mask_doc(d) for d in db[collection].find(filt, proj).limit(n)]
    except Exception as e:
        return {"error": f"Mongo query failed: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        if client is not None:
            client.close()

    return {"collection": collection, "count": len(docs), "documents": docs}
