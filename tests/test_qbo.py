"""The QuickBooks Online connector, entirely offline.

Requests are answered by a scripted transport or by the recorded fixtures in
tests/fixtures/qbo/; nothing here opens a socket to Intuit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledgerlens.connectors.qbo import (
    DEFAULT_REDIRECT_URI,
    QboConfig,
    QboConfigError,
    QboError,
    RecordedTransport,
    Recorder,
    Response,
    UnexpectedRequest,
    canonical_request,
    sanitize,
)

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
