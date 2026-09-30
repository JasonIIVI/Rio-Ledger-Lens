"""QuickBooks Online: sign in, pull a period's books, map them into the ledger contract.

The connector is standard library only and has four layers, each testable
without a network:

* **Configuration and transport.** :class:`QboConfig` reads the ``QBO_*``
  settings; a :class:`Transport` sends one HTTP request. The real one is
  :class:`UrllibTransport`; :class:`RecordedTransport` replays JSON fixtures
  from ``tests/fixtures/qbo/`` and :class:`Recorder` writes them, sanitized,
  from a live sandbox pull. No test touches the network.
* **OAuth 2.0.** :class:`QboAuth` builds the authorisation URL and exchanges
  or refreshes tokens; :class:`CallbackServer` catches the browser's redirect
  on ``localhost``. Tokens are kept by :mod:`ledgerlens.connectors.tokens`,
  outside any checkout.
* **The client.** :class:`QboClient` sends Accounting API requests with the
  bearer token and ``minorversion``, refreshes once on a 401, backs off on a
  429, and pages through queries.
* **The mapping.** Pure functions turn Account rows, JournalEntry entities
  and the GeneralLedger report into ledger lines, which go through
  :func:`ledgerlens.ingest.prepare` exactly as a CSV would. Every line that
  is skipped is counted in :class:`PullStats`; nothing is dropped silently.

Only sandbox companies are pulled into this repository's workflow (rule 1 in
CLAUDE.md): a production pull needs an explicit flag, and recording refuses
production outright.
"""

from __future__ import annotations

import base64
import errno
import json
import re
import secrets
import selectors
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from ..ingest import entry_level, identity_path, prepare
from ..schema import ACCOUNT_TYPES, REQUIRED_COLUMNS
from .tokens import ENVIRONMENTS, Tokens, TokenStore, TokenStoreError

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
#: Intuit discontinued minor versions 1-74 on 2025-08-01; every request names one.
MINOR_VERSION = "75"
BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
#: The query endpoint's MAXRESULTS cap.
PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30.0
USER_AGENT = "ledgerlens-qbo-connector"

_COMPANY_PATH = re.compile(r"^/v3/company/[^/]+")
#: A realm id every part of the tool accepts: the token file name, the URL, and the
#: ``qbo:<realm>`` identity ingest reads back (Intuit's are decimal strings).
REALM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_FORM = "application/x-www-form-urlencoded"
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


# --- errors -------------------------------------------------------------------


class QboError(RuntimeError):
    """A request QuickBooks (or the network) refused. ``status`` 0 means no response."""

    def __init__(self, status: int, fault: Any, message: str):
        super().__init__(message)
        self.status = status
        self.fault = fault
        self.message = message

    @classmethod
    def from_response(cls, response: Response, context: str = "") -> QboError:
        """Read either fault shape Intuit sends (``Fault/Error/Message`` from the API,
        lower-case ``fault/error/message`` on some 401s) or an OAuth ``error`` body."""
        error = cls._read(response, f"{context}: " if context else "")
        tid = response.headers.get("intuit_tid")
        if tid:  # Intuit support asks for it; it identifies the request, not the company
            error.message += f" [intuit_tid {tid}]"
            error.args = (error.message,)
        return error

    @classmethod
    def _read(cls, response: Response, prefix: str) -> QboError:
        try:
            payload = response.json()
        except ValueError:  # some 401s arrive as text/xml; show the start of the body
            text = response.body.decode("utf-8", "replace").strip()[:200]
            return cls(response.status, None,
                       f"{prefix}HTTP {response.status}" + (f": {text}" if text else ""))
        fault = _get_any(payload, "Fault", "fault") if isinstance(payload, dict) else None
        if isinstance(fault, dict):
            errors = _get_any(fault, "Error", "error") or []
            first = errors[0] if isinstance(errors, list) and errors else {}
            if not isinstance(first, dict):
                first = {}
            parts = [str(p) for p in (_get_any(first, "Message", "message"),
                                      _get_any(first, "Detail", "detail")) if p]
            code = _get_any(first, "code", "Code")
            detail = "; ".join(parts) or "no message"
            return cls(response.status, fault,
                       f"{prefix}HTTP {response.status}: {detail}" + (f" (code {code})" if code else ""))
        if isinstance(payload, dict) and "error" in payload:  # the OAuth endpoints' shape
            described = payload.get("error_description")
            return cls(response.status, payload,
                       f"{prefix}HTTP {response.status}: {payload['error']}"
                       + (f" ({described})" if described else ""))
        return cls(response.status, payload, f"{prefix}HTTP {response.status}")


class QboAuthError(QboError):
    """Sign-in failed or the stored authorisation no longer works: run ``ledgerlens qbo-auth``."""


class QboConfigError(ValueError):
    """The ``QBO_*`` settings are missing or unusable."""


