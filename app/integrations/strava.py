from __future__ import annotations

import datetime as dt
import json
import logging
import time
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from stravalib.client import Client

from ..config import settings
from ..crypto import encrypt_payload
from ..models import Activity, ActivityStream, Credential, Profile
from . import SyncResult, register_sync

logger = logging.getLogger("fitdash.strava")

CALLBACK_PATH = "/settings/strava/callback"

_STREAM_TYPES = ["time", "distance", "latlng", "altitude", "heartrate", "cadence", "watts"]


def callback_domain() -> str:
    """Strava uygulama ayarlarındaki "Authorization Callback Domain" alanına girilecek değer."""
    return urlparse(settings.public_url).hostname or settings.public_url


def _app_credentials(app: dict | None) -> tuple[int, str]:
    """Profilin kendi Strava uygulaması varsa onu, yoksa .env'deki ortak uygulamayı döner."""
    client_id = (app or {}).get("client_id") or settings.strava_client_id
    client_secret = (app or {}).get("client_secret") or settings.strava_client_secret
    if not client_id or not client_secret:
        raise RuntimeError(
            "STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET .env dosyasında tanımlı değil. "
            "strava.com/settings/api adresinden bir API Application oluşturup .env'e ekle."
        )
    return int(client_id), client_secret


def authorize_url(profile_id: str, app: dict | None = None) -> str:
    """Kullanıcının tarayıcısını Strava'nın OAuth onay sayfasına yönlendirecek URL üretir."""
    client_id, _ = _app_credentials(app)
    client = Client()
    return client.authorization_url(
        client_id=client_id,
        redirect_uri=f"{settings.public_url}{CALLBACK_PATH}",
        scope=["activity:read_all"],
        state=profile_id,
    )


def exchange_code(code: str, app: dict | None = None) -> dict:
    """OAuth callback'ten gelen code'u access/refresh token'a çevirir.

    Profile özel uygulama kullanıldıysa client_id/secret token payload'ına da yazılır;
    token yenileme (refresh) aynı uygulamayla yapılmak zorunda."""
    client_id, client_secret = _app_credentials(app)
    client = Client()
    info = client.exchange_code_for_token(client_id=client_id, client_secret=client_secret, code=code)
    payload = dict(info)
    if app and app.get("client_id"):
        payload["client_id"] = str(client_id)
        payload["client_secret"] = client_secret
    return payload


def _sync_stream_for_activity(client: Client, db: Session, activity: Activity) -> None:
    """GPS/HR/kadans zaman serisini bir kere çekip saklar; sonraki senkronlarda
    zaten kaydedilmiş aktiviteler için tekrar API çağrısı yapmaz (rate limit)."""
    existing = db.query(ActivityStream).filter(ActivityStream.activity_id == activity.id).first()
    if existing is not None:
        return
    try:
        raw_streams = client.get_activity_streams(
            int(activity.external_id), types=_STREAM_TYPES, series_type="time"
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Aktivite %s için stream çekilemedi: %s", activity.external_id, exc)
        return
    if not raw_streams:
        return
    data = {key: stream.data for key, stream in raw_streams.items() if stream and stream.data}
    if not data:
        return
    point_count = len(data.get("time") or data.get("distance") or [])
    db.add(ActivityStream(
        activity_id=activity.id,
        point_count=point_count,
        data_json=json.dumps(data),
    ))


def _client_from_payload(payload: dict) -> tuple[Client, dict]:
    client_id, client_secret = _app_credentials(payload)
    client = Client(
        access_token=payload.get("access_token"),
        refresh_token=payload.get("refresh_token"),
        token_expires=payload.get("expires_at"),
    )
    expires_at = payload.get("expires_at") or 0
    if expires_at <= int(time.time()) + 60:
        info = client.refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=payload.get("refresh_token"),
        )
        payload = {**payload, **dict(info)}
        client.access_token = info["access_token"]
    return client, payload


@register_sync("strava")
def sync(profile: Profile, db: Session, credential: Credential, payload: dict) -> SyncResult:
    try:
        client, payload = _client_from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return SyncResult("error", f"Token yenileme başarısız: {exc}. Ayarlar'dan yeniden bağlanman gerekebilir.")

    credential.encrypted_payload = encrypt_payload(payload)

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.sync_lookback_days)
    added = 0
    try:
        for act in client.get_activities(after=since):
            if act.id is None or not act.start_date_local:
                continue
            external_id = str(act.id)
            row = (
                db.query(Activity)
                .filter(Activity.source == "strava", Activity.external_id == external_id)
                .first()
                or Activity(profile_id=profile.id, source="strava", external_id=external_id)
            )
            row.profile_id = profile.id
            row.activity_type = act.type.root if act.type else None
            row.start_time = act.start_date_local.replace(tzinfo=None)
            row.duration_s = float(act.moving_time) if act.moving_time is not None else None
            row.distance_m = float(act.distance) if act.distance is not None else None
            row.calories = float(getattr(act, "calories", None) or 0) or None
            row.avg_hr = float(act.average_heartrate) if act.average_heartrate is not None else None
            row.max_hr = float(act.max_heartrate) if act.max_heartrate is not None else None
            if act.total_elevation_gain is not None:
                row.elevation_gain_m = float(act.total_elevation_gain)
            db.add(row)
            db.flush()  # yeni satırlar için id üretilsin ki stream FK'si eklenebilsin
            added += 1
            _sync_stream_for_activity(client, db, row)
    except Exception as exc:  # noqa: BLE001
        return SyncResult("error", f"Aktivite çekme hatası: {exc}")

    return SyncResult("ok", f"{added} aktivite senkronize edildi.")
