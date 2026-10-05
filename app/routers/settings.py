from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..crypto import decrypt_payload, encrypt_payload
from ..db import get_db
from ..integrations import CONNECT_FUNCS, SOURCE_LABELS, SYNC_FUNCS
from ..integrations import strava as strava_integration
from .. import telegram_bot
from ..models import Credential, Profile
from ..templating import templates

router = APIRouter()


def _strava_app(profile: Profile | None) -> dict | None:
    if profile is None or not profile.strava_app_encrypted:
        return None
    try:
        return decrypt_payload(profile.strava_app_encrypted)
    except Exception:  # noqa: BLE001
        return None


def _get_or_create_credential(db: Session, profile_id: str, source: str) -> Credential:
    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == source)
        .first()
    )
    if credential is None:
        credential = Credential(profile_id=profile_id, source=source, encrypted_payload=encrypt_payload({}))
    return credential


@router.get("/settings")
def settings_page(
    request: Request,
    profile: str | None = None,
    avatar_error: str | None = None,
    tg_msg: str | None = None,
    db: Session = Depends(get_db),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if not profiles:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": []})

    active_profile = (db.get(Profile, profile) if profile else None) or profiles[0]

    credentials = {
        c.source: c
        for c in db.query(Credential).filter(Credential.profile_id == active_profile.id).all()
    }
    sources = [
        {
            "key": key,
            "label": label,
            "available": key in SYNC_FUNCS,
            "credential": credentials.get(key),
        }
        for key, label in SOURCE_LABELS.items()
    ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "profiles": profiles,
            "active_profile": active_profile,
            "sources": sources,
            "avatar_error": avatar_error,
            "strava_app": _strava_app(active_profile),
            "strava_callback_domain": strava_integration.callback_domain(),
            "telegram_bot": telegram_bot.profile_bot(active_profile),
            "telegram_shared": bool(telegram_bot.shared_token()),
            "tg_msg": tg_msg,
            "config_evening_hour": telegram_bot.settings.evening_check_hour,
        },
    )


@router.post("/settings/{profile_id}/strava/app")
def save_strava_app(
    profile_id: str,
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, profile_id)
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if profile is not None and client_id.isdigit() and client_secret:
        profile.strava_app_encrypted = encrypt_payload({"client_id": client_id, "client_secret": client_secret})
        db.add(profile)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/settings/{profile_id}/strava/app/delete")
def delete_strava_app(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        profile.strava_app_encrypted = None
        db.add(profile)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.get("/settings/{profile_id}/strava/authorize")
def strava_authorize(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is None:
        return RedirectResponse("/", status_code=303)
    try:
        url = strava_integration.authorize_url(profile_id, _strava_app(profile))
    except Exception as exc:  # noqa: BLE001
        credential = _get_or_create_credential(db, profile_id, "strava")
        credential.last_sync_status = "error"
        credential.last_sync_message = f"Strava yetkilendirme bağlantısı oluşturulamadı: {exc}"
        db.add(credential)
        db.commit()
        return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.get("/settings/strava/callback")
def strava_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    profile_id = state or ""
    if error or not code or not profile_id:
        credential = _get_or_create_credential(db, profile_id, "strava")
        credential.last_sync_status = "error"
        credential.last_sync_message = f"Strava yetkilendirmesi tamamlanamadı: {error or 'kod alınamadı'}"
        db.add(credential)
        db.commit()
        return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)

    try:
        payload = strava_integration.exchange_code(code, _strava_app(db.get(Profile, profile_id)))
    except Exception as exc:  # noqa: BLE001
        credential = _get_or_create_credential(db, profile_id, "strava")
        credential.last_sync_status = "error"
        credential.last_sync_message = f"Token değişimi başarısız: {exc}"
        db.add(credential)
        db.commit()
        return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)

    credential = _get_or_create_credential(db, profile_id, "strava")
    credential.encrypted_payload = encrypt_payload(payload)
    credential.last_sync_status = "ok"
    credential.last_sync_message = "Bağlandı, ilk senkronizasyon bekleniyor."
    db.add(credential)
    db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/settings/{profile_id}/{source}/connect")
async def connect_source(profile_id: str, source: str, request: Request, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    connect_fn = CONNECT_FUNCS.get(source)
    if profile is None or connect_fn is None:
        return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)

    form = dict((await request.form()).items())
    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == source)
        .first()
    )

    try:
        payload = connect_fn(form)
    except Exception as exc:  # noqa: BLE001 - kullanıcıya okunabilir hata göstermek için
        if credential is None:
            credential = Credential(
                profile_id=profile_id, source=source, encrypted_payload=encrypt_payload({})
            )
        credential.last_sync_status = "error"
        credential.last_sync_message = f"Bağlantı başarısız: {exc}"
        db.add(credential)
        db.commit()
        return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)

    if credential is None:
        credential = Credential(profile_id=profile_id, source=source, encrypted_payload=encrypt_payload(payload))
    else:
        credential.encrypted_payload = encrypt_payload(payload)
    credential.last_sync_status = "ok"
    credential.last_sync_message = "Bağlandı, ilk senkronizasyon bekleniyor."
    db.add(credential)
    db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/settings/{profile_id}/{source}/disconnect")
def disconnect_source(profile_id: str, source: str, db: Session = Depends(get_db)):
    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == source)
        .first()
    )
    if credential is not None:
        db.delete(credential)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)
