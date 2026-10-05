from __future__ import annotations

from fastapi import Cookie, Depends
from sqlalchemy.orm import Session

from .db import get_db
from .models import Profile

COOKIE_NAME = "fitdash_profile"


def get_current_profile(
    fitdash_profile: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> Profile | None:
    if not fitdash_profile:
        return None
    return db.get(Profile, fitdash_profile)
