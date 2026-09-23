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

The contract is enforced twice. The request asks the API for JSON matching
:data:`NARRATIVE_SCHEMA` (structured outputs), and :func:`validate` checks the
parsed result again before anything is stored. Free-form prose from a model is
not something to paste into a workpaper unchecked, and neither is JSON that
happens to parse.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pandas as pd

DEFAULT_MODEL = "claude-opus-5"

#: Generous on purpose. Current models think before they answer and those
#: tokens count against this cap; a narrative cut off mid-JSON costs a retry,
#: headroom nobody uses costs nothing.
DEFAULT_MAX_TOKENS = 4096

#: Writing a short reviewer note is not a hard reasoning task.
DEFAULT_EFFORT = "medium"

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

#: What the API is asked to guarantee. Deliberately limited to the keywords
#: structured outputs honour everywhere (types, enum, required,
#: additionalProperties). Non-emptiness is checked in :func:`validate`.
NARRATIVE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence describing the entry in business terms.",
        },
        "why_flagged": {
            "type": "string",
            "description": "Two or three sentences on what is unusual, naming the tests that fired.",
        },
        "evidence_to_request": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific documents or enquiries that would resolve the question.",
        },
        "suggested_control": {
            "type": "string",
            "description": "The control this relates to, or 'none' if not applicable.",
        },
        "confidence": {
            "type": "string",
            "enum": list(VALID_CONFIDENCE),
            "description": "How likely this warrants real investigation.",
        },
    },
    "required": list(REQUIRED_KEYS),
    "additionalProperties": False,
}

#: What each test means to an auditor, in the words the model should use.
#: Kept here rather than in jets.py so detection code stays untouched; the ids
#: and severities mirror the registry and a test asserts they stay in step.
TEST_REFERENCE = (
    ("JET-01", "Round-dollar amount", "Medium",
     "The entry total is an exact round thousand. Real invoices carry cents; a round "
     "number usually means an estimate, an accrual, or a figure somebody chose. All three "
     "are legitimate and all three are worth naming. Resolve with the basis for the amount."),
    ("JET-02", "Weekend posting", "Medium",
     "Keyed on a Saturday or Sunday. The question is why it could not wait for a business "
     "day, and whether this user normally works weekends. Month-end close and scheduled "
     "system jobs are ordinary explanations."),
    ("JET-03", "After-hours posting", "Medium",
     "Keyed outside normal working hours. Compare with the user's usual pattern; a batch "
     "process or an overseas colleague explains most of these."),
    ("JET-04", "Holiday posting", "Medium",
     "Keyed on a US federal holiday, when the office was closed. Same questions as a "
     "weekend posting, with a shorter list of innocent explanations."),
    ("JET-05", "Period-end manual revenue", "High",
     "A manual credit to a revenue account within days of period close. This is the pattern "
     "AU-C 240 directs auditors to test, because it is how revenue is pulled forward. The "
     "resolution is cut-off evidence: when the goods shipped or the service was delivered, "
     "and whether the customer was invoiced in the same period."),
    ("JET-06", "Just under approval threshold", "High",
     "An amount sitting just beneath an approval limit. Sometimes coincidence, sometimes an "
     "invoice split so that no second signature was needed. Ask for the full transaction "
     "trail and whether related entries add up to more than the limit."),
    ("JET-07", "Possible duplicate entry", "High",
     "The same amount, accounts and description as another entry days apart. A reversal, a "
     "corrected re-key or a genuine repeat billing are the ordinary explanations; a double "
     "payment is the one that matters."),
    ("JET-08", "Rare account combination", "Medium",
     "This debit and credit pairing occurs almost nowhere else in the population. Unusual "
     "pairings are how misclassifications and unusual arrangements show up; ask what "
     "business event the entry records and why these accounts were chosen."),
    ("JET-09", "Dormant account activity", "Medium",
     "An account quiet for months suddenly has activity. Ask what changed and who "
     "authorised reopening it."),
    ("JET-10", "Unbalanced entry", "High",
     "Debits do not equal credits. A conforming ERP cannot post this, so either a control "
     "failed or the extract is wrong. Confirm the extract first; a data problem is far more "
     "common than a posting problem."),
    ("JET-11", "Large value outlier", "Medium",
     "Far larger than this account normally sees, measured on a log scale so ordinary "
     "variation does not fire it. Ask for the supporting document and whether the amount "
     "was approved at the right level."),
    ("JET-12", "Manual entry outside approver list", "Low",
     "A manual entry keyed by a user who is not on the approved list. This describes the "
     "control environment more than the entry: the question is about access and "
     "segregation of duties, not about the amount. On its own it is usually low risk, and "
     "it fires on legitimate activity often enough that saying so is helpful."),
)

