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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from ledgerlens.connectors import qbo
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
from ledgerlens.ingest import (
    REQUIRED_COLUMNS,
    entry_level,
    identity_path,
    ledger_identity,
    load_csv,
)
from ledgerlens.schema import SOURCES

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


#: Loopback requests go direct: a developer's proxy variables must not route them.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str) -> tuple[int, str]:
    try:
        with DIRECT.open(url, timeout=5) as resp:
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
    assert slept == [60.0, 60.0] and len(transport.calls) == 3


def test_an_error_names_the_intuit_transaction_id_support_asks_for():
    error = QboError.from_response(reply(500, {"Fault": {"Error": [{"Message": "boom"}]}},
                                         {"intuit_tid": "1-66f9-abc"}), "GET /x")
    assert str(error) == "GET /x: HTTP 500: boom [intuit_tid 1-66f9-abc]"


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


# --- mapping ----------------------------------------------------------------------

PULL = FIXTURES / "pull"  # a sanitized recording of Intuit's sample sandbox company
START, END = date(2026, 7, 1), date(2026, 9, 30)  # the recorded quarter
ACCOUNTS_FILE = "010-post-query-account-p1.json"
JE_FILE = "020-post-query-journalentry-p1.json"
GL_FILE = "030-get-reports-generalledger.json"


def fixture_body(name: str):
    return json.loads((PULL / name).read_text())["response"]["body"]


def fixture_client(directory=PULL, zone=None):
    config = QboConfig.for_fixtures("4620816365", zone)
    return QboClient(config, issued(now=datetime.now(timezone.utc)), RecordedTransport(directory))


def edited_recording(tmp_path, name, edit):
    """A copy of the recording with one response body changed in place by ``edit``: the way
    to reach a shape the recorded quarter does not happen to contain."""
    import shutil

    directory = tmp_path / "pull"
    shutil.copytree(PULL, directory)
    path = directory / name
    fixture = json.loads(path.read_text())
    edit(fixture["response"]["body"])
    path.write_text(json.dumps(fixture))
    return directory


@pytest.fixture(scope="module")
def pulled():
    client = fixture_client()
    frame, stats = qbo.pull(client, START, END)
    assert client.transport.unused == []
    return frame, stats


@pytest.mark.parametrize("label, token, source", [
    ("Journal Entry", "JournalEntry", "Manual"), ("Bill Payment (Check)", "BillPaymentCheck", "AP"),
    ("Bill Payment (Credit Card)", "BillPaymentCreditCard", "AP"), ("Expense", "Expense", "AP"),
    ("Credit Card Expense", "CreditCardExpense", "AP"), ("Cash Expense", "CashExpense", "AP"),
    ("Sales Tax Payment", "SalesTaxPayment", "AP"), ("Invoice", "Invoice", "AR"),
    ("Sales Receipt", "SalesReceipt", "AR"), ("Refund", "Refund", "AR"), ("Deposit", "Deposit", "Bank"),
    ("Transfer", "Transfer", "Bank"), ("Credit Card Payment", "CreditCardPayment", "Bank"),
    ("Paycheck", "Paycheck", "Payroll"),
    ("Payroll Check", "PayrollCheck", "Payroll"), ("Inventory Qty Adjust", "InventoryQtyAdjust", "System"),
])
def test_each_transaction_type_maps_to_a_ledger_source(label, token, source):
    assert qbo.type_token(label) == token
    assert qbo.source_for(token) == source and source in SOURCES
    assert qbo.entry_id_for(token, "1037") == f"QBO-{token}-1037"


def test_every_mapped_source_is_one_the_schema_knows():
    assert set(qbo.TXN_SOURCE.values()) <= set(SOURCES)


def test_accounts_use_acctnum_else_id_full_names_for_sub_accounts_and_the_classification():
    stats = qbo.PullStats()
    raw = fixture_body(ACCOUNTS_FILE)["QueryResponse"]["Account"]
    accounts = qbo.accounts_by_id(raw, stats)
    assert accounts["35"] == qbo.Account("35", "35", "Checking", "Asset")  # numbering is off
    assert accounts["56"].name == "Automobile:Fuel" and accounts["56"].sub_account
    assert accounts["18"].name == "Repair & Maintenance (deleted)" and not accounts["18"].active
    assert (stats.accounts, stats.inactive_accounts, stats.sub_accounts) == (90, 1, 30)
    assert {a.type for a in accounts.values()} == {"Asset", "Liability", "Equity", "Revenue", "Expense"}
    numbered = [dict(row, AcctNum="1010") if row["Id"] == "35" else row for row in raw]
    assert qbo.accounts_by_id(numbered)["35"].code == "1010"


def test_the_pull_replays_every_fixture_and_builds_a_prepared_balanced_ledger(pulled):
    frame, stats = pulled
    assert list(frame.columns[:len(REQUIRED_COLUMNS)]) == list(REQUIRED_COLUMNS)
    assert "entered_at_estimated" in frame.columns and "abs_amount" in frame.columns
    entries = entry_level(frame)
    # counted from the recording by a derivation written without this code
    assert (len(entries), len(frame)) == (116, 297)
    assert entries["entry_id"].str.split("-").str[1].value_counts().to_dict() == {
        "Invoice": 27, "Bill": 14, "Expense": 14, "Payment": 13, "Check": 8, "BillPaymentCheck": 6,
        "CashExpense": 6, "CreditCardExpense": 5, "Deposit": 4, "InventoryQtyAdjust": 4,
        "SalesReceipt": 4, "BillPaymentCreditCard": 3, "JournalEntry": 3, "SalesTaxPayment": 2,
        "CreditCardCredit": 1, "CreditMemo": 1, "Refund": 1}
    assert (entries["imbalance"] == 0).all() and stats.unbalanced_entries == 0
    assert round(frame["debit"].sum(), 2) == round(frame["credit"].sum(), 2) == 73693.65
    assert list(frame["posting_date"]) == sorted(frame["posting_date"])
    assert (stats.journal_entries, stats.adjusting_entries, stats.other_transactions) == (3, 0, 113)
    assert (stats.rows_without_txn_id, stats.zero_amount_lines, stats.zero_transactions) == (12, 10, 1)
    assert (stats.description_only_lines, stats.estimated_entered_at, stats.unreadable_entered_at) == (0, 0, 0)
    assert (stats.je_ids_missing_from_report, stats.je_ids_missing_from_query,
            stats.unknown_account_lines, stats.unknown_users) == (0, 0, 0, 0)
    assert "QBO-Payment-74" not in set(frame["entry_id"])  # QuickBooks' own .00 credit link
    assert stats.unmapped_types == {}  # Inventory Qty Adjust is System on purpose
    assert set(frame["created_by"]) == {"qbo-user-1"} and not frame["entered_at_estimated"].any()


