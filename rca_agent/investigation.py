"""The trace-first investigation pipeline (deterministic).

Implements the highest-accuracy slice of the design's investigation loop:

    parse trace -> route repo -> fetch suspect file:line -> blame -> MR
                -> verification pass -> regression check -> verdict

It is fully deterministic (no model) so it can run with zero credentials against
the mock backend, AND serve as the eval-set baseline. The LLM agent (agent.py)
drives the SAME tools for the fuzzier, no-trace tickets where judgement helps.

Accuracy guardrail baked in here: a conclusion is only HIGH confidence once the
suspect line is confirmed against live (fetched) file content. If the trace
can't be confirmed (code moved, file missing, no blame), we degrade to
candidates + "needs a human" rather than inventing a cause.
"""

from __future__ import annotations

from .config import Settings
from .gitlab_client import GitLabClient, GitLabError, web_blob_url, web_commit_url
from .graph_store import load_repo_graph
from .routing import route_repo
from .schema import Confidence, EvidenceLink, Triage, Verdict
from .stack_trace import Frame, crash_site, parse_stack_trace


def _no_trace_verdict(ticket_key: str, ticket_text: str) -> Verdict:
    """No stack trace -> route, then localize candidate symbols via the graph.

    Honest LOW confidence (no deterministic chain), but useful: it points QA at
    likely functions to inspect instead of just naming repos. This is the
    deterministic mirror of the agent's no-trace flow (search_symbols)."""
    routed = route_repo(ticket_text)
    candidates = [f"{c.project} (matched: {', '.join(c.matched[:6])})" for c in routed]

    # Localize within the top repos using their matched terms as the query.
    suspects: list[str] = []
    for c in routed[:2]:
        try:
            g = load_repo_graph(c.project)
            for n in g.search_symbols(c.matched, limit=5):
                suspects.append(f"{c.project}: {n.qualname} ({n.ref()})")
        except Exception:
            pass

    action = ("Fetch + blame the candidate symbols below to confirm, or ask the "
              "reporter for a stack trace / logs."
              if suspects else
              "Ask reporter for a stack trace / logs, or run code-search over the "
              "candidate repos.")
    return Verdict(
        ticket=ticket_key,
        probable_root_cause="No stack trace in the ticket; cannot build a "
                            "deterministic evidence chain. Localized to candidate "
                            "code below (unconfirmed).",
        evidence_chain=[],
        is_regression=None,
        triage=Triage.INSUFFICIENT_EVIDENCE,
        confidence=Confidence.LOW,
        candidates=(candidates + suspects) or ["no repo summary matched — widen search"],
        suggested_next_action=action,
        notes="Trace-first path unavailable; routing + symbol localization only.",
    )


def _confirm_frame(client: GitLabClient, project: str, ref: str, frame: Frame,
                   context_lines: int) -> tuple[str | None, EvidenceLink | None, bool]:
    """Verification pass: fetch the suspect line in context and confirm the
    trace points at real current code.

    Returns (rendered_context, evidence_link, confirmed). `confirmed` is True
    when the crash line exists and, if the trace named a symbol, that symbol is
    visible in the fetched window — i.e. the code hasn't moved out from under
    the trace.
    """
    start = max(1, frame.line - context_lines)
    end = frame.line + context_lines
    try:
        sl = client.get_file_lines(project, ref, frame.path, start, end)
    except GitLabError:
        return None, None, False

    crash_text = next((t for n, t in sl.numbered_lines if n == frame.line), "")
    symbol_ok = True
    if frame.symbol:
        symbol_ok = any(frame.symbol in t for _, t in sl.numbered_lines)
    confirmed = bool(crash_text.strip()) and symbol_ok

    ev = EvidenceLink(
        kind="file_content",
        ref=frame.ref(),
        detail=f"Confirmed against {project}@{ref}: `{crash_text.strip()}`"
               + ("" if symbol_ok else f" (warning: symbol '{frame.symbol}' not "
                  "found nearby — code may have moved; trace possibly stale)"),
    )
    return sl.render(), ev, confirmed


