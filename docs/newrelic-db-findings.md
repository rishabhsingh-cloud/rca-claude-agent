# New Relic + DB findings — RCA agent evidence layer

Consolidated findings from investigating the production-evidence layer (New Relic + MongoDB +
Postgres) and the cross-store links. PII-free (schema/metadata only). Verified dates noted.

## 1. New Relic

**Was silently dead, now fixed.** Our repo names didn't match NR APM appNames, so
`search_nr_errors('gst-enterprise-service')` ran `appName LIKE '%gst-enterprise-service%'` → 0 rows
on every call. Fixed in `newrelic.py` (`_REPO_TO_NR_APP`, commit `121f35d`, deployed).
- `arap-auth-service → prod-arap` (~4622 err/7d), `gst-enterprise-service → saas-prod` (~88 err/7d).
- `background-processes` → cron, not instrumented → intentionally unmapped.
- `gst-prefect-app` → appName still **unknown/unmapped**.
- Other live NR apps not yet mapped to repos: `api-router-prod` (the Mi-Requestid Router),
  `eway_einvoice_prod` (eDoc), `dsc-service`, `commonapi-docker`, `vanilla-refactored`, `mi-blog-backend`.

**Tools (`newrelic.py`):** `search_nr_errors` (service-scoped, needs the mapping),
`search_nr_logs` / `find_request_ids` / `trace_request` (text/Mi-Requestid based), `query_nr` (raw NRQL).
Access = a hand-rolled tool using `NEWRELIC_API_KEY` (NerdGraph); a separate NR **MCP** exists but is unauthorized.

**Mi-Requestid** = MastersIndia's own request-correlation id. It appears as literal
`"Mi-Requestid: <hexid>"` **in log message TEXT**, NOT as an NR attribute (confirmed: the
`Mi-Requestid` attribute query returned 0). NR's *native* correlation is `traceId`/`trace.id`.
`find_request_ids` regexes the id out of logs; `trace_request` follows it across services (masked).

**GSTIN in NR (verified 2026-07-27):** `request.header.Gstin` is a **queryable attribute** on
`Transaction`/`Span` — **~10,115 txns/day, `saas-prod` (gst-enterprise) ONLY**. `prod-arap` carries
none of the gstin headers. So a GSTIN-first NR lookup is a gst-enterprise capability.
- `TransactionError` does **NOT** carry the Gstin header (0) → to filter errors by gstin, query
  `Transaction WHERE error IS true AND request.header.Gstin = '<gstin>'`, not `TransactionError`.
- Other gstin-ish fields: `request.header.Gstins` / `Authorized-Gstins` (sparse), `request.parameters.gstin` (spans).

## 2. MongoDB (`gstanalyst` DB — 219 collections)

**Error stores** (via `find_error_reason` / `query_app_data`):
- `gstr1_exceptions` (~1.26M, only `_id` indexed) — by `gstin`; `exception` can be a raw Python
  error in a `data_transformation` bucket = **our code bug**.
- `data_retrieval_api_logs` (~125M, **indexed on gstin → fast**) — GST portal / NIC fetch errors;
  `error_case:"gov"` = third-party (NIC).
- `reco_invoice_error_logs` (~1080, tiny/partial) — `error_message`, `reco_type`, **`function`
  (the code that threw → localization pointer)**, `organization_id`, `organization_name`.
- `import_logs` (~23M) — indexed on `reference_id` / `doc_number` (NO gstin index). Real reason is
  nested at `data.error = {GSM-code: message}` (e.g. `GSM05` = "supplier GSTIN not in account").
  Key = **doc_number** (from ticket text/screenshot); no reliable GSTIN→reference_id.

**gstin → org master = `businesses`** (verified 2026-07-27): `gstin → legal_name` (org name) +
`arap_id` + `user_id_id`. Secondary: `gstin_logs` (`gstin → legal_name`). `enterprise_gstin_list` =
`{_id, gstin}` registry only (no org). `organizations_organizationmember` is org↔member (not gstin-keyed).

Mongo tool (`query_app_data`) masks recursively **by column name** — safer than NR free-text.

## 3. Postgres (`prodb_arap`)

- `core_components_gstindata` (gstin, NO org column); `core_components_imssyncschedule`
  (gstin→org_id/sub_org_id, but IMS-sync-only).
- **CAVEAT (verified 2026-07-27): enterprise (saas-prod) gstins are NOT in the arap Postgres** — a
  live NR gstin matched 0 rows in both tables (same gstin matched in Mongo → real absence, not a bug).
  So **gstin→org for enterprise customers is in Mongo `businesses`, not arap PG.**

## 4. Cross-store join (verified live 2026-07-27)

- **NR → Mongo confirmed:** a GSTIN pulled from NR (`request.header.Gstin`) is a working Mongo
  query key — returned 3 docs from `data_retrieval_api_logs` and resolved the org in `businesses`.
- Full chain: `NR request.header.Gstin` → GSTIN → Mongo `businesses` (org name) + the error stores
  above (deep, code-pointing evidence). Postgres(arap) is NOT in this path for enterprise gstins.