def lines_of(frame, entry_id):
    return frame[frame["entry_id"] == entry_id].sort_values("line_no")


def test_journal_entries_come_from_the_entity_with_the_user_from_the_report(pulled):
    frame, _ = pulled
    truck = lines_of(frame, "QBO-JournalEntry-6")
    assert list(truck["line_no"]) == [1, 2]
    assert list(truck["account_name"]) == ["Truck:Original Cost", "Opening Balance Equity"]
    assert list(truck["debit"]) == [13495.0, 0.0] and list(truck["credit"]) == [0.0, 13495.0]
    assert set(truck["description"]) == {"Opening Balance"}
    assert set(truck["source"]) == {"Manual"} and set(truck["created_by"]) == {"qbo-user-1"}
    # posted 19 August, keyed 31 August: CreateTime as given, on the company's clock (-07:00)
    assert truck["posting_date"].iloc[0] == pd.Timestamp("2026-08-19")
    assert truck["entered_at"].iloc[0] == pd.Timestamp("2026-08-31 12:11:06")
    assert not truck["entered_at_estimated"].any()
    notes = lines_of(frame, "QBO-JournalEntry-8")
    assert list(notes["account_type"]) == ["Liability", "Equity"]
    assert list(notes["credit"]) == [25000.0, 0.0] and list(notes["debit"]) == [0.0, 25000.0]


def test_each_journal_entry_takes_its_own_user_from_the_report(tmp_path):
    # the sandbox has one user, so a second is written onto journal entry 8's report rows
    def edit(body):
        for col_data, _ in qbo._report_rows(body):
            if col_data[1] == {"value": "Journal Entry", "id": "8"}:
                col_data[4]["value"] = "qbo-user-2"  # the create_by column, by its ColKey

    assert qbo.report_columns(fixture_body(GL_FILE)).index("create_by") == 4
    frame, stats = qbo.pull(fixture_client(edited_recording(tmp_path, GL_FILE, edit)), START, END)
    assert set(lines_of(frame, "QBO-JournalEntry-8")["created_by"]) == {"qbo-user-2"}
    assert set(lines_of(frame, "QBO-JournalEntry-6")["created_by"]) == {"qbo-user-1"}
    assert stats.unknown_users == 0


def test_journal_entry_shapes_the_recorded_quarter_does_not_contain(tmp_path):
    # The sandbox's three journal entries are two-line opening balances keyed on weekdays;
    # a description-only line, the adjusting flag and a Sunday late-evening entry are made
    # here on a copy of the recording.
    def edit(body):
        entry = next(e for e in body["QueryResponse"]["JournalEntry"] if e["Id"] == "8")
        entry.update(TxnDate="2026-09-27", Adjustment=True,
                     MetaData={"CreateTime": "2026-09-27T22:47:10-07:00"})
        entry["Line"][0]["Description"] = "Note to the bank, per the loan schedule"
        del entry["Line"][1]["Description"]  # this line falls back to the entry's PrivateNote
        entry["Line"].insert(1, {"Id": "9", "DetailType": "DescriptionOnly", "Description": "loan schedule"})

    frame, stats = qbo.pull(fixture_client(edited_recording(tmp_path, JE_FILE, edit)), START, END)
    entry = lines_of(frame, "QBO-JournalEntry-8")
    assert list(entry["line_no"]) == [1, 2]  # the description-only line is not a line
    assert list(entry["description"]) == ["Note to the bank, per the loan schedule", "Opening Balance"]
    assert (stats.description_only_lines, stats.adjusting_entries) == (1, 1)
    assert "Set aside: 1 description-only line(s)" in "\n".join(stats.describe())
    assert entry["entered_at"].iloc[0] == pd.Timestamp("2026-09-27 22:47:10")
    assert entry["is_weekend"].all()  # Sunday 27 September


def test_a_named_timezone_converts_api_timestamps_first():
    frame, stats = qbo.pull(fixture_client(zone="America/New_York"), START, END)
    # -07:00 in the recording, so three hours later in New York; the report's create_date
    # carries its offset as well, so the report-built entries move with the entity's
    assert lines_of(frame, "QBO-JournalEntry-6")["entered_at"].iloc[0] == pd.Timestamp("2026-08-31 15:11:06")
    assert lines_of(frame, "QBO-Check-57")["entered_at"].iloc[0] == pd.Timestamp("2026-09-02 18:14:27")
    assert stats.utc_times_without_zone == 0


