"""The workflow files, checked without a YAML parser (none is a dependency).

``weekly.yml`` and ``claude.yml`` run only from ``main`` (the weekly one on
its schedule or by hand), so a mistake in them surfaces after the change that
made it has merged. What can be pinned before then is pinned here: what the
weekly run is allowed to do, that nothing but its own token is interpolated
into a script, that it mentions nobody, that every ``ledgerlens ...`` line in
any workflow still parses with the CLI as it is today (a renamed flag fails in
this suite rather than on a Monday), and what the step that opens the Issue
does, by running its script against a stand-in for ``gh``.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

from ledgerlens import evaluate, summary
from ledgerlens.cli import build_parser, main
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


_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
#: An fd digit if the word starts with one, the operator, and the whitespace before its target.
_REDIRECT = re.compile(r"((?:(?<=\s)|^)\d+)?(>>|>&|>\||<&|&>>?|>|<)\s*")


def shell_line(line):
    """``line`` with its comment and its redirects removed, the way a shell reads them.

    A ``#`` opens a comment only at the start of a word, outside quotes; inside a
    word (``out/run#1.csv``) or quoted it is a ``#``. A redirect is ``>``, ``>>``,
    ``>|``, ``<``, ``>&``, ``<&`` or ``&>``, an fd digit before it when the word
    starts with one (``2>&1``), and the word after it; the word ends at whitespace or
    at a metacharacter (``;``, ``|``, ``&``, a bracket, another redirect), and a quoted
    part glued to it (``>"$OUT"/s.json``) belongs to it. Outside quotes only.
    """
    out, i, eat_target = [], 0, False
    while i < len(line):
        quoted = _QUOTED.match(line, i)
        if quoted:
            if not eat_target:  # a quoted span inside a target is part of the target
                out.append(quoted.group(0))
            i = quoted.end()
            continue
        ch = line[i]
        if eat_target:
            if ch.isspace() or ch in ";|&()<>":
                eat_target = False  # a metacharacter is read again below, as the shell reads it
                if ch.isspace():
                    out.append(ch)
                    i += 1
                continue
            i += 1  # the target's unquoted part goes with the operator
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        redirect = _REDIRECT.match(line, i)
        if redirect:
            i, eat_target = redirect.end(), True
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def commands(text):
    """Every ``ledgerlens <args>`` a workflow's scripts run, as argv lists.

    Each line is split the way a shell would split it, into simple commands at
    ``;``, ``&&``, ``||``, ``|`` and ``$( )``, and a command counts when its
    first word (after any ``VAR=value``) is ``ledgerlens``. So ``--cov=ledgerlens``,
    ``from ledgerlens import`` (the Python heredoc in ci.yml) and ``ledgerlens-mcp``
    are not taken for one. Comment lines go first: prose may say "; ledgerlens x". Then
    each line loses its trailing comment and its redirects (:func:`shell_line`) before it
    is split.
    """
    text = uncommented(text)
    text = re.sub(r"\\\n[ \t]*", " ", text)        # a continued line is one command
    text = re.sub(r"\$\{\{.*?\}\}", "EXPR", text)  # an expression is one shell word
    found = []
    for line in text.splitlines():
        if "ledgerlens" not in line:
            continue
        lexer = shlex.shlex(shell_line(_YAML_PREFIX.sub("", line)), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""  # comments are gone already (shell_line); a "#" left is a "#"
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
                found.append(words[1:])
            words = []
    return found


#: Every subcommand the CLI has today, for the loose count in the parse check.
SUBCOMMANDS = sorted(next(a for a in build_parser()._actions if a.dest == "command").choices)


def parse(argv):
    try:
        return build_parser().parse_args(argv)
    except SystemExit:
        pytest.fail(f"`ledgerlens {' '.join(argv)}` no longer parses")


def loose_count(text):
    """Every ``ledgerlens <subcommand>`` outside a comment, read the way ``commands`` reads a
    line, so a trailing comment that names a command is not counted against the extractor."""
    shell = "\n".join(shell_line(_YAML_PREFIX.sub("", line)) for line in uncommented(text).splitlines())
    return len(re.findall(rf"\bledgerlens[ \t]+(?:{'|'.join(SUBCOMMANDS)})\b", shell))


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
    assert loose_count(text) == len(found)


def test_a_trailing_comment_that_names_a_command_counts_for_neither_half():
    text = "          ledgerlens generate --out-dir data  # seeded; ledgerlens test reads it next\n"
    assert commands(text) == [["generate", "--out-dir", "data"]] and loose_count(text) == 1


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
        "          ledgerlens score data/ledger.csv --out out/run#1.csv --top 3",
        "          ledgerlens test data/ledger.csv > out/test.log --top 5",
        "          ledgerlens generate --out-dir data  # seeded: the same ledger every week",
        "          ledgerlens test data/ledger.csv --labels data/labels.csv > test.log 2>&1",
        "          ledgerlens summary data/ledger.csv --format json 2>/dev/null >out/s.json",
        "          ledgerlens score data/ledger.csv --top 5 >& out/all.log",
        '          ledgerlens summary "data/#1 ledger.csv" --out "out/a > b.md"  # quoted: text',
        "          ledgerlens benford data/ledger.csv  # the generator's default",
        '          n=$(ledgerlens summary data/ledger.csv --format json 2>/dev/null); echo "$n"',
        "          ledgerlens test data/ledger.csv >out/log; ledgerlens score data/ledger.csv",
        '          ledgerlens score data/ledger.csv --top 5 >"$OUT"/s.json --top 3',
        "          ledgerlens benford data/ledger.csv >| out/b.txt --by created_by|tee out/t.txt",
    ])
    assert commands(text) == [
        ["test", "data/ledger.csv", "--labels", "data/labels.csv"],
        ["generate", "--out-dir", "data"],
        ["score", "data/ledger.csv"],
        ["summary", "data/my ledger.csv", "--format", "json"],
        ["benford", "data/ledger.csv"],
        # a shell reads both of these to the end of the line; a flag hidden after a "#"
        # inside a word, or after a redirect's target, is parsed like any other
        ["score", "data/ledger.csv", "--out", "out/run#1.csv", "--top", "3"],
        ["test", "data/ledger.csv", "--top", "5"],
        # a "#" that starts a word opens a comment (an apostrophe in it is not an unpaired
        # quote), and every redirect form goes with its target; inside quotes both are text
        ["generate", "--out-dir", "data"],
        ["test", "data/ledger.csv", "--labels", "data/labels.csv"],
        ["summary", "data/ledger.csv", "--format", "json"],
        ["score", "data/ledger.csv", "--top", "5"],
        ["summary", "data/#1 ledger.csv", "--out", "out/a > b.md"],
        ["benford", "data/ledger.csv"],
        # a target ends at a metacharacter as well as at whitespace, a quoted part glued to it
        # belongs to it, and `>|` is a redirect: the words after them are the next command's
        ["summary", "data/ledger.csv", "--format", "json"],
        ["test", "data/ledger.csv"],
        ["score", "data/ledger.csv"],
        ["score", "data/ledger.csv", "--top", "5", "--top", "3"],
        ["benford", "data/ledger.csv", "--by", "created_by"],
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


def gate_script(ci):
    """The Python the detection gate hands `python -`, as the runner would."""
    step = ci.split("      - name: Assert detection has not regressed\n", 1)[1]
    body = step.split("python - <<'PY'\n", 1)[1].split("          PY\n", 1)[0]
    return textwrap.dedent(body)


def test_the_gates_script_prints_the_caveat_before_the_figures_it_logs(tmp_path):
    """The detection gate's log is public, and so is the weekly run's: `test` and `score` print
    the caveat themselves (tests/test_cli.py); the gate's own script has to as well. Run, not
    read: a commented-out print, an `if False:` and a line that breaks the heredoc all kept
    the substring in the file."""
    script = gate_script(read("ci.yml"))
    assert "load_labels" in script and "sys.exit(1)" in script
    main(["generate", "--out-dir", str(tmp_path / "data")])  # what the step before it does
    run = subprocess.run([sys.executable, "-"], input=script, cwd=tmp_path,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    printed = " ".join(run.stdout.split())
    assert evaluate.DETECTION_CAVEAT in printed
    assert printed.index(evaluate.DETECTION_CAVEAT) < printed.index("Precision")
    assert printed.endswith("detection quality within expected bounds")
    # and the gate can fail: labels that mark no anomaly put recall at 0, under its floor
    labels = tmp_path / "data" / "labels.csv"
    labels.write_text(re.sub(r"(?m)^([^,\n]+),True,[^\n]*$", r"\1,False,", labels.read_text()))
    failing = subprocess.run([sys.executable, "-"], input=script, cwd=tmp_path,
                             capture_output=True, text=True)
    assert failing.returncode == 1 and "::error::recall 0.000 fell below floor 0.90" in failing.stdout


# --- the step that opens the Issue, run for real against a stand-in gh --------

GH_STUB = r"""#!/bin/bash
# Stands in for gh: records each call (the argument count, the repository it was aimed at,
# then every argument, each ended by a NUL so an empty one or one holding a newline is kept
# whole) and answers the four the step reads from or relies on.
printf '%s\0' "$#" "GH_REPO=${GH_REPO-}" "$@" >> "$GH_LOG"
case "$1 $2" in
  "label create") [ -z "${GH_FAIL_LABEL:-}" ] || exit 1 ;;
  "issue list") [ -z "${GH_FAIL_LIST:-}" ] || exit 1; for number in $GH_PREVIOUS; do echo "$number"; done ;;
  "issue create")
    [ -z "${GH_FAIL_CREATE:-}" ] || exit 1
    while [ $# -gt 1 ]; do [ "$1" != "--body-file" ] || cp "$2" "$GH_POSTED"; shift; done
    echo "https://github.com/example/repo/issues/13" ;;
  "issue close") [ -z "${GH_FAIL_CLOSE:-}" ] || exit 1 ;;
