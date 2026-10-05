from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_profile
from ..models import Activity, NutritionLog, Profile
from ..ring_utils import ring_data
from ..templating import templates

router = APIRouter()


@router.get("/nutrition")
def nutrition(
    request: Request,
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    today = dt.date.today()
    today_log = (
        db.query(NutritionLog)
        .filter(NutritionLog.profile_id == current.id, NutritionLog.date == today)
        .first()
    )

    today_rings = None
    if today_log:
        exercise_calories = (
            db.query(func.sum(Activity.calories))
            .filter(
                Activity.profile_id == current.id,
                Activity.start_time >= dt.datetime.combine(today, dt.time.min),
            )
            .scalar()
            or 0
        )
        today_rings = {
            "date": today,
            "calories": ring_data(today_log.calories, today_log.calories_goal, radius=70),
            "carbs": ring_data(today_log.carbs_g, today_log.carbs_goal_g, radius=42),
            "fat": ring_data(today_log.fat_g, today_log.fat_goal_g, radius=42),
            "protein": ring_data(today_log.protein_g, today_log.protein_goal_g, radius=42),
            "sodium": ring_data(today_log.sodium_mg, today_log.sodium_goal_mg, radius=1),
            "sugar": ring_data(today_log.sugar_g, today_log.sugar_goal_g, radius=1),
            "fiber": ring_data(today_log.fiber_g, today_log.fiber_goal_g, radius=1),
            "cholesterol": ring_data(today_log.cholesterol_mg, today_log.cholesterol_goal_mg, radius=1),
            "exercise_calories": exercise_calories,
        }

    logs = (
        db.query(NutritionLog)
        .filter(NutritionLog.profile_id == current.id)
        .order_by(NutritionLog.date.desc())
        .limit(60)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "nutrition.html",
        {"logs": logs, "today": today_rings, "active_profile": current, "profiles": profiles},
    )