def test_other_transactions_are_rebuilt_from_the_report_whole_and_balanced(pulled):
    frame, _ = pulled
    invoice = lines_of(frame, "QBO-Invoice-12")
    assert sorted(zip(invoice["account_name"], invoice["debit"], invoice["credit"])) == [
        ("Accounts Receivable (A/R)", 2369.52, 0.0), ("Board of Equalization Payable", 0.0, 175.52),
        ("Landscaping Services:Job Materials:Plants and Soil", 0.0, 1750.0),
        ("Sales of Product Income", 0.0, 20.0), ("Sales of Product Income", 0.0, 24.0),
        ("Services", 0.0, 400.0)]
    assert set(invoice["source"]) == {"AR"} and not invoice["entered_at_estimated"].any()
    # a row's memo describes it, else the customer's name (the A/R and tax rows have no memo)
    assert sorted(zip(invoice["account_name"], invoice["credit"], invoice["description"])) == [
        ("Accounts Receivable (A/R)", 0.0, "Cool Cars"), ("Board of Equalization Payable", 175.52, "Cool Cars"),
        ("Landscaping Services:Job Materials:Plants and Soil", 1750.0, "Sod"),
        ("Sales of Product Income", 20.0, "Sprinkler Heads"), ("Sales of Product Income", 24.0, "Sprinkler Pipes"),
        ("Services", 400.0, "Installation Hours")]
    # its sales-tax row is stamped 2026-09-04 12:59:17; the invoice was keyed at the earliest
    assert set(invoice["entered_at"]) == {pd.Timestamp("2026-09-01 15:04:04")}
    fuel = lines_of(frame, "QBO-Check-57")
    assert sorted(zip(fuel["account_name"], fuel["debit"], fuel["credit"])) == [
        ("Automobile:Fuel", 54.55, 0.0), ("Checking", 0.0, 54.55)]
    assert set(fuel["description"]) == {"Chin's Gas and Oil"} and set(fuel["source"]) == {"AP"}
    # posted 16 July, keyed 2 September: the report's -0700 timestamp, read on every Python
    assert fuel["posting_date"].iloc[0] == pd.Timestamp("2026-07-16")
    assert fuel["entered_at"].iloc[0] == pd.Timestamp("2026-09-02 15:14:27")
    sources = {entry_id: set(lines_of(frame, entry_id)["source"]) for entry_id in (
        "QBO-CashExpense-131", "QBO-SalesTaxPayment-123", "QBO-BillPaymentCheck-104",
        "QBO-CreditMemo-73", "QBO-InventoryQtyAdjust-110")}
    assert sources == {"QBO-CashExpense-131": {"AP"}, "QBO-SalesTaxPayment-123": {"AP"},
                       "QBO-BillPaymentCheck-104": {"AP"}, "QBO-CreditMemo-73": {"AR"},
                       "QBO-InventoryQtyAdjust-110": {"System"}}
    # a parent account's own postings sit in a sub-section with no header of their own
    assert len(frame[frame["account_code"] == "45"]) == 15
    assert set(frame.loc[frame["account_code"] == "45", "account_name"]) == {"Landscaping Services"}


def test_a_type_this_tool_does_not_map_is_named_not_filed_as_system_in_silence(tmp_path):
    # the sandbox quarter has no such type, so Credit Card Credit 139 is relabelled
    def edit(body):
        for col_data, _ in qbo._report_rows(body):
            if col_data[1] == {"value": "Credit Card Credit", "id": "139"}:
                col_data[1]["value"] = "Sales Tax Adjustment"

    frame, stats = qbo.pull(fixture_client(edited_recording(tmp_path, GL_FILE, edit)), START, END)
    assert set(lines_of(frame, "QBO-SalesTaxAdjustment-139")["source"]) == {"System"}
    assert stats.unmapped_types == {"SalesTaxAdjustment": 1}
    assert any("1 entrie(s) of a type this tool does not map" in line and "SalesTaxAdjustment" in line
               for line in stats.describe())


def test_a_date_only_create_date_in_the_report_is_estimated_at_noon_on_that_date(tmp_path):
    # The sandbox writes full timestamps; a report that gives the date alone keeps the date
    def edit(body):
        text = json.dumps(body).replace('"2026-09-02T15:14:27-0700"', '"2026-09-02"')
        body.clear()
        body.update(json.loads(text))

    frame, stats = qbo.pull(fixture_client(edited_recording(tmp_path, GL_FILE, edit)), START, END)
    fuel = lines_of(frame, "QBO-Check-57")
    assert fuel["entered_at_estimated"].all()
    assert fuel["entered_at"].iloc[0] == pd.Timestamp("2026-09-02 12:00:00")
    assert (stats.estimated_entered_at, stats.unreadable_entered_at) == (1, 0)


def test_a_date_only_create_date_keeps_its_date_and_estimates_the_time():
    posting = datetime(2025, 10, 6)
    assert qbo.parse_report_datetime("2025-10-09", posting) == (datetime(2025, 10, 9, 12), True)
    assert qbo.parse_report_datetime("", posting) == (datetime(2025, 10, 6, 12), True)
    assert qbo.parse_report_datetime("garbled", posting) == (datetime(2025, 10, 6, 12), True)
    assert qbo.parse_report_datetime("10/09/2025 03:04:05 PM", posting) == (datetime(2025, 10, 9, 15, 4, 5), False)
    assert qbo.parse_report_datetime("2025-10-09T01:02:03-07:00", posting, "UTC") == (
        datetime(2025, 10, 9, 8, 2, 3), False)


def test_a_create_date_whose_offset_has_no_colon_is_read_on_every_python():
    # The sandbox's GL report writes create_date as 2026-09-02T15:14:27-0700. Python 3.9's
    # fromisoformat wants -07:00, and the failed read put the posting date in its place.
    posting = datetime(2026, 7, 16)
    assert qbo.parse_report_datetime("2026-09-02T15:14:27-0700", posting) == (
        datetime(2026, 9, 2, 15, 14, 27), False)
    assert qbo.parse_report_datetime("2026-09-02T15:14:27-0700", posting, "UTC") == (
        datetime(2026, 9, 2, 22, 14, 27), False)
    assert qbo.parse_qbo_datetime("2026-09-02T15:14:27+0530", "UTC") == datetime(2026, 9, 2, 9, 44, 27)
    # a format nobody expected keeps its date, the audit signal, rather than the posting date
    assert qbo.parse_report_datetime("2026-09-02 @ 3:14 PM", posting) == (datetime(2026, 9, 2, 12), True)


