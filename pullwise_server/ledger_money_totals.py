"""Exact aggregate money across SQLite and the Worker's JavaScript boundary."""

MAX_SAFE_INTEGER = 9007199254740991
AGGREGATE_BASE = 1000000000

# A record is at most MAX_SAFE_INTEGER minor units and operator policy caps
# stored records at 1,000,000. Both SUM components therefore remain below
# MAX_SAFE_INTEGER, including at the D1 JavaScript-to-Python boundary. Rebuild
# their potentially much larger total using Python integers, never SQL floats.
AGGREGATE_SQL = (
    f"SUM(amount_minor/{AGGREGATE_BASE}) AS minor_quotient,"
    f"SUM(amount_minor%{AGGREGATE_BASE}) AS minor_remainder"
)


def aggregate_minor(row):
    return row["minor_quotient"] * AGGREGATE_BASE + row["minor_remainder"]


def public_minor(amount):
    """JSON numbers are exact only up to the JavaScript safe integer bound."""
    return amount if amount <= MAX_SAFE_INTEGER else str(amount)
