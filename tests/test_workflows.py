"""The workflow files, checked without a YAML parser (none is a dependency).

A workflow is only exercised once it is on ``main``, and the weekly one only
on its schedule, so a mistake in it surfaces days after the change that made
it. What can be pinned before then is pinned here: what the weekly run is
allowed to do, that nothing but its own token is interpolated into a script,
that it mentions nobody, and that every ``ledgerlens ...`` line in any
workflow still parses with the CLI as it is today, so a renamed flag fails in
this suite rather than on a Monday.
"""

import re
import shlex
from datetime import date
from pathlib import Path

import pytest

from ledgerlens import summary
from ledgerlens.cli import build_parser
from ledgerlens.ledger_context import LedgerContext

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: The ledgerlens commands each workflow runs, in order. A new workflow, or a new
#: command in one, is added here on purpose: the check below cannot pass by
#: finding nothing.
COMMANDS = {
    "ci.yml": ["generate", "test"],
    "claude.yml": [],
    "weekly.yml": ["generate", "test", "score", "summary", "summary"],
}

_YAML_PREFIX = re.compile(r"^[ \t]*(?:-[ \t]+)?(?:run:[ \t]*)?")  # "- run: " before an inline script


def read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def uncommented(text):
    return re.sub(r"(?m)^[ \t]*#.*$", "", text)


