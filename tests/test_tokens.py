"""The token store: outside any checkout, mode 600, and refusing what it should."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ledgerlens.connectors import tokens as tokens_module
from ledgerlens.connectors.tokens import (
    DIR_MODE,
    ENV_TOKEN_DIR,
    FIELDS,
    FILE_MODE,
    Tokens,
    TokenStore,
    TokenStoreError,
    default_token_dir,
    repository_root,
    token_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def sample(**overrides) -> Tokens:
    base = Tokens.issued("ACCESS-1", "REFRESH-1", 3600, 100 * 86400, "4620816365", "sandbox",
                         now=NOW)
    return Tokens(**{**base.to_dict(), **overrides})


def realm_one(**overrides) -> Tokens:
    """A record for the file TokenStore.for_realm("sandbox", "1", ...) names."""
    return sample(**{"realm_id": "1", **overrides})


@pytest.fixture
def outside(tmp_path):
    """A directory no git repository contains, which is the only kind the store accepts."""
    if repository_root(tmp_path) is not None:
        pytest.skip(f"{tmp_path} is inside a git repository")
    return tmp_path / "cfg"


def test_round_trip_writes_a_private_file_in_a_private_directory(outside):
    store = TokenStore.for_realm("sandbox", "4620816365", outside)
    assert store.path == outside / "qbo-sandbox-4620816365.json"
    assert store.load() is None
    assert store.save(sample()) == store.path
    assert store.load() == sample()
    assert stat.S_IMODE(store.path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(outside.stat().st_mode) == DIR_MODE
    assert list(json.loads(store.path.read_text())) == list(FIELDS)
    assert store.delete() is True
    assert store.delete() is False
    assert store.load() is None


def test_a_failed_rename_leaves_the_old_file_and_no_temp_behind(outside, monkeypatch):
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(realm_one())

    def full(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tokens_module.os, "replace", full)
    with pytest.raises(OSError, match="disk full"):
        store.save(realm_one(access_token="ACCESS-2"))
    assert store.load() == realm_one()
    assert [p.name for p in outside.iterdir()] == ["qbo-sandbox-1.json"]


@pytest.mark.parametrize("marker", ["directory", "file"])
def test_a_path_inside_a_git_working_tree_is_refused(outside, marker):
    checkout = outside / "checkout"
    (checkout / "deep" / "er").mkdir(parents=True)
    if marker == "directory":
        (checkout / ".git").mkdir()
    else:  # a worktree's .git is a file pointing at the main repository
        (checkout / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    target = checkout / "deep" / "er" / "qbo-sandbox-1.json"
    assert repository_root(target) == checkout
    with pytest.raises(TokenStoreError, match="inside the git repository") as refused:
        TokenStore(target)
    assert str(checkout) in str(refused.value) and ENV_TOKEN_DIR in str(refused.value)
    assert not target.exists()
    assert repository_root(outside) is None


def test_this_checkout_is_refused_wherever_the_path_points_inside_it():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert repository_root(REPO_ROOT) == REPO_ROOT
    for path in (REPO_ROOT / "qbo-sandbox-1.json", REPO_ROOT / "data" / "qbo-sandbox-1.json",
                 REPO_ROOT / "data" / "not-there" / "x.json"):
        with pytest.raises(TokenStoreError, match="inside the git repository"):
            TokenStore(path)


def test_a_differently_cased_spelling_of_the_checkout_is_still_refused():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    swapped = REPO_ROOT.with_name(REPO_ROOT.name.swapcase())
    if swapped == REPO_ROOT or not swapped.exists():
        pytest.skip("case-sensitive filesystem: the other spelling names nothing")
    with pytest.raises(TokenStoreError, match="inside the git repository"):
        TokenStore(swapped / "qbo-sandbox-1.json")


def test_a_symlink_or_relative_path_into_a_checkout_is_refused(outside, monkeypatch):
    """repository_root resolves the path first: a link from elsewhere, or a bare file
    name typed inside a checkout, still names a place inside it."""
    checkout = outside / "repo"
    (checkout / "sub").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (outside / "elsewhere").mkdir()
    (outside / "elsewhere" / "link").symlink_to(checkout / "sub")
    with pytest.raises(TokenStoreError, match="inside the git repository"):
        TokenStore(outside / "elsewhere" / "link" / "qbo-sandbox-1.json")
    monkeypatch.chdir(checkout / "sub")
    with pytest.raises(TokenStoreError, match="inside the git repository"):
        TokenStore("qbo-sandbox-1.json")


def test_env_file_names_are_refused(outside):
    # Any case: on a case-insensitive disk '.ENV' is the .env beside it.
    for name in (".env", ".env.local", ".env.qbo", ".ENV", ".Env", ".ENV.local"):
        with pytest.raises(TokenStoreError, match=r"\.env"):
            TokenStore(outside / name)


def test_the_default_directory_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(ENV_TOKEN_DIR, raising=False)
    assert default_token_dir() == tmp_path / "home" / ".config" / "ledgerlens"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_token_dir() == tmp_path / "xdg" / "ledgerlens"
    monkeypatch.setenv(ENV_TOKEN_DIR, str(tmp_path / "chosen"))
    assert default_token_dir() == tmp_path / "chosen"
    assert token_path("production", "1") == tmp_path / "chosen" / "qbo-production-1.json"


@pytest.mark.parametrize("environment, realm_id", [
    ("prod", "1"), ("sandbox", "../x"), ("sandbox", ""), ("sandbox", "a/b"), ("sandbox", "1 2"),
])
def test_token_path_rejects_parts_that_could_name_another_file(environment, realm_id, outside):
    with pytest.raises(ValueError):
        token_path(environment, realm_id, outside)


def test_repr_and_str_never_show_a_token():
    text = f"{sample()!r} {sample()}"
    assert "ACCESS-1" not in text and "REFRESH-1" not in text
    assert "4620816365" in text and "<redacted>" in text


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o620, 0o604, 0o602])
def test_a_file_others_can_read_is_refused_not_fixed(outside, mode):
    """Any group or other bit, one class at a time, so a narrowed mask cannot pass."""
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(realm_one())
    os.chmod(store.path, mode)
    with pytest.raises(TokenStoreError, match="600"):
        store.load()
    assert stat.S_IMODE(store.path.stat().st_mode) == mode


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o705, 0o707])
def test_a_shared_directory_is_refused_before_anything_is_written(outside, mode):
    outside.mkdir()
    os.chmod(outside, mode)
    with pytest.raises(TokenStoreError, match="700"):
        TokenStore.for_realm("sandbox", "1", outside).save(realm_one())
    assert not any(outside.iterdir())


def test_a_damaged_file_is_an_error_that_names_the_file(outside):
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(realm_one())
    store.path.write_text("{not json")  # keeps the mode
    with pytest.raises(TokenStoreError, match="not a token file"):
        store.load()
    store.path.write_text(json.dumps({"access_token": "x", "colour": "blue"}))
    with pytest.raises(TokenStoreError, match="missing .* unexpected \\['colour'\\]"):
        store.load()
    record = sample().to_dict()
    record["expires_at"] = "yesterday"
    store.path.write_text(json.dumps(record))
    with pytest.raises(TokenStoreError, match="ISO 8601"):
        store.load()
    record = sample(environment="staging").to_dict()
    store.path.write_text(json.dumps(record))
    with pytest.raises(TokenStoreError, match="staging"):
        store.load()


_GOOD = realm_one().to_dict()


@pytest.mark.parametrize("record", [
    None, 5, [], "tokens",
    {**_GOOD, "access_token": None}, {**_GOOD, "refresh_token": ""}, {**_GOOD, "realm_id": 1},
    {**_GOOD, "refresh_expires_at": "never"}, {**_GOOD, "obtained_at": "garbage"},
    {**_GOOD, "colour": "blue"}, {k: v for k, v in _GOOD.items() if k != "expires_at"},
], ids=["null", "number", "list", "string", "null token", "empty token", "int realm",
        "bad refresh expiry", "bad obtained_at", "extra key", "missing key"])
def test_each_defect_in_a_record_is_refused_and_names_the_file(outside, record):
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(realm_one())
    store.path.write_text(json.dumps(record))  # keeps the mode
    with pytest.raises(TokenStoreError) as refused:
        store.load()
    assert str(store.path) in str(refused.value)


def test_expiry_honours_the_skew_and_reads_naive_and_zulu_timestamps_as_utc():
    tokens = sample()
    assert tokens.expires_at == "2026-09-26T13:00:00+00:00"
    assert tokens.obtained_at == "2026-09-26T12:00:00+00:00"
    assert not tokens.access_expired(now=NOW)
    assert not tokens.access_expired(now=NOW + timedelta(seconds=3600 - 61))
    assert tokens.access_expired(now=NOW + timedelta(seconds=3600 - 60))
    assert tokens.access_expired(now=NOW + timedelta(seconds=3600 - 61), skew=120)
    assert not tokens.refresh_expired(now=NOW + timedelta(days=99))
    assert tokens.refresh_expired(now=NOW + timedelta(days=100))
    naive = sample(expires_at="2026-09-26T13:00:00")
    assert naive.access_expired(now=datetime(2026, 9, 26, 12, 59, 30, tzinfo=timezone.utc))
    assert not naive.access_expired(now=datetime(2026, 9, 26, 12, 58, 30, tzinfo=timezone.utc))
    zulu = sample(expires_at="2026-09-26T13:00:00Z")
    assert not zulu.access_expired(now=NOW)
    assert zulu.access_expired(now=datetime(2026, 9, 26, 12, 59, 30))  # naive `now` is UTC too
    assert not zulu.access_expired(now=datetime(2026, 9, 26, 12, 30))
    assert zulu.access_expired(now=datetime(2026, 9, 26, 14, 0))
    # The refresh token's skew, where it decides: 30 s before expiry is too late, 61 s is not.
    refresh_ends = NOW + timedelta(days=100)
    assert tokens.refresh_expired(now=refresh_ends - timedelta(seconds=30))
    assert not tokens.refresh_expired(now=refresh_ends - timedelta(seconds=61))


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="the local zone cannot be changed here")
def test_naive_timestamps_are_utc_whatever_the_local_zone(monkeypatch):
    """CI runs in UTC, where reading naive times as local would pass: run in Tokyo instead."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        naive = sample(expires_at="2026-09-26T13:00:00")
        assert naive.access_expired(now=datetime(2026, 9, 26, 12, 59, 30, tzinfo=timezone.utc))
        assert not naive.access_expired(now=datetime(2026, 9, 26, 12, 58, 30, tzinfo=timezone.utc))
        assert not sample().access_expired(now=datetime(2026, 9, 26, 12, 30))
        assert sample().access_expired(now=datetime(2026, 9, 26, 14, 0))
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_ignore_rules_cover_token_files_and_their_temps_but_not_fixtures():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    def ignored(name: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO_ROOT).returncode == 0

    for name in ("qbo-sandbox-1.json", "qbo-production-1.json", ".qbo-sandbox-1.json.k3j.tmp",
                 "data/qbo-sandbox-1.json", "data/qbo-ledger.identity.json"):
        assert ignored(name), name
    assert not ignored("tests/fixtures/qbo/pull/020-post-query-account.json")