esac
"""


def issue_step(weekly):
    """The script of the "Open the Issue" step, as bash would be handed it."""
    block = weekly.split("      - name: Open the Issue\n", 1)[1].split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


def run_issue_step(weekly, tmp_path, previous="11 12", fail_create=False, fail_label=False,
                   fail_list=False, fail_close=False):
    """Run the step's script against the stand-in. Returns the run and the calls gh saw, each
    an argv list (so an unquoted "$title" shows as twelve arguments, not as one line)."""
    if not (shutil.which("bash") and shutil.which("jq")):
        pytest.skip("needs bash and jq, as the runner has")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text(GH_STUB)
    (stub / "gh").chmod(0o755)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "summary.md").write_text("THE SUMMARY\n\nits last line\n")
    (tmp_path / "out" / "summary.json").write_text('{"run_date": "2001-02-03", "flagged": 3, "entries": 7}')
    env = {
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",  # the stand-in is found first: no network
        "GH_TOKEN": "not-a-token", "GH_LOG": str(tmp_path / "gh.log"),
        "GH_POSTED": str(tmp_path / "posted.md"), "GH_PREVIOUS": previous,
        "GITHUB_REPOSITORY": "example/repo", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "4242", "GITHUB_SHA": "0123abc",
    }
    for name, failing in (("CREATE", fail_create), ("LABEL", fail_label), ("LIST", fail_list),
                          ("CLOSE", fail_close)):
        if failing:
            env[f"GH_FAIL_{name}"] = "1"
    run = subprocess.run(["bash", "-c", issue_step(weekly)], cwd=tmp_path, env=env,
                         capture_output=True, text=True)
    return run, gh_calls(tmp_path / "gh.log")


def gh_calls(log_path):
    """The calls the stand-in recorded, each an argv list, every one aimed at this run's repository."""
    if not log_path.exists():
        return []
    fields = log_path.read_text().split("\0")[:-1]  # every field ends with a NUL
    calls, i = [], 0
    while i < len(fields):
        count, repo = int(fields[i]), fields[i + 1]
        assert repo == "GH_REPO=example/repo", repo
        calls.append(fields[i + 2:i + 2 + count])
        i += 2 + count
    return calls


