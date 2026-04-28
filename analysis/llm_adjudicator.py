"""
LLM adjudicator for ambiguous WEAK keyword matches.

When the keyword pipeline has hits that are above the count/source threshold
but consist only of WEAK terms (no STRONG concrete-action signal), this
module asks Claude Haiku 4.5 to read the matched-sentence snippets and
classify them as genuine escalation, routine posturing, or undetermined.

Hard constraints (per converged design):
  - Never the SOLE trigger for an alert. Caller decides whether to invoke.
  - NEVER vetoes a STRONG signal. Caller wires that ordering.
  - On any failure (no API key, SDK missing, network error, malformed
    output), returns "undetermined" with the failure reason recorded —
    never throws. Operational effect upstream: don't fire on weak
    signals; log "adjudication unavailable".
  - The system prompt is frozen and prompt-cached.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

try:
    import anthropic
    _HAVE_ANTHROPIC = True
except ImportError:
    _HAVE_ANTHROPIC = False

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

ADJUDICATOR_MODEL = "claude-haiku-4-5"
ADJUDICATOR_MAX_TOKENS = 300
ADJUDICATOR_TIMEOUT_S = 30.0

VERDICTS = ("yes", "no", "undetermined")


# ---------------------------------------------------------------------------
# System prompt — frozen, prompt-cached. Do not interpolate dynamic content
# (timestamps, request IDs, etc.) into this string — that would invalidate
# the cache prefix on every call.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a Taiwan-PRC escalation analyst supporting a personal evacuation-decision tool used by two residents of Taipei. Their dashboard is at GREEN by default; an alert state higher than YELLOW asks them to consider preparing to leave the city. False positives are catastrophic — they either cause unnecessary panic or train the users to ignore future alerts. False negatives are also catastrophic but a different problem; this tool is one of many signals the users monitor.

Your single job: read short text snippets that matched WEAK keyword categories ("aircraft carrier", "amphibious", "carrier strike", "combat readiness patrol", "PLARF", etc.) and decide whether the snippets, taken together, describe genuine escalation toward PRC military action against Taiwan, or routine activity.

Apply this domain knowledge:

1. PLA aircraft enter Taiwan's ADIZ on most days. Daily MND bulletins routinely use the words "aircraft", "fighter", "naval vessels", "carrier" — these are background pressure, not escalation. Mere presence of these terms in MND text is not a signal.

2. PLA conducts named exercises ("Joint Sword 2024-A", "Strait Thunder", annual readiness patrols) periodically. Exercise framing is sometimes accurate (rehearsal that returns to base) and sometimes used to mask staging that does not return to base. Look for concrete, costly, externally observable administrative acts that exceed what an exercise normally requires (e.g. civilian ferry requisition, reserve call-ups, blood drives — these would be STRONG signals handled separately, but if they appear alongside WEAK matches, they raise confidence).

3. Capability and doctrine vocabulary appears constantly in OSINT analyst commentary, PLA propaganda, and academic papers. Phrases like "PLA carrier strike capability", "amphibious deployment doctrine", "fighter forward-deploy posture" are commentary, not events.

4. Speculation and hypotheticals are not events. Watch for "analysts fear", "could", "may", "if China decides", "preparation for possible", "rumored", "allegedly". These are descriptive of someone's opinion, not of an act that has occurred.

5. Multiple tweets describing the same MND bulletin are not multiple sources of evidence — they are one event with several propagation paths. If snippets clearly reference the same underlying observation, treat as one event.

6. Geographic relevance matters. "Aircraft carrier transit" near Russia, the Persian Gulf, or unrelated US Navy operations is not relevant even when an OSINT account that usually covers Taiwan tweets it.

7. Concrete numbers, named locations, and named units increase confidence; unnamed actors and round numbers ("multiple ships", "many aircraft") decrease it.

Output ONLY a JSON object with this exact shape:
{"verdict": "yes" | "no" | "undetermined", "rationale": "<one sentence, ≤140 chars>"}

Verdict definitions:
- "yes" — snippets describe genuine escalation beyond routine PRC posturing. Use this rarely. Requires concrete observable acts or a clearly unusual deviation from background, not just keyword density.
- "no" — snippets describe routine PLA pressure, exercises, doctrine, or unrelated military activity. Default for ambiguous-but-leaning-routine cases.
- "undetermined" — snippets are too sparse, too speculative, or too contradictory to classify either way. Use when you genuinely cannot tell.

Bias toward "no" and "undetermined" when uncertain. Be strict. Do not output anything other than the JSON object."""


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WeakMatchSnippet:
    """One matched sentence + its source + the WEAK keywords it matched."""
    source: str            # e.g. "MND", "osint:sentdefender"
    matched_terms: list[str]
    sentence: str          # the matching sentence (or short context window)


