"""Auto-RCA: poll Jira for NEW AUT tickets and start an RCA for the ones that match
the team's rules — into the dashboard, for human review. Never posts to Jira.

Runs as one background thread inside the webapp process (started from the FastAPI
lifespan in app.py), so it shares the SQLite store, the cached Jira/GitLab clients
and — most importantly — the exact same run pipeline as the "Run RCA" button
(`app._run_rca_background`). Manual and automatic runs produce identical rows.

Rules and caps are edited in the dashboard and stored in `app_settings` (db.py);
they are re-read on every tick, so a change applies without a restart.

Eligibility (all must hold):
  * project AUT, issue type in `allowed_types`, statusCategory != Done
  * created AFTER the moment Auto-RCA was switched on (`enabled_at`) — no backfill
  * never investigated before (no verdict, no earlier automatic attempt)
  * not a QA/non-prod-environment ticket: no `exclude_labels` label, and no hostname
    in the text / Environment field whose labels contain an `exclude_env_keywords`
    token (qa3-enterprise.… → "qa3"). AUT tickets carry no structured environment
    field, so the text is the only signal there is.

Idempotency is per ticket (`reviews.auto_run_at`, stamped once by
`db.claim_auto_run`, never cleared), so overlapping poll windows are harmless.
"""
from __future__ import annotations

import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..tickets import flatten_adf
from . import db as _db

# The work types (Jira issue types) the AUT project defines. A type is interpolated
# into JQL only if it is in this tuple, so a value from the UI can never inject JQL.
WORK_TYPES = ("New Feature", "Task", "Bug", "Epic", "Subtask",
              "Enhancement", "Maintenance", "Incident", "Defect")

SETTINGS_KEY = "autorun"

DEFAULT_SETTINGS: dict = {
    "enabled": False,
    "interval_seconds": 120,
    "allowed_types": ["Bug", "Incident"],
    "max_parallel": 2,
    "daily_cap": 30,
    "lookback_hours": 48,          # JQL window; the real "new" test is enabled_at
    "enabled_at": None,            # UTC ISO, stamped when enabled flips off -> on
    "exclude_labels": ["qa-found"],
    "exclude_env_keywords": ["qa", "qa1", "qa2", "qa3", "uat", "staging", "preprod"],
}

LIMITS = {
    "interval_seconds": (30, 3600),
    "max_parallel": (1, 4),
    "daily_cap": (0, 200),
    "lookback_hours": (1, 168),
}

SEARCH_MAX = 50
_TOKEN_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_URL_HOST_RE = re.compile(r"https?://([a-z0-9.-]+)", re.I)
# Bare hostnames pasted without a scheme ("qa3-enterprise.mastersindia-einv.com").
_BARE_HOST_RE = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.mastersindia[a-z0-9.-]*)", re.I)
# Jira's own attachment/media CDN is media.staging.atl-paas.net — it appears in
# every ticket with a screenshot and must not read as a "staging" environment.
_IGNORED_HOST_SUFFIXES = ("atl-paas.net", "atlassian.net", "atlassian.com")


