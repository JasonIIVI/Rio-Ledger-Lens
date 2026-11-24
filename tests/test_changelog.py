"""CHANGELOG.md and the issue templates: what a reader and a reporter are handed.

The changelog summarises the published release notes, and no test can check a summary's
wording. These hold what a version bump or a careless edit would break: the newest version
listed is the package's, versions and dates run downwards, every version links to its
release, and nothing in it has the shape of a detection figure. The templates are held
above all to what a slip would make expensive: their text becomes the body of every Issue
opened from them."""

import re
from datetime import date
from pathlib import Path

import ledgerlens

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
REPOSITORY = "https://github.com/JasonIIVI/Rio-Ledger-Lens"
HEADING = re.compile(r"## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})")
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def _released():
    """(version, date) for each version heading, in the file's order."""
    return [HEADING.fullmatch(line).groups()
            for line in CHANGELOG.splitlines() if HEADING.fullmatch(line)]


def _key(version):
    return tuple(int(part) for part in version.split("."))


def test_every_second_level_heading_is_unreleased_or_a_dated_version():
    """A heading the pattern does not read (no date, a "v" before the number, a trailing
    space) would drop out of the version and order tests below without a word."""
    headings = [line for line in CHANGELOG.splitlines() if line.startswith("## ")]
    assert headings[0] == "## [Unreleased]"
    assert len(headings) >= 10  # Unreleased, and v0.2.0 to v1.2.2 when this was written
    unread = [line for line in headings[1:] if not HEADING.fullmatch(line)]
    assert not unread, unread


def test_the_newest_version_listed_is_the_packages():
    """A version bump that adds no entry fails here."""
    assert _released()[0][0] == ledgerlens.__version__


def test_versions_run_downwards_with_real_dates_that_never_run_upwards():
    released = _released()
    versions = [_key(version) for version, _ in released]
    assert versions == sorted(set(versions), reverse=True)  # downwards, and none twice
    days = [date.fromisoformat(day) for _, day in released]  # raises on a day that is none
    assert days == sorted(days, reverse=True)


def test_every_version_links_to_its_release_and_unreleased_compares_from_the_newest():
    """The link under [Unreleased] names the newest version, so a bump that renames the
    section and leaves the link comparing from the version before fails too."""
    links = dict(re.findall(r"^\[([^\]]+)\]: (\S+)$", CHANGELOG, re.M))
    newest = _released()[0][0]
    assert links.pop("Unreleased") == f"{REPOSITORY}/compare/v{newest}...HEAD"
    assert links == {version: f"{REPOSITORY}/releases/tag/v{version}" for version, _ in _released()}


def test_nothing_in_the_changelog_has_the_shape_of_a_detection_figure():
    """It says no entry quotes one: a precision, a recall, a lift or a pass rate here would
    travel without the caveat every other surface prints with it. Version numbers
    (0.4.0, pandas 2.2) are not of that shape; a share such as 0.75 would be, and has no
    place in a summary either."""
    found = re.findall(r"(?<![\d.])0\.\d{2,}(?![\d.])|\d\s?%|\d+x\b|times chance", CHANGELOG)
    assert not found, found
    assert "No entry quotes a detection figure." in CHANGELOG


def test_the_issue_templates_cannot_start_the_review_workflow_and_warn_about_real_data():
    """claude.yml runs when a new Issue's title or body holds its mention, and a template's
    text, an HTML comment included, is the body of every Issue opened from it: a mention
    there would start that workflow for every such Issue. The mention is read from the workflow, so
    this follows it if it changes, and every file in the folder is held to it, whatever it is
    called. No template names anyone at all."""
    workflow = (ROOT / ".github" / "workflows" / "claude.yml").read_text(encoding="utf-8")
    mentions = set(re.findall(r"contains\(github\.event\.issue\.(?:body|title), '([^']+)'\)", workflow))
    assert len(mentions) == 1 and next(iter(mentions)).startswith("@")
    mention = next(iter(mentions)).lower()
    names = sorted(path.name for path in TEMPLATES.iterdir())
    assert {"bug_report.md", "feature_request.md"} <= set(names)
    for name in names:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert mention not in text.lower() and "@" not in text, name
        if name.endswith(".md"):
            assert text.startswith("---\nname: ") and "\nabout: " in text, name  # GitHub lists it
            # rule 1 at the front door: the repository is public
            assert "This repository is public." in text and "real ledger" in text, name