def _get_any(mapping: Mapping, *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


# --- configuration --------------------------------------------------------------


@dataclass(frozen=True)
class QboConfig:
    """The Intuit app and company to talk to. The client secret never appears in a repr."""

    client_id: str
    client_secret: str = field(repr=False)
    environment: str = "sandbox"
    realm_id: str | None = None
    redirect_uri: str = DEFAULT_REDIRECT_URI
    timezone: str | None = None

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise QboConfigError(
                f"QBO_ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)}, not {self.environment!r}")
        parts = urllib.parse.urlsplit(self.redirect_uri)
        if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS or not parts.port:
            raise QboConfigError(
                f"QBO_REDIRECT_URI must be http://localhost:<port>/<path> (the sign-in is caught "
                f"on this machine), not {self.redirect_uri!r}")
        if self.timezone:
            _zone(self.timezone)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> QboConfig:
        """Read ``QBO_*`` settings, naming every required one that is missing."""
        import os

        env = os.environ if environ is None else environ
        missing = [name for name in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET") if not env.get(name)]
        if missing:
            raise QboConfigError(
                f"missing {', '.join(missing)}: copy them from the Intuit app's Keys & credentials "
                "page into .env (see .env.example)")
        return cls(
            client_id=env["QBO_CLIENT_ID"],
            client_secret=env["QBO_CLIENT_SECRET"],
            environment=env.get("QBO_ENVIRONMENT") or "sandbox",
            realm_id=env.get("QBO_REALM_ID") or None,
            redirect_uri=env.get("QBO_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
            timezone=env.get("QBO_TIMEZONE") or None,
        )

    @classmethod
    def for_fixtures(cls, realm_id: str, timezone: str | None = None) -> QboConfig:
        """A configuration for replaying recorded fixtures: no real credentials anywhere."""
        return cls("TEST-CLIENT", "TEST-SECRET", "sandbox", realm_id, DEFAULT_REDIRECT_URI, timezone)

    def with_realm(self, realm_id: str) -> QboConfig:
        return QboConfig(self.client_id, self.client_secret, self.environment, realm_id,
                         self.redirect_uri, self.timezone)

    @property
    def base_url(self) -> str:
        if not self.realm_id:
            raise QboConfigError("no realm id: set QBO_REALM_ID or pass --realm-id")
        return f"{BASE_URLS[self.environment]}/v3/company/{self.realm_id}"

    @property
    def callback_port(self) -> int:
        return int(urllib.parse.urlsplit(self.redirect_uri).port)

    @property
    def callback_path(self) -> str:
        return urllib.parse.urlsplit(self.redirect_uri).path or "/"


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError on a malformed key
        raise QboConfigError(f"QBO_TIMEZONE {name!r} is not an IANA time zone ({exc})") from None


# --- transport ------------------------------------------------------------------


@dataclass
class Response:
    """One HTTP response. Header names are lower-cased."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib's default handler follows a 30x with every header but the body's, so a redirect
    to another host (or to plain http) would carry the bearer token or the client secret
    along. No endpoint this connector calls redirects: a 30x comes back as a Response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrllibTransport:
    """The real network, through :mod:`urllib`: https only, redirects never followed, and
    any HTTP status comes back as a Response."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT, https_only: bool = True):
        self.timeout = timeout
        self.https_only = https_only
        self._opener = urllib.request.build_opener(_NoRedirects())

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response:
        if self.https_only and urllib.parse.urlsplit(url).scheme != "https":
            raise QboError(0, None, f"refusing to send credentials over {url.split(':', 1)[0]}: "
                                    "only https URLs are called")
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, _lower(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _lower(exc.headers.items() if exc.headers else ()), exc.read())
        except urllib.error.URLError as exc:
            raise QboError(0, None, f"network: {exc.reason}") from None
        except (socket.timeout, TimeoutError):
            raise QboError(0, None, f"network: no response within {self.timeout:g}s") from None


def default_transport() -> Transport:
    """The transport the CLI uses; tests replace this function, never the network."""
    return UrllibTransport()


def _lower(items) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in items}


def canonical_request(method: str, url: str, body: bytes | None,
                      content_type: str | None) -> dict[str, Any]:
    """The part of a request a fixture matches on, with nothing secret or company-specific.

    The host and the ``/v3/company/<realm>`` prefix are dropped (so fixtures
    never carry a realm id), query parameters are sorted, a form body keeps
    its ``grant_type`` and the names of its other fields but no values (a
    code or a refresh token never reaches a fixture), and a query statement
    has its whitespace collapsed.
    """
    parts = urllib.parse.urlsplit(url)
    path = _COMPANY_PATH.sub("", parts.path) or "/"
    query = sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    text = body.decode("utf-8") if body else ""
    canonical_body: Any = None
    if text and (content_type or "").startswith(_FORM):
        fields_ = dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
        canonical_body = {"grant_type": fields_.get("grant_type"), "fields": sorted(fields_)}
    elif text:
        canonical_body = " ".join(text.split())
    return {"method": method.upper(), "path": path, "query": [list(p) for p in query],
            "body": canonical_body}


class UnexpectedRequest(AssertionError):
    """A replayed session asked for something no fixture answers."""


class RecordedTransport:
    """Replays fixture files: each answers the first matching request, once, in name order.

    A fixture is ``{"request": <canonical_request>, "response": {"status",
    "headers", "body"}}``; a JSON body is stored as JSON so the files read as
    Intuit's documents do. Because each fixture is used once, pagination and a
    401-then-200 sequence replay naturally.
    """

    def __init__(self, fixtures_dir: str | Path):
        self.directory = Path(fixtures_dir)
        paths = sorted(self.directory.glob("*.json"))
        if not paths:
            raise FileNotFoundError(f"no fixtures (*.json) in {self.directory}")
        self.fixtures = [(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in paths]
        self.used: set[str] = set()
        self.requests: list[dict[str, Any]] = []

    @property
    def unused(self) -> list[str]:
        return [name for name, _ in self.fixtures if name not in self.used]

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response:
        wanted = canonical_request(method, url, body, _header(headers, "content-type"))
        self.requests.append(wanted)
        for name, fixture in self.fixtures:
            if name not in self.used and fixture.get("request") == wanted:
                self.used.add(name)
                return _response_from_fixture(fixture["response"])
        raise UnexpectedRequest(
            f"no unused fixture in {self.directory} matches {json.dumps(wanted)}; "
            f"unused: {self.unused}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _response_from_fixture(stored: Mapping[str, Any]) -> Response:
    body = stored.get("body")
    raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    return Response(int(stored["status"]), _lower((stored.get("headers") or {}).items()), raw)


#: Response headers a fixture keeps; request headers are never stored (they carry the token).
KEPT_HEADERS = ("content-type", "retry-after", "www-authenticate")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: GeneralLedger columns that hold a person's display name.
_USER_COLUMNS = ("create_by", "last_mod_by")


class Recorder:
    """Wraps a live transport and writes each exchange as a sanitized fixture.

    The caller gets the real response; the file gets a copy with the realm id
    replaced by ``REALM``, tokens by ``TEST-ACCESS`` / ``TEST-REFRESH`` (the
    ``id_token`` removed), user names by ``qbo-user-N`` and e-mail addresses
    by ``redacted@example.com``. Intuit's sample-company data is kept: it is
    Intuit's own demo data, and it is what makes the fixtures realistic.
    """

    def __init__(self, inner: Transport, out_dir: str | Path, realm_id: str, start: int = 10):
        self.inner = inner
        self.out_dir = Path(out_dir)
        self.realm_id = realm_id
        self.users: dict[str, str] = {}
        self.written: list[Path] = []
        #: Exchanges that could not be written. The caller still gets every response: a
        #: token reply lost to a failed write would be a rotated refresh token lost for good.
        self.failures: list[str] = []
        self._number = start

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response:
        response = self.inner.request(method, url, headers, body)
        try:
            self._write(method, url, headers, body, response)
        except Exception as exc:  # noqa: BLE001 (any failure to record must not lose the response)
            self.failures.append(f"{method} {urllib.parse.urlsplit(url).path}: {exc}")
        return response

    def _write(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
               response: Response) -> None:
        canonical = canonical_request(method, url, body, _header(headers, "content-type"))
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.body.decode("utf-8", "replace")
        fixture = {
            "request": sanitize(canonical, self.realm_id, self.users),
            "response": {
                "status": response.status,
                "headers": {k: v for k, v in response.headers.items() if k in KEPT_HEADERS},
                "body": sanitize(payload, self.realm_id, self.users),
            },
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{self._number:03d}-{method.lower()}-{_slug(canonical)}.json"
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.written.append(path)
        self._number += 10

    def finish(self) -> list[str]:
        """Scrub every written fixture again with every name learned during the recording (a
        name first seen in a later response may already sit in an earlier one's memo), then
        check none still holds the realm id or a user's name. A file that does is deleted and
        reported: a recording goes into a public repository."""
        for path in list(self.written):
            try:
                fixture = scrub_known(json.loads(path.read_text(encoding="utf-8")),
                                      self.realm_id, self.users)
                text = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
                leaked = leftovers(text, self.realm_id, self.users)
                if leaked:
                    path.unlink()
                    self.written.remove(path)
                    self.failures.append(f"{path.name}: still held {', '.join(leaked)} after "
                                         "sanitizing, so it was deleted")
                else:
                    path.write_text(text, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                self.failures.append(f"{path.name}: {exc}")
        return self.failures


def _slug(canonical: Mapping[str, Any]) -> str:
    body = canonical.get("body")
    if isinstance(body, dict) and body.get("grant_type"):
        return f"token-{body['grant_type']}"
    if isinstance(body, str):
        entity = re.search(r"\bfrom\s+(\w+)", body, re.IGNORECASE)
        start = re.search(r"\bstartposition\s+(\d+)", body, re.IGNORECASE)
        return "query-" + (entity.group(1).lower() if entity else "unknown") + (
            f"-p{(int(start.group(1)) - 1) // PAGE_SIZE + 1}" if start else "")
    path = str(canonical.get("path", "")).strip("/")
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "root"


def sanitize(payload: Any, realm_id: str, users: dict[str, str]) -> Any:
    """A copy of ``payload`` safe to commit (see :class:`Recorder`). ``users`` maps each
    real display name to its ``qbo-user-N`` stand-in and is shared across a recording."""

    def user(name: str) -> str:
        if name not in users:
            users[name] = f"qbo-user-{len(users) + 1}"
        return users[name]

    def text(value: str) -> str:
        if realm_id:
            value = value.replace(realm_id, "REALM")
        return _EMAIL.sub("redacted@example.com", value)

    def walk(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            if "Columns" in node and "Rows" in node:
                return _sanitize_report(node, walk, user)
            out = {}
            for k, v in node.items():
                if k == "id_token":
                    continue
                if k == "access_token":
                    out[k] = "TEST-ACCESS"
                elif k == "refresh_token":
                    out[k] = "TEST-REFRESH"
                elif (k.endswith("By") or k.endswith("ByRef") or k == "UserName") and v:
                    out[k] = ({kk: (user(str(vv)) if kk in ("value", "name") and vv else walk(vv))
                               for kk, vv in v.items()} if isinstance(v, dict) else user(str(v)))
                else:
                    out[k] = walk(v, k)
            return out
        if isinstance(node, list):
            return [walk(item, key) for item in node]
        if isinstance(node, str):
            return text(node)
        return node

    return scrub_known(walk(payload), realm_id, users)


def _name_pattern(name: str) -> re.Pattern:
    """A name in any case, with any run of whitespace between its words (``JANE  DEV``,
    a non-breaking space), and not inside a longer word (``Jane`` leaves ``Janet``)."""
    words = r"\s+".join(re.escape(word) for word in name.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE)


def scrub_known(payload: Any, realm_id: str, users: Mapping[str, str]) -> Any:
    """Replace every known user name wherever it appears (a memo, a payee, a key), longest
    first, and the realm id wherever it appears, as text, as a number or as a key."""
    names = sorted(_real_names(users), key=len, reverse=True)
    patterns = [(_name_pattern(n), users[n]) for n in names]

    def text(value: str) -> str:
        if realm_id:
            value = value.replace(realm_id, "REALM")
        for pattern, stand_in in patterns:
            value = pattern.sub(stand_in, value)
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                clean = text(str(key))
                if clean in out:  # two keys scrubbed to one: refuse rather than drop a value
                    raise ValueError(f"two keys scrub to {clean!r}; the fixture cannot be written")
                out[clean] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            return text(node)
        if isinstance(node, (int, float)) and not isinstance(node, bool) and realm_id \
                and str(node) == realm_id:
            return "REALM"
        return node

    return walk(payload)


def leftovers(text: str, realm_id: str, users: Mapping[str, str]) -> list[str]:
    """What a sanitized fixture should no longer contain but does: the realm id anywhere (even
    inside a longer number), or a name, compared case-folded with whitespace collapsed."""
    found = ["the realm id"] if realm_id and realm_id in text else []
    folded = " ".join(text.casefold().split())
    for name in _real_names(users):
        wanted = " ".join(name.casefold().split())
        if re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", folded):
            found.append(f"a user name ({users[name]})")
    return found


def _real_names(users: Mapping[str, str]) -> list[str]:
    """Names to scrub: not empty, and not already a stand-in (data that was sanitized before,
    like a committed recording, names its users qbo-user-N)."""
    stand_ins = set(users.values())
    return [n for n in users if n and n not in stand_ins]


def _sanitize_report(report: dict, walk, user) -> dict:
    """A report's user names are positional (a column's ColData), not keyed."""
    keys = [column_key(c) for c in (report.get("Columns") or {}).get("Column", [])]
    positions = {i for i, k in enumerate(keys) if k in _USER_COLUMNS}

    def rows(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "ColData" and isinstance(v, list) and node.get("type") != "Section":
                    out[k] = [({**walk(cell), "value": user(cell["value"])}
                               if i in positions and isinstance(cell, dict) and cell.get("value")
                               else walk(cell)) for i, cell in enumerate(v)]
                else:
                    out[k] = rows(v)
            return out
        if isinstance(node, list):
            return [rows(item) for item in node]
        return walk(node)

    return {k: (rows(v) if k == "Rows" else walk(v)) for k, v in report.items()}


#: The GeneralLedger columns a pull asks for, by key.
GL_COLUMNS = ("tx_date", "txn_type", "doc_num", "name", "memo", "split_acc",
              "debt_amt", "credit_amt", "create_by", "create_date")
#: Report column titles Intuit shows, for a column that carries no key.
_TITLE_KEYS = {
    "date": "tx_date", "transaction type": "txn_type", "num": "doc_num", "name": "name",
    "memo/description": "memo", "split": "split_acc", "debit": "debt_amt", "credit": "credit_amt",
    "created by": "create_by", "create date": "create_date", "last modified by": "last_mod_by",
    "last modified": "last_mod_date", "account": "account_name",
}
_KNOWN_KEYS = set(GL_COLUMNS) | set(_TITLE_KEYS.values()) | {"account_num", "subt_nat_amount"}


def column_key(column: Mapping[str, Any]) -> str:
    """A report column's key: its ``ColKey`` metadata, else a ``ColType`` that is a key
    (Intuit's own samples put it there), else the key its title stands for, else the title."""
    for meta in column.get("MetaData") or []:
        if isinstance(meta, dict) and meta.get("Name") == "ColKey" and meta.get("Value"):
            return str(meta["Value"])
    if column.get("ColType") in _KNOWN_KEYS:
        return str(column["ColType"])
    title = str(column.get("ColTitle") or "")
    return _TITLE_KEYS.get(title.strip().lower(), title)


# --- OAuth 2.0 --------------------------------------------------------------------


def new_state() -> str:
    """An unguessable ``state`` for one sign-in: the callback must bring it back."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Callback:
    """What the browser brought back after a successful sign-in."""

    code: str = field(repr=False)
    realm_id: str


class _LoopbackServer(ThreadingHTTPServer):
    """One thread per connection, so an idle connection (a browser's speculative preconnect)
    cannot hold up the real callback; threads never outlive the process or block close()."""

    daemon_threads = True
    block_on_close = False


class _LoopbackServer6(_LoopbackServer):
    address_family = socket.AF_INET6


#: How long one connection may take to send its request line before it is dropped.
CALLBACK_READ_TIMEOUT = 30
#: IPv6 unavailable on this machine: then "localhost" cannot resolve to [::1] either.
_NO_IPV6 = {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}


class CallbackServer:
    """Catches Intuit's redirect on this machine: loopback only, one sign-in.

    ``localhost`` resolves to both 127.0.0.1 and ::1, so the server listens on
    both (the same port), or another program listening on [::1] could receive
    the browser's redirect. Only a request to the configured path carrying the
    expected ``state`` counts; anything else is answered (404 or 400) and
    ignored, so a stray request cannot end the sign-in or smuggle in a code.
    """

    def __init__(self, port: int, path: str = "/callback", host: str = "127.0.0.1"):
        self.path = path
        self._state: str | None = None
        self._outcome: Callback | QboAuthError | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = CALLBACK_READ_TIMEOUT

            def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
                server._handle(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # the URL carries the code: never print it

        attempts = 5 if port == 0 else 1  # an ephemeral port may be taken on ::1 already
        for attempt in range(attempts):
            try:
                self.servers = self._bind(host, port, Handler)
                break
            except OSError as exc:
                if attempt == attempts - 1:
                    raise QboAuthError(0, None, f"cannot listen on port {port} for the sign-in "
                                                f"callback ({exc.strerror or exc})") from None
        self.port = self.servers[0].server_address[1]

    @staticmethod
    def _bind(host: str, port: int, handler: type) -> list[_LoopbackServer]:
        first = _LoopbackServer((host, port), handler)
        servers = [first]
        if host == "127.0.0.1":
            try:
                servers.append(_LoopbackServer6(("::1", first.server_address[1]), handler))
            except OSError as exc:
                if exc.errno not in _NO_IPV6:
                    first.server_close()
                    raise OSError(exc.errno, f"[::1]:{first.server_address[1]} is in use by "
                                             "another program") from None
        return servers

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parts = urllib.parse.urlsplit(request.path)
        params = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path != self.path:
            return _reply(request, 404, "Not found.")
        state = params.get("state", "")
        if not self._state or not secrets.compare_digest(state.encode(), self._state.encode()):
            return _reply(request, 400, "This sign-in link is not the one ledgerlens is waiting for.")
        if params.get("error"):
            outcome: Callback | QboAuthError = QboAuthError(
                0, params, "sign-in refused: " + params["error"]
                + (f" ({params['error_description']})" if params.get("error_description") else ""))
            status, text = 400, "Sign-in was refused. You can close this tab."
        elif not params.get("code") or not params.get("realmId"):
            outcome = QboAuthError(0, None, "the callback carried no code or no realmId")
            status, text = 400, "The sign-in response was incomplete."
        else:
            outcome = Callback(params["code"], params["realmId"])
            status, text = 200, "Authorised. You can close this tab."
        with self._lock:  # the first callback with the right state decides
            if self._outcome is None:
                self._outcome = outcome
                self._done.set()
        _reply(request, status, text)

    def wait(self, state: str, timeout: float) -> Callback:
        """Serve requests until the one carrying ``state`` arrives, or ``timeout`` seconds pass."""
        self._state = state
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for listener in self.servers:
                listener.timeout = 0
                selector.register(listener, selectors.EVENT_READ)
            while not self._done.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QboAuthError(0, None, f"no sign-in arrived within {timeout:g}s")
                # Short slices: the callback may arrive on a connection accepted earlier.
                for key, _ in selector.select(min(remaining, 0.2)):
                    key.fileobj.handle_request()
        if isinstance(self._outcome, QboAuthError):
            raise self._outcome
        return self._outcome

    def close(self) -> None:
        for listener in self.servers:
            listener.server_close()

    def __enter__(self) -> CallbackServer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _reply(request: BaseHTTPRequestHandler, status: int, text: str) -> None:
    body = text.encode("utf-8")
    request.send_response(status)
    request.send_header("Content-Type", "text/plain; charset=utf-8")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


_SIGN_IN_AGAIN = "run `ledgerlens qbo-auth` to sign in again"


class QboAuth:
    """The authorisation-code flow: the URL to open, and the token endpoint's two grants."""

    def __init__(self, config: QboConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or default_transport()

    def authorization_url(self, state: str) -> str:
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.config.client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        })

    def exchange(self, code: str, realm_id: str, now: datetime | None = None) -> Tokens:
        """Trade the callback's one-time code for tokens."""
        payload = self._grant({"grant_type": "authorization_code", "code": code,
                               "redirect_uri": self.config.redirect_uri},
                              "the code exchange was refused")
        return self._tokens(payload, realm_id, now)

    def refresh(self, tokens: Tokens, now: datetime | None = None) -> Tokens:
        """New tokens for old. Intuit rotates the refresh token: keep the one returned."""
        payload = self._grant({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
                              f"the stored authorisation was refused; {_SIGN_IN_AGAIN}")
        return self._tokens(payload, tokens.realm_id, now)

    def _grant(self, form: Mapping[str, str], refused: str) -> dict[str, Any]:
        credentials = f"{self.config.client_id}:{self.config.client_secret}".encode()
        headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "Accept": "application/json",
            "Content-Type": _FORM,
            "User-Agent": USER_AGENT,
        }
        response = self.transport.request("POST", TOKEN_URL, headers,
                                          urllib.parse.urlencode(form).encode("ascii"))
        if response.status in (400, 401):
            error = QboError.from_response(response, refused)
            raise QboAuthError(error.status, error.fault, error.message)
        if response.status != 200:
            raise QboError.from_response(response, "token endpoint")
        try:
            payload = response.json()
        except ValueError:
            raise QboError(response.status, None, "token endpoint: the response is not JSON") from None
        missing = [k for k in ("access_token", "refresh_token", "expires_in",
                               "x_refresh_token_expires_in") if not isinstance(payload, dict) or k not in payload]
        if missing:
            raise QboError(response.status, None, f"token endpoint: the response has no {', '.join(missing)}")
        return payload

    def _tokens(self, payload: Mapping[str, Any], realm_id: str, now: datetime | None) -> Tokens:
        return Tokens.issued(payload["access_token"], payload["refresh_token"],
                             int(payload["expires_in"]), int(payload["x_refresh_token_expires_in"]),
                             realm_id, self.config.environment, now=now)


# --- the Accounting API client ------------------------------------------------------

#: How often a throttled request is tried before giving up, and the wait between tries.
#: Intuit documents no Retry-After on a 429 and says to wait 60 seconds; a Retry-After
#: that does arrive is honoured, up to two minutes.
THROTTLE_ATTEMPTS = 3
THROTTLE_DEFAULT_WAIT = 60
THROTTLE_MAX_WAIT = 120


class QboClient:
    """Accounting API requests for one company, with the token kept fresh.

    Every request carries the bearer token, ``Accept: application/json`` and
    ``minorversion``. An access token within a minute of expiry is refreshed
    first; a 401 is answered by one refresh and one retry (a second 401 means
    the authorisation itself is gone); a 429 waits for ``Retry-After`` when
    one is sent (at most 120 s), else the 60 s Intuit asks for, and gives up
    after three throttled attempts. A
    rotated refresh token is saved through ``token_store`` the moment it
    arrives, because the old one stops working.
    """

    def __init__(self, config: QboConfig, tokens: Tokens, transport: Transport,
                 token_store: TokenStore | None = None, auth: QboAuth | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] | None = None):
        self.config = config
        self.tokens = tokens
        self.transport = transport
        self.token_store = token_store
        self.auth = auth or QboAuth(config, transport)
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refreshes = 0
        self.requests = 0

    def _refresh(self) -> None:
        self.tokens = self.auth.refresh(self.tokens, now=self.now())
        self.refreshes += 1
        if self.token_store is not None:
            try:
                self.token_store.save(self.tokens)
            except (TokenStoreError, OSError) as exc:
                raise QboAuthError(0, None, f"QuickBooks issued a new refresh token that could not "
                                            f"be saved ({exc}); the old one no longer works, so "
                                            f"{_SIGN_IN_AGAIN}") from None

    def request(self, method: str, path: str, *, query: Mapping[str, str] | None = None,
                body: str | None = None, content_type: str | None = None) -> Any:
        """One API call; the parsed JSON body, or :class:`QboError`."""
        if self.tokens.refresh_expired(now=self.now()):
            raise QboAuthError(0, None, f"the stored authorisation has expired; {_SIGN_IN_AGAIN}")
        if self.tokens.access_expired(now=self.now()):
            self._refresh()
        params = {**(query or {}), "minorversion": MINOR_VERSION}
        url = f"{self.config.base_url}{path}?{urllib.parse.urlencode(sorted(params.items()))}"
        data = body.encode("utf-8") if body is not None else None
        retried_401 = False
        throttled = 0
        while True:
            headers = {"Authorization": f"Bearer {self.tokens.access_token}",
                       "Accept": "application/json", "User-Agent": USER_AGENT}
            if content_type:
                headers["Content-Type"] = content_type
            self.requests += 1
            response = self.transport.request(method, url, headers, data)
            if response.status == 401:
                if retried_401:
                    error = QboError.from_response(response, f"{method} {path}")
                    raise QboAuthError(401, error.fault,
                                       f"{error.message}; refreshed once and still refused, so "
                                       f"{_SIGN_IN_AGAIN}")
                retried_401 = True
                self._refresh()
                continue
            if response.status == 429:
                throttled += 1
                if throttled >= THROTTLE_ATTEMPTS:
                    raise QboError(429, None, f"{method} {path}: throttled {throttled} times in a "
                                              "row; wait a minute and run the pull again")
                self.sleep(_retry_after(response))
                continue
            if not 200 <= response.status < 300:
                raise QboError.from_response(response, f"{method} {path}")
            try:
                payload = response.json()
            except ValueError:
                raise QboError(response.status, None, f"{method} {path}: the response is not JSON") from None
            if isinstance(payload, dict) and ("Fault" in payload or "fault" in payload):
                raise QboError.from_response(response, f"{method} {path}")
            return payload

    def query(self, statement: str, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
        """Every row a query statement selects, page by page (``STARTPOSITION`` is 1-based)."""
        entity = re.search(r"\bfrom\s+(\w+)", statement, re.IGNORECASE)
        if not entity:
            raise ValueError(f"not a query statement: {statement!r}")
        rows: list[dict[str, Any]] = []
        start = 1
        while True:
            page = self.request("POST", "/query", body=f"{statement} STARTPOSITION {start} "
                                                        f"MAXRESULTS {page_size}",
                                content_type="application/text")
            found = page.get("QueryResponse") or {}
            key = next((k for k in found if k.lower() == entity.group(1).lower()), None)
            got = (found.get(key) or []) if key else []
            rows.extend(got)
            if len(got) < page_size:
                return rows
            start += page_size

    def report(self, name: str, **params: str) -> dict[str, Any]:
        return self.request("GET", f"/reports/{name}", query=params)


def _retry_after(response: Response) -> float:
    try:
        wait = float(response.headers.get("retry-after", ""))
    except ValueError:
        wait = THROTTLE_DEFAULT_WAIT
    return max(0.0, min(wait, THROTTLE_MAX_WAIT))


# --- mapping QuickBooks into the ledger contract -------------------------------------

#: Where each QuickBooks transaction type sits among the ledger's sources.
TXN_SOURCE = {
    "JournalEntry": "Manual",
    "Bill": "AP", "BillPayment": "AP", "BillPaymentCheck": "AP", "BillPaymentCreditCard": "AP",
    "VendorCredit": "AP", "Expense": "AP", "Check": "AP", "Purchase": "AP",
    "CreditCardExpense": "AP", "CreditCardCredit": "AP", "PurchaseOrder": "AP",
    # both seen in a real sandbox pull: a purchase paid in cash, and a remittance that
    # settles what is owed to a tax agency; neither is system-generated
    "CashExpense": "AP", "SalesTaxPayment": "AP",
    "Invoice": "AR", "Payment": "AR", "SalesReceipt": "AR", "CreditMemo": "AR",
    "RefundReceipt": "AR", "Refund": "AR",
    "Deposit": "Bank", "Transfer": "Bank",
    "Paycheck": "Payroll",
}
UNKNOWN_ACCOUNT_TYPE = "Unknown"
UNKNOWN_USER = "qbo-unknown"
#: An estimated entry time sits at noon: inside business hours, so it raises no
#: after-hours flag of its own (the count of estimated times is reported instead).
ESTIMATED_HOUR = 12
EXPORT_COLUMNS = (*REQUIRED_COLUMNS, "entered_at_estimated")
#: Below this many entries the model tier and Benford analysis have too little to work on.
SMALL_LEDGER = 50


def type_token(txn_type: str) -> str:
    """``"Bill Payment (Check)"`` -> ``"BillPaymentCheck"``: the report's label as an entity name."""
    return re.sub(r"[^A-Za-z0-9]", "", txn_type or "")


def source_for(token: str) -> str:
    if token in TXN_SOURCE:
        return TXN_SOURCE[token]
    return "Payroll" if token.startswith("Payroll") else "System"


def entry_id_for(token: str, txn_id: str) -> str:
    """``QBO-Invoice-1037``: the type is part of the id because QuickBooks numbers each type
    separately, so an Invoice and a Bill can share an Id."""
    return f"QBO-{token}-{txn_id}"


@dataclass
class PullStats:
    """What a pull found and what it set aside. Every skipped line is counted here."""

    accounts: int = 0
    inactive_accounts: int = 0
    sub_accounts: int = 0
    journal_entries: int = 0
    adjusting_entries: int = 0
    other_transactions: int = 0
    description_only_lines: int = 0
    other_detail_lines: int = 0
    zero_amount_lines: int = 0
    zero_transactions: int = 0
    unknown_account_lines: int = 0
    rows_without_txn_id: int = 0
    estimated_entered_at: int = 0
    unreadable_entered_at: int = 0
    unknown_users: int = 0
    unbalanced_entries: int = 0
    je_ids_missing_from_report: int = 0
    je_ids_missing_from_query: int = 0
    utc_times_without_zone: int = 0

    def describe(self) -> list[str]:
        return [
            f"Accounts {self.accounts} ({self.inactive_accounts} inactive, {self.sub_accounts} sub-accounts)",
            f"Journal entries {self.journal_entries} ({self.adjusting_entries} adjusting); "
            f"other transactions {self.other_transactions}",
            f"Set aside: {self.description_only_lines} description-only line(s), "
            f"{self.other_detail_lines} other non-posting line(s), {self.zero_amount_lines} zero line(s), "
            f"{self.rows_without_txn_id} report row(s) with no transaction (balances), "
            f"{self.zero_transactions} transaction(s) with no posting line",
            f"Estimated: entry time on {self.estimated_entered_at} entrie(s) (no readable time of day), "
            f"user on {self.unknown_users} entrie(s) ({UNKNOWN_USER})",
            f"Checks: {self.unknown_account_lines} line(s) on accounts the Account query did not "
            f"return, {self.unbalanced_entries} unbalanced entrie(s), "
            f"{self.je_ids_missing_from_report} journal entrie(s) missing from the GL report, "
            f"{self.je_ids_missing_from_query} journal entrie(s) in the GL report the query did "
            "not return (not in the ledger)",
        ] + ([f"Warning: {self.utc_times_without_zone} entry time(s) arrived in UTC and "
              "no QBO_TIMEZONE is set: unless the company keeps UTC, they are not on its "
              "clock; set QBO_TIMEZONE to the company's own time zone"]
             if self.utc_times_without_zone else []) + (
            [f"Warning: {self.unreadable_entered_at} entrie(s) whose create date this tool cannot "
             "read; their entry time is estimated at noon on the date the value starts with, "
             "else on the posting date, so the keying-time tests are weaker for them"]
            if self.unreadable_entered_at else [])


@dataclass(frozen=True)
class Account:
    id: str
    code: str
    name: str
    type: str
    active: bool = True
    sub_account: bool = False


def accounts_by_id(rows: list[Mapping[str, Any]], stats: PullStats | None = None) -> dict[str, Account]:
    """Account query rows by Id. The code is ``AcctNum`` when numbering is on, else the Id;
    a sub-account is named by its full ``Parent:Child`` path; the type is the Classification."""
    stats = stats if stats is not None else PullStats()
    accounts: dict[str, Account] = {}
    for row in rows:
        sub = bool(row.get("SubAccount"))
        classification = row.get("Classification")
        account = Account(
            id=str(row["Id"]),
            code=str(row.get("AcctNum") or row["Id"]),
            name=str((row.get("FullyQualifiedName") if sub else None) or row.get("Name") or row["Id"]),
            type=classification if classification in ACCOUNT_TYPES else UNKNOWN_ACCOUNT_TYPE,
            active=row.get("Active", True) is not False,
            sub_account=sub,
        )
        accounts[account.id] = account
        stats.accounts += 1
        stats.inactive_accounts += not account.active
        stats.sub_accounts += sub
    return accounts


def _account(accounts: Mapping[str, Account], ref_id: Any, ref_name: Any, stats: PullStats) -> Account:
    found = accounts.get(str(ref_id)) if ref_id not in (None, "") else None
    if found is not None:
        return found
    stats.unknown_account_lines += 1
    label = str(ref_id) if ref_id not in (None, "") else "none"
    return Account(label, label, str(ref_name or f"Unknown account {label}"), UNKNOWN_ACCOUNT_TYPE)


#: The timestamp shapes Intuit writes (``2025-12-28T10:15:00-08:00`` from the API,
#: ``...-0800`` in the GL report, ``...Z``, fractional seconds), read with strptime: on
#: fromisoformat, Python 3.9 refuses ``-0800`` and 3.12 takes ``2025-12-28-0800`` (a date
#: and an offset) for 08:00, so the two supported Pythons built different ledgers.
_TIMESTAMP_FORMATS = tuple(
    f"%Y-%m-%d{sep}{clock}{fraction}{offset}"
    for sep in ("T", " ") for clock in ("%H:%M:%S", "%H:%M") for fraction in (".%f", "")
    for offset in ("%z", "") if not (fraction and clock == "%H:%M"))


def parse_qbo_datetime(value: str, zone: str | None = None) -> datetime:
    """A QuickBooks timestamp as a naive datetime: the clock time as given, or converted to
    ``zone`` first when one is named and the value carries an offset."""
    text = value.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            moment = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"not a QuickBooks timestamp: {value!r}")
    if moment.tzinfo is not None and zone:
        moment = moment.astimezone(_zone(zone))
    return moment.replace(tzinfo=None)


_UTC_SUFFIX = re.compile(r"(Z|[+-]00:?00)$")
# a date, then a time of day; as lenient as strptime (unpadded fields, either case of T)
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{1,2}-\d{1,2}[Tt ]\d{1,2}:\d{1,2}")
_REPORT_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S")
_REPORT_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
_LEADING_DATE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})|(\d{1,2}/\d{1,2}/\d{4})")


@dataclass(frozen=True)
class ReportStamp:
    """How a report ``create_date`` was read. ``kind`` is ``time`` (a time of day was read),
    ``date`` (a date alone), ``unreadable-date`` (a format this tool does not read, but it
    starts with a date, which is kept), ``unreadable`` (nothing could be taken from it) or
    ``blank``; the last two stand in the posting date."""

    when: datetime
    kind: str
    utc: bool = False

    @property
    def estimated(self) -> bool:
        return self.kind != "time"

    @property
    def unreadable(self) -> bool:
        return self.kind.startswith("unreadable")

    @property
    def rank(self) -> int:
        """What the stamp tells: its own time of day (0), its own date (1), nothing (2)."""
        return {"time": 0, "date": 1, "unreadable-date": 1}.get(self.kind, 2)

    @property
    def order(self) -> tuple:
        """How rows' stamps compete for a transaction's entry time. The keying date is the
        audit signal, so the earliest date any row gives wins; within that day a time of day
        beats a date alone, and a date that was read beats one taken from an unreadable
        value. Only when no row gives a date does the posting date stand in, and then an
        unreadable value is kept over a blank so that the warning says why."""
        return (self.rank == 2, self.when.date(), self.rank, self.when,
                self.kind in ("unreadable-date", "blank"))


def read_report_stamp(value: str, posting_date: datetime, zone: str | None = None) -> ReportStamp:
    """A report's ``create_date``. A timestamp is used as given. A date alone keeps its date
    with the time estimated at noon, because the date is the audit signal (an entry keyed
    long after its posting date); so does an unreadable value that starts with one. An empty
    or dateless value stands in the posting date at noon."""
    text = (value or "").strip()
    fallback = posting_date.replace(hour=ESTIMATED_HOUR, minute=0, second=0, microsecond=0)
    if not text:
        return ReportStamp(fallback, "blank")
    if _ISO_TIMESTAMP.match(text):
        try:
            return ReportStamp(parse_qbo_datetime(text, zone), "time", bool(_UTC_SUFFIX.search(text)))
        except ValueError:
            pass
    for fmt in _REPORT_TIME_FORMATS:
        try:
            return ReportStamp(datetime.strptime(text, fmt), "time")
        except ValueError:
            continue
    for fmt in _REPORT_DATE_FORMATS:
        try:
            return ReportStamp(datetime.strptime(text, fmt).replace(hour=ESTIMATED_HOUR), "date")
        except ValueError:
            continue
    leading = _LEADING_DATE.match(text)
    if leading:
        try:
            day = datetime.strptime(leading.group(), "%Y-%m-%d" if leading.group(1) else "%m/%d/%Y")
            return ReportStamp(day.replace(hour=ESTIMATED_HOUR), "unreadable-date")
        except ValueError:
            pass
    return ReportStamp(fallback, "unreadable")


def parse_report_datetime(value: str, posting_date: datetime,
                          zone: str | None = None) -> tuple[datetime, bool]:
    """A report's ``create_date`` as ``(entered_at, estimated)``; see ``read_report_stamp``."""
    stamp = read_report_stamp(value, posting_date, zone)
    return stamp.when, stamp.estimated


def _date(value: str) -> datetime:
    return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")


def _money(value: Any) -> float:
    text = str(value if value is not None else "").replace(",", "").strip()
    return float(text) if text else 0.0


def report_columns(report: Mapping[str, Any]) -> list[str]:
    """The report's column keys in order; refuses a report without debit and credit columns
    (a multicurrency company, or a report that ignored the ``columns`` parameter)."""
    keys = [column_key(c) for c in ((report.get("Columns") or {}).get("Column") or [])]
    missing = [k for k in ("tx_date", "txn_type", "debt_amt", "credit_amt") if k not in keys]
    if missing:
        raise ValueError(
            f"the GeneralLedger report has no {', '.join(missing)} column(s) (it has {keys}); "
            "multicurrency companies report amounts differently and are not supported")
    return keys


def _report_rows(report: Mapping[str, Any]):
    """Yield ``(cells, account_cell)`` for every Data row, walking nested sections; the
    account is the innermost section header's first cell (``{"value", "id"}``)."""

    def walk(node: Any, account: Mapping[str, Any] | None):
        for row in (node or {}).get("Row") or []:
            if not isinstance(row, dict):
                continue
            if row.get("type") == "Section" or "Header" in row or "Rows" in row:
                header = ((row.get("Header") or {}).get("ColData") or [None])[0]
                yield from walk(row.get("Rows"), header if isinstance(header, dict) and header.get("id")
                                else account)
            elif isinstance(row.get("ColData"), list):
                yield row["ColData"], account

    yield from walk(report.get("Rows"), None)


def _cells(keys: list[str], col_data: list[Mapping[str, Any]]) -> tuple[dict[str, str], str | None]:
    """A row's values by key, and the transaction id (on the ``txn_type`` cell; ``tx_date``
    is checked too, in case a report variant carries it there)."""
    values: dict[str, str] = {}
    txn_id = None
    for key, cell in zip(keys, col_data):
        cell = cell if isinstance(cell, dict) else {}
        values[key] = str(cell.get("value") or "")
        if key in ("txn_type", "tx_date") and cell.get("id") and txn_id is None:
            txn_id = str(cell["id"])
    return values, txn_id


def report_headers(report: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    """``(type_token, txn_id) -> {"create_by", "create_date"}`` for every transaction in the
    report, journal entries included (the JournalEntry entity carries no user)."""
    keys = report_columns(report)
    headers: dict[tuple[str, str], dict[str, str]] = {}
    for col_data, _ in _report_rows(report):
        values, txn_id = _cells(keys, col_data)
        if txn_id:
            headers.setdefault((type_token(values.get("txn_type", "")), txn_id), {
                "create_by": values.get("create_by", ""), "create_date": values.get("create_date", "")})
    return headers


def journal_entries_to_lines(entries: list[Mapping[str, Any]], accounts: Mapping[str, Account],
                             headers: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
                             zone: str | None = None,
                             stats: PullStats | None = None) -> list[dict[str, Any]]:
    """JournalEntry entities as ledger lines (``source`` Manual). The entry time is the
    entity's ``MetaData.CreateTime``; the user comes from the GL report."""
    stats = stats if stats is not None else PullStats()
    headers = headers or {}
    lines: list[dict[str, Any]] = []
    for entry in entries:
        txn_id = str(entry["Id"])
        posting = _date(entry["TxnDate"])
        # read like a report stamp: a shape the parser does not know is estimated and counted,
        # never a traceback halfway through a pull
        stamp = read_report_stamp((entry.get("MetaData") or {}).get("CreateTime") or "", posting, zone)
        entered_at, estimated = stamp.when, stamp.estimated
        user = (headers.get(("JournalEntry", txn_id)) or {}).get("create_by") or UNKNOWN_USER
        fallback = entry.get("PrivateNote") or f"Journal entry {entry.get('DocNumber') or txn_id}"
        entry_lines: list[dict[str, Any]] = []
        for line in entry.get("Line") or []:
            detail_type = line.get("DetailType")
            if detail_type != "JournalEntryLineDetail":
                if detail_type == "DescriptionOnly":
                    stats.description_only_lines += 1
                else:
                    stats.other_detail_lines += 1
                continue
            detail = line.get("JournalEntryLineDetail") or {}
            amount = _money(line.get("Amount"))
            side = detail.get("PostingType")
            if amount < 0:  # a negative amount posts to the other side
                amount, side = -amount, {"Debit": "Credit", "Credit": "Debit"}.get(side, side)
            if amount == 0 or side not in ("Debit", "Credit"):
                stats.zero_amount_lines += 1
                continue
            ref = detail.get("AccountRef") or {}
            account = _account(accounts, ref.get("value"), ref.get("name"), stats)
            entry_lines.append(_line(entry_id_for("JournalEntry", txn_id), len(entry_lines) + 1, posting,
                                     entered_at, account, line.get("Description") or fallback,
                                     amount if side == "Debit" else 0.0,
                                     amount if side == "Credit" else 0.0, "Manual", user, estimated))
        if not entry_lines:  # no posting line (all zero or description-only): set aside
            stats.zero_transactions += 1
            continue
        stats.journal_entries += 1
        stats.adjusting_entries += bool(entry.get("Adjustment"))
        stats.estimated_entered_at += estimated
        stats.unreadable_entered_at += stamp.unreadable
        stats.unknown_users += user == UNKNOWN_USER
        # QuickBooks writes CreateTime with the company's offset, so "as given" is the
        # company's clock, the report's; a UTC time without a zone is not.
        stats.utc_times_without_zone += not zone and stamp.utc
        lines.extend(entry_lines)
    return lines


def general_ledger_to_lines(report: Mapping[str, Any], accounts: Mapping[str, Account],
                            zone: str | None = None,
                            stats: PullStats | None = None) -> list[dict[str, Any]]:
    """Every transaction but journal entries, rebuilt from the GL report.

    The report lists each posting under the account it hits, so grouping its
    rows by transaction id gives each transaction back whole (an invoice with
    a tax line is three lines). Journal entries are skipped here: the entity
    query has their lines in full. A row with no transaction id (a beginning
    balance) is counted and skipped.
    """
    stats = stats if stats is not None else PullStats()
    keys = report_columns(report)
    groups: dict[tuple[str, str], list[tuple[dict[str, str], Mapping[str, Any] | None]]] = {}
    for col_data, account_cell in _report_rows(report):
        values, txn_id = _cells(keys, col_data)
        if not txn_id:
            stats.rows_without_txn_id += 1
            continue
        token = type_token(values.get("txn_type", ""))
        if token == "JournalEntry":
            continue
        groups.setdefault((token, txn_id), []).append((values, account_cell))

    lines: list[dict[str, Any]] = []
    for (token, txn_id), rows in groups.items():
        posting = _date(rows[0][0]["tx_date"])
        # Rows of one transaction can carry different create dates (the sandbox stamps some
        # invoices' tax rows later), and which row comes first is only the order of the
        # account sections: see ReportStamp.order for which stamp keys the entry.
        stamp, keyed = min(((read_report_stamp(values.get("create_date", ""), posting, zone), values)
                            for values, _ in rows), key=lambda s: s[0].order)
        estimated, entered_at = stamp.estimated, stamp.when
        user = (keyed.get("create_by") or next((v.get("create_by") for v, _ in rows if v.get("create_by")), "")
                or UNKNOWN_USER)
        entry: list[dict[str, Any]] = []
        for values, account_cell in rows:
            debit, credit = _money(values.get("debt_amt")), _money(values.get("credit_amt"))
            if debit < 0:
                debit, credit = 0.0, credit - debit
            if credit < 0:
                debit, credit = debit - credit, 0.0
            if debit == 0 and credit == 0:
                stats.zero_amount_lines += 1
                continue
            cell = account_cell or {}
            account = _account(accounts, cell.get("id"), cell.get("value"), stats)
            label = values.get("txn_type") or token
            description = (values.get("memo") or values.get("name")
                           or (f"{label} {values['doc_num']}" if values.get("doc_num") else label))
            entry.append(_line(entry_id_for(token, txn_id), len(entry) + 1, posting, entered_at, account,
                               description, debit, credit, source_for(token), user, estimated))
        if not entry:  # no posting line: e.g. QuickBooks' own .00 payment linking a credit
            stats.zero_transactions += 1
            continue
        stats.other_transactions += 1
        stats.estimated_entered_at += estimated
        # a silent fallback once passed a whole pull off as "date only": say it instead
        stats.unreadable_entered_at += stamp.unreadable
        stats.utc_times_without_zone += not zone and stamp.utc
        stats.unknown_users += user == UNKNOWN_USER
        lines.extend(entry)
    return lines


def _line(entry_id: str, line_no: int, posting: datetime, entered_at: datetime, account: Account,
          description: str, debit: float, credit: float, source: str, user: str,
          estimated: bool) -> dict[str, Any]:
    return {
        "entry_id": entry_id, "line_no": line_no, "posting_date": posting,
        "entered_at": entered_at, "fiscal_year": posting.year, "period": posting.month,
        "account_code": account.code, "account_name": account.name, "account_type": account.type,
        "description": description, "debit": round(debit, 2), "credit": round(credit, 2),
        "source": source, "created_by": user, "entered_at_estimated": estimated,
    }


def to_ledger_frame(lines: list[Mapping[str, Any]], stats: PullStats | None = None) -> pd.DataFrame:
    """Lines as a prepared ledger, sorted by date, entry and line; counts unbalanced entries."""
    if not lines:
        raise ValueError("no transactions in range")
    frame = pd.DataFrame(list(lines), columns=list(EXPORT_COLUMNS))
    frame = frame.sort_values(["posting_date", "entry_id", "line_no"], kind="mergesort")
    prepared = prepare(frame.reset_index(drop=True))
    if stats is not None:
        stats.unbalanced_entries = int((entry_level(prepared)["imbalance"].abs() > 0.005).sum())
    return prepared


def pull(client: QboClient, start: date, end: date,
         zone: str | None = None) -> tuple[pd.DataFrame, PullStats]:
    """One period's books: accounts, journal entries and the GL report, as a prepared ledger.
    Timestamps are converted to ``zone``, else to the client's ``QBO_TIMEZONE``, else kept."""
    zone = zone or client.config.timezone
    stats = PullStats()
    accounts = accounts_by_id(client.query("select * from Account where Active IN (true, false)"),
                              stats)
    entries = client.query(f"select * from JournalEntry where TxnDate >= '{start:%Y-%m-%d}' "
                           f"and TxnDate <= '{end:%Y-%m-%d}'")
    # Accrual, explicitly: the report otherwise follows the company's preference, and a
    # cash-basis report would drop unpaid invoices and bills beside basis-free journal entries.
    report = client.report("GeneralLedger", start_date=f"{start:%Y-%m-%d}",
                           end_date=f"{end:%Y-%m-%d}", accounting_method="Accrual",
                           columns=",".join(GL_COLUMNS))
    headers = report_headers(report)
    lines = journal_entries_to_lines(entries, accounts, headers, zone, stats)
    lines += general_ledger_to_lines(report, accounts, zone, stats)
    queried = {("JournalEntry", str(e["Id"])) for e in entries}
    stats.je_ids_missing_from_report = len(queried - set(headers))
    stats.je_ids_missing_from_query = sum(1 for key in headers
                                          if key[0] == "JournalEntry" and key not in queried)
    return to_ledger_frame(lines, stats), stats


def write_ledger_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame[list(EXPORT_COLUMNS)].to_csv(path, index=False)
    return path


def write_identity(ledger_path: str | Path, realm_id: str, environment: str, start: date,
                   end: date, pulled_at: datetime | None = None) -> Path:
    """The sidecar that files this ledger's reviews under ``qbo:<realm_id>``, which a
    re-pull keeps (a CSV digest would change with every new transaction)."""
    sidecar = identity_path(ledger_path)
    moment = (pulled_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sidecar.write_text(json.dumps({
        "ledger_id": f"qbo:{realm_id}",
        "source": "quickbooks-online",
        "environment": environment,
        "period": {"start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}"},
        "pulled_at": moment.isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    return sidecar
