from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import requests
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Activity, BodyMeasurement, Credential, Profile
from . import SyncResult, register_connect, register_sync

logger = logging.getLogger("fitdash.hevy")

BASE_URL = "https://api.hevyapp.com/v1"
PAGE_SIZE = 10
MAX_PAGES = 30


def _headers(api_key: str) -> dict:
    return {"api-key": api_key, "Accept": "application/json"}


@register_connect("hevy")
def connect(form: dict) -> dict:
    api_key = (form.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Hevy API key gerekli (Hevy Pro aboneliği ile uygulama içinden alınır).")
    resp = requests.get(
        f"{BASE_URL}/workouts", params={"page": 1, "pageSize": 1}, headers=_headers(api_key), timeout=15
    )
    if resp.status_code == 401:
        raise ValueError("Hevy API key geçersiz.")
    resp.raise_for_status()
    return {"api_key": api_key}


def _parse_dt(value: str | None) -> dt.datetime | None:
    """Hevy zamanları UTC (Z ekli) döner; Garmin/Strava zaten yerel saat sakladığı için
    kaynaklar arası tutarlılık (ve aktivite gruplama) için burada da yerel saate çeviriyoruz."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(settings.local_timezone)).replace(tzinfo=None)
    return parsed


_MEASUREMENT_FIELD_MAP = {
    "weight_kg": "weight_kg",
    "lean_mass_kg": "lean_mass_kg",
    "fat_percent": "fat_percent",
    "neck_cm": "neck_cm",
    "shoulder_cm": "shoulder_cm",
    "chest_cm": "chest_cm",
    "left_bicep_cm": "left_bicep_cm",
    "right_bicep_cm": "right_bicep_cm",
    "left_forearm_cm": "left_forearm_cm",
    "right_forearm_cm": "right_forearm_cm",
    "abdomen": "abdomen_cm",
    "waist": "waist_cm",
    "hips": "hips_cm",
    "left_thigh": "left_thigh_cm",
    "right_thigh": "right_thigh_cm",
    "left_calf": "left_calf_cm",
    "right_calf": "right_calf_cm",
}


def _sync_body_measurements(profile: Profile, db: Session, api_key: str) -> tuple[int, str | None]:
    added = 0
    page = 1
    try:
        while page <= MAX_PAGES:
            resp = requests.get(
                f"{BASE_URL}/body_measurements",
                params={"page": page, "pageSize": PAGE_SIZE},
                headers=_headers(api_key),
                timeout=20,
            )
            if resp.status_code == 401:
                return added, "Hevy API key geçersiz/süresi dolmuş."
            if resp.status_code == 404:
                # Hevy hesabında Measurements özelliği hiç kullanılmamış olabilir.
                return added, None
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("body_measurements") or []
            if not entries:
                break

            for m in entries:
                external_id = m.get("id")
                date_str = m.get("date")
                if external_id is None or not date_str:
                    continue
                try:
                    measurement_date = dt.date.fromisoformat(date_str)
                except ValueError:
                    continue

                row = (
                    db.query(BodyMeasurement)
                    .filter(BodyMeasurement.source == "hevy", BodyMeasurement.external_id == str(external_id))
                    .first()
                    or BodyMeasurement(profile_id=profile.id, source="hevy", external_id=str(external_id))
                )
                row.profile_id = profile.id
                row.date = measurement_date
                for hevy_key, model_attr in _MEASUREMENT_FIELD_MAP.items():
                    value = m.get(hevy_key)
                    if value is not None:
                        setattr(row, model_attr, float(value))
                db.add(row)
                added += 1

            page_count = data.get("page_count") or data.get("pageCount") or 1
            if page >= page_count:
                break
            page += 1
    except Exception as exc:  # noqa: BLE001
        return added, f"ölçüm senkronizasyon hatası: {exc}"

    return added, None


@register_sync("hevy")
def sync(profile: Profile, db: Session, credential: Credential, payload: dict) -> SyncResult:
    api_key = payload.get("api_key")
    if not api_key:
        return SyncResult("error", "Hevy API key eksik.")

    cutoff = dt.datetime.utcnow() - dt.timedelta(days=settings.sync_lookback_days)
    added = 0
    page = 1

    try:
        while page <= MAX_PAGES:
            resp = requests.get(
                f"{BASE_URL}/workouts",
                params={"page": page, "pageSize": PAGE_SIZE},
                headers=_headers(api_key),
                timeout=20,
            )
            if resp.status_code == 401:
                return SyncResult("error", "Hevy API key geçersiz/süresi dolmuş. Ayarlar'dan yeniden bağlan.")
            resp.raise_for_status()
            data = resp.json()
            workouts = data.get("workouts") or []
            if not workouts:
                break

            stop = False
            for w in workouts:
                start_time = _parse_dt(w.get("start_time"))
                if start_time is None:
                    continue
                if start_time < cutoff:
                    stop = True
                    continue

                external_id = w.get("id")
                if not external_id:
                    continue
                row = (
                    db.query(Activity)
                    .filter(Activity.source == "hevy", Activity.external_id == str(external_id))
                    .first()
                    or Activity(profile_id=profile.id, source="hevy", external_id=str(external_id))
                )
                row.profile_id = profile.id
                row.activity_type = w.get("title") or "Kuvvet Antrenmanı"
                row.start_time = start_time
                end_time = _parse_dt(w.get("end_time"))
                if end_time:
                    row.duration_s = (end_time - start_time).total_seconds()
                try:
                    row.raw_json = json.dumps(w)[:20000]
                except (TypeError, ValueError):
                    row.raw_json = None
                db.add(row)
                added += 1

            page_count = data.get("page_count") or data.get("pageCount") or 1
            if stop or page >= page_count:
                break
            page += 1
    except Exception as exc:  # noqa: BLE001
        return SyncResult("error", f"Hevy senkronizasyon hatası: {exc}")

    measurements_added, measurements_error = _sync_body_measurements(profile, db, api_key)

    message = f"{added} kuvvet antrenmanı, {measurements_added} vücut ölçümü senkronize edildi."
    if measurements_error:
        message += f" ({measurements_error})"
    return SyncResult("ok", message)
