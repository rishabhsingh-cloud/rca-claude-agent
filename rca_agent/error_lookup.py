"""find_error_reason — one read-only lookup for the REAL failure reason.

Generic UI messages ("Due to Wrong Input Data", "contact support") hide the true
error, which the app stores in one of a few domain-specific MongoDB collections
(NOT in New Relic, NOT in Postgres). This single tool checks all of them for
whatever identifiers the agent has and returns the real reason(s), so the model
never has to remember collection names / field names / filters itself.

Safety mirrors app_mongo.py: read-only `.find()` only, PII-masked output, a hard
per-query time cap (so a broad scan aborts instead of hanging the agent). It only
touches INDEXED / small collections by the identifiers passed, so it stays fast.

Stores checked (verified 2026-07-06 against the live gstanalyst DB):
  - gstr1_exceptions       (by gstin)          -> GSTR-1 import row-processing errors
  - data_retrieval_api_logs(by gstin[+period]) -> GST portal / NIC fetch errors (error_case)
  - reco_invoice_error_logs(by gstin)          -> reconciliation / force-match errors
  - import_logs            (by reference_id)   -> the specific rejected import rows
"""

from __future__ import annotations

import os

from .app_mongo import _QUERY_TIMEOUT_MS, _mask_doc, _not_configured

_LIMIT = 5


def find_error_reason(gstin: str = "", ret_period: str = "",
                      reference_id: str = "", limit: int = _LIMIT) -> dict:
    """Look up the real failure reason across the known error stores.

    Pass any identifiers available from the ticket: `gstin`, `ret_period`
    (e.g. "062026"), and/or `reference_id` (the import job id). Returns the
    real reason(s) per source, PII-masked. Needs at least a gstin or reference_id.
    """
    gstin = (gstin or "").strip()
    ret_period = (ret_period or "").strip()
    reference_id = (reference_id or "").strip()

    if not os.getenv("APP_MONGO_URI", "").strip():
        return _not_configured()
    if not (gstin or reference_id):
        return {"error": "provide at least a gstin or reference_id (import job id)"}

    n = max(1, min(int(limit or _LIMIT), 20))

    try:
        import pymongo
    except ImportError:
        return {"error": "pymongo not installed"}

    db_name = os.getenv("APP_MONGO_DB", "").strip()
    client = None
    findings: dict = {}
    try:
        client = pymongo.MongoClient(
            os.getenv("APP_MONGO_URI").strip(),
            serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
        db = client[db_name] if db_name else client.get_default_database()

        def q(coll, filt, proj, sort=True):
            cur = db[coll].find(filt, proj)
            if sort:
                cur = cur.sort("_id", -1)
            return [_mask_doc(d) for d in cur.limit(n).max_time_ms(_QUERY_TIMEOUT_MS)]

        if gstin:
            # 1) GSTR-1 import row-processing exceptions (only _id indexed -> may
            #    time out on a huge account; caught and reported, not fatal).
            try:
                findings["gstr1_import_errors"] = q(
                    "gstr1_exceptions", {"gstin": gstin},
                    {"exception": 1, "exception_type": 1, "year": 1, "month": 1,
                     "invoice_type": 1}) or "no records for this gstin"
            except pymongo.errors.ExecutionTimeout:
                findings["gstr1_import_errors"] = (
                    "timed out (no gstin index) — pass ret_period to narrow")

            # 2) GST portal / NIC data-retrieval failures (gstin[+ret_period] indexed).
            filt = {"gstin": gstin, "error_case": {"$exists": True, "$ne": ""}}
            if ret_period:
                filt["ret_period"] = ret_period
            findings["portal_fetch_errors"] = q(
                "data_retrieval_api_logs", filt,
                {"error_case": 1, "response": 1, "action": 1, "gst_type": 1,
                 "request_time": 1}) or "no portal/NIC errors for this gstin"

            # 3) reconciliation / force-match errors (small collection).
            findings["reconciliation_errors"] = q(
                "reco_invoice_error_logs",
                {"$or": [{"supplier_gstin": gstin}, {"buyer_gstin": gstin}]},
                {"error_message": 1, "reco_type": 1, "action": 1,
                 "created_at": 1}) or "no reconciliation errors for this gstin"

        if reference_id:
            # 4) the specific rejected import rows (reference_id is indexed).
            findings["rejected_import_rows"] = q(
                "import_logs", {"reference_id": reference_id, "log_type": "invalid"},
                {"doc_number": 1, "data": 1}, sort=False) or (
                    "no rejected rows for this reference_id")
    except Exception as e:
        return {"error": f"lookup failed: {type(e).__name__}: {str(e)[:180]}"}
    finally:
        if client is not None:
            client.close()

    return {
        "identifiers": {"gstin": bool(gstin), "ret_period": ret_period or None,
                        "reference_id": reference_id or None},
        "findings": findings,
        "note": ("error_case 'gov' = the government/NIC portal rejected the request "
                 "(third_party — NOT a platform bug). An `exception` like "
                 "'inv_typ incorrect' = a bad value in the customer's file "
                 "(data/user_side). Values are PII-masked."),
    }
