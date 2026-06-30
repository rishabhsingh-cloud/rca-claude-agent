"""Read-only, PII-masked access to the application's Postgres (users/orgs).

This lets the RCA agent VERIFY data-related hypotheses against real account data
(e.g. "is this org actually registered?", "is the user's plan field null?")
instead of guessing — the ground-truth source for the `data` cause bucket.

Three non-negotiable safety layers (org rule: customer PII must NEVER enter the
prompt or response):

  1. READ-ONLY, two ways:
       - the connection opens with `default_transaction_read_only=on`, so the DB
         itself rejects any write even if validation is bypassed; and
       - `_assert_select_only` rejects anything that isn't a single SELECT.
  2. PII-MASKED output: results are masked both by column name (an allow/deny
     map) and by value pattern (GSTIN / PAN / email / phone / Aadhaar regexes),
     so a PII value sitting in an unexpected column is still redacted.
  3. BOUNDED: a row cap is injected if the query has no LIMIT, plus a statement
     timeout on the connection.

Required env (point at a READ REPLICA, never primary):
  APP_PG_DSN   e.g. "host=10.x.x.x port=5432 dbname=app user=ro_user password=..."
  (optional) APP_PG_PII_COLUMNS  extra comma-separated column-name fragments to mask
"""

from __future__ import annotations

import os
import re

_ROW_CAP = 50
_STATEMENT_TIMEOUT_MS = 10_000

# Schema-introspection columns that are NEVER PII — exempt from masking so the
# agent can explore table/column structure (information_schema, pg_catalog).
# Matched by EXACT name; checked before the PII-fragment scan below.
_SAFE_COLUMNS = {
    "table_name", "column_name", "table_schema", "schema_name", "data_type",
    "udt_name", "constraint_name", "sequence_name", "index_name", "table_type",
    "is_nullable", "ordinal_position", "column_default", "character_maximum_length",
    "relname", "attname", "typname", "indexname", "tablename", "schemaname",
}

# Column-name fragments treated as PII -> value fully masked. Substring match,
# case-insensitive (so "user_email", "billing_address", "gstin" all hit).
_PII_COLUMN_FRAGMENTS = {
    "name", "email", "phone", "mobile", "contact", "address", "gstin", "gst_no",
    "pan", "aadhaar", "aadhar", "dob", "birth", "password", "passwd", "secret",
    "token", "api_key", "apikey", "otp", "bank", "account_no", "ifsc", "card",
    # hardened for the org / suborg / user tables (credentials + tax identifiers)
    "username", "vat_number", "cin", "client_id", "sign", "whitelist",
    "turnover", "identity", "user_ref", "pincode", "zipcode",
}

# Value-pattern masking (defense in depth) -> applied to every string cell.
_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b")
_AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")


def _extra_pii_columns() -> set[str]:
    raw = os.getenv("APP_PG_PII_COLUMNS", "")
    return {f.strip().lower() for f in raw.split(",") if f.strip()}


def _is_pii_column(name: str) -> bool:
    low = name.lower()
    if low in _SAFE_COLUMNS:
        return False
    return any(frag in low for frag in (_PII_COLUMN_FRAGMENTS | _extra_pii_columns()))


def _mask_value(val: str) -> str:
    """Redact PII patterns inside a free-text value, keeping a tiny hint."""
    def _keep_ends(m: re.Match) -> str:
        s = m.group(0)
        return s[0] + "*" * (len(s) - 2) + s[-1] if len(s) > 4 else "***"
    out = _GSTIN_RE.sub(_keep_ends, val)
    out = _PAN_RE.sub(_keep_ends, out)
    out = _AADHAAR_RE.sub("****-****-****", out)
    out = _PHONE_RE.sub("**********", out)
    out = _EMAIL_RE.sub(lambda m: m.group(0)[0] + "***@***", out)
    return out


def _mask_cell(column: str, value):
    """Mask a single result cell. PII columns are dropped to a presence flag;
    other string values are scrubbed of embedded PII patterns."""
    if value is None:
        return None
    if _is_pii_column(column):
        # Don't leak the value — only whether it was present and non-empty.
        return "<present>" if str(value).strip() else "<empty>"
    if isinstance(value, str):
        return _mask_value(value)
    return value


# --- read-only SELECT guard ----------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"merge|call|copy|comment|vacuum|reindex|do|set|begin|commit|rollback)\b",
    re.IGNORECASE,
)


def _assert_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("empty query")
    if ";" in stripped:
        raise ValueError("multiple statements are not allowed")
    head = stripped.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise ValueError("only SELECT (or WITH ... SELECT) queries are allowed")
    if _FORBIDDEN.search(stripped):
        raise ValueError("query contains a forbidden (non-read) keyword")


def _cap_rows(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\blimit\b\s+\d+\s*$", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped} LIMIT {_ROW_CAP}"


def _not_configured() -> dict:
    return {"error": "Postgres not configured — set APP_PG_DSN in .env "
                     "(point it at a READ REPLICA with a read-only user)"}


def query_postgres(sql: str) -> dict:
    """Run a read-only, row-capped SELECT and return PII-masked results.

    Returns {"columns": [...], "rows": [[...], ...], "rowcount": n} or {"error": ...}.
    """
    dsn = os.getenv("APP_PG_DSN", "").strip()
    if not dsn:
        return _not_configured()

    try:
        _assert_select_only(sql)
    except ValueError as e:
        return {"error": f"rejected: {e}"}

    try:
        import psycopg
    except ImportError:
        return {"error": "psycopg not installed (pip install 'psycopg[binary]')"}

    capped = _cap_rows(sql)
    # default_transaction_read_only=on makes the SERVER reject writes regardless
    # of our own validation; statement_timeout bounds a runaway query.
    options = (f"-c default_transaction_read_only=on "
               f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}")
    try:
        with psycopg.connect(dsn, options=options, connect_timeout=10) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(capped)
                cols = [d.name for d in (cur.description or [])]
                raw_rows = cur.fetchall()
    except Exception as e:
        return {"error": f"Postgres query failed: {type(e).__name__}: {str(e)[:200]}"}

    rows = [[_mask_cell(cols[i], v) for i, v in enumerate(row)] for row in raw_rows]
    return {"columns": cols, "rows": rows, "rowcount": len(rows)}