def _log(msg: str) -> None:
    print(f"[autorun] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- settings -----------------------------------------------------------------

def load_settings(store=_db) -> dict:
    """Stored settings merged over the defaults (so fields added later are seeded)."""
    stored = store.get_setting(SETTINGS_KEY, {}) or {}
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(settings: dict, store=_db) -> None:
    store.set_setting(SETTINGS_KEY, settings)


def _clean_tokens(values, field: str) -> list[str]:
    out: list[str] = []
    for v in values:
        t = str(v).strip().lower()
        if not t:
            continue
        if not _TOKEN_RE.match(t):
            raise ValueError(f"{field}: '{t}' is not a valid token "
                             "(letters, digits, '-' or '_', max 40 chars)")
        if t not in out:
            out.append(t)
    return out


def apply_update(current: dict, update: dict, now: datetime | None = None) -> dict:
    """Validate a partial update from the panel and return the new settings dict.
    Raises ValueError with a field-naming message on bad input. Stamps `enabled_at`
    when `enabled` flips off -> on (that moment is the no-backfill watermark)."""
    new = dict(current)
    for field, (lo, hi) in LIMITS.items():
        if update.get(field) is None:
            continue
        v = update[field]
        if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
            raise ValueError(f"{field} must be an integer between {lo} and {hi}")
        new[field] = v
    if update.get("allowed_types") is not None:
        types = [str(t).strip() for t in update["allowed_types"] if str(t).strip()]
        bad = [t for t in types if t not in WORK_TYPES]
        if bad:
            raise ValueError(f"allowed_types: unknown work type {bad[0]!r}")
        if not types:
            raise ValueError("allowed_types must name at least one work type")
        new["allowed_types"] = list(dict.fromkeys(types))
    for field in ("exclude_labels", "exclude_env_keywords"):
        if update.get(field) is not None:
            new[field] = _clean_tokens(update[field], field)
    if update.get("enabled") is not None:
        enabled = bool(update["enabled"])
        if enabled and not current.get("enabled"):
            new["enabled_at"] = (now or datetime.now(timezone.utc)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        new["enabled"] = enabled
    return new


# --- pure rule helpers ----------------------------------------------------------

def parse_jira_ts(s) -> datetime | None:
    """Jira sends '2026-08-03T16:34:11.469+0530' — a +HHMM offset and 3-digit millis
    that older `fromisoformat` rejects. Returns an aware datetime or None."""
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_jql(allowed_types, lookback_hours: int) -> str:
    types = [t for t in allowed_types]
    bad = [t for t in types if t not in WORK_TYPES]
    if bad or not types:
        raise ValueError(f"allowed_types must be a non-empty subset of WORK_TYPES (bad: {bad})")
    lo, hi = LIMITS["lookback_hours"]
    hours = max(lo, min(hi, int(lookback_hours)))
    quoted = ", ".join(f'"{t}"' for t in types)
    # Relative durations are timezone-independent; absolute JQL dates are read in the
    # API user's Jira profile timezone, so the real watermark test is done in Python.
    return (f"project = AUT AND issuetype in ({quoted}) AND statusCategory != Done "
            f"AND created >= -{hours}h ORDER BY created ASC")


def _text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        try:
            return flatten_adf(value) or ""
        except Exception:  # noqa: BLE001 — malformed ADF is not worth a crash
            return ""
    return str(value)


def hosts_in(text: str) -> set[str]:
    """Hostnames mentioned in `text` (URLs and bare *.mastersindia* names), minus
    Atlassian's own CDN/site hosts."""
    found = {h.lower().rstrip(".") for h in _URL_HOST_RE.findall(text)}
    found |= {h.lower().rstrip(".") for h in _BARE_HOST_RE.findall(text)}
    return {h for h in found if not h.endswith(_IGNORED_HOST_SUFFIXES)}


def is_qa_env(issue: dict, settings: dict) -> str | None:
    """Reason string if the ticket looks like a QA/non-prod-environment ticket, else None."""
    fields = issue.get("fields") or {}
    exclude_labels = {l.lower() for l in settings.get("exclude_labels") or []}
    for label in fields.get("labels") or []:
        if str(label).lower() in exclude_labels:
            return f"label:{label}"
    keywords = {k.lower() for k in settings.get("exclude_env_keywords") or []}
    if not keywords:
        return None
    env_text = _text_of(fields.get("environment"))
    text = " ".join([_text_of(fields.get("summary")), _text_of(fields.get("description")),
                     env_text])
    for host in sorted(hosts_in(text)):
        if set(re.split(r"[.-]", host)) & keywords:
            return f"host:{host}"
    env_words = set(re.findall(r"[a-z0-9]+", env_text.lower()))
    hit = env_words & keywords
    if hit:
        return f"environment:{sorted(hit)[0]}"
    return None


def is_eligible(issue: dict, settings: dict, existing_row: dict | None) -> tuple[bool, str]:
    """Pure eligibility check. Returns (ok, reason) — the reason is logged on skips."""
    key = issue.get("key") or ""
    fields = issue.get("fields") or {}
    if not key.startswith("AUT-"):
        return False, "not an AUT ticket"
    itype = (fields.get("issuetype") or {}).get("name")
    if itype not in (settings.get("allowed_types") or []):
        return False, f"type {itype!r} not in allowed_types"
    cat = ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
    if cat == "done":
        return False, "already Done"
    enabled_at = parse_jira_ts(settings.get("enabled_at"))
    if enabled_at is None:
        return False, "no enabled_at watermark"
    created = parse_jira_ts(fields.get("created"))
    if created is None:
        return False, "no created timestamp"
    if created < enabled_at:
        return False, "created before Auto-RCA was enabled"
    if existing_row is not None:
        if existing_row.get("auto_run_at"):
            return False, "already auto-run once"
        if existing_row.get("bot_rca_json") or existing_row.get("status") != "pending":
            return False, f"already {existing_row.get('status')}"
    why = is_qa_env(issue, settings)
    if why:
        return False, f"qa-env {why}"
    return True, "ok"


# --- the poller ---------------------------------------------------------------

class Poller:
    """One background thread: sleep `interval_seconds`, tick, repeat. Dependencies
    are injected so tests drive `tick()` directly with fakes and no threads."""

    def __init__(self, *, run_rca, sync_issue, jira_factory, store=_db,
                 has_jira=None, boot_delay_s: float = 10.0):
        self._run_rca = run_rca
        self._sync_issue = sync_issue
        self._jira_factory = jira_factory
        self._store = store
        self._has_jira = has_jira or (lambda: get_settings().has_jira)
        self._boot_delay_s = boot_delay_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state: dict = {"state": "not started", "last_poll_at": None,
                             "next_poll_at": None, "last_error": None, "last_found": 0,
                             "last_launched": 0, "last_skipped_qa": 0}
        self._seen_skips: set[tuple[str, str]] = set()

    # -- lifecycle --
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="autorun-poller", daemon=True)
        self._thread.start()
        _log("poller thread started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set(self, **kv) -> None:
        with self._lock:
            self._state.update(kv)

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._state, "poller_alive": self.alive}

    @staticmethod
    def _interval(settings: dict) -> int:
        lo, hi = LIMITS["interval_seconds"]
        try:
            return max(lo, min(hi, int(settings.get("interval_seconds", lo))))
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS["interval_seconds"]

    def _run(self) -> None:
        # Let uvicorn finish booting before the first Jira call.
        if self._stop.wait(self._boot_delay_s):
            return
        while not self._stop.is_set():
            settings = self.run_once()
            interval = self._interval(settings)
            nxt = datetime.now(timezone.utc) + timedelta(seconds=interval)
            self._set(next_poll_at=nxt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if self._stop.wait(interval):
                break
        self._set(state="stopped")

    def run_once(self) -> dict:
        """One guarded tick: never raises. Returns the settings used (for the sleep)."""
        settings = DEFAULT_SETTINGS
        try:
            settings = load_settings(self._store)
            self.tick(settings)
        except Exception as e:  # noqa: BLE001 — the loop must survive anything
            traceback.print_exc()
            self._set(state="error", last_error=f"{type(e).__name__}: {str(e)[:240]}",
                      last_poll_at=_now_iso())
            _log(f"tick failed: {type(e).__name__}: {e}")
        return settings

    # -- the unit of work --
    def tick(self, settings: dict | None = None) -> int:
        """One poll. Returns the number of investigations launched. Raises on
        Jira/transport errors (callers wrap it — see `run_once`)."""
        s = settings or load_settings(self._store)
        now = datetime.now(timezone.utc)
        if not s.get("enabled"):
            self._set(state="disabled", last_poll_at=_now_iso(now), last_error=None)
            return 0
        if not self._has_jira():
            self._set(state="error", last_poll_at=_now_iso(now),
                      last_error="Jira not configured (JIRA_URL / JIRA_EMAIL / JIRA_TOKEN)")
            return 0
        if not s.get("enabled_at"):
            self._set(state="error", last_poll_at=_now_iso(now),
                      last_error="Enabled but no watermark — switch Auto-RCA off and on again")
            return 0

        running = self._store.running_auto_keys()
        runs_today = self._store.count_auto_runs_today(now)
        slots = min(int(s["max_parallel"]) - len(running), int(s["daily_cap"]) - runs_today)
        if slots <= 0:
            state = "daily cap reached" if int(s["daily_cap"]) - runs_today <= 0 else "all slots busy"
            self._set(state=state, last_poll_at=_now_iso(now), last_error=None, last_launched=0)
            return 0

        issues = self._jira_factory().search(build_jql(s["allowed_types"], s["lookback_hours"]),
                                             max_results=SEARCH_MAX)
        if len(issues) >= SEARCH_MAX:
            _log(f"WARN search returned {len(issues)} issues (window truncated at {SEARCH_MAX})")
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        issues = sorted(issues, key=lambda i: parse_jira_ts((i.get("fields") or {}).get("created"))
                        or far_future)

        launched = skipped_qa = 0
        seen: set[str] = set()
        for issue in issues:
            key = issue.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            ok, why = is_eligible(issue, s, self._store.get_ticket(key))
            if not ok:
                if why.startswith("qa-env"):
                    skipped_qa += 1
                self._log_skip(key, why)
                continue
            self._sync_issue(issue)                      # the row must exist to be claimed
            if not self._store.claim_auto_run(key):      # lost a race with a click / other tick
                self._log_skip(key, "claim lost")
                continue
            threading.Thread(target=self._run_rca, args=(key,), daemon=True,
                             name=f"rca-auto-{key}").start()
            launched += 1
            runs_today += 1
            itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name")
            _log(f"launched {key} ({itype}) — {runs_today}/{s['daily_cap']} today")
            if launched >= slots:
                break

        self._set(state="ok", last_poll_at=_now_iso(now), last_error=None,
                  last_found=len(issues), last_launched=launched, last_skipped_qa=skipped_qa)
        return launched

    def _log_skip(self, key: str, why: str) -> None:
        # Log each (ticket, reason) once per process so idle ticks keep the journal quiet.
        if len(self._seen_skips) > 5000:
            self._seen_skips.clear()
        if (key, why) not in self._seen_skips:
            self._seen_skips.add((key, why))
            _log(f"skip {key}: {why}")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Set by the webapp lifespan; None when the poller is not running in this process.
poller: Poller | None = None
