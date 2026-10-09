"""
Battery runtime forecast during an outage and low-battery thresholds
Pure logic: (unix time, SOC %) samples in, estimate out
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Forecast from the recent discharge rate only
WINDOW_S = 30 * 60
# Too short a span gives a noisy slope (SOC is reported in whole percents)
MIN_SPAN_S = 10 * 60
MIN_POINTS = 3


@dataclass(frozen=True)
class Forecast:
    soc: float            # latest SOC, %
    rate_per_hour: float  # SOC drop, % per hour (positive)
    seconds_left: float   # until the empty level


def forecast(samples: Sequence[Tuple[float, Optional[float]]], now: float,
             empty_soc: float) -> Optional[Forecast]:
    """Least-squares SOC slope over the last WINDOW_S; None if SOC is not falling
    or there is too little data"""
    pts = [(t, float(s)) for t, s in samples
           if s is not None and now - WINDOW_S <= t <= now]
    if len(pts) < MIN_POINTS or pts[-1][0] - pts[0][0] < MIN_SPAN_S:
        return None

    n = len(pts)
    mean_t = sum(t for t, _ in pts) / n
    mean_s = sum(s for _, s in pts) / n
    var = sum((t - mean_t) ** 2 for t, _ in pts)
    if var == 0:
        return None
    slope = sum((t - mean_t) * (s - mean_s) for t, s in pts) / var  # %/s
    if slope >= 0:
        return None

    soc = pts[-1][1]
    seconds_left = max(soc - empty_soc, 0.0) / -slope
    return Forecast(soc=soc, rate_per_hour=-slope * 3600,
                    seconds_left=seconds_left)


class LowBatteryWatch:
    """SOC samples of the current outage + thresholds that fire once each"""

    def __init__(self, thresholds: Sequence[int] = (30, 15)):
        self.thresholds = sorted(thresholds, reverse=True)
        self.fired = set()
        self.samples: List[Tuple[float, float]] = []

    def reset(self):
        """Grid is back: re-arm thresholds and forget the outage"""
        self.fired.clear()
        self.samples.clear()

    def observe(self, now: float, soc: float) -> Optional[int]:
        """Add a sample; return the lowest newly crossed threshold, if any"""
        self.samples.append((now, soc))
        cutoff = now - 2 * WINDOW_S
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)

        hit = [t for t in self.thresholds if soc <= t and t not in self.fired]
        if not hit:
            return None
        self.fired.update(hit)
        return min(hit)
