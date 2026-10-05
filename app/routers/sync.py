from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Credential
from ..scheduler import sync_credential

router = APIRouter()


@router.post("/sync/{profile_id}/{source}")
def trigger_sync(profile_id: str, source: str, db: Session = Depends(get_db)):
    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == source)
        .first()
    )
    if credential is not None:
        sync_credential(db, credential)
    return RedirectResponse(f"/settings?profile={profile_id}", status_code=303)
