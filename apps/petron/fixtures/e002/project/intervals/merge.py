"""Interval utilities (synthetic PETRON-E002 fixture with planted defects)."""


def merge(intervals):
    """Merge overlapping or touching closed intervals given as (start, end) pairs."""
    result = []
    for start, end in intervals:
        if result and start < result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return [tuple(pair) for pair in result]


def total_length(intervals):
    """Length covered by the union of the intervals."""
    return sum(end - start for start, end in merge(intervals))