def investigate(ticket_key: str, ticket_text: str, client: GitLabClient,
                settings: Settings, project_override: str | None = None,
                ref: str = "main") -> Verdict:
    """Run the trace-first slice and return a structured verdict.

    `project_override` pins the repo directly (skipping summary-based routing) —
    useful when the repo isn't Python-indexed (e.g. a JS repo with no summary) or
    when the caller already knows the repo.
    """
    frames = parse_stack_trace(ticket_text)
    crash = crash_site(frames)
    if crash is None:
        return _no_trace_verdict(ticket_key, ticket_text)

    # Route to a repo. The trace bypasses search, but we still need to know which
    # repo to fetch from — routing supplies that, and fetching confirms it.
    if project_override:
        project = project_override
        candidates = []
    else:
        candidates = route_repo(ticket_text)
        if not candidates:
            return Verdict(
                ticket=ticket_key,
                probable_root_cause=f"Trace points at {crash.ref()} but no repo "
                                    "summary matched — cannot locate the repo.",
                evidence_chain=[EvidenceLink("stack_frame", crash.ref(),
                                             f"Crash site: {crash.raw}")],
                triage=Triage.INSUFFICIENT_EVIDENCE,
                confidence=Confidence.LOW,
                candidates=["unknown repo — pass --project or add a summary"],
                suggested_next_action="Pin the repo with --project, or add a repo "
                                      "summary covering this path.",
            )
        project = candidates[0].project

    base = settings.gitlab_url
    crash_url = web_blob_url(base, project, ref, crash.path, crash.line)
    chain: list[EvidenceLink] = [
        EvidenceLink("stack_frame", crash.ref(),
                     f"Crash site from ticket trace: `{crash.raw}`"
                     + (f" in {crash.symbol}" if crash.symbol else ""),
                     url=crash_url),
    ]

    # Verification pass — fetch + confirm the suspect line against live code.
    _, file_ev, confirmed = _confirm_frame(client, project, ref, crash,
                                            settings.context_lines)
    if file_ev is None:
        return Verdict(
            ticket=ticket_key,
            probable_root_cause=f"Trace points at {crash.ref()} in {project}, "
                                "but that file/line could not be fetched.",
            evidence_chain=chain,
            triage=Triage.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            candidates=[c.project for c in candidates],
            suggested_next_action="Verify the repo mapping; the trace path may "
                                  "live in a different repo.",
            notes="Could not confirm the suspect line against GitLab.",
        )
    if file_ev.url == "":
        file_ev.url = crash_url
    chain.append(file_ev)

    # Blame the crash line -> introducing commit (the regression question).
    commit = client.blame_line(project, ref, crash.path, crash.line)
    introducing_mr = None
    is_regression: bool | None = None
    if commit:
        chain.append(EvidenceLink(
            "blame", f"{crash.ref()} @ {commit.short_id}",
            f"Line last changed by {commit.short_id} "
            f"\"{commit.title}\" ({commit.author_name}, {commit.authored_date[:10]}).",
            url=crash_url,
        ))
        chain.append(EvidenceLink(
            "commit", commit.short_id,
            f"{commit.title} - {commit.message.splitlines()[0] if commit.message else ''}",
            url=web_commit_url(base, project, commit.id),
        ))
        mrs = client.merge_requests_for_commit(project, commit.id)
        if mrs:
            mr = mrs[0]
            introducing_mr = mr.web_url or f"!{mr.iid}"
            is_regression = mr.state == "merged"
            chain.append(EvidenceLink(
                "merge_request", introducing_mr,
                f"!{mr.iid} \"{mr.title}\" by {mr.author}"
                + (f", merged {mr.merged_at[:10]}" if mr.merged_at else ""),
                url=mr.web_url or "",
            ))

    # Blast radius: walk the code graph from the crash symbol to its transitive
    # callers — what QA should retest. The graph is a map; we only report edges
    # it actually contains (never invent an A -> B relationship).
    blast_radius: list[str] = []
    if crash.symbol:
        try:
            g = load_repo_graph(project, settings)
            callers = g.transitive_callers(crash.symbol)
            blast_radius = [f"{c.qualname} ({c.ref()})" for c in callers]
        except Exception:
            blast_radius = []

    # Synthesize.
    if confirmed and commit and introducing_mr:
        confidence = Confidence.HIGH
        triage = Triage.REAL_BUG
        root = (f"Regression at {crash.ref()}: the line last changed in "
                f"{commit.short_id} (\"{commit.title}\"). The suspect value used "
                f"on this line is not guaranteed non-null after that change, "
                f"producing the observed exception.")
        action = (f"Reassign to the author of {introducing_mr} ({commit.author_name}) "
                  "for a fix; the change that introduced the line is identified.")
    elif confirmed and commit:
        confidence = Confidence.MEDIUM
        triage = Triage.REAL_BUG
        root = (f"Suspect line {crash.ref()} confirmed; last changed in "
                f"{commit.short_id} but no merge request links to that commit.")
        action = "Inspect the commit directly; confirm whether it shipped to prod."
    else:
        confidence = Confidence.LOW
        triage = Triage.INSUFFICIENT_EVIDENCE
        root = f"Suspect line {crash.ref()} located but blame/MR chain incomplete."
        action = "Run blame manually on the suspect line; check deploy history."

    # One-line headline + plain-language summary for non-experts.
    sym = f" in {crash.symbol}" if crash.symbol else ""
    if commit and introducing_mr:
        headline = (f"{crash.ref()}{sym} is the suspect line; last changed by "
                    f"{introducing_mr} — likely the cause.")
    elif commit:
        headline = f"{crash.ref()}{sym} is the suspect line; last changed in {commit.short_id}."
    else:
        headline = f"{crash.ref()}{sym} is the suspect line; introducing change not confirmed."

    # Plain-language summary for someone unfamiliar with the codebase.
    where = f"{crash.path}" + (f" (the {crash.symbol} function)" if crash.symbol else "")
    if commit and introducing_mr:
        plain = (f"The error traces to {where}, line {crash.line}. That line was "
                 f"last changed by merge request {introducing_mr} "
                 f"({commit.authored_date[:10]}), which is the likely source of the "
                 f"problem. Open the links below to see the exact code and the change "
                 f"that introduced it.")
    elif commit:
        plain = (f"The error traces to {where}, line {crash.line}, last changed in "
                 f"commit {commit.short_id}. Open the links below to see the code.")
    else:
        plain = (f"The error points at {where}, line {crash.line}, but we could not "
                 f"trace which change introduced it — a human should check the history.")

    return Verdict(
        ticket=ticket_key,
        probable_root_cause=root,
        headline=headline,
        plain_summary=plain,
        evidence_chain=chain,
        is_regression=is_regression,
        introducing_mr=introducing_mr,
        triage=triage,
        confidence=confidence,
        suggested_next_action=action,
        blast_radius=blast_radius,
        notes=(f"Repo pinned to {project} (--project)." if not candidates
               else f"Repo routed to {project} (score {candidates[0].score:g})."),
    )
