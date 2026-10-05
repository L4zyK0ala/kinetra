from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import health, i18n, telegram_bot
from ..db import get_db
from ..models import HealthRecord, Profile

router = APIRouter()

_BACK = "/coach-brief?tab=saglik"


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


@router.post("/health/{profile_id}/schedule")
def save_schedule(
    profile_id: str,
    blood_last: str = Form(""),
    blood_interval: int = Form(6),
    cardio_last: str = Form(""),
    cardio_interval: int = Form(12),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        for sched in health.ensure_schedules(db, profile):
            last, interval = (blood_last, blood_interval) if sched.kind == "blood" else (cardio_last, cardio_interval)
            new_last = _parse_date(last)
            if new_last != sched.last_date:
                sched.last_reminded_at = None
            sched.last_date = new_last
            sched.interval_months = max(1, min(int(interval), 36))
        db.commit()
    return RedirectResponse(_BACK, status_code=303)


@router.post("/health/{profile_id}/upload")
async def upload(
    profile_id: str,
    file: UploadFile,
    kind: str = Form("blood"),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, profile_id)
    if profile is None or not file.filename:
        return RedirectResponse(_BACK, status_code=303)
    data = await file.read()
    try:
        rel = health.store_file(profile.id, data, file.content_type or "")
    except ValueError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"{_BACK}&health_error={quote(str(exc))}", status_code=303)
    record = HealthRecord(profile_id=profile.id, kind=kind if kind in health.KIND_LABELS else "other",
                          file_path=rel, mime_type=file.content_type, text_note=note.strip() or None, source="web")
    db.add(record)
    db.commit()
    health.analyze_record(db, profile, record)  # yorum başarısız olsa da belge kayıtlı kalır, listeden tekrar denenir
    return RedirectResponse(_BACK, status_code=303)


@router.post("/health/{profile_id}/doctor-note")
def doctor_note(profile_id: str, note: str = Form(...), record_date: str = Form(""), db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None and note.strip():
        telegram_bot.record_doctor_note(db, profile, note.strip(), source="web", record_date=_parse_date(record_date))
    return RedirectResponse(_BACK, status_code=303)


@router.post("/health/{profile_id}/record/{record_id}/analyze")
def reanalyze(profile_id: str, record_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    record = db.get(HealthRecord, record_id)
    if profile is not None and record is not None and record.profile_id == profile.id and record.file_path:
        health.analyze_record(db, profile, record)
    return RedirectResponse(_BACK, status_code=303)


@router.post("/health/{profile_id}/record/{record_id}/delete")
def delete(profile_id: str, record_id: str, db: Session = Depends(get_db)):
    record = db.get(HealthRecord, record_id)
    if record is not None and record.profile_id == profile_id:
        health.delete_record(db, record)
    return RedirectResponse(_BACK, status_code=303)


@router.get("/health/file/{record_id}")
def view_file(record_id: str, db: Session = Depends(get_db)):
    record = db.get(HealthRecord, record_id)
    if record is None or not record.file_path:
        return RedirectResponse(_BACK, status_code=303)
    path = (health.DATA_DIR / record.file_path).resolve()
    if not path.is_relative_to(health.DATA_DIR.resolve()) or not path.exists():
        return RedirectResponse(_BACK, status_code=303)
    return FileResponse(path, media_type=record.mime_type)


@router.post("/settings/{profile_id}/telegram/code")
def telegram_code(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        telegram_bot.new_link_code(db, profile)
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/bot")
def telegram_bot_save(request: Request, profile_id: str, token: str = Form(""), db: Session = Depends(get_db)):
    from urllib.parse import quote

    profile = db.get(Profile, profile_id)
    message = (telegram_bot.save_profile_bot(db, profile, token, request.state.lang) if profile is not None
               else i18n.t(request.state.lang, "Profil bulunamadı."))
    return RedirectResponse(f"/settings?profile={profile_id}&tg_msg={quote(message)}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/bot/delete")
def telegram_bot_delete(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        telegram_bot.remove_profile_bot(db, profile)
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/unlink")
def telegram_unlink(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        profile.telegram_chat_id = None
        profile.telegram_link_code = None
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)
