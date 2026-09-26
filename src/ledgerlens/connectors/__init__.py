"""Connectors: what brings a ledger in from an accounting system.

Only the token store lives here yet; the QuickBooks Online client that uses
it follows. The rule every connector shares: its output goes through
:func:`ledgerlens.ingest.prepare` exactly as a CSV would, so nothing
downstream knows where a ledger came from, and nothing a connector pulls is
ever committed (rule 1 in CLAUDE.md).
"""
