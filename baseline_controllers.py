"""
baseline_controllers.py — Comparison baselines for pi_controller.py

Place this file in the SAME folder as pi_controller.py and config.py
(i.e. inside the `rpo_controller` package). Both classes below expose
the exact same public API as `PIController`:

    ctrl.tick(rpo_hat) -> dwb
    ctrl.reset()
    ctrl.integral   (property)

so they are true drop-in replacements — only the import line and the
class name change in your experiment driver.

Baseline 1 — NaiveHeuristicPIController
    Same architecture as PIController (anti-windup, deadband, actuator
    clamp) but Kp/Ki come from a standard quarter-decay heuristic
    instead of the paper's 25-trial ITAE grid search (Table 3).
    cfg.kp / cfg.ki are deliberately IGNORED. Isolates whether ITAE
    tuning specifically matters (Section 5.8, driver ii).

Baseline 2 — ArimaFeedforwardPIController
    Same ITAE-tuned PI core as PIController (uses cfg.kp / cfg.ki
    unchanged) plus an AR(p) feed-forward term computed from an
    optional load signal (e.g. current TPS), in the spirit of
    Joshi et al. (2024) "ARIMA-PID". Isolates whether forecast-based
    feed-forward buys anything on top of the existing design
    (Section 5.8, driver iv).

    tick() takes one EXTRA optional argument:
        dwb = ctrl.tick(rpo_hat, load_signal=current_tps)
    Passing load_signal=None (or omitting it) makes this reduce
    EXACTLY to PIController's behaviour — useful as a sanity check
    that wiring is correct before trusting the comparison.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from typing import Optional

from rpo_controller.config import EngineConfig, TS, I_MAX, DWB_MIN, DWB_MAX


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class PIState:
    """Same mutable state shape as pi_controller.PIState."""
    sigma: float = 0.0
    last_dwb: float = DWB_MIN


# ---------------------------------------------------------------------
# Baseline 1: quarter-decay heuristic gains (ITAE-tuning ablation)
# ---------------------------------------------------------------------

# Plant parameters measured in the paper (Eq. 3: Ks, and the empirical
# time constant tau_s ~= 60s reported in Remark 1 / Sec 3.3). These are
# used ONLY to derive the naive baseline's gains below — they never
# enter the control loop itself.
_KS_MEASURED = 2.12
_TAU_S_MEASURED = 60.0


def quarter_decay_gains(ks: float = _KS_MEASURED,
                         tau_s: float = _TAU_S_MEASURED) -> tuple[float, float]:
    """Standard quarter-decay-ratio heuristic (Hellerstein et al. 2004,
    already cited in the paper): Kp cancels DC gain, Ki set to a
    quarter of the dominant time constant. NOT grid-searched."""
    kp = 1.0 / ks
    ki = kp / (4.0 * tau_s)
    return kp, ki


class NaiveHeuristicPIController:
    """Drop-in replacement for PIController. cfg.kp / cfg.ki are
    ignored on purpose; gains come from quarter_decay_gains()."""

    def __init__(self, cfg: EngineConfig,
                 ts: float = TS,
                 i_max: float = I_MAX,
                 dwb_min: float = DWB_MIN,
                 dwb_max: float = DWB_MAX):
        self.cfg = cfg
        self.ts = ts
        self.i_max = i_max
        self.dwb_min = dwb_min
        self.dwb_max = dwb_max
        self.kp, self.ki = quarter_decay_gains()
        self.state = PIState()

    def tick(self, rpo_hat: float) -> float:
        e = self.cfg.rpo_star - rpo_hat

        if abs(e) > self.cfg.deadband:
            raw_sigma = self.state.sigma + e * self.ts
            self.state.sigma = _clamp(raw_sigma, -self.i_max, self.i_max)

        u = self.kp * e + self.ki * self.state.sigma
        dwb = _clamp(u, self.dwb_min, self.dwb_max)
        self.state.last_dwb = dwb
        return dwb

    def reset(self):
        self.state = PIState()

    @property
    def integral(self) -> float:
        return self.state.sigma


# ---------------------------------------------------------------------
# Baseline 2: ARIMA-style feed-forward on top of the ITAE-tuned PI core
# ---------------------------------------------------------------------

class _ARForecaster:
    """Lightweight AR(p) forecaster fit by least squares on a sliding
    window (no external dependency needed on the cluster). Mirrors the
    low-order ARIMA(p,d,0) models Joshi et al. (2024) use for
    container-load forecasting."""

    def __init__(self, order: int = 2, window: int = 20, horizon: int = 1):
        self.order = order
        self.window = window
        self.horizon = horizon
        self.history: deque[float] = deque(maxlen=window)

    def update(self, x: float) -> None:
        self.history.append(x)

    def _fit_coeffs(self) -> Optional[list[float]]:
        h = list(self.history)
        n = len(h)
        p = self.order
        if n < p + 5:
            return None
        X, y = [], []
        for t in range(p, n):
            X.append([h[t - i - 1] for i in range(p)] + [1.0])
            y.append(h[t])
        m = len(X[0])
        XtX = [[sum(X[k][i] * X[k][j] for k in range(len(X)))
                for j in range(m)] for i in range(m)]
        Xty = [sum(X[k][i] * y[k] for k in range(len(X))) for i in range(m)]
        try:
            return _solve_linear(XtX, Xty)
        except ZeroDivisionError:
            return None

    def forecast(self) -> Optional[float]:
        coeffs = self._fit_coeffs()
        if coeffs is None:
            return None
        h = list(self.history)
        pred = list(h)
        for _ in range(self.horizon):
            p = self.order
            window = pred[-p:][::-1]
            val = sum(c * w for c, w in zip(coeffs[:-1], window)) + coeffs[-1]
            pred.append(val)
        return pred[-1]


def _solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            raise ZeroDivisionError("singular matrix in AR fit")
        M[col], M[pivot] = M[pivot], M[col]
        for r in range(n):
            if r != col:
                factor = M[r][col] / M[col][col]
                for c in range(col, n + 1):
                    M[r][c] -= factor * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


class ArimaFeedforwardPIController:
    """Drop-in for PIController, with an extra OPTIONAL `load_signal`
    argument to tick(). cfg.kp / cfg.ki are used UNCHANGED (same ITAE
    gains as your main controller) — only a feed-forward term is
    added on top."""

    def __init__(self, cfg: EngineConfig,
                 ts: float = TS,
                 i_max: float = I_MAX,
                 dwb_min: float = DWB_MIN,
                 dwb_max: float = DWB_MAX,
                 kff: float = 0.5,
                 ar_order: int = 2,
                 ar_window: int = 20,
                 forecast_horizon_ticks: int = 1):
        self.cfg = cfg
        self.ts = ts
        self.i_max = i_max
        self.dwb_min = dwb_min
        self.dwb_max = dwb_max
        self.kff = kff
        self._ar_order = ar_order
        self._ar_window = ar_window
        self._ar_horizon = forecast_horizon_ticks
        self.state = PIState()
        self._forecaster = _ARForecaster(order=ar_order, window=ar_window,
                                          horizon=forecast_horizon_ticks)
        self._last_load: Optional[float] = None

    def tick(self, rpo_hat: float, load_signal: Optional[float] = None) -> float:
        e = self.cfg.rpo_star - rpo_hat

        if abs(e) > self.cfg.deadband:
            raw_sigma = self.state.sigma + e * self.ts
            self.state.sigma = _clamp(raw_sigma, -self.i_max, self.i_max)

        u_fb = self.cfg.kp * e + self.cfg.ki * self.state.sigma

        u_ff = 0.0
        if load_signal is not None:
            self._forecaster.update(load_signal)
            forecast = self._forecaster.forecast()
            if forecast is not None and self._last_load is not None:
                delta_load = forecast - self._last_load
                u_ff = self.kff * delta_load
            self._last_load = load_signal

        dwb = _clamp(u_fb + u_ff, self.dwb_min, self.dwb_max)
        self.state.last_dwb = dwb
        return dwb

    def reset(self):
        self.state = PIState()
        self._last_load = None
        self._forecaster = _ARForecaster(order=self._ar_order,
                                          window=self._ar_window,
                                          horizon=self._ar_horizon)

    @property
    def integral(self) -> float:
        return self.state.sigma


# ---------------------------------------------------------------------
# Self-test with the paper's own first-order plant model (Eq. 3),
# run against your ACTUAL EngineConfig entries from config.py.
# Run this locally (no cluster needed) BEFORE spending cluster time.
# ---------------------------------------------------------------------

def _toy_plant_step(rpo_hat: float, dwb_cmd: float, ts: float,
                     ks: float = _KS_MEASURED,
                     tau_s: float = _TAU_S_MEASURED) -> float:
    d_rpo = (ts / tau_s) * (-rpo_hat + ks * dwb_cmd)
    return max(0.0, rpo_hat + d_rpo)


def _self_test():
    from rpo_controller.config import ENGINES
    from rpo_controller.pi_controller import PIController

    for name, cfg in ENGINES.items():
        print(f"\n=== {name}  (rpo_star={cfg.rpo_star}s, "
              f"Kp={cfg.kp}, Ki={cfg.ki}) ===")

        main = PIController(cfg)
        naive = NaiveHeuristicPIController(cfg)
        arima = ArimaFeedforwardPIController(cfg)
        print(f"naive gains: Kp={naive.kp:.4f}  Ki={naive.ki:.5f} "
              f"(vs. ITAE Kp={cfg.kp}, Ki={cfg.ki})")

        for label, ctrl, with_load in [
            ("main (ITAE)", main, False),
            ("naive (quarter-decay)", naive, False),
            ("arima_ff (ITAE + forecast)", arima, True),
        ]:
            ctrl.reset()
            rpo_hat = 0.1
            dwb = DWB_MIN
            tps = 200.0
            for k in range(400):  # 200s at Ts=0.5s
                tps += (5.0 if 100 < k < 150 else 0.0)  # synthetic ramp
                if with_load:
                    dwb = ctrl.tick(rpo_hat, load_signal=tps)
                else:
                    dwb = ctrl.tick(rpo_hat)
                rpo_hat = _toy_plant_step(rpo_hat, dwb, ctrl.ts)
            print(f"  {label:<28} after 200s: rpo_hat={rpo_hat:.4f} "
                  f"(target {cfg.rpo_star})")


if __name__ == "__main__":
    _self_test()
