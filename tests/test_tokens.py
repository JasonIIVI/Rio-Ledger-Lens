"""The token store: outside any checkout, mode 600, and refusing what it should."""

from __future__ import annotations

import json
import os
import stat
import subprocess
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
    store.save(sample())

    def full(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tokens_module.os, "replace", full)
    with pytest.raises(OSError, match="disk full"):
        store.save(sample(access_token="ACCESS-2"))
    assert store.load() == sample()
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
    swapped = REPO_ROOT.with_name(REPO_ROOT.name.swapcase())
    if swapped == REPO_ROOT or not swapped.exists():
        pytest.skip("case-sensitive filesystem: the other spelling names nothing")
    with pytest.raises(TokenStoreError, match="inside the git repository"):
        TokenStore(swapped / "qbo-sandbox-1.json")


def test_env_file_names_are_refused(outside):
    for name in (".env", ".env.local", ".env.qbo"):
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


def test_a_file_others_can_read_is_refused_not_fixed(outside):
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(sample())
    os.chmod(store.path, 0o644)
    with pytest.raises(TokenStoreError, match="600"):
        store.load()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


def test_a_shared_directory_is_refused_before_anything_is_written(outside):
    outside.mkdir()
    os.chmod(outside, 0o755)
    with pytest.raises(TokenStoreError, match="700"):
        TokenStore.for_realm("sandbox", "1", outside).save(sample())
    assert not any(outside.iterdir())


def test_a_damaged_file_is_an_error_that_names_the_file(outside):
    store = TokenStore.for_realm("sandbox", "1", outside)
    store.save(sample())
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


def test_the_ignore_rules_cover_token_files_and_their_temps_but_not_fixtures():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    def ignored(name: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO_ROOT).returncode == 0

    for name in ("qbo-sandbox-1.json", "qbo-production-1.json", ".qbo-sandbox-1.json.k3j.tmp",
                 "data/qbo-sandbox-1.json", "data/qbo-ledger.identity.json"):
        assert ignored(name), name
    assert not ignored("tests/fixtures/qbo/pull/020-post-query-account.json")