@pytest.mark.parametrize("value, expected", [
    # every shape Intuit writes, read the same on Python 3.9 and 3.12
    ("2026-09-02T15:14:27-0700", (datetime(2026, 9, 2, 15, 14, 27), False)),
    ("2026-09-02T15:14:27-07:00", (datetime(2026, 9, 2, 15, 14, 27), False)),
    ("2026-09-30T10:22:13.294-07:00", (datetime(2026, 9, 30, 10, 22, 13, 294000), False)),
    ("2026-09-02T22:14:27Z", (datetime(2026, 9, 2, 22, 14, 27), False)),
    ("2026-09-02 15:14:27-0700", (datetime(2026, 9, 2, 15, 14, 27), False)),
    ("2025-10-09 01:02:03", (datetime(2025, 10, 9, 1, 2, 3), False)),
    # a date and an offset but no time: 3.12's fromisoformat read the offset as 07:00
    ("2026-09-02-0700", (datetime(2026, 9, 2, 12), True)),
    ("2026-09-02+00:00", (datetime(2026, 9, 2, 12), True)),
    # an unreadable value keeps the date it starts with in either of the report's date formats
    ("09/02/2026 at 3 PM", (datetime(2026, 9, 2, 12), True)),
    ("9/2/2026 at 3 PM", (datetime(2026, 9, 2, 12), True)),
    ("2026-9-2 at 3 PM", (datetime(2026, 9, 2, 12), True)),
    ("2026-13-45T99:99", (datetime(2026, 7, 16, 12), True)),
    # shapes fromisoformat read one way on 3.9 and another on 3.12, now one way on both
    ("2026-09-02T15:14:27.1234-07:00", (datetime(2026, 9, 2, 15, 14, 27, 123400), False)),
    ("2026-09-02T15:14:27-07", (datetime(2026, 9, 2, 12), True)),
    ("2026-09-02 15:14", (datetime(2026, 9, 2, 15, 14), False)),
    ("2026-09-02T15:14:27.5", (datetime(2026, 9, 2, 15, 14, 27, 500000), False)),
    # what strptime reads is read: unpadded fields, either case of T, a run of spaces
    ("2026-9-1T10:00:00-07:00", (datetime(2026, 9, 1, 10), False)),
    ("2026-09-01t10:00:00-07:00", (datetime(2026, 9, 1, 10), False)),
    ("2026-09-02T9:05:00-07:00", (datetime(2026, 9, 2, 9, 5), False)),
    ("2026-09-01  10:00:00-07:00", (datetime(2026, 9, 1, 10), False)),
    # every format in _TIMESTAMP_FORMATS is needed by one of these
    ("2026-09-02T15:14:27", (datetime(2026, 9, 2, 15, 14, 27), False)),
    ("2026-09-02T15:14-07:00", (datetime(2026, 9, 2, 15, 14), False)),
    ("2026-09-02T15:14", (datetime(2026, 9, 2, 15, 14), False)),
    ("2026-09-02 15:14:27.5-07:00", (datetime(2026, 9, 2, 15, 14, 27, 500000), False)),
    ("2026-09-02 15:14:27.5", (datetime(2026, 9, 2, 15, 14, 27, 500000), False)),
    ("2026-09-02 15:14-07:00", (datetime(2026, 9, 2, 15, 14), False)),
    # something that only starts like a date is not one
    ("2026-1-12345", (datetime(2026, 7, 16, 12), True)),
    ("2026-01-12345", (datetime(2026, 7, 16, 12), True)),
    ("1/2/20261", (datetime(2026, 7, 16, 12), True)),
])
def test_report_timestamps_are_read_by_explicit_formats(value, expected):
    assert qbo.parse_report_datetime(value, datetime(2026, 7, 16)) == expected


def gl_report(create_date: str, first_row_stamp: str | None = None, amount: str = "54.55",
              first_row_user: str = "qbo-user-1") -> dict:
    """A two-line Check in the report's shape, with the column keys in ColKey metadata; the
    first row in report order can carry a create date and a user of its own."""
    keys = ["tx_date", "txn_type", "create_date", "create_by", "debt_amt", "credit_amt"]

    def section(account_id, name, stamp, user, debit, credit):
        cells = ["2026-07-16", "Check", stamp, user, debit, credit]
        data = [{"value": v, "id": "57"} if k == "txn_type" else {"value": v} for k, v in zip(keys, cells)]
        return {"type": "Section", "Header": {"ColData": [{"value": name, "id": account_id}]},
                "Rows": {"Row": [{"type": "Data", "ColData": data}]}}

    first = create_date if first_row_stamp is None else first_row_stamp
    return {"Columns": {"Column": [{"ColTitle": k, "MetaData": [{"Name": "ColKey", "Value": k}]}
                                   for k in keys]},
            "Rows": {"Row": [section("35", "Checking", first, first_row_user, "", amount),
                             section("56", "Fuel", create_date, "qbo-user-1", amount, "")]}}


def test_a_transaction_is_keyed_when_its_earliest_row_was_created_whatever_the_row_order():
    # In the sandbox, nine invoices' sales-tax rows carry a later create_date than their other
    # rows; which row comes first depends only on the order of the report's account sections.
    later_first = gl_report("2026-09-01T15:04:04-0700", first_row_stamp="2026-09-04T12:59:17-0700",
                            first_row_user="qbo-user-2")
    lines = qbo.general_ledger_to_lines(later_first, {})
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 1, 15, 4, 4)}
    assert {line["created_by"] for line in lines} == {"qbo-user-1"}  # the user who keyed it
    earlier_first = gl_report("2026-09-04T12:59:17-0700", first_row_stamp="2026-09-01T15:04:04-0700",
                              first_row_user="qbo-user-2")
    lines = qbo.general_ledger_to_lines(earlier_first, {})
    assert {(line["entered_at"], line["created_by"]) for line in lines} == {
        (datetime(2026, 9, 1, 15, 4, 4), "qbo-user-2")}
    blank_first = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-01T15:04:04-0700", first_row_stamp=""),
                                        {}, None, blank_first)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 1, 15, 4, 4)}
    assert not any(line["entered_at_estimated"] for line in lines)
    assert blank_first.estimated_entered_at == 0


def test_a_stamp_ranks_by_what_it_tells_and_only_the_stamp_used_counts_as_unreadable():
    # a value that falls back to the posting date says less than another row's own date
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-03", first_row_stamp="garbled"), {}, None, stats)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 3, 12)}
    assert (stats.estimated_entered_at, stats.unreadable_entered_at) == (1, 0)
    # an unreadable row beside a readable one leaves a real entry time and no warning
    read = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-01T15:04:04-0700",
                                                  first_row_stamp="2026-09-02 @ 3:14 PM"), {}, None, read)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 1, 15, 4, 4)}
    assert (read.estimated_entered_at, read.unreadable_entered_at) == (0, 0)
    assert not any("cannot read" in line for line in read.describe())
    # the stamp used is unreadable: counted once per entry, and said so
    lost = qbo.PullStats()
    qbo.general_ledger_to_lines(gl_report("09/02/2026 at 3 PM"), {}, None, lost)
    assert (lost.estimated_entered_at, lost.unreadable_entered_at) == (1, 1)
    assert any("1 entrie(s) whose create date this tool cannot read" in line for line in lost.describe())


