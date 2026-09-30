# QuickBooks Online fixtures

Recorded responses the connector's tests replay through `RecordedTransport`, so no test
touches the network. Each file is `{"request": <canonical request>, "response": {"status",
"headers", "body"}}`. The canonical request has no host, no `/v3/company/<realm>` prefix,
sorted query parameters, a form body reduced to its `grant_type` and field names (no
values), and a query statement with its whitespace collapsed. Each fixture answers the
first matching request once, in file-name order.

## Provenance

**`pull/` is recorded**: a sanitized `pull-qbo --record` of a real QuickBooks Online
**sandbox** company (Intuit's sample company, fictional data), made on 2026-09-30 for the
quarter 2026-07-01 to 2026-09-30, which holds most of the sample company's data: an
unrecorded survey pull of April to September 2026 found 10 entries in Q2 and 116 in Q3,
and Q4 2025 had none. Every file was read value by value before it was committed, and an
independent audit found nothing identifying.

```bash
git rm tests/fixtures/qbo/pull/*.json   # a recording replaces the set whole
ledgerlens pull-qbo --start 2026-07-01 --end 2026-09-30 --record tests/fixtures/qbo/pull
```

`auth/` stays hand-shaped: a real code exchange cannot be replayed, and the token
endpoint's reply is secret by nature.

| Directory | Used by | What it exercises |
|---|---|---|
| `pull/` | `pull-qbo --fixtures`, `test_qbo.py`, `test_cli.py` | 90 accounts (one inactive, 30 sub-accounts, no account numbering); three journal entries (two-line opening balances); a GL report of 57 sections (nested two deep, parents' own postings in a sub-section with no header, 12 beginning-balance rows) and 307 transaction rows of 17 types (one transaction, Payment 74, all zero). Replayed: 116 entries / 297 lines, every one balanced. |
| `auth/` | the `qbo-auth` CLI test | the authorization-code grant's reply |

What the quarter does not contain is reached in `test_qbo.py` without hand-shaped files:
by editing a copy of the recording (`edited_recording`: account numbers, a description-only
journal line, a line with its own description, the adjusting flag, a Sunday late-evening
entry, a date-only `create_date`, a second user, a journal entry the query misses), or, for
row-level cases, by a minimal report built in the test (`gl_report`: a later, blank or
unreadable stamp on the first row, a user on one row only, an all-zero transaction, a UTC
stamp).

The recording found three defects the hand-shaped files could not. The report's
`create_date` offset is written `-0700`, which Python 3.9 could not read: on 3.9 every
report-built entry lost its time of day, and 65 of the 113 their keying date (3.12 read it).
Cash Expense and Sales Tax Payment fell back to the `System` source. And an invoice's rows
can carry different create dates (nine invoices' tax rows are stamped later, five of them
on a later day), so the entry time comes from the stamp that says most, the earliest among
equals, not from whichever row the report lists first. Timestamps are now read by explicit
formats, so both Pythons read every value the same way.

## Sanitization (what `--record` does to a live response)

The realm id becomes `REALM`; `access_token` and `refresh_token` become `TEST-ACCESS` and
`TEST-REFRESH` and `id_token` is removed; user display names (keys ending in `By` or
`ByRef`, `UserName`, and the GL report's `create_by` and `last_mod_by` columns) become
`qbo-user-N`; e-mail addresses become `redacted@example.com`. Request headers are never
stored, and response headers are limited to `content-type`, `retry-after` and
`www-authenticate`. Intuit's sample-company data (customer and vendor names, amounts) is
kept: it is Intuit's own demo data.

## What the recording settled

The questions below were shaped from documentation before any live call; this is what the
2026-09-30 recording (and, for item 1, one request) showed.

1. **A 401's fault is lower-case JSON.** Not in a successful pull, so one request with a
   deliberately malformed bearer token was sent to the sandbox: `application/json`,
   abridged `{"fault": {"error": [{"message": "message=AuthenticationFailed; errorCode=003200;
   statusCode=401", "detail": "Malformed bearer token: too short or too long", "code":
   "3200"}], "type": "AUTHENTICATION"}}`, with `www-authenticate: Bearer realm="Intuit",
   error="invalid_token"` and an `intuit_tid` header. `QboError` reads it; both casings
   stay supported, since the API's other faults are documented as `Fault/Error`.
2. **Inactive accounts come back.** `Active IN (true, false)` returned all 90 accounts,
   one inactive: `Repair & Maintenance (deleted)` (QuickBooks adds the suffix).
3. **`create_date` is a full timestamp**, `YYYY-MM-DDTHH:MM:SS-0700`: no fractional
   seconds, and an offset **without a colon** (the report header's own `Time` has one).
   ColType `TimeStamp`. None of the 307 rows is date-only; beginning-balance rows are blank.
4. **The transaction id is on the `txn_type` cell** (`{"value": "Check", "id": "57"}`); no
   `tx_date` cell carries one. The report's journal-entry ids match the entity's.
5. **Not observed.** Nothing in the quarter is voided (no "void" anywhere). The only zero
   rows are four Inventory Qty Adjusts' blank quantity rows and Payment 74, QuickBooks' own
   `.00` "Created by QB Online to link credits to charges."
6. **Not observed.** Every response was a 200; no throttling was provoked. The client
   still waits Intuit's documented 60 s unless `Retry-After` says otherwise.
7. **`MetaData.CreateTime` is `-07:00`** (Pacific daylight time) on all 3 journal entries
   and 90 accounts. The one `-08:00` is a `LastUpdatedTime` dated in the future (the
   sample data's, on the Mastercard account).
8. **Every column carries a `ColKey`** in `MetaData`, as well as a title and a ColType
   (`Date`, `String`, `TimeStamp`, `Money`). The columns came back in a different order
   from the one requested, so they are mapped by key.
9. **Accrual is echoed**: the request sends `accounting_method=Accrual` and the header
   says `"ReportBasis": "Accrual"`. No cash-basis pull was recorded to compare against.
10. **Both clocks agree.** Each journal entry's `CreateTime` and its report `create_date`
    are the same wall-clock time with the same offset (`2026-08-31T12:11:06-07:00` and
    `2026-08-31T12:11:06-0700` for journal entry 6): the company's clock, not UTC. With
    `QBO_TIMEZONE` set, both are converted.
