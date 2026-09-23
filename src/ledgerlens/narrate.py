"""Claude-written exception narratives.

What the model does here is narrow and deliberate: it takes an entry that the
deterministic tests already flagged, and writes the paragraph a reviewer would
otherwise write themselves - what this entry is, why it surfaced, what evidence
would resolve it, and which control is implicated.

What the model does *not* do:

- decide whether an entry is a finding
- alter a score, a flag, or an accounting record
- see the ground-truth labels

Everything it returns is advisory text attached to an entry. A named human
still makes every accept / dismiss / escalate call, and that decision is what
gets recorded. The separation is the point: an LLM is good at articulating a
concern and bad at being accountable for one.

Output is strict JSON validated against a schema. Free-form prose from a model
is not something to paste into a workpaper unchecked.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pandas as pd

DEFAULT_MODEL = "claude-sonnet-5"

#: Keys every narrative must contain. A response missing any of them is rejected
#: rather than partially used.
REQUIRED_KEYS = (
    "summary",
    "why_flagged",
    "evidence_to_request",
    "suggested_control",
    "confidence",
)

VALID_CONFIDENCE = ("high", "medium", "low")

SYSTEM_PROMPT = """You are assisting an auditor reviewing flagged journal entries.

For each entry you receive the entry's own details and the deterministic tests that flagged it.
Write the note a reviewer would write before investigating.

Rules you must follow:

1. A flag is a question, not a finding. Never assert that an error, a misstatement or an
   irregularity has occurred. Describe what is unusual and what would resolve it.
2. Never speculate about intent or name a person as responsible. "Keyed by a user outside the
   approved list" is a fact; "someone was trying to hide this" is not.
3. Ground every statement in the data you were given. Do not invent amounts, dates, account
   names, policies or counterparties.
4. Prefer the specific over the general. "Obtain the signed approval for this $9,912 payment"
   beats "obtain supporting documentation".
5. If the tests that fired have an innocent explanation that is more likely than a concerning
   one, say so plainly. An auditor's time is the scarce resource.

Return ONLY a JSON object, no markdown fence and no commentary, with exactly these keys:

{
  "summary": "one sentence describing the entry in business terms",
  "why_flagged": "two or three sentences explaining what is unusual, referencing the tests",
  "evidence_to_request": ["specific document or enquiry", "another one"],
  "suggested_control": "the control this relates to, or 'none' if not applicable",
  "confidence": "high | medium | low - how likely this warrants real investigation"
}"""


class NarrativeError(RuntimeError):
    """Raised when a response cannot be used as a narrative."""


@dataclass
class NarrationResult:
    narratives: dict[str, dict]
    failures: dict[str, str]
    model: str
    entries_requested: int

    @property
    def success_rate(self) -> float:
        return len(self.narratives) / self.entries_requested if self.entries_requested else 0.0

    def describe(self) -> str:
        return (
            f"Narrated {len(self.narratives)}/{self.entries_requested} entries with {self.model} ({len(self.failures)} failed)"
        )


def build_prompt(entry: pd.Series, flags: pd.DataFrame, lines: pd.DataFrame) -> str:
    """Assemble the user-turn content for one entry.

    Only the entry's own data goes in. No labels, no other entries' outcomes,
    nothing that would let the model infer the answer rather than read it.
    """
    flag_text = "\n".join(
        f"- [{f.test_id}] {f.test_name} (severity {f.severity}): {f.reason}"
        for f in flags.itertuples()
    ) or "- (no deterministic test fired)"

    line_text = "\n".join(
        f"- line {row.line_no}: {row.account_code} {row.account_name} | debit {row.debit:,.2f} | credit {row.credit:,.2f} | {row.description}"
        for row in lines.itertuples()
    )

    return """Journal entry {entry_id}

Posted: {posting}
Keyed:  {entered} by {user} (source: {source})
Total:  ${amount:,.2f}

Lines:
{lines}

Tests that flagged this entry:
{flags}

