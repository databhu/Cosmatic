"""
Small shared helper for defensively converting values that may be missing,
NaN, blank, or otherwise unparseable - used throughout the app's calculation
engines so a single bad/missing cell (e.g. an empty percent in a manually
edited formula row, or a missing sustainability score on an in-house
material) never crashes a whole tab. Callers decide what "missing" should
mean for their context (skip the row, treat as a default, propagate None) -
this just guarantees the conversion itself never raises.
"""

import pandas as pd


def safe_float(value, default=None):
    """Safely convert a value to float, returning `default` (None unless
    given) for None, NaN, pandas NA, empty/whitespace string, or anything
    else that can't be parsed - never raises."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.strip() == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
