"""Connectors: what brings a ledger in from an accounting system.

``tokens`` keeps OAuth tokens outside any checkout; ``qbo`` signs in to
QuickBooks Online, pulls a period and maps it into the ledger contract. The
rule every connector shares: its output goes through
:func:`ledgerlens.ingest.prepare` exactly as a CSV would, so nothing
downstream knows where a ledger came from, and nothing a connector pulls is
ever committed (rule 1 in CLAUDE.md).
"""
