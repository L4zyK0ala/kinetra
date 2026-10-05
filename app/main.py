from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

# noqa: F401 - importlar CONNECT_FUNCS/SYNC_FUNCS registry'sini doldurur
from .integrations import garmin, hevy, myfitnesspal, strava  # noqa: F401
from . import i18n, telegram_bot
from .db import SessionLocal, init_db
from .deps import COOKIE_NAME
from .models import Profile
from .routers import activities, coach, coach_chat, dashboard, health, nutrition, profiles, settings, strength, sync
from .scheduler import start_scheduler, stop_scheduler

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    telegram_bot.start()
    yield
    telegram_bot.stop()
    stop_scheduler()


app = FastAPI(title="Kinetra", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def resolve_language(request: Request, call_next):
    """İstek dilini belirler: seçili profilin dili → kinetra_lang çerezi → Accept-Language."""
    lang = None
    if not request.url.path.startswith("/static"):
        profile_id = request.cookies.get(COOKIE_NAME)
        if profile_id:
            db = SessionLocal()
            try:
                profile = db.get(Profile, profile_id)
                lang = profile.language if profile else None
            finally:
                db.close()
        lang = lang or request.cookies.get(i18n.LANG_COOKIE) or i18n.from_accept_language(request.headers.get("accept-language"))
    request.state.lang = i18n.normalize(lang)
    return await call_next(request)

app.include_router(profiles.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(activities.router)
app.include_router(strength.router)
app.include_router(nutrition.router)
app.include_router(coach.router)
app.include_router(coach_chat.router)
app.include_router(health.router)
app.include_router(sync.router)
