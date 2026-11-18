from dataclasses import dataclass
import hashlib
import math
import numpy as np

@dataclass(frozen=True)
class ForecastContext:
    symbol: str
    observed_tick_id: int
    observed_epoch: int
    history: tuple[int, ...]  # strictly past and current observed digits; never contains next tick
    cumulative_counts: tuple[int, ...] | None = None

@dataclass(frozen=True)
class Forecast:
    strategy_id: str
    version: str
    parameters: dict
    prediction: int
    probabilities: tuple[float, ...]

class Strategy:
    strategy_id = "abstract"
    version = "1.0.0"
    parameters: dict = {}
    def forecast(self, context: ForecastContext) -> Forecast:
        raise NotImplementedError

def _probabilities(counts: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    return (counts.astype(float) + alpha) / (counts.sum() + 10 * alpha)

def _make(strategy, context, probs):
    p = np.asarray(probs, dtype=float)
    if p.shape != (10,) or not np.isfinite(p).all() or (p < 0).any() or not np.isclose(p.sum(), 1.0):
        raise ValueError("Strategy must return ten valid probabilities summing to one")
    return Forecast(strategy.strategy_id, strategy.version, dict(strategy.parameters), int(np.argmax(p)), tuple(float(x) for x in p))

class UniformBaseline(Strategy):
    strategy_id = "uniform_random"
    version = "1.0.0"
    parameters = {"method": "deterministic_sha256_uniform_for_reproducibility"}
    def forecast(self, context):
        digest = hashlib.sha256(f"{context.symbol}|{context.observed_tick_id}|bromax-baseline-v1".encode()).digest()
        raw = int.from_bytes(digest[:8], "big")
        limit = (1 << 64) - ((1 << 64) % 10)
        counter = 0
        while raw >= limit:
            counter += 1
            raw = int.from_bytes(hashlib.sha256(f"{context.symbol}|{context.observed_tick_id}|bromax-baseline-v1|{counter}".encode()).digest()[:8], "big")
        digit = raw % 10
        probs = np.full(10, 0.1)
        probs[digit] = 0.1
        # The single prediction is a reproducible uniform draw; probabilities stay uniform.
        return Forecast(self.strategy_id, self.version, dict(self.parameters), digit, tuple(probs))

class CumulativeMode(Strategy):
    strategy_id = "cumulative_mode"
    version = "1.0.0"
    parameters = {"window": "all_observed_history", "tie_break": "lowest_digit"}
    def forecast(self, context):
        counts=np.asarray(context.cumulative_counts,dtype=int) if context.cumulative_counts is not None else np.bincount(context.history,minlength=10)
        return _make(self,context,_probabilities(counts,0.0) if counts.sum() else np.full(10,.1))

class RollingMode(Strategy):
    def __init__(self, window: int):
        self.window = window
        self.strategy_id = f"rolling_mode_{window}"
        self.parameters = {"window": window, "tie_break": "lowest_digit"}
    def forecast(self, context):
        h = context.history[-self.window:]
        counts = np.bincount(h, minlength=10) if h else np.zeros(10, dtype=int)
        return _make(self, context, _probabilities(counts, 0.0) if counts.sum() else np.full(10, .1))

class LaplaceFrequency(Strategy):
    def __init__(self, window: int, alpha: float = 1.0):
        self.window, self.alpha = window, alpha
        self.strategy_id = f"laplace_freq_{window}_a{alpha:g}"
        self.parameters = {"window": window, "alpha": alpha, "prior": "symmetric_dirichlet"}
    def forecast(self, context):
        h = context.history[-self.window:]
        counts = np.bincount(h, minlength=10) if h else np.zeros(10, dtype=int)
        return _make(self, context, _probabilities(counts, self.alpha))

class ExponentialFrequency(Strategy):
    def __init__(self, half_life: float):
        self.half_life = float(half_life)
        self.strategy_id = f"exp_frequency_hl{self.half_life:g}"
        self.parameters = {"half_life_ticks": self.half_life, "weight": "exp(-ln(2)*age/half_life)", "alpha": 1.0}
    def forecast(self, context):
        h = context.history[-1000:]
        if not h:
            return _make(self, context, np.full(10, .1))
        ages = np.arange(len(h) - 1, -1, -1, dtype=float)
        weights = np.exp(-math.log(2) * ages / self.half_life)
        counts = np.bincount(np.asarray(h, dtype=int), weights=weights, minlength=10)
        return _make(self, context, _probabilities(counts, 1.0))


class ConditionalTransition(Strategy):
    """Second-order Markov frequency candidate; Laplace-smoothed, fixed order."""
    strategy_id = "conditional_transition_order2"
    version = "1.0.0"
    parameters = {"order": 2, "alpha": 1.0, "minimum_support_for_interpretation": 20,
                  "low_support_fallback": "laplace_frequency_500",
                  "method": "historical_successors_of_last_two_digits"}
    def forecast(self, context):
        h = tuple(int(x) for x in context.history)
        counts = np.zeros(10, dtype=float)
        if len(h) >= 3:
            current = h[-2:]
            for j in range(2, len(h)):
                if h[j-2:j] == current:
                    counts[h[j]] += 1.0
        if counts.sum() < 20:
            fallback = np.bincount(h[-500:], minlength=10) if h else np.zeros(10, dtype=int)
            return _make(self, context, _probabilities(fallback, 1.0))
        return _make(self, context, _probabilities(counts, 1.0))

def candidate_strategies():
    """Pre-registered list. Do not edit after opening the final test split."""
    return [UniformBaseline(), CumulativeMode(), RollingMode(100), RollingMode(500), RollingMode(1000),
            LaplaceFrequency(100, 1.0), LaplaceFrequency(500, 1.0),
            ExponentialFrequency(50), ExponentialFrequency(200), ConditionalTransition()]
