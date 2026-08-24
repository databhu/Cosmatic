"""Rule-based pairwise ingredient compatibility checking."""


def check_compatibility(formula_df, incompat_data):
    """
    Returns a list of dicts: {a, b, severity, reason} for every pair in the
    formula that matches a known rule in incompat_data['pair_rules'].
    Severity is one of: none, low, medium, high.
    """
    names = list(formula_df["inci_name"])
    flags = []

    rules = {}
    for rule in incompat_data.get("pair_rules", []):
        key = frozenset([rule["a"], rule["b"]])
        rules[key] = rule

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            key = frozenset([names[i], names[j]])
            if key in rules:
                rule = rules[key]
                flags.append({
                    "a": rule["a"],
                    "b": rule["b"],
                    "severity": rule["severity"],
                    "reason": rule["reason"],
                })

    return flags


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}


def sort_flags(flags):
    return sorted(flags, key=lambda f: SEVERITY_ORDER.get(f["severity"], 4))
