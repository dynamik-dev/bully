"""PhaseTimer + percentile helper for bench latency aggregation."""

from __future__ import annotations

import time


class PhaseTimer:
    """Callable that records elapsed time for each named phase.

    Usage:
        pt = PhaseTimer()
        with pt("parse_config"):
            ...
        pt.results_ns()  # {"parse_config": [12345, ...], ...}
    """

    def __init__(self) -> None:
        self._samples: dict[str, list[int]] = {}
        self._current: str | None = None
        self._start_ns: int = 0

    def __call__(self, name: str) -> PhaseTimer:
        self._current = name
        return self

    def __enter__(self) -> PhaseTimer:
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, *a) -> bool:
        elapsed = time.perf_counter_ns() - self._start_ns
        assert self._current is not None
        self._samples.setdefault(self._current, []).append(elapsed)
        self._current = None
        return False

    def results_ns(self) -> dict[str, list[int]]:
        return dict(self._samples)


def percentile(values: list[float], pct: float) -> float:
    """Return the `pct`th percentile (0..100) by linear interpolation."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac
