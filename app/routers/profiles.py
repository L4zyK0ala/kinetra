from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..avatars import AvatarError, delete_avatar, save_avatar
from ..db import get_db
from .. import i18n
from ..deps import COOKIE_NAME
from ..models import Profile
from ..templating import templates

router = APIRouter()


@router.get("/")
def profile_picker(request: Request, db: Session = Depends(get_db)):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})


@router.post("/language")
def set_language(request: Request, lang: str = Form(...), db: Session = Depends(get_db)):
    """Dili değiştirir: seçili profil varsa profile kaydeder (koç ve Telegram da bu dili kullanır)."""
    lang = i18n.normalize(lang)
    profile = db.get(Profile, request.cookies.get(COOKIE_NAME) or "")
    if profile is not None:
        profile.language = lang
        db.commit()
    back = request.headers.get("referer") or "/"
    response = RedirectResponse(back if back.startswith(str(request.base_url)) or back.startswith("/") else "/", status_code=303)
    response.set_cookie(i18n.LANG_COOKIE, lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


@router.post("/profiles")
def create_profile(name: str = Form(...), color: str = Form("#2563eb"), db: Session = Depends(get_db)):
    name = name.strip()
    if name:
        db.add(Profile(name=name, color=color))
        db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/profiles/{profile_id}/select")
def select_profile(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is None:
        return RedirectResponse("/", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        COOKIE_NAME, profile.id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax"
    )
    return response


def _parse_optional_float(value: str) -> float | None:
    cleaned = value.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@router.post("/profiles/{profile_id}/physical-info")
def update_physical_info(
    profile_id: str,
    weight_goal_kg: str = Form(""),
    height_cm: str = Form(""),
    gender: str = Form(""),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        profile.weight_goal_kg = _parse_optional_float(weight_goal_kg)
        profile.height_cm = _parse_optional_float(height_cm)
        profile.gender = gender if gender in ("female", "male") else None
        db.add(profile)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/profiles/{profile_id}/language")
def update_language(profile_id: str, lang: str = Form(...), db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    lang = i18n.normalize(lang)
    if profile is not None:
        profile.language = lang
        db.commit()
    response = RedirectResponse(f"/settings?profile={profile_id}", status_code=303)
    response.set_cookie(i18n.LANG_COOKIE, lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


@router.post("/profiles/{profile_id}/avatar")
async def upload_avatar(profile_id: str, avatar: UploadFile, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None and avatar.filename:
        try:
            profile.avatar_path = save_avatar(profile_id, avatar, profile.avatar_path)
        except AvatarError as exc:
            return RedirectResponse(
                f"/settings?profile={profile_id}&avatar_error={quote(str(exc))}", status_code=303
            )
        db.add(profile)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/profiles/{profile_id}/avatar/delete")
def remove_avatar(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        delete_avatar(profile.avatar_path)
        profile.avatar_path = None
        db.add(profile)
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)


@router.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        db.delete(profile)
        db.commit()
    return RedirectResponse("/", status_code=303)