def test_the_issue_step_posts_the_summary_then_closes_only_the_older_issues(weekly, tmp_path):
    run, calls = run_issue_step(weekly, tmp_path)
    assert run.returncode == 0, run.stderr
    new = "https://github.com/example/repo/issues/13"
    closing = ["--comment", f"Superseded by {new}. Closing is not a review of these numbers."]
    assert calls == [  # one argument per element: a title or a comment left unquoted would be many
        ["label", "create", "weekly-run", "--description", "Opened by the weekly synthetic run",
         "--color", "0E8A16", "--force"],
        ["issue", "list", "--label", "weekly-run", "--state", "open", "--limit", "100",
         "--json", "number", "--jq", ".[].number"],
        ["issue", "create", "--title",
         "Weekly synthetic run 2001-02-03: 3 of 7 entries flagged by the rule tier",
         "--label", "weekly-run", "--body-file", "out/summary.md"],
        ["issue", "close", "11", *closing],
        ["issue", "close", "12", *closing],
    ]
    posted = (tmp_path / "posted.md").read_text()
    # the summary itself, a blank line, then the run's own line: exactly that, so the line is
    # never part of the summary's last list item and never in place of anything
    assert posted == ("THE SUMMARY\n\nits last line\n"
                      "\nRun: https://github.com/example/repo/actions/runs/4242 at commit 0123abc\n")
    assert f"Opened {new}" in run.stdout
    assert "@" not in " ".join(" ".join(argv) for argv in calls) + posted