@pytest.mark.parametrize("stamp, first_row_stamp, expected, counts", [
    # the keying date is the audit signal: an earlier date wins over a later time of day ...
    ("2026-09-05T10:00:00-0700", "2026-09-01", (datetime(2026, 9, 1, 12), True), (1, 0)),
    ("2026-09-05T10:00:00-0700", "2026-09-01 @ 3:14 PM", (datetime(2026, 9, 1, 12), True), (1, 1)),
    # ... and within a day a time of day wins over a date alone, morning or afternoon
    ("2026-09-01T10:00:00-0700", "2026-09-01", (datetime(2026, 9, 1, 10), False), (0, 0)),
    ("2026-09-01T15:00:00-0700", "2026-09-01", (datetime(2026, 9, 1, 15), False), (0, 0)),
    ("2026-09-01", "2026-09-01T15:00:00-0700", (datetime(2026, 9, 1, 15), False), (0, 0)),
    # an id that only starts like a date does not become the keying date
    ("2026-09-05T10:00:00-0700", "2026-1-12345", (datetime(2026, 9, 5, 10), False), (0, 0)),
    # a date that was read beats the same date taken from an unreadable value
    ("2026-09-02", "2026-09-02 @ 3:14 PM", (datetime(2026, 9, 2, 12), True), (1, 0)),
    ("2026-09-02 @ 3:14 PM", "2026-09-02", (datetime(2026, 9, 2, 12), True), (1, 0)),
    # with no date anywhere, an unreadable value is what the warning is about, not a blank
    ("", "garbled", (datetime(2026, 7, 16, 12), True), (1, 1)),
    ("garbled", "", (datetime(2026, 7, 16, 12), True), (1, 1)),
])
def test_the_entry_time_is_the_earliest_date_a_row_gives(stamp, first_row_stamp, expected, counts):
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report(stamp, first_row_stamp=first_row_stamp), {}, None, stats)
    assert {(line["entered_at"], line["entered_at_estimated"]) for line in lines} == {expected}
    assert (stats.estimated_entered_at, stats.unreadable_entered_at) == counts
    assert any("cannot read" in line for line in stats.describe()) == bool(counts[1])


def test_the_user_comes_from_another_row_when_the_keyed_row_names_none():
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-04T12:59:17-0700", first_row_stamp="2026-09-01T15:04:04-0700",
                                                  first_row_user=""), {})
    assert {(line["entered_at"], line["created_by"]) for line in lines} == {
        (datetime(2026, 9, 1, 15, 4, 4), "qbo-user-1")}


def test_a_report_time_in_utc_is_flagged_like_a_journal_entry_time():
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-02T22:14:27Z"), {}, None, stats)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 2, 22, 14, 27)}
    assert stats.utc_times_without_zone == 1
    assert any("QBO_TIMEZONE" in line for line in stats.describe())
    zoned = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-02T22:14:27Z"), {}, "America/Los_Angeles", zoned)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 2, 15, 14, 27)}
    assert zoned.utc_times_without_zone == 0


def test_a_create_time_that_is_not_a_timestamp_is_estimated_and_counted_not_a_crash():
    # the parser reads only the shapes Intuit writes, the same on every Python ...
    for value in ("2026-09-02-0700", "2026-09-01T10:00:00-07", "2026-09-01"):
        with pytest.raises(ValueError, match="not a QuickBooks timestamp"):
            qbo.parse_qbo_datetime(value)
    # ... so a journal entry's CreateTime in another shape is read like a report stamp
    entry = {"Id": "9", "TxnDate": "2026-08-19", "MetaData": {"CreateTime": "2026-09-01T10:00:00-07"},
             "Line": [{"Amount": 5, "DetailType": "JournalEntryLineDetail",
                       "JournalEntryLineDetail": {"PostingType": side, "AccountRef": {"value": "35"}}}
                      for side in ("Debit", "Credit")]}
    stats = qbo.PullStats()
    lines = qbo.journal_entries_to_lines([entry], {}, None, None, stats)
    assert {(line["entered_at"], line["entered_at_estimated"]) for line in lines} == {
        (datetime(2026, 9, 1, 12), True)}
    assert (stats.estimated_entered_at, stats.unreadable_entered_at) == (1, 1)
    # a date alone, or none at all, is an estimate the tool expects: not "unreadable"
    for created, when in (("2026-09-01", datetime(2026, 9, 1, 12)), ("   ", datetime(2026, 8, 19, 12))):
        entry["MetaData"]["CreateTime"] = created
        quiet = qbo.PullStats()
        lines = qbo.journal_entries_to_lines([entry], {}, None, None, quiet)
        assert {line["entered_at"] for line in lines} == {when}
        assert (quiet.estimated_entered_at, quiet.unreadable_entered_at) == (1, 0), created


