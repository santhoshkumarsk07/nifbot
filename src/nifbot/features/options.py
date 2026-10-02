"""Option-chain metrics for ONE snapshot (pure functions, no time dimension).

Input: DataFrame with columns strike, option_type ("CE"/"PE"), ltp, oi, volume,
iv, oi_chg; plus the underlying price. Missing values are NaN, never filled in.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

NAN = float("nan")

CHAIN_FEATURES = (
    "pcr_oi",
    "pcr_oi_atm",
    "pcr_vol",
    "max_pain",
    "max_pain_dist_pct",
    "call_wall",
    "put_wall",
    "call_wall_dist_pct",
    "put_wall_dist_pct",
    "ce_oi_chg_atm",
    "pe_oi_chg_atm",
    "oi_chg_ratio_atm",
    "atm_iv",
    "iv_skew",
    "atm_straddle_pct",
)


def _ratio(a: float, b: float) -> float:
    return a / b if b > 0 else NAN


def strike_step(strikes: np.ndarray) -> float:
    """Most common spacing between listed strikes."""
    s = np.unique(strikes)
    if len(s) < 2:
        return NAN
    diffs = np.diff(s)
    vals, counts = np.unique(diffs, return_counts=True)
    return float(vals[np.argmax(counts)])


def max_pain(strikes: np.ndarray, ce_oi: np.ndarray, pe_oi: np.ndarray) -> float:
    """Strike where option writers' total payout to buyers is smallest."""
    if len(strikes) == 0 or (ce_oi.sum() + pe_oi.sum()) <= 0:
        return NAN
    k = strikes[:, None]  # settlement candidates
    payout = (np.maximum(k - strikes[None, :], 0) * ce_oi[None, :]).sum(axis=1) + (
        np.maximum(strikes[None, :] - k, 0) * pe_oi[None, :]
    ).sum(axis=1)
    return float(strikes[int(np.argmin(payout))])


_FIELDS = ("oi", "volume", "iv", "ltp", "oi_chg")


def pivot_chain(snap: pd.DataFrame) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """(strikes, {"ce_oi": ..., "pe_iv": ...}) aligned on the union of strikes."""
    cols = [c for c in _FIELDS if c in snap.columns]
    wide = snap.pivot_table(index="strike", columns="option_type", values=cols, aggfunc="last")
    strikes = wide.index.to_numpy(dtype=float)
    arrays: dict[str, np.ndarray] = {}
    for f in _FIELDS:
        for side in ("CE", "PE"):
            key = f"{side.lower()}_{f}"
            if (f, side) in wide.columns:
                arrays[key] = wide[(f, side)].to_numpy(dtype=float)
            else:
                arrays[key] = np.full(len(strikes), np.nan)
    return strikes, arrays


def chain_metrics(
    snap: pd.DataFrame, spot: float, atm_width: int = 5, skew_steps: int = 4
) -> dict[str, float]:
    """All chain features for one snapshot given as a long DataFrame."""
    if snap.empty:
        return dict.fromkeys(CHAIN_FEATURES, NAN)
    strikes, a = pivot_chain(snap)
    return chain_metrics_arrays(strikes, a, spot, atm_width, skew_steps)


def _at(arr: np.ndarray, i: int | None) -> float:
    if i is None:
        return NAN
    v = float(arr[i])
    return v if math.isfinite(v) else NAN


def chain_metrics_arrays(
    strikes: np.ndarray,
    a: dict[str, np.ndarray],
    spot: float,
    atm_width: int = 5,
    skew_steps: int = 4,
) -> dict[str, float]:
    """Chain features from strike-aligned arrays (fast path used for history)."""
    out = dict.fromkeys(CHAIN_FEATURES, NAN)
    if len(strikes) == 0 or not math.isfinite(spot) or spot <= 0:
        return out
    ce_oi = np.nan_to_num(a["ce_oi"])
    pe_oi = np.nan_to_num(a["pe_oi"])
    step = strike_step(strikes)
    i_atm = int(np.argmin(np.abs(strikes - spot)))
    atm = float(strikes[i_atm])
    near = np.abs(strikes - atm) <= atm_width * step if math.isfinite(step) else strikes == atm

    out["pcr_oi"] = _ratio(pe_oi.sum(), ce_oi.sum())
    out["pcr_oi_atm"] = _ratio(pe_oi[near].sum(), ce_oi[near].sum())
    out["pcr_vol"] = _ratio(np.nansum(a["pe_volume"]), np.nansum(a["ce_volume"]))

    mp = max_pain(strikes, ce_oi, pe_oi)
    out["max_pain"] = mp
    out["max_pain_dist_pct"] = (mp / spot - 1) * 100 if math.isfinite(mp) else NAN

    above = strikes >= spot
    below = strikes <= spot
    if above.any() and ce_oi[above].max() > 0:
        cw = float(strikes[above][int(np.argmax(ce_oi[above]))])
        out["call_wall"], out["call_wall_dist_pct"] = cw, (cw / spot - 1) * 100
    if below.any() and pe_oi[below].max() > 0:
        pw = float(strikes[below][int(np.argmax(pe_oi[below]))])
        out["put_wall"], out["put_wall_dist_pct"] = pw, (pw / spot - 1) * 100

    ce_chg_n, pe_chg_n = a["ce_oi_chg"][near], a["pe_oi_chg"][near]
    if np.isfinite(ce_chg_n).any() and np.isfinite(pe_chg_n).any():
        ce_chg, pe_chg = float(np.nansum(ce_chg_n)), float(np.nansum(pe_chg_n))
        out["ce_oi_chg_atm"], out["pe_oi_chg_atm"] = ce_chg, pe_chg
        denom = ce_oi[near].sum() + pe_oi[near].sum()
        if denom > 0:
            out["oi_chg_ratio_atm"] = (pe_chg - ce_chg) / denom

    def idx(k: float) -> int | None:
        hits = np.flatnonzero(np.isclose(strikes, k))
        return int(hits[0]) if len(hits) else None

    def pos(v: float) -> float:
        return v if v > 0 else NAN

    ivs = [v for v in (pos(_at(a["ce_iv"], i_atm)), pos(_at(a["pe_iv"], i_atm))) if v == v]
    out["atm_iv"] = float(np.mean(ivs)) if ivs else NAN
    if math.isfinite(step):
        put_iv = pos(_at(a["pe_iv"], idx(atm - skew_steps * step)))
        call_iv = pos(_at(a["ce_iv"], idx(atm + skew_steps * step)))
        out["iv_skew"] = put_iv - call_iv
    ce_p, pe_p = _at(a["ce_ltp"], i_atm), _at(a["pe_ltp"], i_atm)
    if ce_p > 0 and pe_p > 0:
        out["atm_straddle_pct"] = (ce_p + pe_p) / spot * 100
    return out
