"""Where QuickBooks tokens live: outside the repository, in a file only its owner's mode bits open.

An OAuth refresh token is a standing credential: whoever holds it can pull the
company's books for up to a hundred days without a password. So it is kept
where neither the ledger nor the code is, in a file whose mode lets only its
owner read it, and the store refuses two places outright: any path inside a git working tree
(rule 1 in CLAUDE.md - a token committed by accident is a token to revoke),
and any file named like ``.env``, which is for configuration typed by hand,
not for credentials a program rotates.

The file is JSON with exactly the keys in :data:`FIELDS`, written atomically
with mode 0600 into a directory created with mode 0700 - ``$LEDGERLENS_TOKEN_DIR``,
else ``$XDG_CONFIG_HOME/ledgerlens``, else ``~/.config/ledgerlens`` - as
``qbo-<environment>-<realm_id>.json``, and a record for another realm or
environment than the name says is refused on the way in and on the way out. A
file or directory whose group or other mode bits are set is refused rather than
fixed: by the time the mode is loose the token may already have been copied,
and the right response is to authorise again, not to chmod. Like ssh, only the
mode bits are checked: an access-control list that shares a parent folder's
contents is not seen, so keep the token directory out of such folders.

Timestamps are ISO 8601 in UTC with a ``+00:00`` offset (Python 3.9's
``fromisoformat`` does not accept ``Z``); a naive timestamp is read as UTC.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENV_TOKEN_DIR = "LEDGERLENS_TOKEN_DIR"
ENVIRONMENTS = ("sandbox", "production")
FILE_MODE = 0o600
DIR_MODE = 0o700
#: Intuit realm ids are decimal strings; the class is wider so a test realm can be
#: named, and narrow enough that an id can never spell a path out of its directory.
_REALM_ID = re.compile(r"[A-Za-z0-9_-]+")
#: token_path's file name, read back: which environment and realm a file is for.
_TOKEN_NAME = re.compile(rf"qbo-({'|'.join(ENVIRONMENTS)})-({_REALM_ID.pattern})\.json")
_REDACTED = "<redacted>"


class TokenStoreError(RuntimeError):
    """The store refused: a path it will not use, or a file it will not trust."""


def default_token_dir() -> Path:
    """``$LEDGERLENS_TOKEN_DIR``, else ``$XDG_CONFIG_HOME/ledgerlens``, else ``~/.config/ledgerlens``."""
    chosen = os.environ.get(ENV_TOKEN_DIR)
    if chosen:
        return Path(chosen).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ledgerlens"


def token_path(environment: str, realm_id: str, directory: str | Path | None = None) -> Path:
    """``<directory>/qbo-<environment>-<realm_id>.json``, the parts checked so the name can
    only ever be one file in that directory."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}, not {environment!r}")
    if not _REALM_ID.fullmatch(realm_id or ""):
        raise ValueError(f"realm id {realm_id!r} is not letters, digits, '_' and '-' only")
    base = Path(directory).expanduser() if directory is not None else default_token_dir()
    return base / f"qbo-{environment}-{realm_id}.json"


def repository_root(path: str | Path) -> Path | None:
    """The nearest ancestor-or-self of ``path`` that is a git working tree (a ``.git``
    directory, or the ``.git`` file a worktree carries), or None.

    Symlinks are resolved first; letter case needs no handling because the
    filesystem answers ``exists()``, so on a case-insensitive disk a
    differently-cased spelling of a checkout is still found to be one.
    """
    resolved = Path(path).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _parse_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise TokenStoreError(f"not an ISO 8601 timestamp: {value!r}") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _moment(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


@dataclass(repr=False)
class Tokens:
    """One authorisation's tokens and their lifetimes: the on-disk record, field for field."""

    access_token: str
    refresh_token: str
    expires_at: str
    refresh_expires_at: str
    realm_id: str
    environment: str
    obtained_at: str

    @classmethod
    def issued(cls, access_token: str, refresh_token: str, expires_in: int,
               refresh_expires_in: int, realm_id: str, environment: str,
               now: datetime | None = None) -> Tokens:
        """Tokens as the token endpoint hands them out: lifetimes in seconds from now."""
        moment = _moment(now)
        return cls(
            access_token, refresh_token,
            _format_time(moment + timedelta(seconds=int(expires_in))),
            _format_time(moment + timedelta(seconds=int(refresh_expires_in))),
            realm_id, environment, _format_time(moment),
        )

    def access_expired(self, now: datetime | None = None, skew: int = 60) -> bool:
        """True when fewer than ``skew`` seconds remain: a request sent now would arrive late."""
        return _parse_time(self.expires_at) <= _moment(now) + timedelta(seconds=skew)

    def refresh_expired(self, now: datetime | None = None, skew: int = 60) -> bool:
        return _parse_time(self.refresh_expires_at) <= _moment(now) + timedelta(seconds=skew)

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in FIELDS}

    @classmethod
    def from_dict(cls, data: object) -> Tokens:
        """The inverse of :meth:`to_dict`, refusing anything but the exact record."""
        if not isinstance(data, dict):
            raise TokenStoreError("a token record is a JSON object")
        missing = [name for name in FIELDS if name not in data]
        unexpected = sorted(set(data) - set(FIELDS))
        if missing or unexpected:
            raise TokenStoreError(f"token record keys: missing {missing}, unexpected {unexpected}")
        empty = [name for name in FIELDS if not isinstance(data[name], str) or not data[name]]
        if empty:
            raise TokenStoreError(f"token record values must be non-empty strings: {empty}")
        tokens = cls(**{name: data[name] for name in FIELDS})
        for name in ("expires_at", "refresh_expires_at", "obtained_at"):
            _parse_time(getattr(tokens, name))
        if tokens.environment not in ENVIRONMENTS:
            raise TokenStoreError(f"environment {tokens.environment!r} is not one of {ENVIRONMENTS}")
        if not _REALM_ID.fullmatch(tokens.realm_id):
            raise TokenStoreError(
                f"realm id {tokens.realm_id!r} is not letters, digits, '_' and '-' only")
        return tokens

    def __repr__(self) -> str:
        return (f"Tokens(realm_id={self.realm_id!r}, environment={self.environment!r}, "
                f"expires_at={self.expires_at!r}, refresh_expires_at={self.refresh_expires_at!r}, "
                f"access_token={_REDACTED}, refresh_token={_REDACTED})")

    __str__ = __repr__


