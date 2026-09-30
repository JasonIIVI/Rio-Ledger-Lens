# QuickBooks Online fixtures

Recorded responses the connector's tests replay through `RecordedTransport`, so no test
touches the network. Each file is `{"request": <canonical request>, "response": {"status",
"headers", "body"}}`. The canonical request has no host, no `/v3/company/<realm>` prefix,
sorted query parameters, a form body reduced to its `grant_type` and field names (no
values), and a query statement with its whitespace collapsed. Each fixture answers the
first matching request once, in file-name order.

## Provenance

**Every file here is hand-shaped for now**, from Intuit's published entity and report
documentation (Account, JournalEntry, the GeneralLedger report, the OAuth token
endpoint), not recorded from a live company. The names and amounts imitate Intuit's
sample sandbox company. Before the connector's pull request merges, `pull/` is replaced
by a sanitized recording of a real sandbox pull:

```bash
ledgerlens pull-qbo --start 2025-10-01 --end 2025-12-31 --record tests/fixtures/qbo/pull
```

`auth/` stays hand-shaped: a real code exchange cannot be replayed, and the token
endpoint's reply is secret by nature.

| Directory | Used by | What it exercises |
|---|---|---|
| `pull/` | `pull-qbo --fixtures`, `test_qbo.py` | Accounts (an `AcctNum`, an inactive account, a sub-account), two journal entries (a description-only line, a `-08:00` and a `Z` CreateTime, a Sunday period-end manual credit to revenue), and a GL report (nested sections, a beginning-balance row, an invoice with a tax line, a date-only and a timed `create_date`, journal-entry rows that are skipped) |
| `auth/` | the `qbo-auth` CLI test | the authorization-code grant's reply |

## Sanitization (what `--record` does to a live response)

The realm id becomes `REALM`; `access_token` and `refresh_token` become `TEST-ACCESS` and
`TEST-REFRESH` and `id_token` is removed; user display names (keys ending in `By` or
`ByRef`, `UserName`, and the GL report's `create_by` and `last_mod_by` columns) become
`qbo-user-N`; e-mail addresses become `redacted@example.com`. Request headers are never
stored, and response headers are limited to `content-type`, `retry-after` and
`www-authenticate`. Intuit's sample-company data (customer and vendor names, amounts) is
kept: it is Intuit's own demo data.

## To confirm when the recording replaces `pull/`

These are shaped from documentation and third-party reports; the recording settles them,
and this section should then say what was found:

1. The casing of a 401's fault (`Fault/Error` or lower-case `fault/error`, or XML).
2. That `select * from Account where Active IN (true, false)` returns inactive accounts.
3. The format of the GL report's `create_date` (date only, or date and time; which format).
4. That the transaction id is the `id` on the `txn_type` cell.
5. Whether voided transactions appear in the GL report, and how.
6. Whether a 429 carries `Retry-After` (Intuit documents none and says to wait 60 s).
7. The offset on `MetaData.CreateTime` for the sandbox company (`-07:00` / `-08:00`).
8. Whether report column keys arrive as `MetaData` `ColKey`, as `ColType`, or only as titles.
