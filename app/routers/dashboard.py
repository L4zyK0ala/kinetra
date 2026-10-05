from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import i18n
from ..activity_display import format_duration, format_pace, group_activities, is_running
from ..db import get_db
from ..deps import get_current_profile
from ..models import Activity, BodyMeasurement, DailyMetric, PlannedWorkout, Profile
from ..ring_utils import compute_trend, ring_data, sparkline_points
from ..templating import templates

router = APIRouter()

_MERGE_FIELDS = (
    "steps",
    "steps_goal",
    "resting_hr",
    "sleep_duration_s",
    "sleep_score",
    "weight_kg",
    "body_battery_max",
    "body_battery_min",
    "stress_avg",
)

def _today_label(lang: str) -> str:
    today = dt.date.today()
    return i18n.t(lang, "Bugün {weekday}, {day} {month}", weekday=i18n.weekday_name(lang, today.weekday()),
                  day=today.day, month=i18n.month_name(lang, today.month))


def _greeting(name: str, lang: str) -> str:
    hour = dt.datetime.now().hour
    if hour < 6:
        prefix = "İyi geceler"
    elif hour < 12:
        prefix = "Günaydın"
    elif hour < 18:
        prefix = "İyi günler"
    else:
        prefix = "İyi akşamlar"
    return f"{i18n.t(lang, prefix)}, {name}"


def _merge_daily_metrics(rows: list[DailyMetric]) -> dict[dt.date, dict]:
    """Aynı profil+tarih için farklı kaynaklardan gelen satırları tek satıra birleştirir."""
    merged: dict[dt.date, dict] = {}
    for row in rows:
        bucket = merged.setdefault(row.date, {})
        for field in _MERGE_FIELDS:
            value = getattr(row, field)
            if value is not None:
                bucket.setdefault(field, value)
    return merged


def _period_totals(db: Session, profile_id: str, start: dt.datetime, end: dt.datetime) -> dict:
    raw_activities = (
        db.query(Activity)
        .filter(Activity.profile_id == profile_id, Activity.start_time >= start, Activity.start_time < end)
        .all()
    )
    # Ayni antrenman Garmin+Strava+Hevy'den ayri ayri gelirse iki kez sayilmasin.
    activities = [item["activity"] for item in group_activities(raw_activities)]
    steps_total = (
        db.query(DailyMetric)
        .filter(DailyMetric.profile_id == profile_id, DailyMetric.date >= start.date(), DailyMetric.date < end.date())
        .all()
    )
    steps_by_date: dict[dt.date, int] = {}
    for row in steps_total:
        if row.steps is not None:
            steps_by_date[row.date] = max(steps_by_date.get(row.date, 0), row.steps)

    return {
        "count": len(activities),
        "distance_m": sum(a.distance_m or 0 for a in activities),
        "duration_s": sum(a.duration_s or 0 for a in activities),
        "calories": sum(a.calories or 0 for a in activities),
        "steps": sum(steps_by_date.values()),
    }


# ==========================================================================
# "Bugün" Panel: toparlanma, Koç'un sıradaki antrenmanı, kısa göstergeler, son aktivite.
# ==========================================================================

_PLAN_TYPE_LABELS = {"running": "Koşu", "cycling": "Bisiklet", "walking": "Yürüyüş", "swimming": "Yüzme", "strength": "Kuvvet", "other": "Diğer"}


def _vs_baseline(merged: dict[dt.date, dict], field: str, higher_is_better: bool, unit: str) -> dict | None:
    """Son günün değerini önceki 7 günün ortalamasıyla karşılaştırır (Whoop/Oura tarzı kişisel referans)."""
    dates = [d for d in sorted(merged) if merged[d].get(field) is not None]
    if not dates:
        return None
    last = dates[-1]
    value = merged[last][field]
    prev = [merged[d][field] for d in dates if last - dt.timedelta(days=7) <= d < last]
    baseline = round(sum(prev) / len(prev), 1) if prev else None
    trend = compute_trend(value, baseline, higher_is_better=higher_is_better, as_percent=False, unit=unit) if baseline else None
    return {"value": value, "baseline": baseline, "trend": trend, "date": last}


def _matches_type(activity: Activity, workout_type: str) -> bool:
    kind = (activity.activity_type or "").lower()
    if workout_type == "running":
        return is_running(activity.source, activity.activity_type)
    if workout_type == "strength":
        return activity.source == "hevy" or "strength" in kind
    keys = {"cycling": ("ride", "cycl", "bike"), "walking": ("walk",), "swimming": ("swim",)}.get(workout_type, ())
    return any(k in kind for k in keys)


def _plan_card(db: Session, profile_id: str, today: dt.date, todays_activities: list[Activity], lang: str = "tr") -> dict | None:
    from .coach import active_plans

    upcoming = active_plans(db, profile_id).filter(PlannedWorkout.date >= today).order_by(PlannedWorkout.date.asc()).first()
    if upcoming is None:
        return None
    done = upcoming.date == today and (
        upcoming.status == "done" or any(_matches_type(a, upcoming.workout_type) for a in todays_activities)
    )
    days_until = (upcoming.date - today).days
    if days_until == 0:
        when = i18n.t(lang, "Bugün")
    elif days_until == 1:
        when = i18n.t(lang, "Yarın")
    else:
        when = f"{i18n.weekday_name(lang, upcoming.date.weekday())}, {upcoming.date.day} {i18n.month_name(lang, upcoming.date.month)}"
    meta = [i18n.t(lang, _PLAN_TYPE_LABELS.get(upcoming.workout_type, upcoming.workout_type))]
    if upcoming.target_distance_m:
        meta.append(f"{upcoming.target_distance_m / 1000:.1f} km")
    if upcoming.target_duration_s:
        meta.append(f"{int(upcoming.target_duration_s // 60)} {i18n.t(lang, 'dk')}")
    return {"workout": upcoming, "when": when, "is_today": days_until == 0, "done": done, "meta": " · ".join(meta)}


