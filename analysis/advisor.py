"""
Advisory LLM layer — read-only commentary on the assembled SystemState.

Per the converged Codex review (rounds 1-2): the LLM does NOT participate
in alert state transitions. It cannot demote indicators, mutate readings,
flip is_destructive, or otherwise change anything the deterministic
critical path would do. It is a pure observer that emits short
heuristic notes about cross-indicator patterns, missing correlates,
internal inconsistencies, or unusual coincidences.

Patterns that recur across evaluations should be promoted into
deterministic logic (in scoring.py / collectors) and the corresponding
advisory class deleted from the prompt. The advisor is a hypothesis
generator, not a control surface.

Usage:
    advisories = generate_advisories(system_state)
    state.advisories = advisories  # purely informational

Failure mode: API down / key missing / SDK missing → empty list. Logged.
Killswitch: ADVISOR_ENABLED env var (default off until first use).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Iterable

try:
    import anthropic
    _HAVE_ANTHROPIC = True
except ImportError:
    _HAVE_ANTHROPIC = False

from config import ANTHROPIC_API_KEY, INDICATORS

log = logging.getLogger(__name__)

ADVISOR_MODEL = "claude-opus-4-7"
ADVISOR_MAX_TOKENS = 1024
ADVISOR_TIMEOUT_S = 60.0
ADVISOR_ENABLED = os.getenv("ADVISOR_ENABLED", "").lower() in ("1", "true", "yes")


_SYSTEM_PROMPT = """You are an advisory observer for a Taiwan-PRC escalation monitoring tool used by two civilians in Taipei to inform their personal evacuation decision. The deterministic scoring engine has already produced an alert state (green/yellow/amber/red) and you do NOT change it. Your job is narrowly to flag observations a human reviewer should consider when sanity-checking the current state.

==== HARD CONSTRAINTS ====
1. You CANNOT change alert_state, indicator activation, evidence_class, is_destructive, or any other field. The scoring engine is authoritative.
2. Your output is read-only commentary. Code stores it as `advisories` and surfaces it on the dashboard, never feeds it back into scoring.
3. PREFER an empty advisories list. If everything is internally consistent and the alert state matches the evidence, return `{"advisories": []}`. Only emit an advisory when something is genuinely worth a human's attention.
4. Be concrete and evidence-based. Do NOT speculate beyond what the input shows. Cite the indicator(s) and what is unusual.
5. Each advisory is at most 2 sentences. Total output should rarely exceed 3 advisories per run.

==== WHAT TO FLAG ====
Use these advisory types:

- "indicator_concern" — a specific active indicator's evidence looks weak, brittle, or potentially misclassified given its summary and raw signals (but the scoring engine has already activated it). Example: "Cyber active on a single ransomware advisory keyword from TWCERT with no Taiwan-targeting language."

- "cross_indicator" — a pattern across multiple indicators that is either suspiciously coincidental (e.g. all four signals appearing simultaneously with no plausible common-cause story) or strangely contradictory (e.g. PLA force concentration high but allied response zero, suggesting one collector may be feed-broken).

- "missing_correlate" — an active indicator that should have a corroborating signal in another indicator if the underlying event is real. Example: "Force Concentration shows PLA fleet east of Taiwan but Allied Response shows no US/Japan reaction; check if OSINT collector for Tier-1 sources is healthy."

- "context_note" — a relevant factual context worth noting (time of day in Taipei, recent state transition pattern) that helps interpret the current state. Use sparingly; only when materially informative.

==== WHAT NOT TO FLAG ====
- Do NOT restate what the deterministic engine already says ("RED because 2 primaries active" — that is the score_detail).
- Do NOT recommend specific actions ("consider leaving") — that is the alert_label's job.
- Do NOT comment on indicators that are inactive and uneventful.
- Do NOT speculate about future PRC actions or geopolitics.

==== SEVERITY ====
- "info" — useful context but does not undermine the current alert.
- "concern" — the human reviewer should genuinely consider whether the current alert state is correct.

