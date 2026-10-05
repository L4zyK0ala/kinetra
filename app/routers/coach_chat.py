from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import coach_chat
from ..db import SessionLocal, get_db
from ..models import Profile
from .coach import build_profile_brief, extract_plan_lines

router = APIRouter()

_MAX_MESSAGE_CHARS = 4000


@router.post("/coach/{profile_id}/chat")
async def chat(profile_id: str, request: Request):
    form = await request.form()
    text = (form.get("message") or "").strip()[:_MAX_MESSAGE_CHARS]

    def events():
        # Akış yanıtı istek bittikten sonra da sürdüğü için kendi DB oturumunu açar.
        db = SessionLocal()
        try:
            profile = db.get(Profile, profile_id)
            if profile is None or not text:
                yield json.dumps({"t": "error", "message": "Mesaj boş veya profil bulunamadı."}, ensure_ascii=False) + "\n"
                return
            thread = coach_chat.current_thread(db, profile)
            # Sadece bu profilin verisi: diğer profilin özeti hiçbir zaman bu isteğe girmez.
            snapshot = build_profile_brief(db, profile, include_prompt=False)
            for event in coach_chat.stream_reply(db, profile, thread, text, snapshot):
                if event["t"] == "done":
                    event["plan_lines"] = extract_plan_lines(event.pop("text"))
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            db.close()

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        # nginx yanıtı tamponlamasın: koçun cevabı yazıldıkça ekrana düşsün.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.post("/coach/{profile_id}/new-thread")
def new_thread(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        coach_chat.start_new_thread(db, profile)
    return RedirectResponse("/coach-brief#koc-sohbet", status_code=303)


@router.post("/coach/{profile_id}/instructions")
def save_instructions(profile_id: str, instructions: str = Form(""), db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        profile.coach_instructions = instructions.strip() or None
        db.add(profile)
        db.commit()
    return RedirectResponse("/coach-brief#koc-talimatlari", status_code=303)
