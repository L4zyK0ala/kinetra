from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .crypto import decrypt_payload
from .db import SessionLocal
from .integrations import SYNC_FUNCS
from .models import Credential, Profile

logger = logging.getLogger("fitdash.scheduler")

_scheduler = BackgroundScheduler(timezone="UTC")

# requests çağrılarının çoğu (özellikle resmi olmayan myfitnesspal kütüphanesi) timeout
# belirtmiyor. requests'te timeout=None ise bağlantı süresiz askıda kalabilir — socket.
# setdefaulttimeout() bile bunu önlemez, çünkü requests timeout verilmediğinde alttaki
# soket üzerinde açıkça settimeout(None) çağırıyor (doğrulandı: kara delik bir IP'ye
# yapılan istek 120s+ boyunca hiç kesilmedi). Bu yama, aksi belirtilmedikçe TÜM
# requests.Session çağrılarına makul bir üst sınır koyar.
_original_session_request = requests.Session.request


def _request_with_default_timeout(self, *args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _original_session_request(self, *args, **kwargs)


requests.Session.request = _request_with_default_timeout

# İkinci savunma katmanı: yukarıdaki yama kapsamadığı bir kütüphane/çağrı yine de askıda
# kalırsa (ör. requests dışı bir mekanizma), APScheduler varsayılan olarak aynı iş için tek
# instance çalıştırdığı (max_instances=1) için TEK bir donan senkronizasyon TÜM gelecekteki
# periyodik senkronları sessizce iptal ettirir. Her kaynağı kendi thread'inde ve sabit bir üst
# sınırla çalıştırıp bunu önlüyoruz — süre aşılırsa o kaynak "zaman aşımı" olarak işaretlenir
# ama sync_all() bir sonraki periyotta yine de zamanında tetiklenmeye devam eder.
_SYNC_TIMEOUT_S = 60


def sync_credential(db, credential: Credential) -> None:
    sync_fn = SYNC_FUNCS.get(credential.source)
    if sync_fn is None:
        return
    profile = db.get(Profile, credential.profile_id)
    if profile is None:
        return
    try:
        payload = decrypt_payload(credential.encrypted_payload)
        result = sync_fn(profile, db, credential, payload)
        credential.last_sync_status = result.status
        credential.last_sync_message = result.message
    except Exception as exc:  # noqa: BLE001 - senkronizasyon hatası servisi çökertmemeli
        logger.exception("Senkronizasyon hatası: profile=%s source=%s", credential.profile_id, credential.source)
        credential.last_sync_status = "error"
        credential.last_sync_message = str(exc)
    credential.last_synced_at = dt.datetime.utcnow()
    db.add(credential)
    db.commit()


def _sync_credential_isolated(credential_id: str) -> None:
    """sync_credential'ı kendi DB session'ıyla çalıştırır — ayrı bir thread'de koşacağı
    için sync_all()'un session'ıyla paylaşılamaz (SQLAlchemy Session thread-safe değil)."""
    db = SessionLocal()
    try:
        credential = db.get(Credential, credential_id)
        if credential is not None:
            sync_credential(db, credential)
    finally:
        db.close()


def sync_all() -> None:
    db = SessionLocal()
    try:
        credential_ids = [c.id for c in db.query(Credential).all()]
    finally:
        db.close()

    if not credential_ids:
        return

    pool = ThreadPoolExecutor(max_workers=len(credential_ids))
    futures = {pool.submit(_sync_credential_isolated, cid): cid for cid in credential_ids}
    for future, credential_id in futures.items():
        try:
            future.result(timeout=_SYNC_TIMEOUT_S)
        except FutureTimeoutError:
            logger.error(
                "Senkronizasyon %ss içinde tamamlanmadı (askıda kalmış olabilir): credential_id=%s",
                _SYNC_TIMEOUT_S, credential_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Senkronizasyon thread hatası: credential_id=%s", credential_id)
    # wait=False: zaman aşımına uğrayan bir thread hâlâ arka planda çalışıyor olabilir;
    # sync_all()'un bir sonraki periyotta zamanında tetiklenebilmesi için burada beklemiyoruz.
    pool.shutdown(wait=False)


def _evening_check() -> None:
    from .daily_check import run_evening_check  # döngüsel import olmasın diye geç import

    run_evening_check()


def start_scheduler() -> None:
    _scheduler.add_job(
        _evening_check,
        CronTrigger(hour=settings.evening_check_hour, minute=0, timezone=settings.local_timezone),
        id="evening_check",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=1800,  # servis o an yeniden başlıyorsa 30 dk içinde yine çalışsın
    )
    _scheduler.add_job(
        sync_all,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="sync_all",
        replace_existing=True,
        next_run_time=dt.datetime.utcnow(),
    )
    _scheduler.start()


def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)
