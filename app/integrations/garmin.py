from __future__ import annotations

import datetime as dt
import json
import logging

import garminconnect
from sqlalchemy.orm import Session

from ..config import settings
from ..crypto import encrypt_payload
from ..models import Activity, Credential, DailyMetric, PlannedWorkout, Profile
from . import SyncResult, register_connect, register_sync

logger = logging.getLogger("fitdash.garmin")

# Garmin'in workout-oluşturma API'sinde desteklenen sport type ID/key çiftleri
# (garminconnect.workout modülündeki *Workout sınıflarının varsayılanlarıyla eşleşir).
_WORKOUT_SPORT_INFO = {
    "running": (1, "running"),
    "cycling": (2, "cycling"),
    "walking": (17, "walking"),
    "swimming": (4, "swimming"),
    # Kuvvet: egzersiz listesi yerine süreli tek adım + notlar (Claude planı egzersiz kodu içermiyor).
    "strength": (5, "strength_training"),
}


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - bir alanın çekilememesi tüm sync'i durdurmamalı
        return None


@register_connect("garmin")
def connect(form: dict) -> dict:
    """Ayarlar formundan gelen email/şifre(/mfa) ile giriş dener, session'ı döner."""
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    mfa_code = (form.get("mfa_code") or "").strip()
    if not email or not password:
        raise ValueError("E-posta ve şifre gerekli.")

    client = garminconnect.Garmin(email, password, return_on_mfa=True)
    mfa_status, _ = client.login()
    if mfa_status == "needs_mfa":
        if not mfa_code:
            raise ValueError(
                "Bu hesap için MFA (iki adımlı doğrulama) kodu gerekiyor. "
                "Telefonuna/e-postana gelen kodu 'MFA kodu' alanına girip formu tekrar gönder."
            )
        client.resume_login(None, mfa_code)

    session = client.client.dumps()
    return {"email": email, "password": password, "session": session}


def _login_from_payload(payload: dict) -> garminconnect.Garmin:
    client = garminconnect.Garmin(payload.get("email"), payload.get("password"))
    session = payload.get("session")
    if session:
        client.login(tokenstore=session)
    else:
        client.login()
    return client


def _sync_daily_metrics(client, profile: Profile, db: Session, start: dt.date, end: dt.date) -> int:
    days_ok = 0
    day = start
    while day <= end:
        cdate = day.isoformat()
        stats = _safe(client.get_stats, cdate)
        sleep = _safe(client.get_sleep_data, cdate)
        stress = _safe(client.get_stress_data, cdate)
        weigh = _safe(client.get_daily_weigh_ins, cdate)

        steps = stats.get("totalSteps") if isinstance(stats, dict) else None
        steps_goal = stats.get("dailyStepGoal") if isinstance(stats, dict) else None
        resting_hr = stats.get("restingHeartRate") if isinstance(stats, dict) else None

        sleep_seconds = None
        sleep_score = None
        if isinstance(sleep, dict):
            dto = sleep.get("dailySleepDTO") or {}
            sleep_seconds = dto.get("sleepTimeSeconds")
            sleep_score = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")

        stress_avg = stress.get("avgStressLevel") if isinstance(stress, dict) else None

        weight_kg = None
        if isinstance(weigh, dict):
            entries = weigh.get("dateWeightList") or weigh.get("weightList") or []
            if entries:
                grams = entries[-1].get("weight")
                if grams:
                    weight_kg = grams / 1000

        if any(v is not None for v in (steps, resting_hr, sleep_seconds, sleep_score, stress_avg, weight_kg)):
            row = (
                db.query(DailyMetric)
                .filter(
                    DailyMetric.profile_id == profile.id,
                    DailyMetric.date == day,
                    DailyMetric.source == "garmin",
                )
                .first()
                or DailyMetric(profile_id=profile.id, date=day, source="garmin")
            )
            if steps is not None:
                row.steps = int(steps)
            if steps_goal is not None:
                row.steps_goal = int(steps_goal)
            if resting_hr is not None:
                row.resting_hr = int(resting_hr)
            if sleep_seconds is not None:
                row.sleep_duration_s = float(sleep_seconds)
            if sleep_score is not None:
                row.sleep_score = int(sleep_score)
            if stress_avg is not None:
                row.stress_avg = int(stress_avg)
            if weight_kg is not None:
                row.weight_kg = float(weight_kg)
            db.add(row)
            days_ok += 1

        day += dt.timedelta(days=1)

    battery = _safe(client.get_body_battery, start.isoformat(), end.isoformat())
    if isinstance(battery, list):
        for entry in battery:
            date_str = entry.get("date") or entry.get("calendarDate")
            if not date_str:
                continue
            try:
                d = dt.date.fromisoformat(date_str)
            except ValueError:
                continue
            row = (
                db.query(DailyMetric)
                .filter(
                    DailyMetric.profile_id == profile.id,
                    DailyMetric.date == d,
                    DailyMetric.source == "garmin",
                )
                .first()
            )
            if row is None:
                continue
            if entry.get("charged") is not None:
                row.body_battery_max = int(entry["charged"])
            if entry.get("drained") is not None:
                row.body_battery_min = int(entry["drained"])
            db.add(row)

    return days_ok


