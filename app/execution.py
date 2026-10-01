"""
Call auction order execution utilities.

Provides power-law order splitting and participation rate estimation
for minimizing self-inflicted price impact during 集合竞价.

All functions are pure computation — no network I/O.
"""

from __future__ import annotations

import math
from typing import Any


def power_law_split(
    total_qty: int,
    p0: float,
    pmax: float,
    n_levels: int = 10,
    alpha: float = 1.0,
    lot_size: int = 100,
) -> list[dict[str, Any]]:
    """Split a total order across N price levels using power-law decay.

    q(p) = Q × ((Pmax - p) / (Pmax - P0))^α

    Args:
        total_qty: Total shares to buy (will be adjusted to lot_size)
        p0: Current indicative auction price (模拟撮合价)
        pmax: Maximum acceptable price
        n_levels: Number of price levels between p0 and pmax
        alpha: Aggression parameter. <1 = aggressive, 1 = linear, >1 = conservative
        lot_size: Minimum trading unit (A-share = 100)

    Returns:
        List of {price, qty, qty_pct} for each price level.
        Total qty may be slightly less than total_qty due to lot rounding.
    """
    if p0 <= 0 or pmax <= p0 or n_levels < 2:
        return [{"price": round(pmax, 2), "qty": total_qty, "qty_pct": 1.0}]

    step = (pmax - p0) / (n_levels - 1)
    levels = [p0 + step * i for i in range(n_levels)]

    # Compute raw weights: (Pmax - p)^α, normalized
    raw_weights = [(pmax - p) ** alpha for p in levels]
    total_weight = sum(raw_weights)

    if total_weight <= 0:
        return [{"price": round(pmax, 2), "qty": total_qty, "qty_pct": 1.0}]

    weights = [w / total_weight for w in raw_weights]

    # Allocate, rounding down to lot_size
    allocated = []
    remaining = total_qty
    for i, (price, w) in enumerate(zip(levels, weights)):
        if i == n_levels - 1:
            qty = remaining  # last level takes remainder
        else:
            qty = (int(total_qty * w) // lot_size) * lot_size
        allocated.append({
            "price": round(price, 2),
            "qty": qty,
            "qty_pct": round(qty / max(1, total_qty), 4),
        })
        remaining -= qty

    return allocated


def estimate_participation_rate(
    order_qty: int,
    estimated_auction_volume: int,
) -> float:
    """Estimate order participation rate ρ = Q / V_est.

    Args:
        order_qty: Your intended order quantity (shares)
        estimated_auction_volume: Estimated total auction volume (shares)

    Returns:
        Participation rate as a fraction (0.0 ~ 1.0+)
    """
    if estimated_auction_volume <= 0:
        return 1.0
    return min(1.0, order_qty / estimated_auction_volume)


def recommend_alpha(
    participation_rate: float,
    signal_strength: float = 0.5,
) -> dict[str, Any]:
    """Recommend α parameter based on order size and signal confidence.

    Args:
        participation_rate: ρ = order_qty / auction_volume
        signal_strength: 0.0 (weak) ~ 1.0 (strong)

    Returns:
        {alpha, mode, recommendation}
    """
    # Base alpha from participation rate
    if participation_rate <= 0.05:
        base_alpha = 0.5
        mode = "激进（订单很小）"
    elif participation_rate <= 0.15:
        base_alpha = 1.0
        mode = "平衡（中等订单）"
    elif participation_rate <= 0.30:
        base_alpha = 2.0
        mode = "保守（较大订单）"
    else:
        return {
            "alpha": None,
            "mode": "建议降量",
            "recommendation": "订单占比>30%，拆单无法消除冲击，建议降低买入量",
        }

    # Adjust by signal strength: stronger signal → more aggressive (lower alpha)
    adjusted = base_alpha * (1.0 - signal_strength * 0.5)
    adjusted = max(0.3, min(3.0, adjusted))

    return {
        "alpha": round(adjusted, 1),
        "mode": mode,
        "recommendation": (
            f"ρ={participation_rate:.1%}, signal={signal_strength:.1%} → α={adjusted:.1f}"
        ),
    }
