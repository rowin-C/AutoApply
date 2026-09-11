"""Human-like pacing helpers.

Everything here exists to make automated traffic *statistically* look like
a slow, distracted human: random delays inside a window, occasional longer
pauses, and never a uniform cadence.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger("aa.pacing")

_PAUSE_PROBABILITY = 0.08
_PAUSE_EXTRA = (6.0, 25.0)


def one_of(range_pair: list[float] | tuple[float, float]) -> float:
    if not range_pair:
        return 0.0
    if len(range_pair) == 1:
        return float(range_pair[0])
    lo, hi = sorted(range_pair)
    return random.uniform(lo, hi)


def human_delay(range_pair: list[float] | tuple[float, float]) -> None:
    target = one_of(range_pair)
    if random.random() < _PAUSE_PROBABILITY:
        target += random.uniform(*_PAUSE_EXTRA)
    time.sleep(target)
