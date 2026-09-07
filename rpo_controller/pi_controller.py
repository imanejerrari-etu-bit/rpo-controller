"""
PI Write-Behind Controller — Algorithm 1 from the paper.

Implements discrete-time PI (Eq. 2–3) with:
  - Anti-windup integral clamping
  - Deadband suppression
  - Actuator output clamping
"""
from __future__ import annotations
from dataclasses import dataclass, field
from rpo_controller.config import EngineConfig, TS, I_MAX, DWB_MIN, DWB_MAX


@dataclass
class PIState:
    """Mutable state carried across ticks."""
    sigma: float = 0.0          # Σ e[j]·Ts  (integral accumulator)
    last_dwb: float = DWB_MIN   # last actuator output (s)


class PIController:
    """
    Discrete-time PI controller, T_s = 0.5 s.

    Control law (Eq. 2):
        u[k] = Kp · e[k] + Ki · σ[k]

    where σ[k] = clamp(σ[k-1] + e[k]·Ts, -I_max, +I_max)  [anti-windup]

    Output (Eq. 3):
        dwb[k] = clamp(u[k], dwb_min, dwb_max)

    Deadband:
        integral updated only when |e[k]| > ε_d = 0.05 · RPO*
    """

    def __init__(self, cfg: EngineConfig,
                 ts: float = TS,
                 i_max: float = I_MAX,
                 dwb_min: float = DWB_MIN,
                 dwb_max: float = DWB_MAX):
        self.cfg     = cfg
        self.ts      = ts
        self.i_max   = i_max
        self.dwb_min = dwb_min
        self.dwb_max = dwb_max
        self.state   = PIState()

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self, rpo_hat: float) -> float:
        """
        Advance the controller by one sample.

        Args:
            rpo_hat: current RPO proxy measurement (s)

        Returns:
            dwb: write-behind interval to apply to the engine (s)
        """
        e = self.cfg.rpo_star - rpo_hat          # tracking error

        # Anti-windup: only integrate if outside deadband
        if abs(e) > self.cfg.deadband:
            raw_sigma = self.state.sigma + e * self.ts
            self.state.sigma = _clamp(raw_sigma, -self.i_max, self.i_max)

        u = self.cfg.kp * e + self.cfg.ki * self.state.sigma
        dwb = _clamp(u, self.dwb_min, self.dwb_max)
        self.state.last_dwb = dwb
        return dwb

    def reset(self):
        """Reset integrator — call between independent runs."""
        self.state = PIState()

    @property
    def integral(self) -> float:
        return self.state.sigma


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