def _body_summary(db: Session, profile: Profile) -> list[dict]:
    """Kilo / yağ / bel için son değer ve son 6 aydaki değişim — detaylı grafikler ayrı sayfada kalır."""
    since = dt.date.today() - dt.timedelta(days=182)
    rows = (
        db.query(BodyMeasurement)
        .filter(BodyMeasurement.profile_id == profile.id, BodyMeasurement.date >= since)
        .order_by(BodyMeasurement.date.asc())
        .all()
    )
    out = []
    for field, label, unit in (("weight_kg", "Kilo", "kg"), ("fat_percent", "Yağ Oranı", "%"), ("waist_cm", "Bel", "cm")):
        values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        if not values:
            continue
        out.append({
            "label": label, "key": field, "unit": unit, "value": values[-1],
            "change": round(values[-1] - values[0], 1) if len(values) > 1 else None,
            "spark": sparkline_points(values) if len(values) > 1 else None,
            "goal": f"{profile.weight_goal_kg:g} kg" if field == "weight_kg" and profile.weight_goal_kg else None,
        })
    return out


@router.get("/panel-onizleme")
def old_preview_url():
    return RedirectResponse("/dashboard", status_code=307)


@router.get("/dashboard")
def dashboard(
    request: Request,
    profile: str | None = None,
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    active_profile = (db.get(Profile, profile) if profile else None) or current or (profiles[0] if profiles else None)
    if active_profile is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    today = dt.date.today()
    now = dt.datetime.now()
    daily = (
        db.query(DailyMetric)
        .filter(DailyMetric.profile_id == active_profile.id, DailyMetric.date >= today - dt.timedelta(days=30))
        .all()
    )
    merged = _merge_daily_metrics(daily)
    dates = sorted(merged)

    sleep = _vs_baseline(merged, "sleep_score", True, "")
    readiness = None
    if dates:
        last = merged[dates[-1]]
        readiness = {
            "date": dates[-1],
            "sleep": sleep,
            "sleep_ring": ring_data(sleep["value"] if sleep else None, 100, radius=52),
            "sleep_hours": round(last["sleep_duration_s"] / 3600, 1) if last.get("sleep_duration_s") else None,
            "signals": [
                s for s in (
                    {"label": "Dinlenik nabız", "unit": "bpm", **(_vs_baseline(merged, "resting_hr", False, " bpm") or {})},
                    {"label": "Body Battery (zirve)", "unit": "/100", **(_vs_baseline(merged, "body_battery_max", True, "") or {})},
                    {"label": "Stres", "unit": "/100", **(_vs_baseline(merged, "stress_avg", False, "") or {})},
                ) if s.get("value") is not None
            ],
        }

    day_start = dt.datetime.combine(today, dt.time.min)
    recent = (
        db.query(Activity)
        .filter(Activity.profile_id == active_profile.id, Activity.start_time >= now - dt.timedelta(days=60))
        .order_by(Activity.start_time.desc())
        .all()
    )
    feed = group_activities(recent)
    todays = [a for a in recent if a.start_time >= day_start]

    steps_today = merged.get(today, {}).get("steps")
    steps_goal = merged.get(today, {}).get("steps_goal") or 10000

    week = _period_totals(db, active_profile.id, now - dt.timedelta(days=7), now)
    prev_week = _period_totals(db, active_profile.id, now - dt.timedelta(days=14), now - dt.timedelta(days=7))

    latest = feed[0] if feed else None
    hero = None
    if latest:
        a = latest["activity"]
        hero = {
            "item": latest,
            "distance": f"{a.distance_m / 1000:.2f}" if a.distance_m else None,
            "duration": format_duration(a.duration_s),
            "pace": format_pace(a.distance_m, a.duration_s) if is_running(a.source, a.activity_type) else None,
            "hr": int(a.avg_hr) if a.avg_hr else None,
        }

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "profiles": profiles,
            "active_profile": active_profile,
            "greeting": _greeting(active_profile.name, request.state.lang),
            "today_label": _today_label(request.state.lang),
            "readiness": readiness,
            "plan": _plan_card(db, active_profile.id, today, todays, request.state.lang),
            "steps": {"value": steps_today, "goal": steps_goal, "ring": ring_data(steps_today, steps_goal, radius=34)} if steps_today is not None else None,
            "week": {
                "count": week["count"],
                "km": round(week["distance_m"] / 1000, 1),
                "duration": format_duration(week["duration_s"]) or "0:00",
                "km_trend": compute_trend(week["distance_m"], prev_week["distance_m"]),
                "count_trend": compute_trend(week["count"], prev_week["count"]),
            },
            "hero": hero,
            "more_activities": feed[1:4],
            "body": _body_summary(db, active_profile),
            "trend_labels": [d.strftime("%d.%m") for d in dates],
            "trend_hr": [merged[d].get("resting_hr") for d in dates],
            "trend_sleep": [round(merged[d]["sleep_duration_s"] / 3600, 1) if merged[d].get("sleep_duration_s") else None for d in dates],
        },
    )
