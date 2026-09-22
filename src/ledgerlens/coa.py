"""A small but realistic chart of accounts for the fictional test company.

Brightline Supply Co. is a wholesale distributor: it buys inventory, sells on
credit, runs a payroll, and carries a note payable. That mix is enough to
exercise every journal-entry test without inventing a conglomerate.
"""

from __future__ import annotations

COMPANY_NAME = "Brightline Supply Co."

# account_code -> (account_name, account_type)
CHART: dict[str, tuple[str, str]] = {
    "1000": ("Cash - Operating", "Asset"),
    "1010": ("Cash - Payroll", "Asset"),
    "1200": ("Accounts Receivable", "Asset"),
    "1300": ("Inventory", "Asset"),
    "1400": ("Prepaid Expenses", "Asset"),
    "1500": ("Equipment", "Asset"),
    "1510": ("Accumulated Depreciation", "Asset"),
    "2000": ("Accounts Payable", "Liability"),
    "2100": ("Accrued Liabilities", "Liability"),
    "2200": ("Payroll Liabilities", "Liability"),
    "2300": ("Sales Tax Payable", "Liability"),
    "2500": ("Note Payable", "Liability"),
    "3000": ("Owner's Equity", "Equity"),
    "3100": ("Retained Earnings", "Equity"),
    "4000": ("Sales Revenue", "Revenue"),
    "4100": ("Service Revenue", "Revenue"),
    "4900": ("Sales Returns & Allowances", "Revenue"),
    "5000": ("Cost of Goods Sold", "Expense"),
    "6000": ("Salaries & Wages", "Expense"),
    "6010": ("Payroll Taxes", "Expense"),
    "6100": ("Rent Expense", "Expense"),
    "6200": ("Utilities", "Expense"),
    "6300": ("Insurance", "Expense"),
    "6400": ("Professional Fees", "Expense"),
    "6500": ("Marketing & Advertising", "Expense"),
    "6600": ("Office Supplies", "Expense"),
    "6700": ("Repairs & Maintenance", "Expense"),
    "6800": ("Depreciation Expense", "Expense"),
    "6900": ("Miscellaneous Expense", "Expense"),
    "7000": ("Interest Expense", "Expense"),
}

#: Accounts that see almost no activity in a normal year. The dormant-account
#: test looks for sudden life in these; the generator uses them for the
#: dormant-account archetype.
RARELY_USED: tuple[str, ...] = ("6900", "4900", "1400")


def account_name(code: str) -> str:
    return CHART[code][0]


def account_type(code: str) -> str:
    return CHART[code][1]


def codes_of_type(account_type_name: str) -> list[str]:
    return [c for c, (_, t) in CHART.items() if t == account_type_name]


#: Normal, boring pairings a bookkeeper posts every week. The rare-pair test
#: learns the real distribution from the data, but the generator needs a
#: baseline of legitimate combinations to draw from.
COMMON_PAIRS: tuple[tuple[str, str], ...] = (
    ("1200", "4000"),   # credit sale
    ("1000", "1200"),   # customer payment received
    ("5000", "1300"),   # COGS recognised
    ("1300", "2000"),   # inventory purchased on account
    ("2000", "1000"),   # vendor paid
    ("6000", "2200"),   # payroll accrued
    ("2200", "1010"),   # payroll paid
    ("6100", "1000"),   # rent paid
    ("6200", "2000"),   # utility bill
    ("6400", "2000"),   # professional fees
    ("6500", "1000"),   # marketing spend
    ("6600", "1000"),   # office supplies
    ("6800", "1510"),   # depreciation
    ("7000", "1000"),   # interest
    ("1000", "4100"),   # cash service revenue
)
