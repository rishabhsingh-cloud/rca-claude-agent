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


def _period_to_fy_month(ret_period: str) -> tuple[str, str] | None:
    """Convert a ret_period 'MMYYYY' (e.g. '062026') into the (financial_year, month)
    shape gstr1_exceptions actually stores: year='2026-27', month='06'. The GST
    financial year runs Apr–Mar, so Jan–Mar belong to the PREVIOUS start-year
    (Jan 2022 -> FY '2021-22'). Returns None if the period can't be parsed."""
    p = (ret_period or "").strip()
    if len(p) != 6 or not p.isdigit():
        return None
    mm, cal = p[:2], int(p[2:])
    m = int(mm)
    if not (1 <= m <= 12):
        return None
    start = cal if m >= 4 else cal - 1
    return f"{start}-{str(start + 1)[2:]}", mm


# Markers that mean an `exception` string is a Python runtime error (OUR code
# crashing) rather than a GST data-validation message. Kept conservative so a
# validation phrase is never mislabelled as code.
_CODE_ERR_MARKERS = (
    "is not defined", "traceback (most recent call last)", "has no attribute",
    "nonetype", "keyerror", "indexerror", "typeerror", "valueerror",
    "attributeerror", "not subscriptable", "not iterable", "unexpected keyword",
    "positional argument", "division by zero", "cannot import",
)


def _looks_like_code_error(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _CODE_ERR_MARKERS)


def _flag_code_errors(rows, get_text):
    """Tag rows whose error text reads like a Python error (our bug, not the
    customer's data) so the agent localizes in code instead of blaming user_side."""
    for r in (rows if isinstance(rows, list) else []):
        if _looks_like_code_error(get_text(r)):
            r["_hint"] = ("reads like a CODE exception (our bug) — localize by grepping the "
                          "named symbol + git_blame; do NOT classify as user_side/data")
    return rows


def _import_error_text(r) -> str:
    """Join the messages in an import_logs row's data.error dict (values only)."""
    err = (r.get("data") or {}).get("error") or {}
    return " ".join(str(v) for v in err.values()) if isinstance(err, dict) else str(err)


