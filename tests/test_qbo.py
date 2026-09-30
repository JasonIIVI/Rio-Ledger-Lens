"""The QuickBooks Online connector, entirely offline.

Requests are answered by a scripted transport or by the recorded fixtures in
tests/fixtures/qbo/; nothing here opens a socket to Intuit.
"""

from __future__ import annotations

import base64
import json
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ledgerlens.connectors.qbo import (
    AUTH_URL,
    DEFAULT_REDIRECT_URI,
    MINOR_VERSION,
    SCOPE,
    TOKEN_URL,
    CallbackServer,
    QboAuth,
    QboAuthError,
    QboClient,
    QboConfig,
    QboConfigError,
    QboError,
    RecordedTransport,
    Recorder,
    Response,
    UnexpectedRequest,
    canonical_request,
    new_state,
    sanitize,
)
from ledgerlens.connectors.tokens import Tokens, TokenStore, repository_root

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qbo"
BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company/4620816365"
TOKEN = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
TEXT = {"Content-Type": "application/text"}


def reply(status=200, body=None, headers=None) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, raw)


class ScriptedTransport:
    """Answers requests from a list, in order, and records what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def write_fixture(directory: Path, name: str, request: dict, status=200, body=None, headers=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({
        "request": request,
        "response": {"status": status, "headers": headers or {}, "body": body},
    }))


# --- configuration --------------------------------------------------------------


def test_config_names_every_missing_variable_and_never_shows_the_secret():
    with pytest.raises(QboConfigError, match="QBO_CLIENT_ID, QBO_CLIENT_SECRET"):
        QboConfig.from_env({})
    config = QboConfig.from_env({"QBO_CLIENT_ID": "id", "QBO_CLIENT_SECRET": "s3cret-value",
                                 "QBO_REALM_ID": "4620816365"})
    assert "s3cret-value" not in repr(config)
    assert config.environment == "sandbox"
    assert config.redirect_uri == DEFAULT_REDIRECT_URI
    assert (config.callback_port, config.callback_path) == (8765, "/callback")
    assert config.base_url == BASE


@pytest.mark.parametrize("override, why", [
    ({"QBO_ENVIRONMENT": "Sandbox"}, "QBO_ENVIRONMENT"),
    ({"QBO_REDIRECT_URI": "https://example.com/callback"}, "QBO_REDIRECT_URI"),
    ({"QBO_REDIRECT_URI": "http://localhost/callback"}, "QBO_REDIRECT_URI"),
    ({"QBO_TIMEZONE": "Not/AZone"}, "QBO_TIMEZONE"),
])
def test_config_refuses_settings_it_cannot_use(override, why):
    env = {"QBO_CLIENT_ID": "id", "QBO_CLIENT_SECRET": "s", **override}
    with pytest.raises(QboConfigError, match=why):
        QboConfig.from_env(env)


def test_a_config_without_a_realm_has_no_base_url():
    with pytest.raises(QboConfigError, match="realm"):
        _ = QboConfig("id", "s").base_url


# --- canonical requests and fixtures --------------------------------------------


def test_canonical_request_drops_the_host_the_realm_and_every_secret_value():
    query = canonical_request("post", f"{BASE}/query?minorversion=75",
                              b"select * from   Account\n where Active IN (true, false)",
                              "application/text")
    assert query == {"method": "POST", "path": "/query", "query": [["minorversion", "75"]],
                     "body": "select * from Account where Active IN (true, false)"}
    token = canonical_request("POST", TOKEN, b"grant_type=refresh_token&refresh_token=RT-SECRET",
                              "application/x-www-form-urlencoded")
    assert token["body"] == {"grant_type": "refresh_token", "fields": ["grant_type", "refresh_token"]}
    assert "RT-SECRET" not in json.dumps(token) and "4620816365" not in json.dumps(query)
    report = canonical_request("GET", f"{BASE}/reports/GeneralLedger?start_date=2025-10-01&"
                               "minorversion=75&end_date=2025-12-31", None, None)
    assert report["query"] == [["end_date", "2025-12-31"], ["minorversion", "75"],
                               ["start_date", "2025-10-01"]]


def test_recorded_transport_replays_each_fixture_once_in_name_order(tmp_path):
    ask = canonical_request("GET", f"{BASE}/companyinfo/1?minorversion=75", None, None)
    write_fixture(tmp_path, "020-second.json", ask, body={"n": 2})
    write_fixture(tmp_path, "010-first.json", ask, status=401, body={"n": 1})
    transport = RecordedTransport(tmp_path)
    assert transport.unused == ["010-first.json", "020-second.json"]
    first = transport.request("GET", f"{BASE}/companyinfo/1?minorversion=75", {})
    second = transport.request("GET", f"{BASE}/companyinfo/1?minorversion=75", {})
    assert (first.status, first.json(), second.json()) == (401, {"n": 1}, {"n": 2})
    assert transport.unused == []
    with pytest.raises(UnexpectedRequest, match="companyinfo"):
        transport.request("GET", f"{BASE}/companyinfo/1?minorversion=75", {})


def test_a_directory_without_fixtures_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no fixtures"):
        RecordedTransport(tmp_path)


def test_the_recorder_writes_sanitized_fixtures_that_replay(tmp_path):
    token_body = {"access_token": "eyJ-real-access", "refresh_token": "AB11-real-refresh",
                  "id_token": "eyJ-identity", "expires_in": 3600,
                  "x_refresh_token_expires_in": 8640000, "token_type": "bearer"}
    rows = {"QueryResponse": {"JournalEntry": [{
        "Id": "146", "PrivateNote": "Accrual for 4620816365, ask jane.doe@example.org",
        "MetaData": {"CreateTime": "2025-12-28T10:15:00-08:00", "LastModifiedByRef": {"value": "Jane Doe"}},
    }]}}
    report = {
        "Header": {"ReportName": "GeneralLedger"},
        "Columns": {"Column": [
            {"ColTitle": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]},
            {"ColTitle": "Created By", "MetaData": [{"Name": "ColKey", "Value": "create_by"}]},
        ]},
        "Rows": {"Row": [{"type": "Section", "Header": {"ColData": [{"value": "Checking", "id": "35"}]},
                          "Rows": {"Row": [{"type": "Data", "ColData": [
                              {"value": "2025-12-30"}, {"value": "Jane Doe"}]}]}}]},
    }
    live = ScriptedTransport([reply(body=token_body, headers={"Content-Type": "application/json",
                                                              "intuit_tid": "abc"}),
                              reply(body=rows), reply(body=report)])
    recorder = Recorder(live, tmp_path, "4620816365")
    got = recorder.request("POST", TOKEN, FORM, b"grant_type=refresh_token&refresh_token=AB11-real-refresh")
    assert got.json()["access_token"] == "eyJ-real-access"  # the caller sees the real thing
    recorder.request("POST", f"{BASE}/query?minorversion=75", TEXT,
                     b"select * from JournalEntry STARTPOSITION 1 MAXRESULTS 1000")
    recorder.request("GET", f"{BASE}/reports/GeneralLedger?minorversion=75", {})

    names = [p.name for p in recorder.written]
    assert names == ["010-post-token-refresh_token.json", "020-post-query-journalentry-p1.json",
                     "030-get-reports-generalledger.json"]
    text = "".join(p.read_text() for p in recorder.written)
    for secret in ("eyJ-real-access", "AB11-real-refresh", "eyJ-identity", "4620816365",
                   "jane.doe@example.org", "Jane Doe", "intuit_tid"):
        assert secret not in text, secret
    stored = json.loads(recorder.written[0].read_text())
    assert stored["response"]["body"]["access_token"] == "TEST-ACCESS"
    assert stored["response"]["body"]["refresh_token"] == "TEST-REFRESH"
    assert "id_token" not in stored["response"]["body"]
    assert stored["response"]["headers"] == {"content-type": "application/json"}
    entry = json.loads(recorder.written[1].read_text())["response"]["body"]["QueryResponse"]["JournalEntry"][0]
    assert entry["PrivateNote"] == "Accrual for REALM, ask redacted@example.com"
    assert entry["MetaData"]["LastModifiedByRef"]["value"] == "qbo-user-1"
    gl = json.loads(recorder.written[2].read_text())["response"]["body"]
    assert gl["Rows"]["Row"][0]["Rows"]["Row"][0]["ColData"][1]["value"] == "qbo-user-1"
    assert gl["Rows"]["Row"][0]["Header"]["ColData"][0] == {"value": "Checking", "id": "35"}

    replay = RecordedTransport(tmp_path)
    again = replay.request("POST", f"{BASE}/query?minorversion=75", TEXT,
                           b"select * from JournalEntry STARTPOSITION 1 MAXRESULTS 1000")
    assert again.json()["QueryResponse"]["JournalEntry"][0]["Id"] == "146"


def test_sanitize_gives_each_person_one_stand_in():
    users: dict[str, str] = {}
    out = sanitize([{"CreatedBy": "Ann"}, {"LastModifiedBy": "Bo"}, {"UserName": "Ann"}], "", users)
    assert out == [{"CreatedBy": "qbo-user-1"}, {"LastModifiedBy": "qbo-user-2"},
                   {"UserName": "qbo-user-1"}]


# --- faults ---------------------------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    ({"Fault": {"Error": [{"Message": "message=AuthenticationFailed", "Detail": "Token expired",
                           "code": "3200"}], "type": "AUTHENTICATION"}},
     "HTTP 401: message=AuthenticationFailed; Token expired (code 3200)"),
    ({"fault": {"error": [{"message": "message=AuthenticationFailed", "detail": "Token expired",
                           "code": "3200"}], "type": "AUTHENTICATION"}},
     "HTTP 401: message=AuthenticationFailed; Token expired (code 3200)"),
    ({"error": "invalid_grant", "error_description": "Incorrect Token type or clientID"},
     "HTTP 401: invalid_grant (Incorrect Token type or clientID)"),
    (b"<html>gateway</html>", "HTTP 401: <html>gateway</html>"),
])
def test_both_fault_shapes_and_oauth_errors_read_as_one_message(body, expected):
    error = QboError.from_response(reply(401, body))
    assert (error.status, str(error)) == (401, expected)


# --- OAuth ----------------------------------------------------------------------

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
CONFIG = QboConfig("CLIENT-ID", "CLIENT-SECRET", "sandbox", "4620816365")
TOKEN_REPLY = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2", "expires_in": 3600,
               "x_refresh_token_expires_in": 8640000, "token_type": "bearer"}


def issued(access="ACCESS-1", refresh="REFRESH-1", now=NOW, expires_in=3600) -> Tokens:
    return Tokens.issued(access, refresh, expires_in, 100 * 86400, "4620816365", "sandbox", now=now)


def test_the_authorization_url_asks_for_accounting_only_and_carries_the_state():
    state = new_state()
    url = QboAuth(CONFIG, ScriptedTransport([])).authorization_url(state)
    base, query = url.split("?", 1)
    assert base == AUTH_URL
    assert dict(urllib.parse.parse_qsl(query)) == {
        "client_id": "CLIENT-ID", "response_type": "code", "scope": SCOPE,
        "redirect_uri": DEFAULT_REDIRECT_URI, "state": state}
    assert len(state) >= 40 and new_state() != state


def test_the_code_exchange_uses_basic_auth_and_a_form_body():
    transport = ScriptedTransport([reply(body=TOKEN_REPLY)])
    tokens = QboAuth(CONFIG, transport).exchange("CODE-1", "4620816365", now=NOW)
    call = transport.calls[0]
    assert (call["method"], call["url"]) == ("POST", TOKEN_URL)
    assert call["headers"]["Authorization"] == "Basic " + base64.b64encode(
        b"CLIENT-ID:CLIENT-SECRET").decode()
    assert call["headers"]["Accept"] == "application/json"
    assert dict(urllib.parse.parse_qsl(call["body"].decode())) == {
        "grant_type": "authorization_code", "code": "CODE-1", "redirect_uri": DEFAULT_REDIRECT_URI}
    assert (tokens.access_token, tokens.refresh_token, tokens.realm_id) == ("ACCESS-2", "REFRESH-2", "4620816365")
    assert tokens.expires_at == "2026-09-30T13:00:00+00:00"
    assert tokens.refresh_expires_at == "2027-01-08T12:00:00+00:00"


def test_a_refused_refresh_says_to_sign_in_again():
    transport = ScriptedTransport([reply(400, {"error": "invalid_grant"})])
    with pytest.raises(QboAuthError, match="qbo-auth.*invalid_grant|invalid_grant.*qbo-auth"):
        QboAuth(CONFIG, transport).refresh(issued(), now=NOW)


def test_a_token_reply_without_its_fields_is_an_error_not_a_crash():
    transport = ScriptedTransport([reply(body={"access_token": "A"})])
    with pytest.raises(QboError, match="refresh_token, expires_in, x_refresh_token_expires_in"):
        QboAuth(CONFIG, transport).exchange("CODE", "1", now=NOW)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (loopback)
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _visit(server: CallbackServer, *queries: str, path: str = "/callback"):
    """Browser stand-in: after the server starts waiting, GET each query in turn."""
    results = []

    def run():
        for q in queries:
            results.append(_get(f"http://127.0.0.1:{server.port}{path}?{q}"))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results


def test_the_callback_server_ignores_strays_and_returns_the_real_callback():
    with CallbackServer(0) as server:
        good = urllib.parse.urlencode({"code": "CODE-9", "state": "S-1", "realmId": "4620816365"})
        thread, results = _visit(server, "code=x&state=wrong&realmId=1", good)
        callback = server.wait("S-1", timeout=10)
        thread.join()
    assert (callback.code, callback.realm_id) == ("CODE-9", "4620816365")
    assert "CODE-9" not in repr(callback)
    assert [status for status, _ in results] == [400, 200]
    assert "close this tab" in results[1][1]


def test_the_callback_server_answers_other_paths_with_404():
    with CallbackServer(0) as server:
        thread, results = _visit(server, "code=x&state=S&realmId=1", path="/elsewhere")
        with pytest.raises(QboAuthError, match="within"):
            server.wait("S", timeout=1.5)
        thread.join()
    assert results[0][0] == 404


def test_a_refused_sign_in_and_a_timeout_are_errors():
    with CallbackServer(0) as server:
        thread, _ = _visit(server, "error=access_denied&state=S")
        with pytest.raises(QboAuthError, match="access_denied"):
            server.wait("S", timeout=10)
        thread.join()
    with CallbackServer(0) as server, pytest.raises(QboAuthError, match="no sign-in arrived within 0.3s"):
        server.wait("S", timeout=0.3)


# --- the client -----------------------------------------------------------------


@pytest.fixture
def outside(tmp_path):
    if repository_root(tmp_path) is not None:
        pytest.skip(f"{tmp_path} is inside a git repository")
    return tmp_path / "cfg"


def client_for(responses, tokens=None, store=None, now=NOW):
    transport = ScriptedTransport(responses)
    slept = []
    client = QboClient(CONFIG, tokens or issued(), transport, token_store=store,
                       sleep=slept.append, now=lambda: now)
    return client, transport, slept


def test_every_request_carries_the_bearer_token_accept_and_the_minor_version():
    client, transport, _ = client_for([reply(body={"CompanyInfo": {}})])
    client.request("GET", "/companyinfo/4620816365")
    call = transport.calls[0]
    assert call["url"] == f"{BASE}/companyinfo/4620816365?minorversion={MINOR_VERSION}"
    assert call["headers"]["Authorization"] == "Bearer ACCESS-1"
    assert call["headers"]["Accept"] == "application/json"


def test_a_401_refreshes_once_saves_the_rotated_token_and_retries(outside):
    store = TokenStore.for_realm("sandbox", "4620816365", outside)
    client, transport, _ = client_for(
        [reply(401, {"fault": {"error": [{"message": "AuthenticationFailed", "code": "3200"}]}}),
         reply(body=TOKEN_REPLY), reply(body={"ok": True})], store=store)
    assert client.request("GET", "/companyinfo/1") == {"ok": True}
    assert [c["url"].split("?")[0] for c in transport.calls] == [
        f"{BASE}/companyinfo/1", TOKEN_URL, f"{BASE}/companyinfo/1"]
    assert transport.calls[2]["headers"]["Authorization"] == "Bearer ACCESS-2"
    saved = store.load()
    assert (saved.access_token, saved.refresh_token) == ("ACCESS-2", "REFRESH-2")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert client.refreshes == 1


def test_a_second_401_after_a_refresh_means_the_authorisation_is_gone():
    unauthorised = reply(401, {"Fault": {"Error": [{"Message": "AuthenticationFailed", "code": "3200"}]}})
    client, transport, _ = client_for([unauthorised, reply(body=TOKEN_REPLY), unauthorised])
    with pytest.raises(QboAuthError, match="refreshed once and still refused.*qbo-auth"):
        client.request("GET", "/companyinfo/1")
    assert len(transport.calls) == 3


def test_an_access_token_about_to_expire_is_refreshed_before_the_request():
    client, transport, _ = client_for([reply(body=TOKEN_REPLY), reply(body={"ok": True})],
                                      tokens=issued(now=NOW - timedelta(seconds=3570)))
    client.request("GET", "/companyinfo/1")
    assert transport.calls[0]["url"] == TOKEN_URL
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer ACCESS-2"


def test_an_expired_refresh_token_stops_before_any_request():
    old = Tokens.issued("A", "R", 3600, 86400, "4620816365", "sandbox", now=NOW - timedelta(days=2))
    client, transport, _ = client_for([], tokens=old)
    with pytest.raises(QboAuthError, match="expired.*qbo-auth"):
        client.request("GET", "/companyinfo/1")
    assert transport.calls == []


def test_throttling_waits_for_retry_after_then_gives_up_after_three_tries():
    client, _, slept = client_for([reply(429, b"", {"Retry-After": "2"}), reply(body={"ok": 1})])
    assert client.request("GET", "/companyinfo/1") == {"ok": 1}
    assert slept == [2.0]
    client, transport, slept = client_for([reply(429, b"")] * 3)
    with pytest.raises(QboError, match="throttled 3 times"):
        client.request("GET", "/companyinfo/1")
    assert slept == [5.0, 10.0] and len(transport.calls) == 3


def test_a_200_carrying_a_fault_is_an_error():
    client, _, _ = client_for([reply(body={"Fault": {"Error": [{"Message": "Invalid query"}]}})])
    with pytest.raises(QboError, match="Invalid query"):
        client.request("POST", "/query", body="select * from Nothing")


def test_query_pages_until_a_short_page():
    first = [{"Id": str(i)} for i in range(3)]
    client, transport, _ = client_for([
        reply(body={"QueryResponse": {"JournalEntry": first, "startPosition": 1, "maxResults": 3}}),
        reply(body={"QueryResponse": {"JournalEntry": [{"Id": "9"}], "startPosition": 4}}),
    ])
    rows = client.query("select * from JournalEntry where TxnDate >= '2025-10-01'", page_size=3)
    assert [r["Id"] for r in rows] == ["0", "1", "2", "9"]
    assert [c["body"].decode() for c in transport.calls] == [
        "select * from JournalEntry where TxnDate >= '2025-10-01' STARTPOSITION 1 MAXRESULTS 3",
        "select * from JournalEntry where TxnDate >= '2025-10-01' STARTPOSITION 4 MAXRESULTS 3"]
    assert transport.calls[0]["headers"]["Content-Type"] == "application/text"
    client, _, _ = client_for([reply(body={"QueryResponse": {}})])
    assert client.query("select * from Account") == []


def test_report_sends_sorted_parameters():
    client, transport, _ = client_for([reply(body={"Header": {}})])
    client.report("GeneralLedger", start_date="2025-10-01", end_date="2025-12-31", columns="tx_date")
    assert transport.calls[0]["url"] == (f"{BASE}/reports/GeneralLedger?columns=tx_date&"
                                         f"end_date=2025-12-31&minorversion={MINOR_VERSION}&"
                                         "start_date=2025-10-01")