Return ONLY the JSON object specified by the schema. No prose around it."""


def _output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "advisories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["indicator_concern", "cross_indicator",
                                     "missing_correlate", "context_note"],
                        },
                        "indicator_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["info", "concern"],
                        },
                        "message": {"type": "string"},
                    },
                    "required": ["type", "indicator_ids", "severity", "message"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["advisories"],
        "additionalProperties": False,
    }


def _format_input(system_state) -> str:
    """Serialize a SystemState into a compact human-readable block for the LLM."""
    out: list[str] = []
    out.append(f"alert_state: {system_state.alert_state.value}")
    out.append(f"alert_label: {system_state.alert_label}")
    out.append(f"score_detail: {system_state.score_detail}")
    out.append(f"evaluated_at: {system_state.evaluated_at}")
    out.append(f"degraded: {system_state.degraded}")
    if system_state.degraded_feeds:
        out.append(f"degraded_feeds: {', '.join(system_state.degraded_feeds)}")
    out.append(f"overt_hostilities: {system_state.overt_hostilities}")
    out.append("")
    out.append("=== INDICATORS ===")
    for ind_id in sorted(system_state.indicators):
        r = system_state.indicators[ind_id]
        defn = INDICATORS.get(ind_id)
        name = defn.name if defn else f"indicator-{ind_id}"
        category = defn.category.value if defn else "unknown"
        marker = "[ACTIVE]" if r.active else "[inactive]"
        out.append(f"#{ind_id} {name} ({category}) {marker}")
        out.append(f"  evidence_class: {r.evidence_class}  confidence: {r.confidence}")
        out.append(f"  is_destructive: {r.is_destructive}  consecutive_active_runs: {r.consecutive_active_runs}")
        out.append(f"  summary: {r.summary}")
        if r.rationale:
            out.append(f"  rationale: {r.rationale}")
        if r.evidence_quotes:
            for q in r.evidence_quotes[:3]:
                src = q.get("source") or q.get("family") or "?"
                ct = q.get("claim_type") or "?"
                kp = (q.get("key_phrase") or "")[:200]
                why = q.get("why") or ""
                out.append(f"  quote [{src} / {ct}]: {kp}")
                if why:
                    out.append(f"    why: {why}")
        out.append("")
    return "\n".join(out)


def generate_advisories(system_state) -> list[dict]:
    """
    Run the advisory observer over the assembled SystemState.

    Returns a list of advisory dicts:
      {type, indicator_ids: [int], severity: "info"|"concern", message: str}

    Empty list on:
      - ADVISOR_ENABLED unset/false
      - SDK / API key missing
      - LLM call failure
      - Model returns empty
    """
    if not ADVISOR_ENABLED:
        return []
    if not _HAVE_ANTHROPIC:
        log.info("Advisor: anthropic SDK not installed — skipping")
        return []
    if not ANTHROPIC_API_KEY:
        log.info("Advisor: ANTHROPIC_API_KEY not configured — skipping")
        return []
    # Bypass during overt hostilities — judge has nothing useful to add and
    # we don't want a model call on the critical real-emergency path.
    if system_state.overt_hostilities:
        return []

    user_content = _format_input(system_state)

    try:
        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=ADVISOR_TIMEOUT_S,
        )
        response = client.messages.create(
            model=ADVISOR_MODEL,
            max_tokens=ADVISOR_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": _output_schema(),
                },
            },
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        log.warning("Advisor API call failed: %s: %s", type(e).__name__, e)
        return []

    # Extract structured output
    raw = None
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "json":
            raw = getattr(block, "json", None) or getattr(block, "data", None)
            break
        if block_type == "text":
            try:
                raw = json.loads(getattr(block, "text", ""))
                break
            except (json.JSONDecodeError, TypeError):
                continue

    if not isinstance(raw, dict):
        log.warning("Advisor: model returned non-dict output, ignoring")
        return []

    advisories = raw.get("advisories") or []
    if not isinstance(advisories, list):
        log.warning("Advisor: advisories field is not a list, ignoring")
        return []

    # Validate and coerce each entry; drop malformed
    valid_types = {"indicator_concern", "cross_indicator",
                   "missing_correlate", "context_note"}
    valid_severity = {"info", "concern"}
    cleaned: list[dict] = []
    for a in advisories:
        if not isinstance(a, dict):
            continue
        atype = str(a.get("type", ""))
        sev = str(a.get("severity", ""))
        msg = str(a.get("message", "")).strip()
        ids_raw = a.get("indicator_ids") or []
        if atype not in valid_types or sev not in valid_severity or not msg:
            continue
        ids: list[int] = []
        for x in ids_raw if isinstance(ids_raw, list) else []:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        cleaned.append({
            "type": atype,
            "indicator_ids": ids,
            "severity": sev,
            "message": msg[:400],
        })

    # Cap output at 5 entries to bound noise; keep severity=concern first
    cleaned.sort(key=lambda a: 0 if a["severity"] == "concern" else 1)
    cleaned = cleaned[:5]

    if cleaned:
        log.info("Advisor produced %d advisor%s",
                 len(cleaned), "y" if len(cleaned) == 1 else "ies")
        for a in cleaned:
            log.info("  [%s/%s] %s -> %s",
                     a["severity"], a["type"], a["indicator_ids"],
                     a["message"][:120])
    return cleaned
