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

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .tokens import ENVIRONMENTS

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
        prefix = f"{context}: " if context else ""
        try:
            payload = response.json()
        except ValueError:
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


class UrllibTransport:
    """The real network, through :mod:`urllib`. Any HTTP status comes back as a Response."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT):
        self.timeout = timeout

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response:
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (https only)
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
        self._number = start

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None = None) -> Response:
        response = self.inner.request(method, url, headers, body)
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
        return response


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

    return walk(payload)


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


def column_key(column: Mapping[str, Any]) -> str:
    """A report column's key: its ``ColKey`` metadata when present, else its title."""
    for meta in column.get("MetaData") or []:
        if isinstance(meta, dict) and meta.get("Name") == "ColKey" and meta.get("Value"):
            return str(meta["Value"])
    return str(column.get("ColTitle") or "")
