"""Kinetra Telegram botları: her profilin kendi botu ve grubu.

Her profilin bot token'ı Ayarlar → Telegram'dan girilir ve şifreli saklanır (eski kurulumlar
için .env'deki TELEGRAM_BOT_TOKEN ortak bot olarak da çalışır). Uzun yoklama (getUpdates)
kullanılır: sunucu Telegram'a kendisi bağlandığı için Kinetra'nın internete açılması gerekmez.
Bir grup /bagla KOD ile bağlanır; bir profilin botu sadece o profili bağlayabilir ve sadece o
profilin grubuna cevap verir.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from . import i18n
from .config import settings
from .crypto import decrypt_payload, encrypt_payload
from .db import SessionLocal
from .models import Credential, HealthRecord, PlannedWorkout, PlanSuggestion, Profile

logger = logging.getLogger("fitdash.telegram")

_MAX_TEXT = 3900  # Telegram sınırı 4096; HTML etiketleri için pay
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram")
_pollers: dict[str, tuple[threading.Thread, threading.Event]] = {}
_pollers_lock = threading.Lock()


# --------------------------------------------------------------------------- token / API

def shared_token() -> str | None:
    """Eski kurulum: .env'deki tek ortak bot (profil botu olmayan profiller için)."""
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def profile_bot(profile: Profile) -> dict | None:
    """Profilin kendi botu: {"token", "username"} (şifreli saklanır)."""
    if not profile.telegram_bot_encrypted:
        return None
    try:
        data = decrypt_payload(profile.telegram_bot_encrypted)
    except Exception:  # noqa: BLE001
        return None
    return data if data.get("token") else None


def token_for(profile: Profile) -> str | None:
    bot = profile_bot(profile)
    return bot["token"] if bot else shared_token()


def enabled() -> bool:
    if shared_token():
        return True
    db = SessionLocal()
    try:
        return db.query(Profile).filter(Profile.telegram_bot_encrypted.isnot(None)).count() > 0
    finally:
        db.close()


def api(method: str, token: str, http_timeout: float = 30, **params) -> dict | None:
    try:
        resp = requests.post(f"{settings.telegram_api_base}/bot{token}/{method}", json=params, timeout=http_timeout)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Telegram %s başarısız: %s", method, exc)
        return None
    if not data.get("ok"):
        logger.warning("Telegram %s hata: %s", method, data.get("description"))
        return None
    return data.get("result")


def save_profile_bot(db, profile: Profile, token: str, lang: str = "tr") -> str:
    """Token'ı doğrular, varsa başka bir servise (ör. Manybot) giden webhook'u kaldırır ve şifreli
    kaydeder. Kullanıcıya gösterilecek sonuç metnini döner."""
    token = token.strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        return i18n.t(lang, "Token biçimi geçersiz. BotFather'ın verdiği 123456789:ABC... biçimindeki değeri gir.")
    me = api("getMe", token)
    if not me:
        return i18n.t(lang, "Telegram bu token'ı kabul etmedi. BotFather'dan yeni token aldıysan eskisini değil yenisini gir.")
    other = next((p for p in db.query(Profile).filter(Profile.id != profile.id).all()
                  if (profile_bot(p) or {}).get("token") == token), None)
    if other:
        return i18n.t(lang, "Bu bot zaten {name} profiline tanımlı. Her profil için ayrı bot kullan.", name=other.name)
    notes = [i18n.t(lang, "@{bot} kaydedildi.", bot=me["username"])]
    info = api("getWebhookInfo", token) or {}
    if info.get("url"):
        from urllib.parse import urlparse
        api("deleteWebhook", token)
        notes.append(i18n.t(lang, "Botun {host} webhook'u kaldırıldı; mesajlar artık Kinetra'ya geliyor.", host=urlparse(info["url"]).hostname))
    if profile_bot(profile) and profile_bot(profile)["token"] != token:
        profile.telegram_chat_id = None  # farklı bot: grup yeniden /bagla ile bağlanmalı
    profile.telegram_bot_encrypted = encrypt_payload({"token": token, "username": me["username"]})
    db.commit()
    refresh_pollers()
    return " ".join(notes)