#: The on-disk keys, in the order they are written.
FIELDS: tuple[str, ...] = tuple(field.name for field in fields(Tokens))


class TokenStore:
    """One token file: where it is, and the rules for reading and writing it.

    Constructing the store already refuses a path inside a git working tree or
    named like ``.env``; nothing is read or written until :meth:`load` or
    :meth:`save`.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        # Any case: on a case-insensitive disk '.ENV' is the same file as '.env'.
        if self.path.name.lower().startswith(".env"):
            raise TokenStoreError(
                f"refusing {self.path}: a .env file is for configuration typed by hand; "
                "tokens have a file of their own")
        root = repository_root(self.path)
        if root is not None:
            raise TokenStoreError(
                f"refusing to keep tokens at {self.path}: it is inside the git repository at "
                f"{root}. Tokens live outside any checkout (the default is {default_token_dir()}; "
                f"{ENV_TOKEN_DIR} chooses another directory).")
        named = _TOKEN_NAME.fullmatch(self.path.name)
        #: (environment, realm_id) when the file is named as token_path names it: a
        #: record for anything else is refused, so one realm's tokens never answer for another's.
        self.expected = (named.group(1), named.group(2)) if named else None

    def _refuse_other_realm(self, tokens: Tokens, verb: str) -> None:
        if self.expected and (tokens.environment, tokens.realm_id) != self.expected:
            raise TokenStoreError(
                f"refusing to {verb} {self.path}: the file is for {self.expected[0]} realm "
                f"{self.expected[1]}, the record for {tokens.environment} realm {tokens.realm_id}")

    @classmethod
    def for_realm(cls, environment: str, realm_id: str,
                  directory: str | Path | None = None) -> TokenStore:
        return cls(token_path(environment, realm_id, directory))

    def load(self) -> Tokens | None:
        """The stored tokens, or None when there are none yet."""
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            return None
        if mode & 0o077:
            raise TokenStoreError(
                f"refusing to read {self.path}: its mode is {mode:03o}, so other users of this "
                "machine could have copied it. Delete it and authorise again, or chmod 600 it "
                "if you know how it came to be shared.")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenStoreError(f"{self.path} is not a token file: {exc}") from exc
        try:
            tokens = Tokens.from_dict(data)
        except TokenStoreError as exc:
            raise TokenStoreError(f"{self.path}: {exc}") from None
        self._refuse_other_realm(tokens, "read")
        return tokens

    def save(self, tokens: Tokens) -> Path:
        """Write the record atomically (temp file, fsync, rename) with mode 0600.

        Only a record :meth:`load` would accept is written, so a save never leaves
        tokens behind that cannot be read back (a code exchange is single-use).
        """
        try:
            tokens = Tokens.from_dict(tokens.to_dict())
        except TokenStoreError as exc:
            raise TokenStoreError(f"refusing to write {self.path}: {exc}") from None
        self._refuse_other_realm(tokens, "write")
        directory = self.path.parent
        try:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        except OSError as exc:
            raise TokenStoreError(f"cannot create {directory}: {exc}") from exc
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode & 0o077:
            raise TokenStoreError(
                f"refusing to write into {directory}: its mode is {mode:03o}; a token directory "
                "is private, so chmod 700 it (or point "
                f"{ENV_TOKEN_DIR} at one that is).")
        payload = json.dumps(tokens.to_dict(), indent=2) + "\n"
        handle, temp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            os.fchmod(handle, FILE_MODE)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
        return self.path

    def delete(self) -> bool:
        """Remove the file; False when there was none."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True
