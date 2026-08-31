# RCA cause-pattern playbook

A data-derived guide to *where root causes actually live* and *how to stay calibrated* when
they don't live in application code. Meant to counter the agent's built-in bias toward
code-level explanations.

**Provenance:** mined from `data/recon_precedent.jsonl` — **404 resolved tickets** with
human/label `cause_bucket`, `module`, `symptom`, `keywords`, and `cause_confidence`.
Regenerate the numbers when the corpus grows (see *Maintenance* at the end). Last mined
2026-07-30. This file is content only — it describes patterns; it does not itself change
agent behavior until it's wired into the prompt or a retrieval step.

---

## 1. The base rate: 41% of causes are NOT code

Across 404 resolved tickets:

| cause bucket | share | what it means |
|---|---|---|
| **code** | 59% | a logic/field/handling defect in our source |
| **infra** | 16% | timeout / slowness / capacity / gateway / queue / replica |
| **data** | 12% | bad, missing, unsynced, or mismatched data state |
| **user_side** | 9% | login/OTP/account/permission or a user action, not our bug |
| **config** | 5% | endpoint/credential/server/integration configuration |

**Non-code causes = 41% (164/404).** The agent's evidence sources (New Relic APM errors,
Mongo error stores, code search) are all app/data-layer, so it *sees* code and data causes
readily and is structurally blind to infra/config/user_side. **Do not assume the cause is
code just because code is the majority class and the easiest thing to find.**

---

## 2. Confidence collapses exactly where the cause leaves the app

Share of each bucket that was resolved at **high** confidence:

| bucket | high-confidence | read this as |
|---|---|---|
| code | 45% | when it's code, we usually nail it |
| user_side | 29% | |
| config | 26% | |
| data | 15% | hard to pin |
| **infra** | **11%** | hardest of all — evidence is off-app |

The buckets whose evidence lives *outside* application code and APM are precisely where
confidence is lowest. **The correct response to "no app-layer evidence" is LOW confidence
plus enumerated candidates — not a confident code story.** (See §5.)

---

## 3. Symptom → likely cause bucket (router)

The corpus has a clear vocabulary signal. Use the ticket's own words (title, description,
screenshot text) to widen the hypothesis set *before* diving into code. These are lifts over
the corpus baseline — signals, not proof.

| if the symptom talks about… | suspect | typical real cause |
|---|---|---|
| `timeout`, `slow`/`slowness`, `down`, `unresponsive`, `stuck`, `midway`, `queue`, `proxy`, `replica`, `413`, `502`, `504`, large file | **infra** | gateway/web-server limit, capacity, DB/replica lag, a hung queue/consumer |
| `server`, `ftp`, `vendor`, `fetched … via`, `unable to connect`, credentials, endpoint, `log` | **config** | wrong/expired endpoint, credential, or integration setting |
| `login`, `otp`, `account`, `accept`, `receiving`, `permission`, `access` | **user_side** | auth/access/user action — not our defect |
| `sync`, `matching`, `cancelled`, `active`, `fetching`, `outward`, `reflecting`, snapshot mismatch | **data** | missing/stale/mismatched data state |
| `field`, `row`, `submit`, `prepare`, `ineligible`, `zip`, specific value handling | **code** | logic/field/handling defect |

A symptom can hit more than one row — carry all matching buckets as candidates until
evidence rules them out.

---

## 4. Per-bucket playbook — where the evidence lives

### code (59%)
- **Evidence:** NR APM stack trace → `parse_stack_trace` → `fetch_file_lines` at the pinned
  SHA → `git_blame` + `merge_requests_for_commit`. Mongo error stores (`find_error_reason`)
  when the real exception is a Python error in a `data_transformation`/reco bucket.
- **Confidence is earned by** a real trace or a real stored exception that names a symbol you
  can open. No trace + no stored exception ≠ "it's code."

### infra (16%) — the biggest blind spot
- **Evidence lives OFF the app:** web-server (nginx) access logs / status codes, gateway
  timeouts, capacity/latency trends, queue/consumer health, DB replica lag.
- **A request rejected at the edge never reaches the app** — so there is **no APM Transaction,
  no Mongo error row, no exception**. Empty app evidence here is *expected*, not exonerating.
- **Check:** `search_nr_logs` / `query_nr` for `413` / `"Request Entity Too Large"` / `5xx`
  by path; latency and error-rate trend around onset; a deploy marker; **file/payload size vs
  limits**; whether a consumer/queue is backed up.