@pytest.mark.parametrize("record, why", [
    (lambda: realm_one(environment="Sandbox"), "Sandbox"),
    (lambda: realm_one(refresh_token=""), "non-empty strings"),
    (lambda: realm_one(expires_at="soon"), "ISO 8601"),
    (lambda: realm_one(realm_id="../x"), "realm id"),
    (lambda: sample(realm_id="999", environment="production"), "production realm 999"),
    (lambda: realm_one(environment="production"), "production realm 1"),
])
def test_save_writes_only_a_record_load_accepts_for_the_realm_the_file_names(outside, record, why):
    """A code exchange is single-use: a record that cannot be read back is lost tokens, and
    one realm's tokens filed under another's name would pull the wrong company's books."""
    store = TokenStore.for_realm("sandbox", "1", outside)
    with pytest.raises(TokenStoreError, match=why) as refused:
        store.save(record())
    assert str(store.path) in str(refused.value)
    assert not outside.exists() or not any(outside.iterdir())


def test_load_refuses_a_record_for_another_realm_than_the_file_names(outside):
    TokenStore.for_realm("production", "999", outside).save(
        sample(realm_id="999", environment="production"))
    (outside / "qbo-production-999.json").rename(outside / "qbo-sandbox-1.json")
    with pytest.raises(TokenStoreError, match="for sandbox realm 1, the record for production realm 999"):
        TokenStore.for_realm("sandbox", "1", outside).load()
    # A file named some other way is not bound to a realm: its record is taken as written.
    custom = TokenStore(outside / "tokens.json")
    assert custom.expected is None
    custom.save(sample())
    assert custom.load() == sample()
