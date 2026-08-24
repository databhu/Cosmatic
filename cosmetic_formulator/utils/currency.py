"""
Currency support for cost calculations.

The worldwide ingredient database (data/ingredients.csv) is authored in USD.
In-house material costs are assumed to already be in whatever currency the
user has selected (their own real supplier costs, in their own currency -
no conversion needed for those). When a non-USD currency is selected, the
app converts worldwide USD costs using the exchange rate below - which is a
static, approximate, illustrative default (NOT a live feed) that the user
can override in the sidebar with today's actual rate.
"""

# Approximate USD conversion rates - illustrative defaults only, editable by
# the user in the sidebar. Not a live feed; update as needed for accuracy.
CURRENCY_OPTIONS = {
    "USD": {"symbol": "$", "name": "US Dollar", "approx_rate_per_usd": 1.0},
    "EUR": {"symbol": "€", "name": "Euro", "approx_rate_per_usd": 0.92},
    "GBP": {"symbol": "£", "name": "British Pound", "approx_rate_per_usd": 0.79},
    "INR": {"symbol": "₹", "name": "Indian Rupee", "approx_rate_per_usd": 87.0},
    "JPY": {"symbol": "¥", "name": "Japanese Yen", "approx_rate_per_usd": 152.0},
    "AUD": {"symbol": "A$", "name": "Australian Dollar", "approx_rate_per_usd": 1.54},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar", "approx_rate_per_usd": 1.40},
    "CNY": {"symbol": "¥", "name": "Chinese Yuan", "approx_rate_per_usd": 7.25},
    "AED": {"symbol": "د.إ", "name": "UAE Dirham", "approx_rate_per_usd": 3.67},
}

DEFAULT_CURRENCY = "USD"


def currency_label(code: str) -> str:
    info = CURRENCY_OPTIONS[code]
    return f"{info['symbol']} {code} - {info['name']}"


def convert_from_usd(usd_value, rate_per_usd: float):
    """Convert a USD-denominated value into the target currency using the
    given rate (target units per 1 USD). Passes through None/NaN untouched."""
    if usd_value is None:
        return None
    try:
        import math
        if isinstance(usd_value, float) and math.isnan(usd_value):
            return usd_value
    except TypeError:
        pass
    return usd_value * rate_per_usd


def format_money(value, symbol: str, decimals: int = 2) -> str:
    """Format a number with a currency symbol and thousands separators.
    Returns a placeholder for missing values instead of crashing."""
    if value is None:
        return "n/a"
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return "n/a"
    except TypeError:
        pass
    try:
        return f"{symbol}{value:,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"
