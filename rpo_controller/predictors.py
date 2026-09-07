"""
Load predictors for the MPC write-behind controller.

Two interchangeable predictors sharing a common interface, so the MPC
controller can swap between them without any change to its own code
(used for the MPC-MA vs MPC-ARIMA ablation in the paper).

Both predictors operate on the same signal the PI controller already
observes: the RPO proxy time series (or, equivalently, the write-rate
time series feeding it). We forecast H steps ahead at each tick.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from time import perf_counter
import numpy as np


@dataclass
class PredictionResult:
    """Return value shared by every predictor."""
    forecast: np.ndarray      # shape (horizon,), predicted signal values
    compute_ms: float         # wall-clock cost of this predict() call
    fallback_used: bool = False  # True if predictor could not fit (e.g. cold start)


class LoadPredictor(ABC):
    """Common interface for all predictors used by MPCController."""

    def __init__(self, history_len: int = 40):
        self.history_len = history_len
        self.history: deque[float] = deque(maxlen=history_len)

    def observe(self, value: float) -> None:
        """Feed the latest observed sample (call once per tick, before predict)."""
        self.history.append(value)

    @abstractmethod
    def predict(self, horizon: int) -> PredictionResult:
        """Forecast `horizon` steps ahead. Must not raise; use fallback on failure."""
        ...

    def reset(self) -> None:
        self.history.clear()

    # Shared fallback: persist last value flat across the horizon.
    # Used by both predictors during cold start (insufficient history)
    # and by ARIMA if the fit fails to converge.
    def _flat_fallback(self, horizon: int, compute_ms: float) -> PredictionResult:
        last = self.history[-1] if self.history else 0.0
        return PredictionResult(
            forecast=np.full(horizon, last, dtype=float),
            compute_ms=compute_ms,
            fallback_used=True,
        )


class MovingAveragePredictor(LoadPredictor):
    """
    Weighted moving average predictor (MPC-MA in the paper).

    Forecast is a flat projection of the exponentially-weighted mean of
    the last `window` samples — cheap (O(window) per tick), no fitting
    step, negligible compute cost. Weights favour recent samples
    (alpha close to 1 reacts faster to trend changes).
    """

    def __init__(self, history_len: int = 40, window: int = 10, alpha: float = 0.35,
                 use_trend: bool = True):
        super().__init__(history_len)
        self.window = window
        self.alpha = alpha  # EWMA smoothing factor
        # When False, forecast is a flat projection of the EWMA level
        # only (persistence forecast, no slope term) — tested as a
        # diagnostic to check whether the trend/slope estimate, fit on
        # a noisy window, was itself a source of amplified tracking
        # error (see paper Sec. 5.3, "MPC-Persistence" ablation).
        self.use_trend = use_trend

    def predict(self, horizon: int) -> PredictionResult:
        t0 = perf_counter()
        n = len(self.history)
        if n < 3:
            return self._flat_fallback(horizon, (perf_counter() - t0) * 1000)

        recent = np.array(list(self.history)[-self.window:], dtype=float)
        # Exponentially-weighted mean: most recent sample weighted alpha,
        # decaying geometrically backwards.
        weights = self.alpha * (1 - self.alpha) ** np.arange(len(recent))[::-1]
        weights /= weights.sum()
        level = float(np.dot(recent, weights))

        # Linear trend estimate over the window (simple least squares),
        # extrapolated across the horizon. This is what lets MA react to
        # a ramping load rather than just its current level. Disabled
        # when use_trend=False (persistence-only forecast).
        slope = 0.0
        if self.use_trend and len(recent) >= 2:
            x = np.arange(len(recent))
            slope = float(np.polyfit(x, recent, 1)[0])

        steps = np.arange(1, horizon + 1)
        forecast = level + slope * steps
        forecast = np.clip(forecast, 0.0, None)  # loads/RPO proxies are non-negative

        return PredictionResult(forecast=forecast, compute_ms=(perf_counter() - t0) * 1000)


class ARIMAPredictor(LoadPredictor):
    """
    Lightweight ARIMA(p, d, q) predictor (MPC-ARIMA in the paper).

    Refits every `refit_every` ticks (not every tick) to keep the
    average per-tick compute cost bounded — refitting a small ARIMA
    model on ~40 points is fast, but doing it at 500 ms cadence on
    every single tick is wasteful and, on slower nodes, risks missing
    the control-loop deadline.

    Default order is AR(2), no differencing (d=0): the RPO proxy
    oscillates around a target level rather than trending, so it is
    already approximately stationary. An earlier ARIMA(1,1,0) variant
    was tested on real cluster data and produced a pathological ~4-tick
    (= refit_every) oscillation in dwb — each refit on a differenced,
    noisy short window produced a wildly different forecast, causing
    the controller to overreact every refit cycle (tracking error
    45% vs MPC-MA's 8.4% and PI's 3.2%, see paper Sec. 5.3). Removing
    the differencing term is the hypothesized fix — pending
    confirmation on a fresh real-cluster run before drawing
    conclusions about ARIMA's viability for this task.
    """

    def __init__(self, history_len: int = 40, order: tuple[int, int, int] = (2, 0, 0),
                 refit_every: int = 4, max_fit_iter: int = 50):
        super().__init__(history_len)
        self.order = order
        self.refit_every = refit_every
        self.max_fit_iter = max_fit_iter
        self._ticks_since_fit = 0
        self._fitted_model = None

    def predict(self, horizon: int) -> PredictionResult:
        import warnings
        t0 = perf_counter()
        n = len(self.history)
        if n < 12:  # ARIMA needs a minimum history to fit meaningfully
            return self._flat_fallback(horizon, (perf_counter() - t0) * 1000)

        try:
            from statsmodels.tsa.arima.model import ARIMA

            need_refit = (self._fitted_model is None
                          or self._ticks_since_fit >= self.refit_every)
            if need_refit:
                series = np.array(self.history, dtype=float)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    model = ARIMA(series, order=self.order,
                                   enforce_stationarity=False,
                                   enforce_invertibility=False)
                    # method_kwargs caps solver iterations — a fit that
                    # hasn't converged in max_fit_iter steps is cheaper
                    # to accept as-is (or fall back) than to let run
                    # unbounded inside a 500ms tick budget.
                    self._fitted_model = model.fit(
                        method_kwargs={"maxiter": self.max_fit_iter}
                    )
                self._ticks_since_fit = 0
            else:
                self._ticks_since_fit += 1

            forecast = self._fitted_model.forecast(steps=horizon)
            forecast = np.clip(np.asarray(forecast, dtype=float), 0.0, None)
            return PredictionResult(forecast=forecast, compute_ms=(perf_counter() - t0) * 1000)

        except Exception:
            # Non-convergence or numerical issue: fall back rather than
            # crash the control loop. Logged upstream by the caller.
            self._fitted_model = None
            return self._flat_fallback(horizon, (perf_counter() - t0) * 1000)

    def reset(self) -> None:
        super().reset()
        self._fitted_model = None
        self._ticks_since_fit = 0


class OracleForecastPredictor(LoadPredictor):
    """
    MPC-Oracle (paper upper-bound baseline).

    Cheats by reading the *actual future* trajectory from a pre-recorded
    run, rather than forecasting it. Used only offline, on replayed
    campaign data, to establish how much headroom exists between a real
    predictor and a perfect one. Never used online.
    """

    def __init__(self, future_trajectory: np.ndarray, history_len: int = 40):
        super().__init__(history_len)
        self.future_trajectory = future_trajectory
        self._t = 0

    def observe(self, value: float) -> None:
        super().observe(value)
        self._t += 1

    def predict(self, horizon: int) -> PredictionResult:
        t0 = perf_counter()
        end = min(self._t + horizon, len(self.future_trajectory))
        forecast = self.future_trajectory[self._t:end]
        if len(forecast) < horizon:
            pad = np.full(horizon - len(forecast),
                           forecast[-1] if len(forecast) else 0.0)
            forecast = np.concatenate([forecast, pad])
        return PredictionResult(forecast=np.asarray(forecast, dtype=float),
                                 compute_ms=(perf_counter() - t0) * 1000)
