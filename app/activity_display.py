from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from .models import Activity

_ICON_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("run",), "🏃"),
    (("ride", "cycl", "bike"), "🚴"),
    (("walk",), "🚶"),
    (("swim",), "🏊"),
    (("strength", "weight", "gym"), "🏋️"),
    (("hik",), "🥾"),
    (("yoga",), "🧘"),
    (("ski", "snowboard"), "🎿"),
]

_LABELS = {
    "running": "Koşu",
    "run": "Koşu",
    "cycling": "Bisiklet",
    "ride": "Bisiklet",
    "walking": "Yürüyüş",
    "walk": "Yürüyüş",
    "swimming": "Yüzme",
    "swim": "Yüzme",
    "strength_training": "Kuvvet Antrenmanı",
    "weighttraining": "Kuvvet Antrenmanı",
    "hiking": "Doğa Yürüyüşü",
    "hike": "Doğa Yürüyüşü",
}


def activity_icon(source: str, activity_type: str | None) -> str:
    if source == "hevy":
        return "🏋️"
    key = (activity_type or "").lower()
    for keywords, icon in _ICON_KEYWORDS:
        if any(k in key for k in keywords):
            return icon
    return "⚡"


def activity_label(source: str, activity_type: str | None) -> str:
    if source == "hevy":
        return activity_type or "Kuvvet Antrenmanı"
    key = (activity_type or "").lower()
    if key in _LABELS:
        return _LABELS[key]
    return (activity_type or "Aktivite").replace("_", " ").title()


def is_running(source: str, activity_type: str | None) -> bool:
    if source == "hevy":
        return False
    return "run" in (activity_type or "").lower()


def format_duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_pace(distance_m: float | None, duration_s: float | None) -> str | None:
    if not distance_m or not duration_s:
        return None
    km = distance_m / 1000
    if km <= 0:
        return None
    pace_s_per_km = duration_s / km
    m, s = divmod(int(pace_s_per_km), 60)
    return f"{m}:{s:02d}/km"


def activity_stats(activity) -> list[str]:
    parts = []
    if activity.distance_m:
        parts.append(f"{activity.distance_m / 1000:.1f} km")
    duration_str = format_duration(activity.duration_s)
    if duration_str:
        parts.append(duration_str)
    if is_running(activity.source, activity.activity_type):
        pace = format_pace(activity.distance_m, activity.duration_s)
        if pace:
            parts.append(pace)
    return parts


def feed_item(activity) -> dict:
    return {
        "activity": activity,
        "icon": activity_icon(activity.source, activity.activity_type),
        "label": activity_label(activity.source, activity.activity_type),
        "stats": activity_stats(activity),
        "also_from": [],
    }


_CARDIO_SOURCE_PRIORITY = ["garmin", "strava", "hevy"]
_STRENGTH_SOURCE_PRIORITY = ["hevy", "garmin", "strava"]
GROUP_TOLERANCE_S = 20 * 60  # Garmin cihazdan Strava'ya otomatik yüklendiğinde saniyeler
# içinde eşleşir; Hevy manuel başlatıldığı için birkaç dakika kayabilir.


def group_activities(activities: list[Activity]) -> list[dict]:
    """Aynı gerçek antrenmanın Garmin/Strava/Hevy'den ayrı ayrı senkronize edilen
    kayıtlarını (yakın başlangıç saatine göre) tek feed öğesinde birleştirir.

    En zengin veriye sahip kaynak (kardiyoda Garmin/Strava, kuvvette Hevy) "primary"
    olarak gösterilir, diğer kaynaklar item['also_from'] içinde küçük rozet olarak yer alır.
    """
    remaining = sorted(activities, key=lambda a: a.start_time, reverse=True)
    groups: list[list[Activity]] = []
    while remaining:
        anchor = remaining.pop(0)
        group = [anchor]
        rest = []
        for other in remaining:
            same_window = abs((other.start_time - anchor.start_time).total_seconds()) <= GROUP_TOLERANCE_S
            if other.profile_id == anchor.profile_id and same_window:
                group.append(other)
            else:
                rest.append(other)
        remaining = rest
        groups.append(group)

    result = []
    for group in groups:
        is_strength = any(a.source == "hevy" or "strength" in (a.activity_type or "").lower() for a in group)
        priority = _STRENGTH_SOURCE_PRIORITY if is_strength else _CARDIO_SOURCE_PRIORITY
        group_sorted = sorted(group, key=lambda a: priority.index(a.source) if a.source in priority else 99)
        item = feed_item(group_sorted[0])
        item["also_from"] = [a.source for a in group_sorted[1:]]
        result.append(item)
    return result


def running_activities(db: Session, profile_id: str, since: dt.datetime) -> list[Activity]:
    rows = (
        db.query(Activity)
        .filter(Activity.profile_id == profile_id, Activity.start_time >= since)
        .order_by(Activity.start_time.desc())
        .all()
    )
    return [a for a in rows if is_running(a.source, a.activity_type)]


def _deduped_runs(db: Session, profile_id: str, since: dt.datetime) -> list[Activity]:
    """Garmin cihazdan Strava'ya otomatik yüklenen koşular aynı antrenmanı iki kez
    saymasın diye grup başına tek (primary) aktiviteyi döner."""
    runs = running_activities(db, profile_id, since)
    return [item["activity"] for item in group_activities(runs)]


def monthly_running_summary(db: Session, profile_id: str) -> dict | None:
    month_start = dt.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = _deduped_runs(db, profile_id, month_start)
    if not runs:
        return None
    total_distance = sum(a.distance_m or 0 for a in runs)
    total_duration = sum(a.duration_s or 0 for a in runs)
    total_calories = sum(a.calories or 0 for a in runs)
    return {
        "count": len(runs),
        "distance_km": total_distance / 1000,
        "avg_pace": format_pace(total_distance, total_duration),
        "avg_calories": round(total_calories / len(runs)) if runs else None,
        "total_duration": format_duration(total_duration),
    }


def weekly_running_distance(db: Session, profile_id: str, weeks: int = 10) -> dict:
    since = dt.datetime.utcnow() - dt.timedelta(weeks=weeks)
    runs = _deduped_runs(db, profile_id, since)

    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    buckets: dict[dt.date, float] = {}
    for i in range(weeks):
        buckets[week_start - dt.timedelta(weeks=i)] = 0.0

    for a in runs:
        d = a.start_time.date()
        bucket_key = d - dt.timedelta(days=d.weekday())
        if bucket_key in buckets:
            buckets[bucket_key] += (a.distance_m or 0) / 1000

    ordered_weeks = sorted(buckets.keys())
    return {
        "labels": [w.strftime("%d %b") for w in ordered_weeks],
        "distance_km": [round(buckets[w], 1) for w in ordered_weeks],
    }
