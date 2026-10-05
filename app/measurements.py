from __future__ import annotations

from .models import BodyMeasurement, Profile
from .ring_utils import compute_trend

MEASUREMENT_FIELDS = [
    ("weight_kg", "Kilo", "kg"),
    ("fat_percent", "Yağ Oranı", "%"),
    ("lean_mass_kg", "Yağsız Kütle", "kg"),
    ("neck_cm", "Boyun", "cm"),
    ("shoulder_cm", "Omuz", "cm"),
    ("chest_cm", "Göğüs", "cm"),
    ("left_bicep_cm", "Sol Pazı", "cm"),
    ("right_bicep_cm", "Sağ Pazı", "cm"),
    ("left_forearm_cm", "Sol Ön Kol", "cm"),
    ("right_forearm_cm", "Sağ Ön Kol", "cm"),
    ("abdomen_cm", "Karın", "cm"),
    ("waist_cm", "Bel", "cm"),
    ("hips_cm", "Kalça", "cm"),
    ("left_thigh_cm", "Sol Uyluk", "cm"),
    ("right_thigh_cm", "Sağ Uyluk", "cm"),
    ("left_calf_cm", "Sol Baldır", "cm"),
    ("right_calf_cm", "Sağ Baldır", "cm"),
]

# Sol/sağ çiftleri tek grafikte iki çizgi olarak gösterilir (13 grafik yerine 17 olmasın,
# ayrıca iki taraf arasındaki fark doğrudan karşılaştırılabilsin).
_CHART_GROUPS = [
    ("Kilo", "kg", [("weight_kg", None)]),
    ("Yağ Oranı", "%", [("fat_percent", None)]),
    ("Yağsız Kütle", "kg", [("lean_mass_kg", None)]),
    ("Boyun", "cm", [("neck_cm", None)]),
    ("Omuz", "cm", [("shoulder_cm", None)]),
    ("Göğüs", "cm", [("chest_cm", None)]),
    ("Pazı", "cm", [("left_bicep_cm", "Sol"), ("right_bicep_cm", "Sağ")]),
    ("Ön Kol", "cm", [("left_forearm_cm", "Sol"), ("right_forearm_cm", "Sağ")]),
    ("Karın", "cm", [("abdomen_cm", None)]),
    ("Bel", "cm", [("waist_cm", None)]),
    ("Kalça", "cm", [("hips_cm", None)]),
    ("Uyluk", "cm", [("left_thigh_cm", "Sol"), ("right_thigh_cm", "Sağ")]),
    ("Baldır", "cm", [("left_calf_cm", "Sol"), ("right_calf_cm", "Sağ")]),
]

# Renkler CSS token adı olarak gönderilir (açık/koyu mod için ayrı tonlar style.css'te).
# Tek seri: mavi (yağ oranı turuncu). İki seri: Sol mavi, Sağ turuncu — dataviz validator ile
# açık ve koyu zeminde renk körlüğü/kontrast kontrollerinden geçti.
_SERIES_COLORS = ["--viz-blue", "--viz-orange"]


def _stats(m: BodyMeasurement, profile: Profile) -> list[dict]:
    stats = []
    for field, label, unit in MEASUREMENT_FIELDS:
        value = getattr(m, field)
        if value is not None:
            stats.append({"label": label, "value": value, "unit": unit})
    if profile.height_cm:
        stats.insert(1, {"label": "Boy", "value": profile.height_cm, "unit": "cm"})
    return stats


def _charts(chrono: list[BodyMeasurement]) -> list[dict]:
    charts = []
    for title, unit, fields in _CHART_GROUPS:
        rows = [m for m in chrono if any(getattr(m, f) is not None for f, _ in fields)]
        series = []
        for i, (field, name) in enumerate(fields):
            data = [getattr(m, field) for m in rows]
            points = [v for v in data if v is not None]
            if len(points) < 2:
                continue  # tek noktalık seri trend göstermez
            color = "--viz-orange" if title == "Yağ Oranı" else _SERIES_COLORS[i]
            series.append({
                "name": name,
                "color": color,
                "data": data,
                "latest": points[-1],
                "change": round(points[-1] - points[0], 1),
            })
        if series:
            charts.append({
                "title": title,
                "unit": unit,
                "labels": [m.date.strftime("%d.%m.%y") for m in rows],
                "dates": [m.date.isoformat() for m in rows],  # Panel'deki aralık seçicisi bununla filtreliyor
                "series": series,
            })
    return charts


def measurement_overview(measurements_desc: list[BodyMeasurement], profile: Profile) -> dict | None:
    """Tarihe göre yeniden eskiye sıralı ölçümlerden Panel'deki "Vücut Ölçümleri"
    bölümünün verisini üretir: güncel değerler, son iki ölçüm arası kilo/yağ trendi
    ve her ölçüm için trend grafiği."""
    if not measurements_desc:
        return None
    latest = measurements_desc[0]
    previous = measurements_desc[1] if len(measurements_desc) > 1 else None

    trends = {}
    if previous:
        for field, label, unit in (("weight_kg", "Kilo", " kg"), ("fat_percent", "Yağ Oranı", "%")):
            now, before = getattr(latest, field), getattr(previous, field)
            if now is not None and before is not None:
                trends[label] = compute_trend(now, before, higher_is_better=False, as_percent=False, unit=unit)

    return {
        "latest": latest,
        "stats": _stats(latest, profile),
        "trends": trends,
        "charts": _charts(list(reversed(measurements_desc))),
    }