- Join keys: **GSTIN** (fast into data_retrieval_api_logs / businesses), **doc_number** (import_logs),
  **Mi-Requestid** (stitch NR logs cross-service).

## 5. Masking (investigated, PARKED — prerequisite for surfacing the above)

`app_db._mask_value` value-patterns cover: GSTIN, PAN, email, phone, Aadhaar, IPv4 (and 12-digit
e-way, incidentally as Aadhaar). Verified **LEAKS**: invoice numbers (`INV/2024/00012345`), IRN
(64-hex), doc/order numbers (`ORD-2024-8899`), and **customer/company names in free text**.
Broken **both ways**: over-masks on read (`<present>` misread as "valid" → wrong verdicts; Aadhaar
regex eats e-way numbers) AND absent on the write side (`fix_mr` posts model rationale to GitLab
MRs unmasked). Among NR tools, only `trace_request` masks; `query_app_data` masks by column but
over-masks. **Fix needed = precise masking (both directions), not more masking** — before any
GSTIN-scoped customer evidence is surfaced/posted.

## 6. Actionable next steps (roadmap)

Sequenced. Step 0 gates everything that surfaces customer data.

- [ ] **0. Fix masking (PREREQUISITE).** Precise, both directions:
  - Add value patterns: IRN (`\b[0-9a-fA-F]{32,}\b`), invoice/doc/order numbers, long digit runs.
  - Field-aware masking for **names** (drop columns keyed `name`/`customer`/`supplier`/`party` to `<present>`), since names can't be regex'd out of free text.
  - Fix **over**-masking: the Aadhaar regex eats 12-digit e-way numbers; `<present>` is misread as "valid" → wrong verdicts (use a clearer marker / don't blank structured signal).
  - Add **write-side** masking to `fix_mr` (it posts model rationale to GitLab MRs unmasked).
  - Nothing GSTIN-scoped should surface/post until this is trustworthy.

- [x] **1. Map GSTIN → New Relic. — DONE 2026-07-27.** Added a `gstin` mode to `search_nr_errors`
  (`newrelic.py`) + the tool schema (`tools.py`). Given the ticket's GSTIN it runs, over `Transaction`
  (which carries `request.header.Gstin`; `TransactionError` does NOT):
  `SELECT count(*), average(duration) FROM Transaction WHERE error IS true AND request.header.Gstin = '<gstin>' FACET appName, name, response.status`.
  Design choices found during the build:
  - **Cross-service**, not pinned to one app — the header rides `saas-prod` + `gst-backend-service` +
    `gst-service`, and a customer's errors can be on any of them; facets by `appName` so you see WHERE.
  - **PII-safe by construction** — surfaces only `appName` + transaction `name` + HTTP status + count +
    avg duration (no `uri`/`error.message`), so no masking overhaul needed to ship this. The gstin is a
    filter, never returned. Bonus: transaction `name` is often the exact view/function (feeds step 3).
  - Live-validated: for an error-having customer it returned `gst-backend-service` /
    `GSTR1SummaryViewSet.get_multi_preference` / `500` / 6× / ~30s. Tests in `tests/test_newrelic.py`.
  - For the exception REASON, chain to step 2 (`find_error_reason(gstin=...)`).

- [x] **2. Map that NR output → MongoDB. — DONE 2026-07-27** (was ~90% already built).
  `find_error_reason(gstin | doc_number)` already queries all four stores and is PII-safe:
  - GSTIN → `gstr1_import_errors` / `portal_fetch_errors` (`error_case:"gov"` = NIC/third-party) /
    `reconciliation_errors` (projects **`function`** = code pointer; flags Python-error `exception`s
    with a `_hint`). doc_number → `rejected_import_rows_by_invoice` (the GSM-code reason).
  - **Verified live PII-safe**: leak scan = 0 for both the gstin and doc_number paths.
  - **Closed the chain to code** (step 3): enhanced the `find_error_reason` tool description to say
    "localize with `search_code_local` (+ git_blame) on the returned `function`/symbol", and that it's
    the Mongo step after `search_nr_errors(gstin)`. So NR → Mongo → code is now wired via tool text.
  - **Skipped `businesses`/org `legal_name`** on purpose: a customer name is PII (not value-maskable)
    and isn't RCA evidence (the agent already has the customer from the ticket). Keeps step 2 PII-safe
    by construction, like step 1.

- [ ] **3. Localize to code.** Take the `function` (from `reco_invoice_error_logs`) or the
  exception/GSM code → `search_code_local` in the right repo → the offending code → root cause.
  Closes the loop symptom → code.

- [ ] **4. Chain it in the agent.** Orchestrate/prompt the deliberate pivot
  **GSTIN → NR → Mongo (`businesses` + error store) → code** as one evidence chain; GSTIN used as an
  internal key only, output masked. (Decide: general capability vs. a profile.)

- [ ] **5. Complete the NR service map.** Find `gst-prefect-app`'s appName; optionally map
  `api-router-prod` / `eway_einvoice_prod` / others so more of the stack is reachable by service.

- [ ] **6. (Optional) NR MCP.** Authorize + evaluate the New Relic MCP if richer NRQL/entity access
  is wanted beyond the hand-rolled `newrelic.py` tools.
