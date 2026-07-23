"""
Temporal Dithering Actuator for Redis — Section VI of the paper.

Instead of snapping to the nearest discrete appendfsync mode,
temporal dithering alternates between two adjacent modes at a
duty cycle proportional to the desired fractional I_f value.

Example: I_f_target = 0.7 s
  - Lower mode: always  (I_f_lo = 0.001 s)
  - Upper mode: everysec (I_f_hi = 1.0 s)
  - Duty cycle α = (I_f_target - I_f_lo) / (I_f_hi - I_f_lo) = 0.699
  - Over a window W=10 ticks: 7 ticks everysec, 3 ticks always

This is analogous to PWM (Pulse-Width Modulation) control,
a standard technique for actuating discrete-valued plants
(Åström & Wittenmark, §6.4).

The effective fsync interval over one dithering window W is:
  I_f_eff = α·I_f_hi + (1-α)·I_f_lo ≈ I_f_target

With W=10 ticks × 500ms = 5s, the controller can achieve any
I_f_target ∈ [0.001, 30] s with resolution ≈ I_f_step / W.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── Mode definitions ─────────────────────────────────────────────────────────
MODES = ["always", "everysec", "no"]
MODE_IF = {"always": 0.001, "everysec": 1.0, "no": 30.0}

# Dithering window: number of ticks over which duty cycle is computed
DITHER_WINDOW = 10   # 10 × 500ms = 5 s

# Adjacent mode pairs and their I_f range
DITHER_PAIRS = [
    ("always",   "everysec", 0.001, 1.0),    # low range:  [0.001, 1.0] s
    ("everysec", "no",       1.0,   30.0),   # high range: [1.0,  30.0] s
]


@dataclass
class DitherState:
    """State carried across ticks for one Redis instance."""
    tick_count:   int   = 0         # ticks since last window reset
    hi_ticks_due: int   = 0         # hi-mode ticks remaining in window
    lo_ticks_due: int   = 0         # lo-mode ticks remaining in window
    window_alpha: float = 0.0       # duty cycle for this window
    last_mode:    str   = "everysec"


class TemporalDitheringActuator:
    """
    Redis actuator with temporal dithering.

    Replaces the discrete snap-to-nearest actuator.
    On each tick, returns the appendfsync mode to apply.

    Usage:
        dither = TemporalDitheringActuator()
        mode   = dither.tick(dwb, r)      # dwb from PI controller (s)
    """

    def __init__(self, window: int = DITHER_WINDOW):
        self.window = window
        self.state  = DitherState()

    def tick(self, dwb: float, r) -> str:
        """
        Compute and apply the appendfsync mode for this tick.

        Args:
            dwb: PI controller output (desired write-behind interval, s)
            r:   redis.Redis client

        Returns:
            mode: applied appendfsync mode string
        """
        s = self.state

        # At window boundary, recompute duty cycle
        if s.tick_count % self.window == 0:
            lo_mode, hi_mode, i_lo, i_hi = _select_pair(dwb)
            alpha = _duty_cycle(dwb, i_lo, i_hi)

            hi_ticks = round(alpha * self.window)
            lo_ticks = self.window - hi_ticks

            s.hi_ticks_due = hi_ticks
            s.lo_ticks_due = lo_ticks
            s.window_alpha  = alpha

            log.debug("Dither window: dwb=%.3fs lo=%s hi=%s α=%.2f "
                      "(%d/%d ticks)",
                      dwb, lo_mode, hi_mode, alpha,
                      hi_ticks, lo_ticks)
        else:
            lo_mode, hi_mode, _, _ = _select_pair(dwb)

        # Assign mode for this tick
        if s.hi_ticks_due > 0:
            mode = hi_mode
            s.hi_ticks_due -= 1
        else:
            mode = lo_mode
            s.lo_ticks_due -= 1

        s.tick_count += 1

        # Apply to Redis only if mode changed (reduces actuator calls)
        if mode != s.last_mode:
            r.config_set("appendfsync", mode)
            s.last_mode = mode
            log.debug("Redis dither: appendfsync → %s", mode)

        return mode

    def reset(self):
        self.state = DitherState()

    @property
    def effective_if(self) -> float:
        """Expected effective I_f over current window."""
        s = self.state
        lo_mode, hi_mode, i_lo, i_hi = _select_pair_from_alpha(s.window_alpha)
        return s.window_alpha * i_hi + (1 - s.window_alpha) * i_lo


# ── Helpers ───────────────────────────────────────────────────────────────────

def _select_pair(dwb: float):
    """Choose adjacent mode pair that brackets dwb."""
    if dwb <= 1.0:
        return ("always", "everysec", 0.001, 1.0)
    else:
        return ("everysec", "no", 1.0, 30.0)


def _select_pair_from_alpha(alpha: float):
    return ("always", "everysec", 0.001, 1.0)


def _duty_cycle(dwb: float, i_lo: float, i_hi: float) -> float:
    """
    Compute α such that α·i_hi + (1-α)·i_lo = dwb.

    α = (dwb - i_lo) / (i_hi - i_lo), clamped to [0, 1].
    """
    if i_hi <= i_lo:
        return 0.0
    alpha = (dwb - i_lo) / (i_hi - i_lo)
    return max(0.0, min(1.0, alpha))