def remove_profile_bot(db, profile: Profile) -> None:
    profile.telegram_bot_encrypted = None
    profile.telegram_chat_id = None
    profile.telegram_link_code = None
    db.commit()
    refresh_pollers()


# --------------------------------------------------------------------------- biçimlendirme

def md_to_html(text: str) -> str:
    """Koçun Markdown cevabını Telegram'ın desteklediği HTML alt kümesine çevirir."""
    out, table = [], []

    def flush_table():
        if table:
            out.append("<pre>" + "\n".join(html.escape(r, quote=False) for r in table) + "</pre>")
            table.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            if not re.fullmatch(r"\s*\|[\s:|-]+\|\s*", line):  # ayraç satırını atla
                table.append(line.strip())
            continue
        flush_table()
        esc = html.escape(line, quote=False)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        m = re.match(r"#{1,4}\s+(.*)", esc)
        if m:
            esc = f"<b>{m.group(1)}</b>"
        esc = re.sub(r"^(\s*)[-*]\s+", r"\1• ", esc)
        out.append(esc)
    flush_table()
    return "\n".join(out).strip()


def _chunks(text: str) -> list[str]:
    parts, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > _MAX_TEXT and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + para
    if current:
        parts.append(current)
    return parts or [""]


def send(token: str, chat_id: str, markdown: str, buttons: list[list[dict]] | None = None) -> str | None:
    """Mesajı gönderir (gerekirse böler); butonlar son parçaya eklenir. Son mesajın id'sini döner."""
    message_id = None
    pieces = _chunks(md_to_html(markdown))
    for i, piece in enumerate(pieces):
        params = {"chat_id": chat_id, "text": piece, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons and i == len(pieces) - 1:
            params["reply_markup"] = {"inline_keyboard": buttons}
        result = api("sendMessage", token, **params)
        if result is None:  # HTML reddedildiyse düz metin dene
            params.pop("parse_mode")
            params["text"] = re.sub(r"<[^>]+>", "", piece)
            result = api("sendMessage", token, **params)
        if result:
            message_id = str(result["message_id"])
    return message_id


def send_to_profile(profile: Profile, markdown: str, buttons: list[list[dict]] | None = None) -> str | None:
    token = token_for(profile)
    if not token or not profile.telegram_chat_id:
        return None
    return send(token, profile.telegram_chat_id, markdown, buttons)


def typing(profile: Profile) -> None:
    token = token_for(profile)
    if token and profile.telegram_chat_id:
        api("sendChatAction", token, chat_id=profile.telegram_chat_id, action="typing")


# --------------------------------------------------------------------------- koç akışları

def T(profile: Profile, text: str, **kwargs) -> str:
    """Profilin diline çevirir (Telegram mesajları ve butonları)."""
    return i18n.t(i18n.normalize(profile.language), text, **kwargs)


def suggestion_buttons(suggestion_id: str, profile: Profile) -> list[list[dict]]:
    return [[
        {"text": T(profile, "✅ Plana ekle + Garmin"), "callback_data": f"ps:{suggestion_id}:ok"},
        {"text": T(profile, "❌ Atla"), "callback_data": f"ps:{suggestion_id}:no"},
    ]]


def send_coach_reply(db, profile: Profile, result: dict) -> None:
    """Koç cevabını gruba yazar; plan satırı varsa onay butonlarıyla öneri olarak ekler."""
    from .routers.coach import extract_plan_lines

    if result["t"] == "error":
        send_to_profile(profile, f"⚠️ {result['message']}")
        return
    text = result.get("text", "")
    lines = extract_plan_lines(text)
    buttons = suggestion = None
    if lines:
        suggestion = PlanSuggestion(profile_id=profile.id, plan_text="\n".join(lines))
        db.add(suggestion)
        db.commit()
        buttons = suggestion_buttons(suggestion.id, profile)
    message_id = send_to_profile(profile, text or T(profile, "(boş yanıt)"), buttons)
    if suggestion and message_id:
        suggestion.telegram_message_id = message_id
        db.commit()


def apply_suggestion(db, suggestion: PlanSuggestion) -> str:
    """Onaylanan öneriyi plana ekler ve uygun olanları Garmin'e gönderir; sonuç metnini döner."""
    from .integrations import garmin as garmin_integration
    from .routers.coach import _GARMIN_PUSHABLE_TYPES, parse_plan_text, replace_existing_plans

    rows, _ = parse_plan_text(suggestion.plan_text)
    credential = (
        db.query(Credential).filter(Credential.profile_id == suggestion.profile_id, Credential.source == "garmin").first()
    )
    payload = decrypt_payload(credential.encrypted_payload) if credential else None
    owner = db.get(Profile, suggestion.profile_id)
    notes = [T(owner, "♻️ {label}: yerine yenisi geldi", label=label) for label in
             replace_existing_plans(db, suggestion.profile_id, {row["date"] for row in rows}, payload)]
    for row in rows:
        workout = PlannedWorkout(profile_id=suggestion.profile_id, **row)
        label = f"{row['date']:%d.%m} {row['title']}"
        if row["workout_type"] not in _GARMIN_PUSHABLE_TYPES:
            notes.append(T(owner, "📅 {label}: plana eklendi (dinlenme/diğer — Garmin'e gönderilmez)", label=label))
        elif payload is None:
            notes.append(T(owner, "📅 {label}: plana eklendi, Garmin bağlı olmadığı için gönderilmedi", label=label))
        else:
            workout_id, error = garmin_integration.push_planned_workout(payload, workout)
            workout.garmin_workout_id = workout_id
            workout.garmin_error = error
            notes.append(T(owner, "⌚ {label}: Garmin'e eklendi", label=label) if workout_id and not error
                         else T(owner, "⚠️ {label}: plana eklendi, Garmin hatası: {error}", label=label, error=error))
        db.add(workout)
    suggestion.status = "accepted"
    suggestion.result = "\n".join(notes) or T(owner, "Satır okunamadı.")
    db.commit()
    return suggestion.result


def workout_label(w: PlannedWorkout, profile: Profile | None = None) -> str:
    bits = [f"{w.date:%d.%m}", w.title]
    if w.target_distance_m:
        bits.append(f"{w.target_distance_m / 1000:g} km")
    if w.target_duration_s:
        bits.append(f"{int(w.target_duration_s // 60)} " + (T(profile, "dk") if profile else "dk"))
    return " · ".join(bits) + (" ⌚" if w.garmin_workout_id else "")


def send_workout_card(profile: Profile, w: PlannedWorkout, intro: str = "") -> None:
    """Planlı antrenman + Yapıldı / Yapılmadı / Yarına taşı butonları."""
    text = (intro + "\n" if intro else "") + f"📅 **{workout_label(w, profile)}**" + (f"\n{w.notes}" if w.notes else "")
    send_to_profile(profile, text, [[
        {"text": T(profile, "✅ Yapıldı"), "callback_data": f"pw:{w.id}:done"},
        {"text": T(profile, "❌ Yapılmadı"), "callback_data": f"pw:{w.id}:skip"},
        {"text": T(profile, "📅 Yarına taşı"), "callback_data": f"pw:{w.id}:tom"},
    ]])


def _workout_action(db, profile: Profile, w: PlannedWorkout, action: str) -> tuple[str, list | None]:
    """Buton aksiyonu uygular; (cevap metni, varsa ek butonlar) döner."""
    from .integrations import garmin as garmin_integration
    from .routers.coach import active_plans, move_planned_workout

    replan = [[{"text": T(profile, "🔄 Koç yeniden planlasın"), "callback_data": f"rp:{w.id}"}]]
    if action == "done":
        w.status, w.status_at = "done", dt.datetime.utcnow()
        db.commit()
        if not active_plans(db, profile.id).filter(PlannedWorkout.date > max(w.date, dt.date.today())).first():
            return (T(profile, "✅ {title} yapıldı olarak işaretlendi. Sonrası için plan yok.", title=w.title),
                    [[{"text": T(profile, "🔄 Koç sıradakini planlasın"), "callback_data": f"rp:{w.id}"}]])
        return T(profile, "✅ {title} yapıldı olarak işaretlendi.", title=w.title), None
    if action == "skip":
        w.status, w.status_at = "skipped", dt.datetime.utcnow()
        note = ""
        if w.garmin_workout_id:
            credential = db.query(Credential).filter(Credential.profile_id == profile.id, Credential.source == "garmin").first()
            error = garmin_integration.unschedule_planned_workout(decrypt_payload(credential.encrypted_payload), w) if credential else T(profile, "Garmin bağlı değil.")
            note = " " + T(profile, "Garmin takviminden kaldırıldı.") if not error else f" ⚠️ {error}"
        db.commit()
        return T(profile, "❌ {title} ({date}) yapılmadı olarak işaretlendi.{note} Koç sonraki önerilerinde bunu dikkate alacak.",
                 title=w.title, date=f"{w.date:%d.%m}", note=note), replan
    if action == "tom":
        target = max(w.date, dt.date.today()) + dt.timedelta(days=1)
        old = w.date
        move_planned_workout(db, w, target)
        if w.date == old:
            return T(profile, "⚠️ Taşınamadı: {error}", error=w.garmin_error), None
        clash = [o for o in active_plans(db, profile.id).filter(PlannedWorkout.date == target).all() if o.id != w.id]
        text = T(profile, "📅 {title} {date} tarihine taşındı", title=w.title, date=f"{target:%d.%m}") + (
            " " + T(profile, "(Garmin takvimi güncellendi).") if w.garmin_workout_id and not w.garmin_error else ".")
        if w.garmin_error:
            text += f"\n⚠️ {w.garmin_error}"
        if clash:
            text += "\n" + T(profile, "O gün zaten planlı: {titles}. İstersen koç yeniden planlasın.", titles=", ".join(o.title for o in clash))
            return text, replan
        return text, None
    return T(profile, "Bilinmeyen işlem."), None


def new_link_code(db, profile: Profile) -> str:
    profile.telegram_link_code = f"{secrets.randbelow(900000) + 100000}"
    db.commit()
    return profile.telegram_link_code


def record_doctor_note(db, profile: Profile, note: str, source: str, record_date: dt.date | None = None) -> None:
    """Doktor notunu kaydeder; koça planın buna göre nasıl değişeceğini sorar ve gruba yazar."""
    from . import coach_chat

    db.add(HealthRecord(profile_id=profile.id, kind="doctor_note", text_note=note,
                        record_date=record_date or dt.date.today(), source=source))
    db.commit()
    prompt = (f"[Doktor notu eklendi] {note}\n\nBu nota göre antrenman kurallarının ve planın nasıl değişmesi gerektiğini "
              "kısaca değerlendir. Koç talimatlarındaki hangi kuralın artık geçerli olmadığını açıkça yaz.")
    send_to_profile(profile, T(profile, "🩺 Doktor notu kaydedildi, koç planı gözden geçiriyor…"))
    typing(profile)
    result = coach_chat.reply(db, profile, prompt, display_text=T(profile, "🩺 Doktor notu: {note}", note=note), channel=source)
    send_coach_reply(db, profile, result)


WORKOUT_TYPE_LABELS_TR = {"running": "koşu", "cycling": "bisiklet", "walking": "yürüyüş", "swimming": "yüzme", "strength": "kuvvet", "other": "diğer"}


_HELP = """**Kinetra Koç** — bu grup {name} profiline bağlı.
• Mesaj yaz: koça gider, cevap buraya gelir (Kinetra'daki sohbetle aynı geçmiş).
• Tahlil/rapor PDF'i veya fotoğrafı gönder: türünü seçince Claude yorumlar.
• /doktor <not> — doktorun söylediklerini kaydet, koç planı buna göre günceller.
• /plan — planlı antrenmanlar (✅ Yapıldı / ❌ Yapılmadı / 📅 Yarına taşı)
• /hafta — haftalık hedef durumu (koşu / uzun koşu / kuvvet) · /saglik — kontrol tarihleri
• Her akşam {hour}:00'de günün değerlendirmesi ve yarının önerisi; Pazar akşamı gelecek haftanın planı gelir."""


def _help(profile: Profile) -> str:
    return T(profile, _HELP, name=profile.name, hour=settings.evening_check_hour)


def _profiles_for_token(db, token: str) -> list[Profile]:
    """Bu botun hizmet verebileceği profiller: kendi botu bu olanlar; ortak botsa kendi botu olmayanlar."""
    own = [p for p in db.query(Profile).all() if (profile_bot(p) or {}).get("token") == token]
    if own:
        return own
    if token == shared_token():
        return [p for p in db.query(Profile).all() if not profile_bot(p)]
    return []


def _handle_message(msg: dict, token: str) -> None:
    from . import coach_chat, health

    chat_id = str(msg["chat"]["id"])
    if msg.get("from", {}).get("is_bot"):
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    db = SessionLocal()
    try:
        allowed = _profiles_for_token(db, token)
        if text.startswith("/bagla") or text.startswith("/link"):
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            profile = next((p for p in allowed if code and p.telegram_link_code == code), None)
            if profile is None:
                api("sendMessage", token, chat_id=chat_id, text="Kod geçersiz. Kinetra → Ayarlar → Telegram'dan yeni kod al. / Invalid code. Get a new one in Kinetra → Settings → Telegram.")
                return
            profile.telegram_chat_id = chat_id
            profile.telegram_link_code = None
            db.commit()
            send(token, chat_id, T(profile, "✅ Bu grup **{name}** profiline bağlandı.", name=profile.name) + "\n\n" + _help(profile))
            return

        profile = next((p for p in allowed if p.telegram_chat_id == chat_id), None)
        if profile is None:
            return  # bu bota bağlı olmayan gruplarda sessiz kal

        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/yardim", "/help"):
            send(token, chat_id, _help(profile))
            return
        if command in ("/hafta", "/week"):
            from . import training

            send(token, chat_id, training.telegram_week_text(db, profile))
            return

        if command == "/plan":
            from .routers.coach import active_plans

            today = dt.date.today()
            upcoming = active_plans(db, profile.id).filter(PlannedWorkout.date >= today).order_by(PlannedWorkout.date).all()
            if not upcoming:
                send(token, chat_id, T(profile, "Planlı antrenman yok."))
                return
            later = [w for w in upcoming if w.date > today + dt.timedelta(days=1)]
            for w in upcoming:
                if w not in later:
                    send_workout_card(profile, w)
            if later:
                send(token, chat_id, f"**{T(profile, 'Sonraki günler')}**\n" + "\n".join(f"• {workout_label(w, profile)}" for w in later))
            return
        if command in ("/saglik", "/health"):
            items = health.schedule_status(db, profile)
            lines = [
                "• " + T(profile, "{label}: son {last}, sıradaki {due}", label=T(profile, i["label"]),
                         last=f"{i['last_date']:%d.%m.%Y}", due=f"{i['due_date']:%d.%m.%Y}") if i["last_date"]
                else "• " + T(profile, "{label}: son tarih kayıtlı değil", label=T(profile, i["label"])) for i in items
            ]
            send(token, chat_id, f"**{T(profile, 'Sağlık kontrolleri')}**\n" + "\n".join(lines))
            return
        if command in ("/doktor", "/doctor"):
            parts = text.split(maxsplit=1)
            note = parts[1].strip() if len(parts) > 1 else ""
            if not note:
                send(token, chat_id, T(profile, "Kullanım: /doktor Kontrol normal, tempo antrenmanları serbest."))
                return
            record_doctor_note(db, profile, note, source="telegram")
            return

        file_info = None
        if msg.get("photo"):
            file_info = (msg["photo"][-1]["file_id"], "image/jpeg")
        elif msg.get("document") and msg["document"].get("mime_type") in health.SUPPORTED_MIME:
            file_info = (msg["document"]["file_id"], msg["document"]["mime_type"])
        if file_info:
            _receive_file(db, profile, token, file_info, text)
            return

        if not text or text.startswith("/"):
            return
        typing(profile)
        result = coach_chat.reply(db, profile, text, channel="telegram")
        send_coach_reply(db, profile, result)
    finally:
        db.close()


def _receive_file(db, profile: Profile, token: str, file_info: tuple[str, str], caption: str) -> None:
    from . import health

    file_id, mime = file_info
    meta = api("getFile", token, file_id=file_id)
    if not meta:
        send_to_profile(profile, T(profile, "⚠️ Dosya Telegram'dan alınamadı."))
        return
    try:
        resp = requests.get(f"{settings.telegram_api_base}/file/bot{token}/{meta['file_path']}", timeout=60)
        resp.raise_for_status()
        rel = health.store_file(profile.id, resp.content, mime)
    except (requests.RequestException, ValueError) as exc:
        send_to_profile(profile, T(profile, "⚠️ Dosya kaydedilemedi: {error}", error=T(profile, str(exc))))
        return
    record = HealthRecord(profile_id=profile.id, kind=None, file_path=rel, mime_type=mime,
                          text_note=caption or None, source="telegram")
    db.add(record)
    db.commit()
    send_to_profile(profile, T(profile, "Bu dosya ne?"), [
        [{"text": T(profile, "🩸 Kan tahlili"), "callback_data": f"hr:{record.id}:blood"},
         {"text": T(profile, "❤️ Kardiyoloji"), "callback_data": f"hr:{record.id}:cardio"}],
        [{"text": T(profile, "📝 Doktor raporu"), "callback_data": f"hr:{record.id}:doctor_note"},
         {"text": T(profile, "💬 Koça sor"), "callback_data": f"hr:{record.id}:chat"}],
        [{"text": T(profile, "❌ Vazgeç"), "callback_data": f"hr:{record.id}:cancel"}],
    ])


def _handle_callback(cb: dict, token: str) -> None:
    from . import coach_chat, health

    chat_id = str(cb["message"]["chat"]["id"])
    message_id = int(cb["message"]["message_id"])
    data = cb.get("data", "")
    db = SessionLocal()
    try:
        profile = next((p for p in _profiles_for_token(db, token) if p.telegram_chat_id == chat_id), None)
        if profile is None:
            api("answerCallbackQuery", token, callback_query_id=cb["id"])
            return
        kind, _, rest = data.partition(":")
        obj_id, _, action = rest.partition(":")

        if kind == "ps":
            s = db.get(PlanSuggestion, obj_id)
            if s is None or s.profile_id != profile.id or s.status != "pending":
                api("answerCallbackQuery", token, callback_query_id=cb["id"], text=T(profile, "Bu öneri zaten işlendi."))
                return
            api("answerCallbackQuery", token, callback_query_id=cb["id"], text=T(profile, "İşleniyor…"))
            if action == "ok":
                result = apply_suggestion(db, s)
            else:
                s.status = "rejected"
                db.commit()
                result = T(profile, "❌ Atlandı.")
            api("editMessageReplyMarkup", token, chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
            send_to_profile(profile, result)
            return

        if kind == "pw":
            w = db.get(PlannedWorkout, obj_id)
            if w is None or w.profile_id != profile.id or w.status in ("done", "skipped"):
                api("answerCallbackQuery", token, callback_query_id=cb["id"], text=T(profile, "Bu antrenman zaten işaretlendi."))
                return
            api("answerCallbackQuery", token, callback_query_id=cb["id"], text=T(profile, "İşleniyor…"))
            api("editMessageReplyMarkup", token, chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
            text, buttons = _workout_action(db, profile, w, action)
            send_to_profile(profile, text, buttons)
            return

        if kind == "rp":
            w = db.get(PlannedWorkout, obj_id)
            api("answerCallbackQuery", token, callback_query_id=cb["id"])
            api("editMessageReplyMarkup", token, chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
            if w is None or w.profile_id != profile.id:
                return
            state = {"skipped": "yapılmadı", "done": "yapıldı"}.get(w.status, f"{w.date:%d.%m} tarihine taşındı")
            prompt = (f"[Plan değişikliği] {w.title} ({WORKOUT_TYPE_LABELS_TR.get(w.workout_type, w.workout_type)}) {state}. "
                      "Plan durumunu, haftalık hedefi (veya yarış dönemi fazını) ve toparlanmayı dikkate alarak haftanın kalanını düzenle: "
                      "sıradaki antrenmanı ve hedef için gerekiyorsa değişen diğer günleri öner. "
                      "Telegram'da okunacak, en fazla 8 satır; sonunda değişen her gün için bir plan satırı.")
            typing(profile)
            result = coach_chat.reply(db, profile, prompt, display_text=T(profile, "🔄 Yeniden planla: {title}", title=w.title), channel="telegram")
            send_coach_reply(db, profile, result)
            return

        if kind == "hr":
            record = db.get(HealthRecord, obj_id)
            if record is None or record.profile_id != profile.id or record.kind is not None:
                api("answerCallbackQuery", token, callback_query_id=cb["id"], text=T(profile, "Bu dosya zaten işlendi."))
                return
            api("answerCallbackQuery", token, callback_query_id=cb["id"])
            api("editMessageReplyMarkup", token, chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
            if action == "cancel":
                health.delete_record(db, record)
                send_to_profile(profile, "Dosya silindi.")
                return
            if action == "chat":
                data_bytes = health.file_bytes(record)
                if data_bytes is None:
                    send_to_profile(profile, T(profile, "⚠️ Dosya bulunamadı."))
                    return
                b64 = base64.standard_b64encode(data_bytes).decode()
                block = ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
                         if record.mime_type == "application/pdf"
                         else {"type": "image", "source": {"type": "base64", "media_type": record.mime_type, "data": b64}})
                question = record.text_note or "Bu görseli yorumla."
                health.delete_record(db, record)  # sağlık kaydı değil; içerik sohbet geçmişinde kalır
                typing(profile)
                result = coach_chat.reply(db, profile, [block, {"type": "text", "text": question}],
                                          display_text=f"🖼️ {question}", channel="telegram")
                send_coach_reply(db, profile, result)
                return
            record.kind = action if action in health.KIND_LABELS else "other"
            db.commit()
            send_to_profile(profile, T(profile, "📄 {kind} kaydedildi, Claude yorumluyor…", kind=T(profile, health.KIND_LABELS[record.kind])))
            typing(profile)
            outcome = health.analyze_record(db, profile, record)
            send_to_profile(profile, outcome["summary"] if outcome["ok"] else f"⚠️ {outcome['message']}")
            return
        api("answerCallbackQuery", token, callback_query_id=cb["id"])
    finally:
        db.close()


def _dispatch(update: dict, token: str) -> None:
    try:
        if "message" in update:
            _handle_message(update["message"], token)
        elif "callback_query" in update:
            _handle_callback(update["callback_query"], token)
    except Exception:  # noqa: BLE001 - bir güncelleme botu durdurmasın
        logger.exception("Telegram güncellemesi işlenemedi")


# --------------------------------------------------------------------------- dinleyiciler

def _poll_loop(token: str, stop: threading.Event) -> None:
    offset = None
    failures = 0
    while not stop.is_set():
        params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        updates = api("getUpdates", token, http_timeout=65, **params)
        if updates is None:
            failures += 1
            stop.wait(min(10 * failures, 300))  # hatada giderek daha uzun bekle (geçersiz token günlüğü doldurmasın)
            continue
        failures = 0
        for update in updates:
            offset = update["update_id"] + 1
            _executor.submit(_dispatch, update, token)


def _wanted_tokens() -> set[str]:
    tokens = set()
    if shared_token():
        tokens.add(shared_token())
    db = SessionLocal()
    try:
        for p in db.query(Profile).filter(Profile.telegram_bot_encrypted.isnot(None)).all():
            bot = profile_bot(p)
            if bot:
                tokens.add(bot["token"])
    finally:
        db.close()
    return tokens


def refresh_pollers() -> None:
    """Tanımlı her bot için bir dinleyici çalıştırır; kaldırılan botların dinleyicisini durdurur."""
    wanted = _wanted_tokens()
    with _pollers_lock:
        for token in list(_pollers):
            thread, stop = _pollers[token]
            if token not in wanted or not thread.is_alive():
                stop.set()
                del _pollers[token]
        for token in wanted - set(_pollers):
            stop = threading.Event()
            thread = threading.Thread(target=_poll_loop, args=(token, stop), name="telegram-poll", daemon=True)
            thread.start()
            _pollers[token] = (thread, stop)
    if wanted:
        logger.info("Telegram dinleyicileri: %d bot", len(wanted))


def start() -> None:
    refresh_pollers()


def stop() -> None:
    with _pollers_lock:
        for _, stop_event in _pollers.values():
            stop_event.set()
        _pollers.clear()