@dataclass
class AdjudicationResult:
    verdict: str           # "yes" | "no" | "undetermined"
    rationale: str
    available: bool        # False if API key/SDK missing or call failed


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def adjudicate_weak_signal(
    indicator_name: str,
    snippets: Iterable[WeakMatchSnippet],
) -> AdjudicationResult:
    """
    Ask Claude Haiku 4.5 whether the WEAK-keyword snippets, taken together,
    describe genuine escalation. Returns ('undetermined', reason) for any
    failure path; never raises.
    """
    snippet_list = list(snippets)

    if not snippet_list:
        return AdjudicationResult("no", "No snippets supplied.", available=True)

    if not _HAVE_ANTHROPIC:
        return AdjudicationResult(
            "undetermined",
            "anthropic SDK not installed — adjudication unavailable.",
            available=False,
        )

    if not ANTHROPIC_API_KEY:
        return AdjudicationResult(
            "undetermined",
            "ANTHROPIC_API_KEY not configured — adjudication unavailable.",
            available=False,
        )

    user_content = _format_user_content(indicator_name, snippet_list)

    try:
        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=ADJUDICATOR_TIMEOUT_S,
        )
        response = client.messages.create(
            model=ADJUDICATOR_MODEL,
            max_tokens=ADJUDICATOR_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string", "enum": list(VERDICTS)},
                            "rationale": {"type": "string"},
                        },
                        "required": ["verdict", "rationale"],
                        "additionalProperties": False,
                    },
                }
            },
        )
    except Exception as e:
        log.warning("LLM adjudicator API call failed: %s: %s", type(e).__name__, e)
        return AdjudicationResult(
            "undetermined",
            f"Adjudicator API failed: {type(e).__name__}",
            available=False,
        )

    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("LLM adjudicator returned non-JSON: %r (%s)", text[:200], e)
        return AdjudicationResult(
            "undetermined",
            "Adjudicator returned malformed output.",
            available=False,
        )

    verdict = data.get("verdict", "undetermined")
    if verdict not in VERDICTS:
        log.warning("LLM adjudicator returned unknown verdict %r", verdict)
        verdict = "undetermined"

    rationale = (data.get("rationale") or "").strip()[:200]
    if not rationale:
        rationale = "(no rationale provided)"

    return AdjudicationResult(verdict, rationale, available=True)


# ---------------------------------------------------------------------------
# User-message formatting
# ---------------------------------------------------------------------------

def _format_user_content(indicator_name: str, snippets: list[WeakMatchSnippet]) -> str:
    """
    Render snippets as a compact, faithful list. Do NOT pre-summarize —
    the model needs the exact wording to resolve ambiguity. Truncate
    individual sentences to keep the request bounded; cap total snippets
    at 30.
    """
    MAX_SENTENCE_CHARS = 500
    MAX_SNIPPETS = 30

    lines = [
        f"Indicator: {indicator_name}",
        f"Number of matching sentences: {len(snippets)} (showing up to {MAX_SNIPPETS} below).",
        "",
        "Matched snippets (each is one sentence that contained one or more WEAK keywords):",
    ]

    for i, s in enumerate(snippets[:MAX_SNIPPETS], 1):
        sentence = s.sentence.strip().replace("\n", " ")
        if len(sentence) > MAX_SENTENCE_CHARS:
            sentence = sentence[:MAX_SENTENCE_CHARS] + "..."
        terms = ", ".join(s.matched_terms[:8])
        lines.append(f"[{i}] source={s.source} | matched={terms}")
        lines.append(f"    {sentence}")

    lines.append("")
    lines.append(
        'Return ONLY the JSON object {"verdict": ..., "rationale": ...} described in your instructions.'
    )

    return "\n".join(lines)