def find_error_reason(gstin: str = "", ret_period: str = "",
                      reference_id: str = "", doc_number: str = "",
                      limit: int = _LIMIT) -> dict:
    """Look up the real failure reason across the known error stores.

    Pass any identifiers available from the ticket: `gstin`, `ret_period`
    (e.g. "062026"), `reference_id` (the import job id), and/or `doc_number` (the
    invoice/document number — read it from the ticket text OR an attached
    screenshot). Returns the real reason(s) per source, PII-masked. Needs at
    least a gstin, reference_id, or doc_number.
    """
    gstin = (gstin or "").strip()
    ret_period = (ret_period or "").strip()
    reference_id = (reference_id or "").strip()
    doc_number = (doc_number or "").strip()

    if not os.getenv("APP_MONGO_URI", "").strip():
        return _not_configured()
    if not (gstin or reference_id or doc_number):
        return {"error": "provide at least a gstin, reference_id, or doc_number"}

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
            # 1) GSTR-1 import row-processing exceptions. gstr1_exceptions stores
            #    FY-year + month (not MMYYYY) and is only _id-indexed. We prefer the
            #    ticket's period, but a period filter over ~1.26M unindexed docs can
            #    time out finding 5 matches — so on timeout we fall back to the most
            #    RECENT exceptions (fast, early-terminates) with a clear caveat.
            def _run_gstr1(filt):
                rows = q("gstr1_exceptions", filt,
                         {"exception": 1, "exception_type": 1, "year": 1, "month": 1,
                          "invoice_type": 1})
                for r in (rows if isinstance(rows, list) else []):
                    # An `exception` can be a real Python error (our bug), NOT just a
                    # data-validation phrase — flag those so the agent localizes in
                    # code instead of blaming the customer's file.
                    if _looks_like_code_error(r.get("exception", "")):
                        r["_hint"] = ("reads like a CODE exception (our bug) — localize by "
                                      "grepping the named symbol + git_blame; do NOT "
                                      "classify as user_side/data")
                return rows

            fy = _period_to_fy_month(ret_period)
            try:
                if fy:
                    try:
                        rows = _run_gstr1({"gstin": gstin, "year": fy[0], "month": fy[1]})
                        findings["gstr1_import_errors"] = rows or (
                            "no recognized GSTR-1 exception for this gstin + period "
                            "(none recorded — NOT proof there was no error)")
                    except pymongo.errors.ExecutionTimeout:
                        rows = _run_gstr1({"gstin": gstin})   # fast: early-terminates at 5
                        findings["gstr1_import_errors"] = {
                            "warning": "could not scope to the requested period without a DB "
                                       "index (scan timed out) — showing the most RECENT "
                                       "exceptions instead; they may be from a different period",
                            "rows": rows or "none recorded",
                        }
                else:
                    rows = _run_gstr1({"gstin": gstin})
                    findings["gstr1_import_errors"] = rows or (
                        "no recognized GSTR-1 exception for this gstin "
                        "(none recorded — NOT proof there was no error)")
            except pymongo.errors.ExecutionTimeout:
                findings["gstr1_import_errors"] = (
                    "timed out scanning gstr1_exceptions (no gstin index on ~1.26M docs) "
                    "— result unavailable; a DB index on (gstin, year, month) would fix this")

            # 2) GST portal / NIC data-retrieval failures (gstin[+ret_period] indexed).
            filt = {"gstin": gstin, "error_case": {"$exists": True, "$ne": ""}}
            if ret_period:
                filt["ret_period"] = ret_period
            findings["portal_fetch_errors"] = q(
                "data_retrieval_api_logs", filt,
                {"error_case": 1, "response": 1, "action": 1, "gst_type": 1,
                 "request_time": 1}) or "no portal/NIC errors for this gstin"

            # 3) reconciliation / force-match errors (small collection). `function`
            #    names the code that threw -> a direct code-localization pointer.
            reco = q(
                "reco_invoice_error_logs",
                {"$or": [{"supplier_gstin": gstin}, {"buyer_gstin": gstin}]},
                {"error_message": 1, "reco_type": 1, "action": 1, "function": 1,
                 "match_status": 1, "bucket_type": 1, "created_at": 1})
            _flag_code_errors(reco, lambda r: r.get("error_message", ""))
            findings["reconciliation_errors"] = reco or "no reconciliation errors for this gstin"

        # import_logs (23M docs) is opened by its INDEXED keys only: reference_id
        # (the import job id) or doc_number (the invoice/document number). We pull
        # just data.error (the GSM-code reasons), never the whole PII-heavy invoice.
        _imp_proj = {"doc_number": 1, "reference_id": 1, "log_type": 1,
                     "invoice_type": 1, "created": 1, "data.error": 1}
        if reference_id:
            # reference_id is stored as an INT in Mongo — a string filter matched
            # nothing (the lookup was silently dead). Coerce to int.
            ref_q = int(reference_id) if reference_id.isdigit() else reference_id
            rows = q("import_logs", {"reference_id": ref_q, "log_type": "invalid"},
                     _imp_proj, sort=False)
            _flag_code_errors(rows, _import_error_text)
            findings["rejected_import_rows"] = rows or "no rejected rows for this reference_id"

        if doc_number:
            # A document number is indexed on import_logs (fast) but can recur across
            # customers, so also filter to the ticket's gstin (the raw value is used
            # in the query; masking only affects the OUTPUT). If gstin doesn't match
            # (e.g. misread from a screenshot), broaden to doc_number-only and flag it.
            base = {"doc_number": doc_number, "log_type": "invalid"}
            filt = dict(base)
            if gstin:
                filt["$or"] = [{"data.supplier_gstin": gstin},
                               {"data.customer_gstin": gstin}, {"data.gstin": gstin}]
            rows = q("import_logs", filt, _imp_proj, sort=False)
            broadened = False
            if not rows and gstin:
                rows = q("import_logs", base, _imp_proj, sort=False)
                broadened = bool(rows)
            _flag_code_errors(rows, _import_error_text)
            if not rows:
                findings["rejected_import_rows_by_invoice"] = (
                    "no rejected rows for this invoice/document number "
                    "(may be misread from the screenshot, or a different period)")
            elif broadened:
                findings["rejected_import_rows_by_invoice"] = {
                    "warning": "the gstin did not match this document number — showing all "
                               "customers with it; confirm it's the right one before citing",
                    "rows": rows,
                }
            else:
                findings["rejected_import_rows_by_invoice"] = rows
    except Exception as e:
        return {"error": f"lookup failed: {type(e).__name__}: {str(e)[:180]}"}
    finally:
        if client is not None:
            client.close()

    return {
        "identifiers": {"gstin": bool(gstin), "ret_period": ret_period or None,
                        "reference_id": reference_id or None,
                        "doc_number": doc_number or None},
        "findings": findings,
        "note": ("Classify each reason by READING it, do not assume: an `exception` "
                 "that looks like a Python error (e.g. \"name 'x' is not defined\", a "
                 "traceback, 'has no attribute') is OUR CODE crashing -> code bucket, "
                 "localize by grepping the symbol + git_blame, NOT user_side (see any "
                 "_hint on the record). A GST-validation phrase ('inv_typ incorrect', "
                 "'GSTIN does not exist') = a bad value in the customer's file "
                 "(data/user_side). error_case 'gov' in portal_fetch_errors = the "
                 "government/NIC portal rejected it (third_party — not our bug). Values "
                 "are PII-masked; an empty result means 'none recorded', NOT 'no error'."),
    }