def test_a_journal_entry_with_no_posting_line_is_set_aside_like_a_report_transaction():
    zero = {"Id": "9", "TxnDate": "2026-09-01", "Adjustment": True, "MetaData": {"CreateTime": "2026-09-01T17:00:00Z"},
            "Line": [{"Amount": 0, "DetailType": "JournalEntryLineDetail",
                      "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "35"}}},
                     {"DetailType": "DescriptionOnly", "Description": "memo only"}]}
    note = {"Id": "10", "TxnDate": "2026-09-01", "MetaData": {"CreateTime": "garbled"},
            "Line": [{"DetailType": "DescriptionOnly", "Description": "note"}]}
    stats = qbo.PullStats()
    assert qbo.journal_entries_to_lines([zero, note], {}, None, None, stats) == []
    assert (stats.journal_entries, stats.zero_transactions) == (0, 2)
    assert (stats.zero_amount_lines, stats.description_only_lines) == (1, 2)
    # nothing about an entry that never reaches the ledger is counted as if it did
    assert (stats.adjusting_entries, stats.estimated_entered_at, stats.unreadable_entered_at,
            stats.utc_times_without_zone, stats.unknown_users) == (0, 0, 0, 0, 0)
    assert "2 transaction(s) with no posting line" in "\n".join(stats.describe())


def relabelled(report: dict, txn_type: str, txn_id: str) -> dict:
    """``report``'s rows as another transaction (a type and an id on the txn_type cell)."""
    report = json.loads(json.dumps(report))
    for col_data, _ in qbo._report_rows(report):
        col_data[1] = {"value": txn_type, "id": txn_id}
    return report


def joined(*reports: dict) -> dict:
    return {"Columns": reports[0]["Columns"], "Rows": {"Row": [row for r in reports for row in r["Rows"]["Row"]]}}


def test_unmapped_types_are_counted_per_entry_and_named_in_order():
    report = joined(relabelled(gl_report("2026-09-01"), "Statement Charge", "3"),
                    relabelled(gl_report("2026-09-01"), "Sales Tax Adjustment", "1"),
                    relabelled(gl_report("2026-09-01"), "Sales Tax Adjustment", "2"),
                    relabelled(gl_report("2026-09-01", amount=".00"), "Sales Tax Adjustment", "4"),
                    relabelled(gl_report("2026-09-01"), "Payroll Check", "5"))
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(report, {}, None, stats)
    assert {line["entry_id"]: line["source"] for line in lines}["QBO-PayrollCheck-5"] == "Payroll"
    assert stats.unmapped_types == {"StatementCharge": 1, "SalesTaxAdjustment": 2}
    assert stats.zero_transactions == 1
    assert ("Warning: 3 entrie(s) of a type this tool does not map were given the System source: "
            "SalesTaxAdjustment (2), StatementCharge (1)") in stats.describe()


def test_a_row_with_no_type_is_filed_under_a_named_placeholder():
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(relabelled(gl_report("2026-09-01"), "", "57"), {}, None, stats)
    assert {line["entry_id"] for line in lines} == {"QBO-UnknownType-57"}
    assert stats.unmapped_types == {"UnknownType": 1}


def test_a_transaction_with_only_zero_rows_is_set_aside_not_counted_as_an_entry():
    # QuickBooks' own "Created by QB Online to link credits to charges." payment is all .00
    stats = qbo.PullStats()
    assert qbo.general_ledger_to_lines(gl_report("2026-08-18T10:00:00-0700", amount=".00"),
                                       {}, None, stats) == []
    assert (stats.other_transactions, stats.zero_transactions, stats.zero_amount_lines) == (0, 1, 2)
    assert "1 transaction(s) with no posting line" in "\n".join(stats.describe())
    # nothing about it is counted as if it had reached the ledger, whatever its stamps say
    for stamp, first in (("2026-08-18T17:00:00Z", "2026-08-18T17:00:00Z"), ("garbled", "garbled")):
        report = gl_report(stamp, first_row_stamp=first, amount=".00", first_row_user="")
        for col_data, _ in qbo._report_rows(report):
            col_data[3]["value"] = ""  # no user on either row
        quiet = qbo.PullStats()
        assert qbo.general_ledger_to_lines(report, {}, None, quiet) == []
        assert (quiet.estimated_entered_at, quiet.unreadable_entered_at, quiet.utc_times_without_zone,
                quiet.unknown_users, quiet.unmapped_types) == (0, 0, 0, 0, {}), stamp


def test_a_create_date_this_tool_cannot_read_is_counted_and_said_not_called_date_only():
    stats = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-02 @ 3:14 PM"), {}, None, stats)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 2, 12)}
    assert (stats.estimated_entered_at, stats.unreadable_entered_at) == (1, 1)
    assert any("1 entrie(s) whose create date this tool cannot read" in line for line in stats.describe())
    read = qbo.PullStats()
    lines = qbo.general_ledger_to_lines(gl_report("2026-09-02T15:14:27-0700"), {}, None, read)
    assert {line["entered_at"] for line in lines} == {datetime(2026, 9, 2, 15, 14, 27)}
    assert (read.estimated_entered_at, read.unreadable_entered_at) == (0, 0)
    assert not any("cannot read" in line for line in read.describe())
    dated = qbo.PullStats()
    qbo.general_ledger_to_lines(gl_report("2026-09-02"), {}, None, dated)
    assert (dated.estimated_entered_at, dated.unreadable_entered_at) == (1, 0)


def test_unknown_accounts_users_and_missing_entries_are_counted_not_dropped():
    stats = qbo.PullStats()
    entry = {"Id": "9", "TxnDate": "2025-10-01", "Line": [
        {"Amount": -25.0, "DetailType": "JournalEntryLineDetail",
         "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "999", "name": "Gone"}}},
        {"Amount": 0, "DetailType": "JournalEntryLineDetail",
         "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "35"}}},
        {"Amount": 5, "DetailType": "SubTotalLineDetail"}]}
    lines = qbo.journal_entries_to_lines([entry], {}, None, None, stats)
    assert len(lines) == 1
    assert (lines[0]["debit"], lines[0]["credit"]) == (0.0, 25.0)  # a negative debit is a credit
    assert (lines[0]["account_code"], lines[0]["account_name"], lines[0]["account_type"]) == (
        "999", "Gone", "Unknown")
    assert lines[0]["created_by"] == "qbo-unknown" and lines[0]["entered_at_estimated"]
    assert lines[0]["description"] == "Journal entry 9"
    assert (stats.unknown_account_lines, stats.zero_amount_lines, stats.other_detail_lines,
            stats.unknown_users, stats.estimated_entered_at, stats.unreadable_entered_at) == (1, 1, 1, 1, 1, 0)


def test_an_unbalanced_entry_is_counted():
    stats = qbo.PullStats()
    lines = [qbo._line("QBO-Deposit-1", 1, datetime(2025, 10, 1), datetime(2025, 10, 1, 9),
                       qbo.Account("35", "1010", "Checking", "Asset"), "x", 10.0, 0.0, "Bank", "u", False)]
    qbo.to_ledger_frame(lines, stats)
    assert stats.unbalanced_entries == 1
    with pytest.raises(ValueError, match="no transactions in range"):
        qbo.to_ledger_frame([])


def test_a_report_without_debit_and_credit_columns_is_refused():
    with pytest.raises(ValueError, match="debt_amt, credit_amt.*multicurrency"):
        qbo.report_columns({"Columns": {"Column": [{"ColTitle": "Date", "ColType": "Date"},
                                                   {"ColTitle": "Transaction Type"},
                                                   {"ColTitle": "Amount", "ColType": "Money"}]}})


def test_column_keys_fall_back_to_col_type_then_the_title():
    assert qbo.column_key({"ColTitle": "Date", "ColType": "tx_date"}) == "tx_date"
    assert qbo.column_key({"ColTitle": "Debit", "ColType": "Money"}) == "debt_amt"
    assert qbo.column_key({"ColTitle": "Something New", "ColType": "String"}) == "Something New"


def test_the_csv_and_sidecar_round_trip_to_the_realm_identity(pulled, tmp_path):
    frame, _ = pulled
    path = qbo.write_ledger_csv(frame, tmp_path / "qbo-ledger.csv")
    sidecar = qbo.write_identity(path, "4620816365", "sandbox", START, END,
                                 pulled_at=datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc))
    assert sidecar == identity_path(path) == tmp_path / "qbo-ledger.identity.json"
    assert json.loads(sidecar.read_text()) == {
        "ledger_id": "qbo:4620816365", "source": "quickbooks-online", "environment": "sandbox",
        "period": {"start": "2026-07-01", "end": "2026-09-30"}, "pulled_at": "2026-09-30T15:00:00+00:00"}
    back = load_csv(path)
    assert ledger_identity(back, path) == "qbo:4620816365"
    assert list(back.columns[:len(qbo.EXPORT_COLUMNS)]) == list(qbo.EXPORT_COLUMNS)
    assert back["entered_at_estimated"].dtype == bool
    pd.testing.assert_frame_equal(back[list(REQUIRED_COLUMNS)], frame[list(REQUIRED_COLUMNS)],
                                  check_dtype=False)


# --- a rotated refresh token is never lost ------------------------------------------


def test_a_refresh_that_cannot_be_saved_says_to_sign_in_again(outside):
    store = TokenStore.for_realm("sandbox", "4620816365", outside)
    store.save(issued())
    outside.chmod(0o755)  # loosened after qbo-auth: loads, but a save is refused
    client, _, _ = client_for([reply(body=TOKEN_REPLY)], store=store,
                              tokens=issued(now=NOW - timedelta(hours=2)))
    with pytest.raises(QboAuthError, match="could not be saved.*qbo-auth"):
        client.request("GET", "/companyinfo/1")


def test_a_recording_that_cannot_be_written_still_returns_the_response(tmp_path):
    blocked = tmp_path / "rec"
    blocked.write_text("a file, not a directory")
    recorder = Recorder(ScriptedTransport([reply(body=TOKEN_REPLY)]), blocked, "4620816365")
    got = recorder.request("POST", TOKEN, FORM, b"grant_type=refresh_token&refresh_token=R")
    assert got.json()["refresh_token"] == "REFRESH-2"  # the rotated token reaches the caller
    assert recorder.written == [] and len(recorder.failures) == 1
    assert "/oauth2/v1/tokens/bearer" in recorder.failures[0]


# --- the real transport, against loopback servers only ---------------------------


@pytest.fixture
def no_proxy(monkeypatch):
    """urllib honours proxy variables; a developer's proxy must not see loopback traffic."""
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")


def _serve(handler_body):
    """A one-thread loopback server whose GET is ``handler_body(request)``; returns (server, hits)."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            handler_body(self)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def test_a_redirect_is_returned_not_followed_so_the_token_stays_home(no_proxy):
    elsewhere, stolen = _serve(lambda req: (req.send_response(200), req.end_headers()))

    def redirect(req):
        req.send_response(302)
        req.send_header("Location", f"http://127.0.0.1:{elsewhere.server_address[1]}/steal")
        req.end_headers()

    origin, hits = _serve(redirect)
    try:
        transport = qbo.UrllibTransport(timeout=5, https_only=False)
        got = transport.request("GET", f"http://127.0.0.1:{origin.server_address[1]}/v3/x",
                                {"Authorization": "Bearer SECRET-ACCESS"})
    finally:
        origin.shutdown()
        elsewhere.shutdown()
    assert got.status == 302
    assert hits == [{"path": "/v3/x", "authorization": "Bearer SECRET-ACCESS"}]
    assert stolen == []


def test_the_real_transport_calls_https_only():
    with pytest.raises(QboError, match="only https"):
        qbo.UrllibTransport().request("GET", "http://quickbooks.api.intuit.com/v3/x",
                                      {"Authorization": "Bearer T"})


def test_an_idle_connection_does_not_hold_up_the_real_callback():
    """A browser may open a connection to localhost and send nothing on it (a preconnect)."""
    import socket
    import time

    with CallbackServer(0) as server:
        idle = socket.create_connection(("127.0.0.1", server.port))
        try:
            good = urllib.parse.urlencode({"code": "C", "state": "S", "realmId": "1"})
            thread, results = _visit(server, good)
            started = time.monotonic()
            callback = server.wait("S", timeout=10)
            elapsed = time.monotonic() - started
            thread.join(5)
        finally:
            idle.close()
    assert callback.realm_id == "1" and results[0][0] == 200
    assert elapsed < 3


def _ipv6_loopback() -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback on this machine")
def test_the_server_holds_both_loopback_addresses_localhost_can_mean():
    import socket

    with CallbackServer(0) as server:
        with socket.socket(socket.AF_INET6) as other, pytest.raises(OSError):
            other.bind(("::1", server.port))  # nobody else can take [::1] on our port
    with socket.socket(socket.AF_INET6) as squatter:
        squatter.bind(("::1", 0))
        squatter.listen()
        with pytest.raises(QboAuthError, match=r"\[::1\].*in use"):
            CallbackServer(squatter.getsockname()[1])


# --- a recording goes into a public repository --------------------------------------


def test_a_known_name_is_replaced_wherever_it_appears_and_the_realm_in_any_form():
    report = {"Columns": {"Column": [
        {"ColTitle": "Name", "MetaData": [{"Name": "ColKey", "Value": "name"}]},
        {"ColTitle": "Memo", "MetaData": [{"Name": "ColKey", "Value": "memo"}]},
        {"ColTitle": "Created By", "MetaData": [{"Name": "ColKey", "Value": "create_by"}]}]},
        "Rows": {"Row": [{"type": "Data", "ColData": [
            {"value": "Jane Dev"}, {"value": "reimburse Jane Dev; Janet stays"}, {"value": "Jane Dev"}]}]}}
    users: dict[str, str] = {}
    out = sanitize(report, "9341453512345678", users)
    cells = [c["value"] for c in out["Rows"]["Row"][0]["ColData"]]
    assert cells == ["qbo-user-1", "reimburse qbo-user-1; Janet stays", "qbo-user-1"]
    other = sanitize({"realm": 9341453512345678, "9341453512345678": "x", "Owner": "Jane Dev"},
                     "9341453512345678", users)
    assert other == {"realm": "REALM", "REALM": "x", "Owner": "qbo-user-1"}


def test_finish_scrubs_names_learned_later_and_deletes_a_file_that_still_leaks(tmp_path):
    early = {"QueryResponse": {"JournalEntry": [{"Id": "1", "PrivateNote": "Paid by Jane Dev"}]}}
    later = {"QueryResponse": {"Purchase": [{"Id": "2", "MetaData": {"LastModifiedByRef": {"value": "Jane Dev"}}}]}}
    recorder = Recorder(ScriptedTransport([reply(body=early), reply(body=later)]), tmp_path, "4620816365")
    recorder.request("POST", f"{BASE}/query", TEXT, b"select * from JournalEntry")
    recorder.request("POST", f"{BASE}/query", TEXT, b"select * from Purchase")
    assert "Jane Dev" in recorder.written[0].read_text()  # the name was not known yet
    assert recorder.finish() == []
    assert "Jane Dev" not in recorder.written[0].read_text()
    assert "Paid by qbo-user-1" in recorder.written[0].read_text()



def test_finish_deletes_a_fixture_the_scrubber_missed(tmp_path, monkeypatch):
    """The last line of defence: if scrubbing ever misses the realm or a name, the file goes."""
    recorder = Recorder(ScriptedTransport([reply(body={"note": "company 4620816365"})]),
                        tmp_path, "4620816365")
    monkeypatch.setattr(qbo, "sanitize", lambda payload, realm, users: payload)
    monkeypatch.setattr(qbo, "scrub_known", lambda payload, realm, users: payload)
    recorder.request("GET", f"{BASE}/companyinfo/4620816365", {})
    written = recorder.written[0]
    failures = recorder.finish()
    assert failures == [f"{written.name}: still held the realm id after sanitizing, so it was deleted"]
    assert not written.exists() and recorder.written == []


def test_leftovers_names_what_slipped_through():
    assert qbo.leftovers('{"a": "4620816365 and Jane Dev"}', "4620816365", {"Jane Dev": "qbo-user-1"}) == [
        "the realm id", "a user name (qbo-user-1)"]
    assert qbo.leftovers('{"a": "Janet"}', "4620816365", {"Jane": "qbo-user-1"}) == []
    already = {"qbo-user-1": "qbo-user-1", "qbo-user-2": "qbo-user-2"}  # re-recording sanitized data
    assert qbo.leftovers('{"a": "qbo-user-1 qbo-user-2"}', "", already) == []


def test_a_journal_entry_the_report_lists_but_the_query_missed_is_counted(tmp_path):
    def drop_six(body):
        entries = body["QueryResponse"]["JournalEntry"]
        entries[:] = [e for e in entries if e["Id"] != "6"]

    frame, stats = qbo.pull(fixture_client(edited_recording(tmp_path, JE_FILE, drop_six)), START, END)
    assert "QBO-JournalEntry-6" not in set(frame["entry_id"])
    assert (stats.je_ids_missing_from_query, stats.je_ids_missing_from_report) == (1, 0)
    assert "1 journal entrie(s) in the GL report the query did not return" in "\n".join(stats.describe())


def test_utc_journal_entry_times_without_a_zone_are_flagged_not_mixed_in_silently():
    entry = {"Id": "5", "TxnDate": "2025-10-01", "MetaData": {"CreateTime": "2025-10-01T23:30:00Z"},
             "Line": [{"Amount": 1, "DetailType": "JournalEntryLineDetail",
                       "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "1"}}}]}
    stats = qbo.PullStats()
    lines = qbo.journal_entries_to_lines([entry], {}, None, None, stats)
    assert lines[0]["entered_at"] == datetime(2025, 10, 1, 23, 30)
    assert stats.utc_times_without_zone == 1
    assert any("QBO_TIMEZONE" in line for line in stats.describe())
    zoned = qbo.PullStats()
    lines = qbo.journal_entries_to_lines([entry], {}, None, "America/Los_Angeles", zoned)
    assert lines[0]["entered_at"] == datetime(2025, 10, 1, 16, 30) and zoned.utc_times_without_zone == 0
    assert qbo.parse_qbo_datetime("2025-10-01T23:30:00Z") == datetime(2025, 10, 1, 23, 30)


def test_names_are_found_in_any_case_and_spacing_and_colliding_keys_are_refused():
    users = {"Jane Dev": "qbo-user-1"}
    out = qbo.scrub_known({"a": "JANE DEV paid", "b": "jane\u00a0dev", "c": "Jane  Dev", "d": "Janet"},
                          "", users)
    assert out == {"a": "qbo-user-1 paid", "b": "qbo-user-1", "c": "qbo-user-1", "d": "Janet"}
    assert qbo.leftovers("paid by JANE\u00a0 dev", "", users) == ["a user name (qbo-user-1)"]
    with pytest.raises(ValueError, match="two keys scrub to 'REALM'"):
        qbo.scrub_known({"4620816365": "a", "REALM": "b"}, "4620816365", {})