def _sync_activities(client, profile: Profile, db: Session, start: dt.date, end: dt.date) -> tuple[int, str | None]:
    try:
        acts = client.get_activities_by_date(start.isoformat(), end.isoformat()) or []
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)

    added = 0
    for act in acts:
        external_id = act.get("activityId")
        start_str = act.get("startTimeLocal") or act.get("startTimeGMT")
        if external_id is None or not start_str:
            continue
        try:
            start_time = dt.datetime.fromisoformat(str(start_str).replace(" ", "T"))
        except ValueError:
            continue

        row = (
            db.query(Activity)
            .filter(Activity.source == "garmin", Activity.external_id == str(external_id))
            .first()
            or Activity(profile_id=profile.id, source="garmin", external_id=str(external_id))
        )
        row.profile_id = profile.id
        row.start_time = start_time
        row.activity_type = (act.get("activityType") or {}).get("typeKey")
        row.duration_s = act.get("duration")
        row.distance_m = act.get("distance")
        row.calories = act.get("calories")
        row.avg_hr = act.get("averageHR")
        row.max_hr = act.get("maxHR")
        row.elevation_gain_m = act.get("elevationGain")
        try:
            row.raw_json = json.dumps(act)[:20000]
        except (TypeError, ValueError):
            row.raw_json = None
        db.add(row)
        added += 1

    return added, None


@register_sync("garmin")
def sync(profile: Profile, db: Session, credential: Credential, payload: dict) -> SyncResult:
    try:
        client = _login_from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(
            "error",
            f"Giriş başarısız: {exc}. Ayarlar sayfasından yeniden bağlanman gerekebilir (MFA kodu tekrar istenebilir).",
        )

    try:
        credential.encrypted_payload = encrypt_payload({**payload, "session": client.client.dumps()})
    except Exception:  # noqa: BLE001 - session yenileme başarısız olursa eski payload'la devam
        logger.debug("Garmin session dumps() başarısız, eski oturum korunuyor")

    end = dt.date.today()
    start = end - dt.timedelta(days=settings.sync_lookback_days)

    days_ok = _sync_daily_metrics(client, profile, db, start, end)
    activities_added, activities_error = _sync_activities(client, profile, db, start, end)

    parts = [f"{days_ok} gün günlük metrik", f"{activities_added} aktivite"]
    if activities_error:
        parts.append(f"aktivite hatası: {activities_error}")
    return SyncResult("ok", ", ".join(parts) + ".")


