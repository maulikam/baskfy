"""PM0.11: how a name's return path resolves from a cohort entry. Arrays in, arrays out."""

from __future__ import annotations

import numpy as np

from portfolioml.labels import DEATH_SESSIONS

FALLS = (0.2, 0.3, 0.4, 0.5)


def ffill(a: np.ndarray) -> np.ndarray:
    """Forward-fills each column; leading NaNs stay."""
    idx = np.where(~np.isnan(a), np.arange(a.shape[0])[:, None], 0)
    return a[np.maximum.accumulate(idx, axis=0), np.arange(a.shape[1])]


def paths(closes: np.ndarray, entry: np.ndarray, bars_left: np.ndarray, write_off: np.ndarray, variant: str) -> np.ndarray:
    """Multiples of entry on each session after it (rows = sessions, columns = names).

    A session with no bar carries the last close. ``bars_left``: sessions of the window with a bar still to
    come for that name. Variant ``b`` takes a ``write_off`` name to zero 60 sessions after its last bar."""
    p = ffill(closes / entry[None, :])
    p = np.where(np.isnan(p), 1.0, p)
    if variant == "b":
        steps = np.arange(p.shape[0])[:, None]
        p = np.where(write_off[None, :] & (steps >= bars_left[None, :] + DEATH_SESSIONS), 0.0, p)
    return p


def first_touch(p: np.ndarray, multiple: float) -> np.ndarray:
    """Sessions from entry to the first close at or above ``multiple``, 0 when it never gets there."""
    hit = p >= multiple
    return np.where(hit.any(axis=0), hit.argmax(axis=0) + 1, 0)


def low_before(p: np.ndarray, touch: np.ndarray) -> np.ndarray:
    """The lowest multiple up to the first touch; NaN when there was no touch."""
    lows = np.minimum.accumulate(np.minimum(p, 1.0), axis=0)
    out = np.full(p.shape[1], np.nan)
    won = touch > 0
    out[won] = lows[touch[won] - 1, np.nonzero(won)[0]]
    return out


def suspicious_step(p: np.ndarray, limit: float = 0.6) -> np.ndarray:
    """A one-session move beyond ``limit`` inside the window, the signature of an unadjusted corporate action. A write-off to zero does not count."""
    prior = np.vstack([np.ones(p.shape[1]), p[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        step = np.where((p > 0) & (prior > 0), p / prior - 1, 0.0)
    return (np.abs(step) > limit).any(axis=0)


def peak_to_trough(p: np.ndarray) -> np.ndarray:
    """Worst fall from a running high over the window, entry included (a negative number)."""
    high = np.maximum.accumulate(np.maximum(p, 1.0), axis=0)
    return (p / high - 1).min(axis=0)