- **Tell-tales:** "worked yesterday", "works for small files but not large", "spins then
  nothing", intermittent, time-correlated, or affects many customers at once.

### config (5%)
- **Evidence:** the integration/endpoint/credential setting itself, and the *connection*
  attempt in logs ("unable to fetch via …", auth failures against a vendor/FTP/portal).
- **Check:** is the endpoint/credential correct and current? did it change? is it environment-
  specific (works in one env, not another)? `error_case:"gov"` in `find_error_reason` = the
  government/NIC side, a third-party/config boundary, not our code.

### data (12%)
- **Evidence:** the business documents themselves — `query_app_data` (Mongo) /
  `query_users_db` (Postgres). Reason about presence / null-ness / status / mismatch, never
  raw values.
- **Check:** does the snapshot/return/import doc exist? is a field null? are two sides out of
  sync (2B vs purchase, portal vs local)? was a source record cancelled/inactive?

### user_side (9%)
- **Evidence:** account/permission/plan state (`query_users_db`), and the *action* the user
  took. Often there is no platform defect at all.
- **Check:** is the org registered / the plan active / the permission present? is this a
  login/OTP/access problem? did the user supply a bad file/value (a bad value in *their* file
  is user_side, a crash *on* their value is code)?

---

## 5. Calibration rules (how not to confabulate)

These are the rules the AUT-9957 miss (§6) violated. They apply to every bucket.

1. **Empty app-layer evidence ≠ no cause.** APM + error stores are silent for infra/config/
   user_side by construction. When they come back empty, *widen the layer* (edge, gateway,
   integration, account) — do not fabricate a code narrative to fill the gap.
2. **Never cite a specific commit/MR as "the fix" without blame/diff evidence in hand.** A
   named SHA reads as fact to a human triager. If you didn't open the diff and tie it to *this*
   symptom, don't name it.
3. **When evidence is thin, output LOW confidence + a candidate list** spanning the buckets the
   symptom points at (§3). "Not sure; here are the two most likely layers and how to confirm
   each" is a *better* answer than a confident wrong one.
4. **A specific, plausible, wrong answer is worse than "I don't know."** The triager will act
   on the specific answer. Calibrate down when you're inferring, not observing.
5. **Match the confidence to the bucket's base rate (§2).** An infra conclusion drawn without
   off-app evidence should almost never be "high".

---

## 6. Worked example — AUT-9957 (customer data masked)

- **Symptom:** "Purchase File Not Imported in Data Import tab; no error message shown." Ticket
  carried a GSTIN and an attached file. Module = Data Import (Enterprise).
- **Actual cause (config/infra):** nginx `client_max_body_size` was at the 1 MB default → the
  (large) purchase file was rejected with **413 Request Entity Too Large** at the web-server
  layer, before the app. Fix: raise the limit.
- **What the agent produced (wrong):** a confident *code/infra-async* story — a transient
  Kafka/Mongo timeout swallowed by the GSTR2 consumer — pinned to a specific commit SHA as
  "the fix."
- **Why it missed:** a 413 is rejected at the edge, so there was **no APM Transaction and no
  Mongo import-job row** — every source the agent checked was legitimately empty. Instead of
  going LOW + candidates, it filled the vacuum with the majority-class (code) explanation and
  named an unverified commit. It never considered the **infra** row of §3 despite the
  tell-tales ("large file", "nothing happens", "no error").
- **What this playbook would do:** the symptom words ("file not imported", large file, silent)
  raise **infra/config** as candidates; §4-infra says empty app evidence is expected and to
  check `413`/size-vs-limit in web-server logs; §5 forbids naming a commit without a diff and
  caps confidence when the evidence is off-app.

---

## Maintenance

- **Regenerate** the base rates, confidence table, and signal words whenever
  `data/recon_precedent.jsonl` grows materially (the mining is a short aggregate over the
  labeled fields — buckets, per-bucket high-confidence %, and token lift of
  `keywords`+`symptom` per bucket).
- Keep every number tied to the corpus; if you can't reproduce a figure from the data, remove
  it. This file's authority is that it's measured, not asserted.
- The full 404-row corpus remains the seed for the planned precedent-retrieval agent; this
  playbook is the *distilled, always-on* version of the same signal.
