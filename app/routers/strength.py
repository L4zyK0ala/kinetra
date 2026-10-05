from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import i18n
from ..db import get_db
from ..deps import get_current_profile
from ..measurements import measurement_overview
from ..models import Activity, BodyMeasurement, Profile
from ..templating import templates

router = APIRouter()

def _sets_summary(sets: list[dict]) -> str:
    parts = []
    for s in sets or []:
        w = s.get("weight_kg")
        r = s.get("reps")
        if w is not None and r is not None:
            parts.append(f"{w:g}kg×{r}")
        elif r is not None:
            parts.append(f"{r} tekrar")
    return " · ".join(parts) if parts else "—"


def _parse_workout(row: Activity) -> dict:
    try:
        raw = json.loads(row.raw_json) if row.raw_json else {}
    except (TypeError, ValueError):
        raw = {}
    exercises = []
    total_sets = 0
    volume = 0.0
    for ex in raw.get("exercises") or []:
        sets = ex.get("sets") or []
        total_sets += len(sets)
        for s in sets:
            if s.get("weight_kg") and s.get("reps"):
                volume += s["weight_kg"] * s["reps"]
        exercises.append({"title": ex.get("title") or "Egzersiz", "summary": _sets_summary(sets), "set_count": len(sets)})
    return {
        "activity": row,
        "title": raw.get("title") or row.activity_type or "Antrenman",
        "exercises": exercises,
        "total_sets": total_sets,
        "volume": round(volume),
    }


def _localized_overview(body: dict | None, lang: str) -> dict | None:
    """Grafik başlıkları ve seri adları JS'e JSON olarak gittiği için burada çevrilir."""
    if body:
        for chart in body.get("charts", []):
            chart["title"] = i18n.t(lang, chart["title"])
            for series in chart.get("series", []):
                if series.get("name"):
                    series["name"] = i18n.t(lang, series["name"])
    return body


@router.get("/strength")
def strength(
    request: Request,
    tab: str = "workouts",
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    if tab == "measurements":
        measurements = (
            db.query(BodyMeasurement)
            .filter(BodyMeasurement.profile_id == current.id)
            .order_by(BodyMeasurement.date.desc())
            .all()
        )
        return templates.TemplateResponse(
            request,
            "strength.html",
            {
                "tab": "measurements",
                "profiles": profiles,
                "active_profile": current,
                "measurements": measurements,
                "body": _localized_overview(measurement_overview(measurements, current), request.state.lang),
            },
        )

    rows = (
        db.query(Activity)
        .filter(Activity.profile_id == current.id, Activity.source == "hevy")
        .order_by(Activity.start_time.desc())
        .limit(30)
        .all()
    )
    workouts = [_parse_workout(row) for row in rows]

    month_start = dt.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_workouts = [w for w in workouts if w["activity"].start_time >= month_start]
    summary = None
    if month_workouts:
        summary = {
            "count": len(month_workouts),
            "total_sets": sum(w["total_sets"] for w in month_workouts),
            "volume": sum(w["volume"] for w in month_workouts),
        }

    return templates.TemplateResponse(
        request,
        "strength.html",
        {"tab": "workouts", "workouts": workouts, "summary": summary, "active_profile": current, "profiles": profiles},
    )
