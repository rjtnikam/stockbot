"""Number formatting for the Telegram messages (Indian style: ₹ crore, %, points)."""
from __future__ import annotations

import math


def num(v) -> float | None:
    try:
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def cr(v) -> str:
    x = num(v)
    if x is None:
        return "n/a"
    return f"₹{x:,.1f} Cr" if abs(x) < 10 else f"₹{x:,.0f} Cr"


def pct(v, signed: bool = True) -> str:
    """v is a fraction: 0.24 -> '+24%'."""
    x = num(v)
    if x is None:
        return "n/a"
    s = f"{x * 100:+.0f}%" if signed else f"{x * 100:.0f}%"
    return s if abs(x) < 10 else ("+999%+" if x > 0 else s)


def pctv(v) -> str:
    """v is already in percent: 11.2 -> '11.2%'."""
    x = num(v)
    return "n/a" if x is None else f"{x:.1f}%" if abs(x) < 100 else f"{x:.0f}%"


def pts(v) -> str:
    x = num(v)
    return "n/a" if x is None else f"{x:+.1f} pts"


def plain_num(v, nd: int = 1) -> str:
    x = num(v)
    return "n/a" if x is None else f"{x:,.{nd}f}"


def growth(values: list, n: int = 4) -> float | None:
    """Sum of the last n quarters vs the n before (TTM growth)."""
    vals = [num(v) for v in (values or [])]
    if len(vals) < 2 * n or any(v is None for v in vals[-2 * n:]):
        return None
    prev = sum(vals[-2 * n:-n])
    return None if prev <= 0 else sum(vals[-n:]) / prev - 1
