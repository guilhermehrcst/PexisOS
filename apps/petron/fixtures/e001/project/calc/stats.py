"""Small statistics helpers (synthetic PETRON-E001 fixture with a planted defect)."""


def mean(values):
    if not values:
        raise ValueError("mean() of empty sequence")
    return sum(values) / len(values)


def median(values):
    if not values:
        raise ValueError("median() of empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle]