SEVERITY_WEIGHTS = (("High", 3.0), ("Medium", 2.0), ("Low", 1.0))


def build_system_prompt() -> str:
    """Assemble the system prompt.

    Deterministic by construction - no dates, ids or environment in it - because
    the prompt is cached as a prefix and one changed byte invalidates the cache
    for every request after it. It is also deliberately long enough to cache at
    all: the API silently declines to cache prefixes below roughly a thousand
    tokens, so the test reference below is not padding, it is what makes the
    cache real (and it makes the narratives better).
    """
    tests = "\n".join(
        f"- {test_id} {name} [{severity}]: {meaning}"
        for test_id, name, severity, meaning in TEST_REFERENCE
    )
    weights = ", ".join(f"{sev} = {w:.0f}" for sev, w in SEVERITY_WEIGHTS)
    return f"""You are assisting an auditor reviewing flagged journal entries.

For each entry you receive the entry's own details and the deterministic tests that flagged it.
Write the note a reviewer would write before investigating: what this entry is, why it surfaced,
what evidence would resolve the question, and which control it touches.

Rules you must follow:

1. A flag is a question, not a finding. Never assert that an error, a misstatement or an
   irregularity has occurred. Describe what is unusual and what would resolve it.
2. Never speculate about intent or name a person as responsible. "Keyed by a user outside the
   approved list" is a fact; "someone was trying to hide this" is not. Do not use words such as
   fraud, concealment, manipulation or deliberate unless you are quoting the data.
3. Ground every statement in the data you were given. Do not invent amounts, dates, account
   names, policies or counterparties. Quote amounts exactly as they appear in the entry.
4. Prefer the specific over the general. "Obtain the signed approval for this $9,912 payment"
   beats "obtain supporting documentation". Name the account and the amount in the evidence you
   ask for.
5. If the tests that fired have an innocent explanation that is more likely than a concerning
   one, say so plainly and set confidence to low. An auditor's time is the scarce resource, and
   a note that sends someone chasing a routine system posting wastes it.

How the flags were produced

Two independent tiers score every entry. The first is a set of deterministic journal-entry
tests, each of which produces a written reason; those reasons are what you are given. The second
is an unsupervised model that scores how unusual an entry looks against the population; you are
not given that score and should not guess at it. The risk score you may see is a
severity-weighted count of the distinct tests that fired ({weights}), so a score of 5 means, for
example, one High test and one Medium test. Nothing in the score is a judgement about the entry.

The tests, and what each one is asking:

{tests}

Confidence means how likely the entry warrants a real investigation, not how sure you are of the
facts. Use high when a control may have failed or cut-off must be tested, medium when the entry
is unusual and one document would settle it, and low when the innocent explanation is more
likely than not.

How your note will be used

A named reviewer reads your note next to the entry and records one of three decisions:
accept (the concern is valid and is now being dealt with), dismiss (the entry is explained), or
escalate (someone more senior needs to look). Your note informs that decision; it never makes
it. The reviewer will also see your evidence list as the to-do list for the file, so each item
should be something a person can actually go and get: a named document, a named enquiry to a
named role, or a specific comparison against the ledger.

Style

Plain business English, present tense, no headings and no bullet characters inside the strings.
Do not repeat the flag text verbatim; explain what it means for this entry. Refer to tests by
their ids. Name accounts by name and code as they appear in the lines. Two or three sentences is
the right length for why_flagged; one sentence for the summary.

Not this: "This entry was posted at the weekend to conceal a revenue adjustment."
This: "The entry was keyed on a Sunday by user jchen, outside that user's normal pattern, and
credits Sales Revenue (4000) two days before period close, so cut-off support is the first thing
to obtain."

Return a JSON object with exactly these keys and no others:

{{
  "summary": "one sentence describing the entry in business terms",
  "why_flagged": "two or three sentences explaining what is unusual, referencing the tests",
  "evidence_to_request": ["specific document or enquiry", "another one"],
  "suggested_control": "the control this relates to, or 'none' if not applicable",
  "confidence": "high | medium | low"
}}"""


SYSTEM_PROMPT = build_system_prompt()


class NarrativeError(RuntimeError):
    """Raised when a response cannot be used as a narrative."""


