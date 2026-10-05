from __future__ import annotations

import math


def sparkline_points(values: list, width: float = 100, height: float = 36, pad: float = 3) -> str:
    """Bir değer listesinden (None'lar atlanır) SVG <polyline points="..."> string'i üretir."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    step = (width - 2 * pad) / (len(pts) - 1)
    coords = []
    for i, v in enumerate(pts):
        x = pad + i * step
        y = height - pad - ((v - lo) / span) * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def compute_trend(
    current: float | None,
    previous: float | None,
    higher_is_better: bool = True,
    as_percent: bool = True,
    unit: str = "",
) -> dict | None:
    """İki dönem arası değişimi (yön + okunabilir metin) hesaplar, kart trend rozeti için."""
    if current is None or previous is None or previous == 0:
        return None
    diff = current - previous
    if abs(diff) < 1e-9:
        return {"direction": "flat", "arrow": "→", "text": "değişim yok", "cls": "flat"}
    text = f"{abs(diff / previous * 100):.0f}%" if as_percent else f"{abs(diff):.0f}{unit}"
    direction = "up" if diff > 0 else "down"
    good = higher_is_better if direction == "up" else not higher_is_better
    return {"direction": direction, "arrow": "↑" if direction == "up" else "↓", "text": text, "cls": "good" if good else "bad"}


def ring_data(value: float | None, goal: float | None, radius: float = 42) -> dict:
    """SVG halka (ring) çizimi için gereken pct/circumference/offset değerlerini hesaplar."""
    value = value or 0
    pct = max(0.0, min(100.0, (value / goal * 100))) if goal else 0.0
    remaining = (goal - value) if goal is not None else None
    circumference = 2 * math.pi * radius
    offset = circumference * (1 - pct / 100)
    return {
        "value": value,
        "goal": goal,
        "pct": round(pct, 1),
        "remaining": remaining,
        "circumference": round(circumference, 2),
        "offset": round(offset, 2),
        "radius": radius,
    }