def commands(text):
    """Every ``ledgerlens <args>`` a workflow's scripts run, as argv lists.

    Each line is split the way a shell would split it, into simple commands at
    ``;``, ``&&``, ``||``, ``|`` and ``$( )``, and a command counts when its
    first word (after any ``VAR=value``) is ``ledgerlens``. So ``--cov=ledgerlens``,
    ``from ledgerlens import`` (the Python heredoc in ci.yml) and ``ledgerlens-mcp``
    are not taken for one. Comment lines go first: prose may say "; ledgerlens x".
    """
    text = uncommented(text)
    text = re.sub(r"\\\n[ \t]*", " ", text)        # a continued line is one command
    text = re.sub(r"\$\{\{.*?\}\}", "EXPR", text)  # an expression is one shell word
    found = []
    for line in text.splitlines():
        if "ledgerlens" not in line:
            continue
        lexer = shlex.shlex(_YAML_PREFIX.sub("", line), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:  # not shell (an unpaired quote); the count check below notices a miss
            continue
        words = []
        for token in [*tokens, ";"]:
            if not set(token) <= set("();|&"):
                words.append(token)
                continue
            while words and re.fullmatch(r"\w+=.*", words[0]):  # VAR=value before the command
                words.pop(0)
            if words[:1] == ["ledgerlens"]:
                redirect = [i for i, word in enumerate(words) if set(word) <= set("<>")]
                found.append(words[1:redirect[0]] if redirect else words[1:])
            words = []
    return found


#: Every subcommand the CLI has today, for the loose count in the parse check.
SUBCOMMANDS = sorted(next(a for a in build_parser()._actions if a.dest == "command").choices)


def parse(argv):
    try:
        return build_parser().parse_args(argv)
    except SystemExit:
        pytest.fail(f"`ledgerlens {' '.join(argv)}` no longer parses")


def test_every_workflow_is_listed_here():
    assert {p.name for p in WORKFLOWS.iterdir()} == set(COMMANDS)


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_every_ledgerlens_command_in_a_workflow_still_parses(name):
    text = read(name)
    found = commands(text)
    assert [argv[0] for argv in found] == COMMANDS[name]
    for argv in found:
        parse(argv)
    # Nothing the splitting above cannot see (`time ledgerlens ...`, a line it could not
    # read as shell): every "ledgerlens <subcommand>" outside a comment is a command found.
    names = "|".join(SUBCOMMANDS)
    loose = re.findall(rf"\bledgerlens[ \t]+(?:{names})\b", uncommented(text))
    assert len(loose) == len(found)


def test_the_extractor_reads_scripts_the_way_a_shell_would():
    text = "\n".join([
        "        run: pytest -q --cov=ledgerlens --cov-report=term-missing",
        "          from ledgerlens import evaluate, jets",
        "          from ledgerlens.ingest import load_csv",
        "          # then; ledgerlens generate is seeded",
        "          ledgerlens-mcp --ledger data/ledger.csv",
        "        run: ledgerlens test data/ledger.csv --labels data/labels.csv > log.txt",
        "          cd x && ledgerlens generate --out-dir data; ledgerlens score \\",
        "            data/ledger.csv",
        '          n=$(ledgerlens summary "data/my ledger.csv" --format json | jq .entries)',
        "          TZ=UTC ledgerlens benford data/ledger.csv || true",
        "          echo \"the run's log names ledgerlens\"",
    ])
    assert commands(text) == [
        ["test", "data/ledger.csv", "--labels", "data/labels.csv"],
        ["generate", "--out-dir", "data"],
        ["score", "data/ledger.csv"],
        ["summary", "data/my ledger.csv", "--format", "json"],
        ["benford", "data/ledger.csv"],
    ]


def test_a_flag_the_cli_does_not_have_fails_the_check():
    with pytest.raises(pytest.fail.Exception, match="no longer parses"):
        parse(["summary", "data/ledger.csv", "--fmt", "json"])


# --- the weekly run ----------------------------------------------------------


@pytest.fixture(scope="module")
def weekly():
    return read("weekly.yml")


def block(text, key):
    """The lines under a top-level ``key:``, up to the next line that is not indented."""
    match = re.search(rf"(?m)^{key}:[ \t]*\n((?:[ \t]+\S.*\n|[ \t]*\n)+)", text)
    assert match, f"no top-level {key}: block"
    return [line for line in match.group(1).splitlines() if line.strip()]


def test_the_weekly_run_may_write_issues_and_nothing_else(weekly):
    assert len(re.findall(r"(?m)^[ \t]*permissions:", weekly)) == 1  # no job widens it
    assert block(weekly, "permissions") == ["  contents: read", "  issues: write"]
    assert len(re.findall(r":[ \t]*write\b", uncommented(weekly))) == 1


def test_the_weekly_run_starts_on_its_schedule_or_by_hand_only(weekly):
    triggers = [line for line in block(uncommented(weekly), "on") if re.match(r"  \S", line)]
    assert triggers == ["  schedule:", "  workflow_dispatch:"]
    assert re.search(r'(?m)^    - cron: "23 13 \* \* 1"', weekly)  # Mondays, 13:23 UTC
    # nothing a stranger can cause: no pull request, no comment, no Issue starts it
    for event in ("pull_request", "pull_request_target", "issue_comment", "issues:", "push:"):
        assert event not in uncommented(weekly).replace("issues: write", ""), event


def test_nothing_but_the_runs_own_token_is_interpolated(weekly):
    """An expression pasted into a script is how event text becomes shell."""
    assert re.findall(r"\$\{\{(.*?)\}\}", weekly) == [" github.token "]
    assert "GH_TOKEN: ${{ github.token }}" in weekly
    assert "secrets." not in weekly and "github.event" not in weekly
    assert "QBO_" not in weekly  # no QuickBooks setting reaches CI


def test_the_weekly_workflow_mentions_nobody(weekly):
    """claude.yml answers an "@" mention in an Issue's title or body, also when one is assigned."""
    text = re.sub(r"(?m)^[ \t]*-?[ \t]*uses:.*$", "", weekly)  # actions/checkout@v7 is not a mention
    assert "@" not in text


def test_the_weekly_run_posts_the_summary_it_wrote(weekly):
    argvs = commands(weekly)
    generate, test, score, markdown, as_json = (parse(argv) for argv in argvs)
    assert generate.out_dir == "data"
    assert {a.ledger for a in (test, score, markdown, as_json)} == {"data/ledger.csv"}
    assert {a.labels for a in (test, score, markdown, as_json)} == {"data/labels.csv"}
    assert (markdown.format, markdown.out) == ("markdown", "out/summary.md")
    assert (as_json.format, as_json.out) == ("json", "out/summary.json")
    # out/ is where a summary may be written inside a checkout, and where git ignores it
    assert re.search(r"gh issue create .*--body-file out/summary\.md\b", weekly)
    assert re.search(r"jq -r '[^']*' out/summary\.json\b", weekly)
    assert weekly.count("--label weekly-run") == 2 and "gh label create weekly-run" in weekly
    assert weekly.index("gh issue create") < weekly.index("gh issue close")  # never closes without a successor


def test_the_issue_title_is_built_from_keys_the_summary_has(weekly, small_ledger):
    """jq prints "null" for a missing key and exits 0: a renamed key would title every Issue
    "null of null entries" without failing anything."""
    title = re.search(r"jq -r '\"([^']*)\"' out/summary\.json", weekly).group(1)
    keys = re.findall(r"\\\(\.(\w+)\)", title)
    assert keys == ["run_date", "flagged", "entries"]
    payload = summary.collect(LedgerContext(small_ledger[0]).load(), today=date(2001, 2, 3))
    assert payload["run_date"] == "2001-02-03"
    assert isinstance(payload["flagged"], int) and isinstance(payload["entries"], int)
    assert "rule tier" in title  # "flagged" is the rule tier's count, and the title says so


def test_the_weekly_run_uses_the_actions_ci_already_trusts(weekly):
    def uses(text):
        return set(re.findall(r"(?m)^[ \t]*-?[ \t]*uses:[ \t]*(\S+)", text))

    assert uses(weekly) and uses(weekly) <= uses(read("ci.yml"))
    assert re.search(r"uses: actions/checkout@\S+\n[ \t]+with:\n[ \t]+persist-credentials: false", weekly)
    assert "timeout-minutes:" in weekly


def test_the_workflows_keep_to_what_the_extractor_can_read():
    for name in COMMANDS:
        text = read(name)
        assert "\t" not in text, name
        assert not re.search(r"run:[ \t]*>", text), name  # a folded scalar joins commands into one line


# --- the other two, as the weekly run relies on them ---------------------------


def test_ci_still_has_its_rule_1_job_and_claude_yml_its_mention_gate():
    assert re.search(r"(?m)^  rule-1:$", read("ci.yml"))
    claude = read("claude.yml")
    # one test per place a mention can arrive; the weekly Issue relies on the last two
    assert len(re.findall(r"contains\(github\.event\.[\w.]+, '@claude'\)", claude)) == 5
    assert "contains(github.event.issue.body, '@claude')" in claude
    assert "contains(github.event.issue.title, '@claude')" in claude


def test_ci_prints_the_caveat_with_the_figures_its_gate_logs():
    """The detection gate's log is public, and so is the weekly run's: `test` and `score` print
    the caveat themselves (tests/test_cli.py); the gate's own script has to as well."""
    ci = read("ci.yml")
    assert ci.index("print(evaluate.DETECTION_CAVEAT)") < ci.index("print(evaluate.format_report(m))")