def push_planned_workout(payload: dict, planned: PlannedWorkout) -> tuple[str | None, str | None]:
    """Bir PlannedWorkout'u Garmin Connect'e yapılandırılmış antrenman olarak
    yükler ve planlanan tarihe zamanlar. (workout_id, hata_mesajı) döner.
    Tam başarıda hata_mesajı None'dır. Yükleme hiç başarısız olursa workout_id
    None olur; yükleme başarılı ama takvime ekleme başarısız olursa workout_id
    dolu döner ama hata_mesajı da dolu olur (kısmi başarı)."""
    if planned.workout_type not in _WORKOUT_SPORT_INFO:
        return None, (
            f"'{planned.workout_type}' tipi için Garmin'e gönderme henüz desteklenmiyor "
            "(şu an koşu/bisiklet/yürüyüş/yüzme/kuvvet destekleniyor)."
        )

    try:
        from garminconnect.workout import (
            CyclingWorkout,
            RunningWorkout,
            StrengthWorkout,
            SwimmingWorkout,
            WalkingWorkout,
            WorkoutSegment,
            create_distance_interval_step,
            create_interval_step,
        )
    except ImportError as exc:  # noqa: BLE001
        return None, f"garminconnect workout modülü yüklenemedi: {exc}"

    workout_classes = {
        "running": RunningWorkout,
        "cycling": CyclingWorkout,
        "walking": WalkingWorkout,
        "swimming": SwimmingWorkout,
        "strength": StrengthWorkout,
    }
    sport_type_id, sport_type_key = _WORKOUT_SPORT_INFO[planned.workout_type]

    if planned.target_distance_m and planned.workout_type != "strength":
        step = create_distance_interval_step(planned.target_distance_m, step_order=1)
        duration_estimate = max(600, int(planned.target_distance_m / 1000 * 390))  # ~6.5 dk/km kaba tahmin
    elif planned.target_duration_s:
        step = create_interval_step(planned.target_duration_s, step_order=1)
        duration_estimate = int(planned.target_duration_s)
    else:
        step = create_interval_step(1800, step_order=1)
        duration_estimate = 1800

    segment = WorkoutSegment(
        segmentOrder=1,
        sportType={"sportTypeId": sport_type_id, "sportTypeKey": sport_type_key, "displayOrder": 1},
        workoutSteps=[step],
    )

    workout_cls = workout_classes[planned.workout_type]
    workout = workout_cls(
        workoutName=planned.title[:80],
        estimatedDurationInSecs=duration_estimate,
        workoutSegments=[segment],
        description=(planned.notes or None),
    )

    try:
        client = _login_from_payload(payload)
        result = client.upload_workout(workout.to_dict())
    except Exception as exc:  # noqa: BLE001
        return None, f"Garmin'e yükleme başarısız: {exc}"

    workout_id = None
    if isinstance(result, dict):
        workout_id = (
            result.get("workoutId")
            or result.get("workoutID")
            or (result.get("workout") or {}).get("workoutId")
        )
    if workout_id is None:
        return None, f"Garmin workout ID alınamadı. Yanıt: {str(result)[:300]}"

    try:
        client.schedule_workout(workout_id, planned.date.isoformat())
    except Exception as exc:  # noqa: BLE001
        return str(workout_id), f"Antrenman yüklendi ama takvime eklenemedi: {exc}"

    return str(workout_id), None


def reschedule_planned_workout(payload: dict, planned: PlannedWorkout, old_date: dt.date) -> tuple[bool, str | None]:
    """Garmin takvimindeki planlı antrenmanı old_date'ten planned.date'e taşır ve
    antrenman adını planned.title ile eşitler (ör. Monday Easy -> Wednesday Easy).
    (garmin_takvimi_degisti_mi, hata_mesajı) döner; ilk değer False ise Garmin'de hiçbir
    şey değişmemiştir ve çağıran taraf Kinetra'daki tarihi de geri almalıdır.

    Önce yeni güne eklenir, sonra eski günden kaldırılır: arada bir adım başarısız
    olursa antrenman takvimden tamamen kaybolmaz, en kötü ihtimalle iki günde birden görünür."""
    workout_id = int(planned.garmin_workout_id)
    try:
        client = _login_from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"Garmin'e bağlanılamadı: {exc}"

    try:
        client.schedule_workout(workout_id, planned.date.isoformat())
    except Exception as exc:  # noqa: BLE001
        return False, f"Garmin takvimine eklenemediği için taşınmadı, tekrar dene: {exc}"

    try:
        _unschedule_on(client, workout_id, old_date)
    except Exception as exc:  # noqa: BLE001
        return True, f"Yeni tarihe eklendi ama eski tarihten kaldırılamadı, Garmin'de elle silmen gerekebilir: {exc}"

    try:
        workout = client.get_workout_by_id(workout_id)
        new_name = planned.title[:80]
        if isinstance(workout, dict) and workout.get("workoutName") != new_name:
            workout["workoutName"] = new_name
            client.update_workout(workout_id, workout)
    except Exception as exc:  # noqa: BLE001
        logger.info("Garmin antrenman adı güncellenemedi (taşıma yine de başarılı): %s", exc)

    return True, None



def _unschedule_on(client, workout_id: int, day: dt.date) -> int:
    """Takvim kaydının kendi ID'si Kinetra'da saklanmıyor; ay takviminden workoutId + tarih ile
    bulunup kaldırılır. Kaldırılan kayıt sayısını döner."""
    removed = 0
    for item in client.get_scheduled_workouts(day.year, day.month).get("calendarItems", []):
        if (
            item.get("itemType") == "workout"
            and str(item.get("workoutId")) == str(workout_id)
            and item.get("date") == day.isoformat()
        ):
            client.unschedule_workout(item["id"])
            removed += 1
    return removed


def unschedule_planned_workout(payload: dict, planned: PlannedWorkout) -> str | None:
    """Yapılmayan antrenmanı Garmin takviminden kaldırır (şablon kütüphanede kalır). Hata mesajı veya None."""
    try:
        client = _login_from_payload(payload)
        _unschedule_on(client, int(planned.garmin_workout_id), planned.date)
    except Exception as exc:  # noqa: BLE001
        return f"Garmin takviminden kaldırılamadı: {exc}"
    return None