Write the reviewer note as JSON.""".format(
        entry_id=entry["entry_id"],
        posting=pd.to_datetime(entry["posting_date"]).strftime("%Y-%m-%d"),
        entered=pd.to_datetime(entry["entered_at"]).strftime("%Y-%m-%d %H:%M"),
        user=entry["created_by"],
        source=entry["source"],
        amount=entry["entry_amount"],
        lines=line_text,
        flags=flag_text,
    )


def validate(payload: dict) -> dict:
    """Check a parsed response against the contract, or raise."""
    if not isinstance(payload, dict):
        raise NarrativeError("response was not a JSON object")

    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        raise NarrativeError("missing key(s): {}".format(", ".join(missing)))

    if not isinstance(payload["evidence_to_request"], list):
        raise NarrativeError("evidence_to_request must be a list")
    if not payload["evidence_to_request"]:
        raise NarrativeError("evidence_to_request must not be empty")

    confidence = str(payload["confidence"]).strip().lower()
    if confidence not in VALID_CONFIDENCE:
        raise NarrativeError(
            "confidence must be one of {}, got {!r}".format(
                ", ".join(VALID_CONFIDENCE), payload["confidence"])
        )

    for key in ("summary", "why_flagged", "suggested_control"):
        if not str(payload[key]).strip():
            raise NarrativeError(f"{key} must not be empty")

    cleaned = dict(payload)
    cleaned["confidence"] = confidence
    cleaned["evidence_to_request"] = [
        str(item).strip() for item in payload["evidence_to_request"] if str(item).strip()
    ]
    return cleaned


def parse_response(text: str) -> dict:
    """Parse a model response into a validated narrative.

    Tolerates a markdown fence because models add them regardless of
    instructions, but nothing beyond that - anything else is a failure worth
    seeing rather than papering over.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise NarrativeError(f"response was not valid JSON: {exc}") from exc
    return validate(payload)


class Narrator:
    """Generates narratives for flagged entries via the Claude API.

    The client is injected rather than constructed here so the whole pipeline
    can be tested without a network call or an API key - see tests/test_narrate.py.
    """

    def __init__(self, client=None, model: str = DEFAULT_MODEL, max_tokens: int = 1024) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise NarrativeError(
                    "ANTHROPIC_API_KEY is not set. Add it to .env for local runs, or to the "
                    "repository secrets for CI."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise NarrativeError("the 'anthropic' package is required") from exc
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def narrate_one(self, prompt: str) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The system prompt is identical for every entry in a batch,
                    # so caching it makes a run over hundreds of exceptions
                    # substantially cheaper.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_response(response.content[0].text)

    def narrate(
        self,
        scored: pd.DataFrame,
        flags: pd.DataFrame,
        lines: pd.DataFrame,
        top_n: int = 25,
        skip: set | None = None,
    ) -> NarrationResult:
        """Narrate the highest-risk flagged entries.

        Only the top N are narrated. Running an LLM over a whole population
        would be slow and expensive for no benefit - the entries nobody will
        open do not need a paragraph written about them.
        """
        skip = skip or set()
        queue = scored[scored["risk_score"] > 0]
        queue = queue[~queue["entry_id"].isin(skip)].head(top_n)

        narratives: dict[str, dict] = {}
        failures: dict[str, str] = {}

        for entry in queue.itertuples():
            entry_id = entry.entry_id
            row = scored[scored["entry_id"] == entry_id].iloc[0]
            entry_flags = flags[flags["entry_id"] == entry_id]
            entry_lines = lines[lines["entry_id"] == entry_id]
            try:
                narratives[entry_id] = self.narrate_one(
                    build_prompt(row, entry_flags, entry_lines)
                )
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the batch
                failures[entry_id] = str(exc)

        return NarrationResult(
            narratives=narratives,
            failures=failures,
            model=self.model,
            entries_requested=len(queue),
        )


def format_narrative(narrative: dict) -> str:
    """Render a narrative as the text block that goes in a workpaper."""
    evidence: list[str] = narrative.get("evidence_to_request", [])
    return "\n".join([
        narrative.get("summary", ""),
        "",
        narrative.get("why_flagged", ""),
        "",
        "Evidence to request:",
        *[f"  - {item}" for item in evidence],
        "",
        "Control: {}".format(narrative.get("suggested_control", "none")),
        "Confidence: {}".format(narrative.get("confidence", "unknown")),
    ])
