from __future__ import annotations

import datetime as dt
import logging

from requests.cookies import RequestsCookieJar
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Credential, NutritionLog, Profile
from . import SyncResult, register_connect, register_sync

logger = logging.getLogger("fitdash.myfitnesspal")

# myfitnesspal 2.x kullanıcı adı/şifre ile girişi kaldırdı; sadece tarayıcıdan
# kopyalanan bir çerezle (cookiejar) veya yerel tarayıcı çerezleriyle çalışıyor.
# Sunucuda tarayıcı olmadığı için kullanıcı çerezi Ayarlar formundan elle yapıştırır.


def _cookiejar_from_string(cookie_string: str) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            jar.set(name, value, domain=".myfitnesspal.com", path="/")
    return jar


def _client_from_cookie_string(cookie_string: str):
    try:
        import myfitnesspal  # gecikmeli import: lxml/cloudscraper sadece bu entegrasyon kullanılırsa gerekli
    except ImportError as exc:
        raise RuntimeError(
            "myfitnesspal paketi kurulu değil. `venv/bin/pip install --no-deps myfitnesspal` "
            "ve ardından `venv/bin/pip install blessed rich browser_cookie3 cloudscraper "
            "measurement lxml` çalıştır (bkz. README Kurulum)."
        ) from exc

    jar = _cookiejar_from_string(cookie_string)
    return myfitnesspal.Client(cookiejar=jar)


def _close_client(client) -> None:
    """myfitnesspal.Client, cloudscraper ile sarmalanmış kendi requests.Session'ını
    (self.session) hiç kapatmıyor. Bu session'ı her sync() çağrısında yeniden oluşturup
    kapatmadan bırakmak, Cloudflare'e (myfitnesspal.com'un CDN'i) açılan TCP bağlantılarının
    CLOSE-WAIT durumunda sonsuza kadar birikmesine yol açıyor — 5 dakikada bir çalışan
    zamanlanmış senkron ile bu birikim bir hafta içinde process'in dosya tanıtıcısı (fd)
    limitini (1024) doldurup TÜM uygulamayı (DB dahil) çökertti. Bu yüzden session'ı
    her zaman açıkça kapatıyoruz."""
    session = getattr(client, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


@register_connect("mfp")
def connect(form: dict) -> dict:
    cookie_string = (form.get("cookie_string") or "").strip()
    if not cookie_string:
        raise ValueError(
            "MyFitnessPal çerez (cookie) değeri gerekli. Tarayıcında myfitnesspal.com'a "
            "giriş yapıp DevTools > Network'ten bir isteğin Cookie başlığını kopyala."
        )
    client = None
    try:
        client = _client_from_cookie_string(cookie_string)
        _ = client.effective_username
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Çerez ile giriş doğrulanamadı: {exc}") from exc
    finally:
        if client is not None:
            _close_client(client)
    return {"cookie_string": cookie_string}


@register_sync("mfp")
def sync(profile: Profile, db: Session, credential: Credential, payload: dict) -> SyncResult:
    cookie_string = payload.get("cookie_string")
    if not cookie_string:
        return SyncResult("error", "MyFitnessPal çerezi eksik.")

    try:
        client = _client_from_cookie_string(cookie_string)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(
            "error",
            f"Giriş başarısız (çerez süresi dolmuş olabilir): {exc}. Ayarlar'dan çerezi güncelle.",
        )

    try:
        today = dt.date.today()
        added = 0
        errors = 0
        for offset in range(settings.sync_lookback_days):
            day_date = today - dt.timedelta(days=offset)
            try:
                day = client.get_date(day_date)
                totals = day.totals or {}
                goals = day.goals or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("MFP gün verisi alınamadı %s: %s", day_date, exc)
                errors += 1
                continue

            if not totals:
                continue

            row = (
                db.query(NutritionLog)
                .filter(
                    NutritionLog.profile_id == profile.id,
                    NutritionLog.date == day_date,
                    NutritionLog.source == "mfp",
                )
                .first()
                or NutritionLog(profile_id=profile.id, date=day_date, source="mfp")
            )
            row.calories = totals.get("calories")
            row.protein_g = totals.get("protein")
            row.carbs_g = totals.get("carbohydrates")
            row.fat_g = totals.get("fat")
            row.sodium_mg = totals.get("sodium")
            row.sugar_g = totals.get("sugar")
            row.fiber_g = totals.get("fiber")
            row.cholesterol_mg = totals.get("cholesterol")
            if goals:
                row.calories_goal = goals.get("calories")
                row.protein_goal_g = goals.get("protein")
                row.carbs_goal_g = goals.get("carbohydrates")
                row.fat_goal_g = goals.get("fat")
                row.sodium_goal_mg = goals.get("sodium")
                row.sugar_goal_g = goals.get("sugar")
                row.fiber_goal_g = goals.get("fiber")
                row.cholesterol_goal_mg = goals.get("cholesterol")
            db.add(row)
            added += 1
    finally:
        _close_client(client)

    if added == 0 and errors > 0:
        return SyncResult(
            "error",
            f"Hiçbir gün için veri alınamadı ({errors} hata). Çerez süresi dolmuş olabilir, Ayarlar'dan güncelle.",
        )

    message = f"{added} gün beslenme verisi senkronize edildi."
    if errors:
        message += f" ({errors} gün alınamadı.)"
    return SyncResult("ok", message)
