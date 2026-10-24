"""
MPC Write-Behind Controller — predictive counterpart to PIController.

Same public interface as PIController (tick / reset / properties) so it
can be swapped in `service.py` and `run_experiment.py` behind a single
`--controller pi|mpc` flag, with no other code changes required.

Control law:
    At each tick k, predict the RPO proxy trajectory over a horizon H
    using an interchangeable LoadPredictor (moving-average, ARIMA, or
    oracle), then choose dwb[k] to minimise a horizon cost that
    penalises predicted SLA violations and large actuator moves:

        J(dwb) = sum_{i=1..H} w_i * max(0, rhohat[k+i] - rpo_star)^2
                 + lambda * (dwb - dwb_prev)^2

    Because the plant model (rhohat as a function of dwb) is not a
    closed-form differentiable expression here (it depends on the
    engine's write-behind mechanics), we optimise dwb by a bounded
    grid/line search over [dwb_min, dwb_max] rather than a QP solve —
    cheap enough at H<=8 and a candidate grid of ~20 points to fit
    comfortably inside the 500 ms tick budget (see paper Sec. 5 for the
    compute-cost ablation).
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np

from rpo_controller.config import EngineConfig, TS, DWB_MIN, DWB_MAX
from rpo_controller.predictors import LoadPredictor, PredictionResult


@dataclass
class MPCState:
    """Mutable state carried across ticks."""
    last_dwb: float = DWB_MIN
    last_prediction: PredictionResult | None = None
    integral_error: float = 0.0  # cumulative offset-free correction term


class MPCController:
    """
    Model Predictive write-behind controller, T_s = 0.5 s, horizon H.

    Drop-in replacement for PIController: same tick()/reset() interface,
    same actuator bounds, same config object.

    Includes an offset-free correction term (Muske & Badgwell, 1998;
    Pannocchia & Rawlings, 2003): a pure horizon-optimising MPC has no
    memory of past tracking error and cannot reject a persistent
    measurement/disturbance offset the way an integral controller does
    — it reacts fresh each tick to noisy forecasts, which empirically
    increased tracking error under measurement noise relative to the
    PI baseline (see paper's ablation, Sec. 5.3). The integral term
    below accumulates the observed error and biases the plant
    projection accordingly, giving the MPC the same steady-state
    disturbance rejection as PI while keeping its predictive,
    horizon-aware decision rule.
    """

    def __init__(self, cfg: EngineConfig, predictor: LoadPredictor,
                 horizon: int = 6,
                 ts: float = TS,
                 dwb_min: float = DWB_MIN,
                 dwb_max: float = DWB_MAX,
                 n_candidates: int = 21,
                 move_penalty: float = 0.05,
                 horizon_weight_decay: float = 0.85,
                 ki_offset: float = 0.05,
                 integral_clip: float = 0.3,
                 process_gain: float = 0.37):
        self.cfg = cfg
        self.predictor = predictor
        self.horizon = horizon
        self.ts = ts
        self.dwb_min = dwb_min
        self.dwb_max = dwb_max
        self.n_candidates = n_candidates
        self.move_penalty = move_penalty
        # Weight predictions near the start of the horizon more heavily
        # than the far end, where the forecast is naturally less reliable.
        self.horizon_weights = horizon_weight_decay ** np.arange(horizon)
        # Offset-free correction: gain on the accumulated tracking error,
        # and a clip to bound the correction's influence (avoids integral
        # windup during long transients/actuator saturation).
        self.ki_offset = ki_offset
        self.integral_clip = integral_clip
        # Empirically calibrated plant gain (dwb -> rpo_hat), estimated by
        # linear regression on real MongoDB pilot-run data (drho/ddwb,
        # n=5 runs, ~4500 usable ticks): gain ≈ 0.37, not the naively
        # assumed 1:1 relationship. Using the wrong gain here caused the
        # controller to systematically under-correct in earlier pilot
        # runs (see paper Sec. 5.3 — gain-mismatch ablation). This should
        # be re-estimated per engine before running MySQL/Redis campaigns,
        # since the write-behind mechanics differ across engines.
        self.process_gain = process_gain
        self.state = MPCState()

    # ── Public API (mirrors PIController) ───────────────────────────────────

    def tick(self, rpo_hat: float) -> float:
        """
        Advance the controller by one sample.

        Args:
            rpo_hat: current RPO proxy measurement (s)

        Returns:
            dwb: write-behind interval to apply to the engine (s)
        """
        self.predictor.observe(rpo_hat)
        prediction = self.predictor.predict(self.horizon)
        self.state.last_prediction = prediction

        dwb = self._optimise(prediction.forecast, self.state.last_dwb)

        # Offset-free correction, applied AFTER the predictive grid search
        # (not folded into its cost — folding it in was tested and found
        # to destabilise the discrete optimisation, see paper Sec. 5.3).
        # This mirrors PI's own structure: a predictive/proportional term
        # plus a separate additive integral term.
        current_error = self.cfg.rpo_star - rpo_hat
        self.state.integral_error = float(np.clip(
            self.state.integral_error + current_error,
            -self.integral_clip, self.integral_clip,
        ))
        dwb = float(np.clip(dwb + self.ki_offset * self.state.integral_error,
                             self.dwb_min, self.dwb_max))

        self.state.last_dwb = dwb
        return dwb

    def reset(self):
        """Reset predictor + actuator memory — call between independent runs."""
        self.state = MPCState()
        self.predictor.reset()

    @property
    def last_compute_ms(self) -> float:
        """Wall-clock cost of the most recent predict() call (for the cost ablation)."""
        if self.state.last_prediction is None:
            return 0.0
        return self.state.last_prediction.compute_ms

    @property
    def fallback_active(self) -> bool:
        """True if the last prediction used the flat fallback (cold start / fit failure)."""
        if self.state.last_prediction is None:
            return True
        return self.state.last_prediction.fallback_used

    # ── Internal optimisation ────────────────────────────────────────────────

    def _optimise(self, forecast: np.ndarray, dwb_prev: float) -> float:
        """
        Bounded grid search over candidate dwb values.

        For each candidate dwb, the predicted RPO-proxy trajectory is
        approximated by an additive shift of the forecast (Eq. plant
        approximation, paper Sec. 4.2). The offset-free integral
        correction is applied afterwards, in tick(), not here.
        """
        candidates = np.linspace(self.dwb_min, self.dwb_max, self.n_candidates)
        rpo_star = self.cfg.rpo_star

        best_dwb = dwb_prev
        best_cost = np.inf

        for dwb in candidates:
            # Plant approximation: the forecast was produced under the
            # interval actually applied while its history was collected
            # (dwb_prev). Per Eqs. 4-6 of the paper, the RPO proxy
            # tracks the applied write-behind interval roughly linearly
            # in steady state; the effect of a candidate dwb is
            # approximated as an additive shift of the forecast, scaled
            # by the empirically calibrated process_gain (see paper
            # Sec. 5.3 for the system-identification procedure and the
            # gain-mismatch ablation motivating this correction).
            projected = forecast + self.process_gain * (dwb - dwb_prev)

            # Symmetric tracking cost toward rpo_star — mirrors the PI
            # controller's own objective (drive e = rpo_star - rho_hat
            # to zero), not just "stay under the ceiling". A one-sided
            # violation-only cost has no incentive to avoid tracking
            # far *below* target, which collapses dwb to dwb_min and
            # wastes I/O for no SLA benefit; asymmetric weighting below
            # keeps violations penalised more heavily than undershoot.
            error = rpo_star - projected
            overshoot = np.maximum(0.0, -error)   # projected > rpo_star (bad: violation)
            undershoot = np.maximum(0.0, error)   # projected < rpo_star (wasteful, not unsafe)
            stage_cost = np.dot(self.horizon_weights, overshoot ** 2) \
                + 0.15 * np.dot(self.horizon_weights, undershoot ** 2)
            move_cost = self.move_penalty * (dwb - dwb_prev) ** 2

            total_cost = stage_cost + move_cost
            if total_cost < best_cost:
                best_cost = total_cost
                best_dwb = dwb

        return float(best_dwb)
