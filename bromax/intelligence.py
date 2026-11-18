"""Exploratory, causal digit-pattern and market-state diagnostics.

These diagnostics describe history; they are not proof of predictive edge and do
not independently authorize paper trades. Candidate strategies are evaluated by
research.py on chronological unseen data before any approval is possible.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import math
import numpy as np
from scipy.stats import chi2


def _wilson(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return None
    p = k / n
    den = 1 + z*z/n
    center = (p + z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/den
    return [max(0.0, center-half), min(1.0, center+half)]


def _sequence_stats(digits: list[int], order: int):
    if len(digits) <= order:
        return {"order": order, "context": digits[-order:] if digits else [], "support": 0,
                "successor_counts": {str(i): 0 for i in range(10)}, "probabilities": None,
                "top_digit": None, "top_probability": None, "historical_hit_rate_ci95": None}
    context = tuple(digits[-order:])
    counts = Counter()
    hits = 0
    n = 0
    # Every target is historical and has a complete preceding context.
    for j in range(order, len(digits)):
        if tuple(digits[j-order:j]) == context:
            counts[digits[j]] += 1
            n += 1
    # Current context is the final order digits; it has no known successor yet.
    total = sum(counts.values())
    smooth = np.asarray([counts[i] + 1.0 for i in range(10)], dtype=float)
    probs = smooth / (total + 10.0)
    top = int(np.argmax(probs))
    hits = counts[top]
    return {"order": order, "context": list(context), "support": total,
            "successor_counts": {str(i): int(counts[i]) for i in range(10)},
            "probabilities": [float(x) for x in probs], "top_digit": top,
            "top_probability": float(probs[top]),
            "historical_hit_rate_ci95": _wilson(hits, total) if total else None,
            "warning": "Exploratory historical conditional frequency; selection and reuse create bias. Not a validated forecast."}


def analyze_ticks(ticks, symbol: str, window: int = 1000):
    rows = ticks[-max(100, min(int(window), 10000)):]
    digits = [int(r["digit"] if isinstance(r, dict) else r.digit) for r in rows]
    n = len(digits)
    counts = np.bincount(digits, minlength=10) if n else np.zeros(10, dtype=int)
    expected = n / 10 if n else 0
    statistic = float(np.sum((counts-expected)**2/expected)) if expected else None
    pvalue = float(chi2.sf(statistic, 9)) if statistic is not None else None
    transition = np.zeros((10, 10), dtype=int)
    for a, b in zip(digits[:-1], digits[1:]):
        transition[a, b] += 1
    row_sums = transition.sum(axis=1)
    transition_probs = [[float((transition[i,j]+1)/(row_sums[i]+10)) for j in range(10)] for i in range(10)]
    run = 0
    if digits:
        last = digits[-1]
        for d in reversed(digits):
            if d != last: break
            run += 1
    price_changes = []
    for r in rows:
        value = r.get("quote_text") if isinstance(r, dict) else getattr(r, "quote_text", None)
        if value is None: continue
        try:
            price = Decimal(str(value))
            if price.is_finite(): price_changes.append(float(price))
        except (InvalidOperation, ValueError):
            continue
    abs_returns = []
    for a, b in zip(price_changes[:-1], price_changes[1:]):
        if a != 0 and math.isfinite(a) and math.isfinite(b):
            abs_returns.append(abs((b-a)/a))
    short_vol = float(np.mean(abs_returns[-100:])) if abs_returns else None
    long_vol = float(np.mean(abs_returns)) if abs_returns else None
    return {
        "symbol": symbol, "window": len(digits), "sample_size": n,
        "digit_counts": {str(i): int(counts[i]) for i in range(10)},
        "digit_frequencies": {str(i): (float(counts[i]/n) if n else None) for i in range(10)},
        "uniformity_diagnostic": {"chi_square_statistic": statistic, "degrees_of_freedom": 9 if n else None,
                                  "p_value": pvalue,
                                  "interpretation": "Descriptive goodness-of-fit diagnostic only; serial dependence and repeated testing are not accounted for."},
        "patterns": {"last_digit_run_length": run, "conditional_order_1": _sequence_stats(digits, 1),
                     "conditional_order_2": _sequence_stats(digits, 2),
                     "conditional_order_3": _sequence_stats(digits, 3)},
        "transition_matrix_smoothed": transition_probs,
        "market_state": {"absolute_return_observations": len(abs_returns),
                         "mean_absolute_return_last_100": short_vol,
                         "mean_absolute_return_full_window": long_vol,
                         "short_to_long_volatility_ratio": (short_vol/long_vol if short_vol is not None and long_vol not in (None, 0) else None),
                         "note": "Price-return diagnostics are descriptive, not evidence that digit outcomes are forecastable."},
        "signal_status": "RESEARCH_ONLY_NOT_APPROVED",
        "disclaimer": "Pattern recurrence and frequency imbalance can occur by chance. Only pre-registered out-of-sample testing with actual contract quotes can establish evidence of edge."
    }