def test_the_issue_step_with_no_older_issue_closes_nothing(weekly, tmp_path):
    run, calls = run_issue_step(weekly, tmp_path, previous="")
    assert run.returncode == 0, run.stderr  # `set -u` and an empty list
    assert [argv[:2] for argv in calls] == [["label", "create"], ["issue", "list"], ["issue", "create"]]


def test_the_issue_step_closes_nothing_when_the_new_issue_could_not_be_opened(weekly, tmp_path):
    run, calls = run_issue_step(weekly, tmp_path, fail_create=True)
    assert run.returncode != 0
    assert not any(argv[:2] == ["issue", "close"] for argv in calls)  # the last run's Issue stays open


def test_the_issue_step_stops_at_the_first_call_that_fails(weekly, tmp_path):
    """A label that cannot be made is a token or permission problem: said there, not three calls on."""
    run, calls = run_issue_step(weekly, tmp_path, fail_label=True)
    assert run.returncode != 0
    assert [argv[:2] for argv in calls] == [["label", "create"]]


def test_the_issue_step_opens_nothing_when_the_older_issues_cannot_be_listed(weekly, tmp_path):
    """A list that failed quietly would open a new Issue and close nothing: two open at once."""
    run, calls = run_issue_step(weekly, tmp_path, fail_list=True)
    assert run.returncode != 0
    assert [argv[:2] for argv in calls] == [["label", "create"], ["issue", "list"]]


def test_the_issue_step_fails_when_an_older_issue_cannot_be_closed(weekly, tmp_path):
    """The new Issue exists by then; the run still fails, so an older one left open is seen."""
    run, calls = run_issue_step(weekly, tmp_path, fail_close=True)
    assert run.returncode != 0 and calls[-1][:2] == ["issue", "close"]
    assert (tmp_path / "posted.md").exists()


def test_the_issue_step_runs_in_bash_with_the_runs_token_and_nothing_else(weekly):
    """`shell: bash` (the script relies on it), the token as GH_TOKEN, and no `if:` or
    `continue-on-error:` that would let the step be skipped or its failure ignored."""
    step = weekly.split("      - name: Open the Issue\n", 1)[1].split("\n      - ", 1)[0]
    header = step.split("        run: |\n", 1)[0]
    assert [line.strip() for line in header.splitlines()] == [
        "shell: bash", "env:", "GH_TOKEN: ${{ github.token }}"]
    # a YAML key can follow the script as well as precede it: the step's keys, wherever they are
    keys = [line.strip() for line in step.splitlines()
            if line.startswith(" " * 8) and not line.startswith(" " * 9)]
    assert keys == ["shell: bash", "env:", "run: |"]


def test_the_stand_in_records_an_empty_argument_and_one_holding_a_newline(tmp_path):
    """One argument per line could not hold either: the record format is NUL-separated with
    the argument count first, so what the step hands gh is read back exactly."""
    stub = tmp_path / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "GH_LOG": str(tmp_path / "gh.log"), "GH_REPO": "example/repo"}
    subprocess.run([str(stub), "issue", "close", "11", "--comment", "", "two\nlines"], env=env, check=True)
    subprocess.run([str(stub), "issue", "list"], env=env, check=True)
    assert gh_calls(tmp_path / "gh.log") == [
        ["issue", "close", "11", "--comment", "", "two\nlines"], ["issue", "list"]]
