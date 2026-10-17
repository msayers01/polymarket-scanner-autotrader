"""Probability calibration: isotonic regression (PAV) + Beta–binomial shrinkage (docs/STRATEGIES.md §6)."""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path


def pav(x: Sequence[float], y: Sequence[float], w: Sequence[float] | None = None) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators isotonic (non-decreasing) regression.

    Returns (x_sorted_unique_block_means, fitted_values) suitable for piecewise-linear interpolation.
    """
    if len(x) != len(y):
        raise ValueError("x and y must have equal length")
    if not x:
        return [], []
    weights = list(w) if w is not None else [1.0] * len(x)
    order = sorted(range(len(x)), key=lambda i: x[i])
    # blocks: [sum_wy, sum_w, sum_wx]
    blocks: list[list[float]] = []
    for i in order:
        blocks.append([weights[i] * y[i], weights[i], weights[i] * x[i]])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            blocks[-1][0] += b[0]
            blocks[-1][1] += b[1]
            blocks[-1][2] += b[2]
    xs = [b[2] / b[1] for b in blocks]
    ys = [b[0] / b[1] for b in blocks]
    return xs, ys


def beta_shrink(successes: float, n: float, prior_mean: float, kappa: float) -> tuple[float, float]:
    """Posterior mean and std of a Beta(κ·m, κ·(1−m)) prior updated with ``successes``/``n``."""
    a = kappa * prior_mean + successes
    b = kappa * (1 - prior_mean) + (n - successes)
    mean = a / (a + b)
    var = a * b / ((a + b) ** 2 * (a + b + 1))
    return mean, math.sqrt(var)


@dataclass
class CalibrationCurve:
    """Monotone map from market price to calibrated probability, plus per-bin counts for stderr."""

    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    bin_edges: list[float] = field(default_factory=lambda: [i / 20 for i in range(21)])
    bin_n: list[float] = field(default_factory=list)
    bin_successes: list[float] = field(default_factory=list)
    kappa: float = 20.0
    meta: dict[str, object] = field(default_factory=dict)

    @classmethod
    def fit(
        cls, prices: Sequence[float], outcomes: Sequence[int], kappa: float = 20.0, **meta: object
    ) -> CalibrationCurve:
        xs, ys = pav(prices, [float(o) for o in outcomes])
        curve = cls(xs=xs, ys=ys, kappa=kappa, meta=dict(meta))
        nb = len(curve.bin_edges) - 1
        curve.bin_n = [0.0] * nb
        curve.bin_successes = [0.0] * nb
        for p, o in zip(prices, outcomes, strict=True):
            b = curve._bin(p)
            curve.bin_n[b] += 1
            curve.bin_successes[b] += o
        return curve

    def _bin(self, p: float) -> int:
        return min(max(bisect.bisect_right(self.bin_edges, p) - 1, 0), len(self.bin_edges) - 2)

    def raw(self, p: float) -> float:
        """Isotonic fit, linearly interpolated (identity if unfitted)."""
        if not self.xs:
            return p
        if p <= self.xs[0]:
            return self.ys[0]
        if p >= self.xs[-1]:
            return self.ys[-1]
        j = bisect.bisect_right(self.xs, p)
        x0, x1, y0, y1 = self.xs[j - 1], self.xs[j], self.ys[j - 1], self.ys[j]
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0) if x1 > x0 else y0

    def predict(self, p: float) -> tuple[float, float]:
        """Calibrated probability and its std-error, shrunk toward the market price.

        The isotonic value is treated as the data estimate with the bin's effective sample size,
        then combined with a Beta prior centred on ``p`` with strength κ.
        """
        if not self.bin_n:
            return p, 0.5
        b = self._bin(p)
        n = self.bin_n[b]
        return beta_shrink(self.raw(p) * n, n, prior_mean=p, kappa=self.kappa)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1))

    @classmethod
    def load(cls, path: Path) -> CalibrationCurve:
        return cls(**json.loads(path.read_text()))


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes, strict=True)) / max(len(probs), 1)