@dataclass
class Usage:
    """Token accounting across a batch, straight from the API's usage blocks.

    Kept because the cost claim for caching is otherwise unverifiable: if
    ``cache_read_input_tokens`` stays at zero, the cache is not working, whatever
    the code says.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")

    def add(self, usage) -> None:
        self.requests += 1
        for name in self.FIELDS:
            setattr(self, name, getattr(self, name) + int(getattr(usage, name, 0) or 0))

    @property
    def cache_read_share(self) -> float:
        total = self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0

    def describe(self) -> str:
        return (
            f"{self.requests} request(s): {self.input_tokens:,} uncached input tokens, "
            f"{self.cache_read_input_tokens:,} read from cache ({self.cache_read_share:.0%} of input), "
            f"{self.cache_creation_input_tokens:,} written to cache, {self.output_tokens:,} output"
        )


@dataclass
class NarrationResult:
    narratives: dict[str, dict]
    failures: dict[str, str]
    model: str
    entries_requested: int
    usage: Usage = field(default_factory=Usage)

    @property
    def success_rate(self) -> float:
        return len(self.narratives) / self.entries_requested if self.entries_requested else 0.0

    def describe(self) -> str:
        return (
            f"Narrated {len(self.narratives)}/{self.entries_requested} entries with {self.model} "
            f"({len(self.failures)} failed)\n{self.usage.describe()}"
        )


def entry_context(
    scored: pd.DataFrame, flags: pd.DataFrame, lines: pd.DataFrame, entry_id: str,
) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    """The three slices that describe one entry: its scored row, its flags, its lines."""
    rows = scored[scored["entry_id"] == entry_id]
    if rows.empty:
        raise KeyError(entry_id)
    return rows.iloc[0], flags[flags["entry_id"] == entry_id], lines[lines["entry_id"] == entry_id]


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
    evidence = [str(item).strip() for item in payload["evidence_to_request"] if str(item).strip()]
    if not evidence:
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
    cleaned["evidence_to_request"] = evidence
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


def response_text(response) -> str:
    """The text block of a response.

    Not ``content[0]``: current models return a thinking block first, and the
    thinking text is empty by default, so indexing blindly either raises or
    returns nothing.
    """
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise NarrativeError("response contained no text block")


def check_stop_reason(response, max_tokens: int) -> None:
    """Refuse to parse a response that did not finish normally."""
    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        raise NarrativeError(
            f"response was cut off at max_tokens={max_tokens}; raise the limit and retry"
        )
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        raise NarrativeError(f"the model declined to write this narrative (refusal: {category})")
    if stop == "model_context_window_exceeded":
        raise NarrativeError("the request exceeded the model's context window")


def _is_auth_error(exc: BaseException) -> bool:
    # By name rather than by import: the SDK is optional at import time and the
    # client is often a test double.
    return type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError")


class Narrator:
    """Generates narratives for flagged entries via the Claude API.

    The client is injected rather than constructed here so the whole pipeline
    can be tested without a network call or an API key - see tests/test_narrate.py.
    """

    def __init__(
        self,
        client=None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = DEFAULT_EFFORT,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client
        self.usage = Usage()

    @property
    def client(self):
        if self._client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise NarrativeError(
                    "ANTHROPIC_API_KEY is not set. Put it in .env for local runs (loaded "
                    "automatically), or in the repository secrets for CI."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise NarrativeError(
                    "the 'anthropic' package is required: pip install -e '.[llm]'"
                ) from exc
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def request_params(self, prompt: str) -> dict:
        """The exact request sent for one entry.

        Separate from the call so tests can inspect it and the cache prefix is
        easy to audit: the system block is byte-identical for every entry and
        carries the cache marker; only the user turn varies.
        """
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": NARRATIVE_SCHEMA},
            },
            "messages": [{"role": "user", "content": prompt}],
        }

    def narrate_one(self, prompt: str) -> dict:
        response = self.client.messages.create(**self.request_params(prompt))
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.add(usage)
        check_stop_reason(response, self.max_tokens)
        return parse_response(response_text(response))

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
        self.usage = Usage()
        _ = self.client  # a missing key should fail once, here, not once per entry
        # Explicit, total ordering: the caller's frame may arrive in any order,
        # and "top N" must mean the same entries every run.
        order = [c for c in ("risk_score", "model_score", "entry_amount") if c in scored.columns]
        queue = scored[scored["risk_score"] > 0].sort_values(
            order + ["entry_id"], ascending=[False] * len(order) + [True], kind="mergesort",
        )
        queue = queue[~queue["entry_id"].isin(skip)].head(top_n)

        narratives: dict[str, dict] = {}
        failures: dict[str, str] = {}

        for entry in queue.itertuples():
            entry_id = entry.entry_id
            row, entry_flags, entry_lines = entry_context(scored, flags, lines, entry_id)
            try:
                narratives[entry_id] = self.narrate_one(
                    build_prompt(row, entry_flags, entry_lines)
                )
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the batch
                if _is_auth_error(exc) and not narratives:
                    # A bad key fails every entry the same way; say so once.
                    raise NarrativeError(f"authentication failed on the first request: {exc}") from exc
                failures[entry_id] = str(exc)

        return NarrationResult(
            narratives=narratives,
            failures=failures,
            model=self.model,
            entries_requested=len(queue),
            usage=self.usage,
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
