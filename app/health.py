"""Sağlık kontrolleri: periyodik hatırlatmalar, tahlil/rapor saklama ve Claude ile yorumlama.

Claude teşhis koymaz: sonuçları önceki kayıtlarla karşılaştırır, doktora sorulacak soruları
hazırlar ve koça antrenman açısından kısa bir not bırakır. Doktor notları koçun tıbbi
kurallarının önüne geçer (bkz. coach_chat._BASE_SYSTEM).
"""
from __future__ import annotations

import base64
import calendar
import datetime as dt
import logging
import re
import uuid
from pathlib import Path

import anthropic
from sqlalchemy.orm import Session

from . import i18n
from .config import settings
from .models import CoachMessage, HealthRecord, HealthSchedule, Profile

logger = logging.getLogger("fitdash.health")

DATA_DIR = Path(__file__).resolve().parent / "data" / "health"

_FORMAT_RULES = {
    "tr": """Yanıtını Türkçe ve tam olarak şu bölümlerle yaz:
TARİH: belgedeki tahlil/muayene tarihi (YYYY-AA-GG); okunamıyorsa "bilinmiyor"
## Özet
3-5 madde.
## Tablo
| Parametre | Değer | Referans | Önceki | Değişim |
(Sadece belgede gerçekten olan değerler; tahmin etme.)
## Yorum
Parametre bazında kısa yorum; önceki sonuçlarla trendi belirt. Teşhis koyma.
## Doktora sorulacak sorular
3-6 soru.
KOÇ İÇİN NOT: antrenmanı etkileyen 1-3 cümle (yoksa "Antrenmana etkisi yok, mevcut kurallar geçerli.")""",
    "en": """Yanıtını İngilizce (English) ve tam olarak şu bölümlerle yaz (başlıkları aynen kullan):
DATE: belgedeki tahlil/muayene tarihi (YYYY-AA-GG); okunamıyorsa "unknown"
## Summary
3-5 madde.
## Table
| Parameter | Value | Reference | Previous | Change |
(Sadece belgede gerçekten olan değerler; tahmin etme.)
## Interpretation
Parametre bazında kısa yorum; önceki sonuçlarla trendi belirt. Teşhis koyma.
## Questions for your doctor
3-6 soru.
COACH NOTE: antrenmanı etkileyen 1-3 cümle (yoksa "No effect on training, current rules apply.")""",
}

KIND_LABELS = {
    "blood": "Kan tahlili",
    "cardio": "Kardiyoloji kontrolü",
    "doctor_note": "Doktor notu / raporu",
    "other": "Diğer sağlık belgesi",
}
DEFAULT_INTERVALS = {"blood": 6, "cardio": 12}
SUPPORTED_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_FILE_BYTES = 20 * 1024 * 1024


def add_months(d: dt.date, months: int) -> dt.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def ensure_schedules(db: Session, profile: Profile) -> list[HealthSchedule]:
    rows = {s.kind: s for s in db.query(HealthSchedule).filter(HealthSchedule.profile_id == profile.id)}
    for kind, months in DEFAULT_INTERVALS.items():
        if kind not in rows:
            rows[kind] = HealthSchedule(profile_id=profile.id, kind=kind, interval_months=months)
            db.add(rows[kind])
    db.commit()
    return [rows[k] for k in DEFAULT_INTERVALS]


def schedule_status(db: Session, profile: Profile, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    out = []
    for s in ensure_schedules(db, profile):
        due = add_months(s.last_date, s.interval_months) if s.last_date else None
        if due is None:
            state = "unknown"
        elif due < today:
            state = "overdue"
        elif (due - today).days <= 21:
            state = "soon"
        else:
            state = "ok"
        out.append({
            "kind": s.kind, "label": KIND_LABELS[s.kind], "interval_months": s.interval_months,
            "last_date": s.last_date, "due_date": due, "state": state,
            "days": (due - today).days if due else None, "schedule": s,
        })
    return out


def due_reminders(db: Session, profile: Profile, now: dt.datetime | None = None) -> list[str]:
    """Zamanı gelen/geçen kontroller için hatırlatma metinleri; aynı kontrol haftada bir kez hatırlatılır."""
    now = now or dt.datetime.utcnow()
    messages = []
    for item in schedule_status(db, profile, now.date()):
        s = item["schedule"]
        if item["state"] not in ("overdue", "soon", "unknown"):
            continue
        if s.last_reminded_at and now - s.last_reminded_at < dt.timedelta(days=7):
            continue
        lang = i18n.normalize(profile.language)
        label = i18n.t(lang, item["label"])
        if item["state"] == "unknown":
            text = "🩺 " + i18n.t(lang, "{label} için son tarih kayıtlı değil. Kinetra → Koç → Sağlık'tan son tarihi girersen her {n} ayda bir hatırlatırım.",
                                 label=label, n=item["interval_months"])
        elif item["state"] == "overdue":
            text = "🩺 " + i18n.t(lang, "{label} zamanı geçti ({days} gün). Son: {last}, her {n} ayda bir yapılmalı. Randevu almayı unutma; sonucu buraya gönderirsen yorumlarım.",
                                 label=label, days=-item["days"], last=f"{item['last_date']:%d.%m.%Y}", n=item["interval_months"])
        else:
            text = "🩺 " + i18n.t(lang, "{label} {days} gün sonra ({due}). Randevu almayı unutma.",
                                 label=label, days=item["days"], due=f"{item['due_date']:%d.%m.%Y}")
        messages.append(text)
        s.last_reminded_at = now
    db.commit()
    return messages


def store_file(profile_id: str, data: bytes, mime_type: str) -> str:
    if mime_type not in SUPPORTED_MIME:
        raise ValueError("Desteklenmeyen dosya türü. PDF veya fotoğraf (JPEG/PNG) gönder.")
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Dosya çok büyük (maks. 20 MB).")
    ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime_type]
    folder = DATA_DIR / profile_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(data)
    return str(path.relative_to(DATA_DIR))


def file_bytes(record: HealthRecord) -> bytes | None:
    if not record.file_path:
        return None
    path = (DATA_DIR / record.file_path).resolve()
    if not path.is_relative_to(DATA_DIR.resolve()) or not path.exists():
        return None
    return path.read_bytes()


def delete_record(db: Session, record: HealthRecord) -> None:
    if record.file_path:
        path = (DATA_DIR / record.file_path).resolve()
        if path.is_relative_to(DATA_DIR.resolve()) and path.exists():
            path.unlink()
    db.delete(record)
    db.commit()


def brief_lines(db: Session, profile: Profile) -> list[str]:
    """Koç özetine eklenen sağlık bölümü: kontrol tarihleri, doktor notları, son yorumların koç notu."""
    lines = []
    for item in schedule_status(db, profile):
        last = f"{item['last_date']:%d.%m.%Y}" if item["last_date"] else "bilinmiyor"
        due = f", sıradaki: {item['due_date']:%d.%m.%Y}" if item["due_date"] else ""
        lines.append(f"- {item['label']}: son {last}{due}")
    notes = (
        db.query(HealthRecord)
        .filter(HealthRecord.profile_id == profile.id, HealthRecord.kind == "doctor_note", HealthRecord.text_note.isnot(None))
        .order_by(HealthRecord.created_at.desc())
        .limit(3)
        .all()
    )
    for n in notes:
        when = f"{n.record_date:%d.%m.%Y}" if n.record_date else f"{n.created_at:%d.%m.%Y}"
        lines.append(f"- Doktor notu ({when}): {n.text_note}")
    for kind in ("blood", "cardio", "doctor_note"):
        rec = (
            db.query(HealthRecord)
            .filter(HealthRecord.profile_id == profile.id, HealthRecord.kind == kind, HealthRecord.coach_note.isnot(None))
            .order_by(HealthRecord.created_at.desc())
            .first()
        )
        if rec:
            when = f"{rec.record_date:%d.%m.%Y}" if rec.record_date else f"{rec.created_at:%d.%m.%Y}"
            lines.append(f"- {KIND_LABELS[kind]} yorumu ({when}): {rec.coach_note}")
    if not lines:
        return []
    return ["## Sağlık Kayıtları (doktor notları tıbbi kuralların önüne geçer)", *lines, ""]


_ANALYSIS_SYSTEM = """Sen {name} için bir sağlık verisi asistanısın; doktor değilsin ve teşhis koymazsın. Görevin, gönderilen {kind} belgesini okuyup kişinin önceki kayıtlarıyla karşılaştırmak, doktoruyla konuşmasına hazırlanmasına yardım etmek ve antrenman koçuna kısa bir not bırakmak.

Kişinin geçmişi ve koç talimatları:
{history}

Önceki kayıtların yorumları:
{previous}

{format_rule}

Acil değerlendirme gerektirebilecek bir bulgu görürsen bunu en başta açıkça yaz ve doktoruna/acil servise başvurmasını öner."""


def analyze_record(db: Session, profile: Profile, record: HealthRecord) -> dict:
    """Belgeyi Claude'a yorumlatır. {"ok": bool, "summary": str, "message": str} döner."""
    from . import coach_chat  # döngüsel import olmasın diye geç import

    lang = i18n.normalize(profile.language)
    if not coach_chat.api_key_configured():
        return {"ok": False, "message": i18n.t(lang, "Claude API anahtarı tanımlı değil.")}
    spent = coach_chat.month_cost(db)
    if spent >= settings.coach_monthly_budget_usd:
        return {"ok": False, "message": i18n.t(lang, "Bu ayki Claude bütçesi doldu (${spent}). Belge kaydedildi ama yorumlanmadı.", spent=f"{spent:.2f}")}
    data = file_bytes(record)
    if data is None:
        return {"ok": False, "message": i18n.t(lang, "Belge dosyası bulunamadı.")}

    previous = (
        db.query(HealthRecord)
        .filter(HealthRecord.profile_id == profile.id, HealthRecord.id != record.id, HealthRecord.ai_summary.isnot(None))
        .order_by(HealthRecord.created_at.desc())
        .limit(4)
        .all()
    )
    prev_text = "\n\n".join(
        f"[{KIND_LABELS.get(p.kind, p.kind)} — {p.record_date or p.created_at.date()}]\n{p.ai_summary}" for p in previous
    ) or "Önceki yorumlanmış kayıt yok."
    b64 = base64.standard_b64encode(data).decode()
    block = (
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
        if record.mime_type == "application/pdf"
        else {"type": "image", "source": {"type": "base64", "media_type": record.mime_type, "data": b64}}
    )
    user_text = f"Ekteki {KIND_LABELS.get(record.kind, 'sağlık belgesi').lower()}ni yorumla."
    if record.text_note:
        user_text += f"\nKişinin notu: {record.text_note}"

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=settings.coach_model,
            max_tokens=32000,
            system=_ANALYSIS_SYSTEM.format(
                name=profile.name, kind=KIND_LABELS.get(record.kind, "sağlık"), format_rule=_FORMAT_RULES[lang],
                history=(profile.coach_instructions or "Kayıtlı geçmiş yok.").strip(), previous=prev_text,
            ),
            messages=[{"role": "user", "content": [block, {"type": "text", "text": user_text}]}],
            output_config={"effort": settings.coach_effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            final = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        logger.warning("Sağlık belgesi yorumlanamadı: %s", exc)
        return {"ok": False, "message": i18n.t(lang, "Claude API hatası ({code}). Belge kaydedildi, sonra tekrar deneyebilirsin.", code=exc.status_code)}
    except anthropic.APIConnectionError:
        return {"ok": False, "message": i18n.t(lang, "Claude API'ye bağlanılamadı. Belge kaydedildi, sonra tekrar deneyebilirsin.")}

    cost = coach_chat._cost(final.usage, final.model)
    thread = coach_chat.current_thread(db, profile)
    db.add(CoachMessage(  # bütçe ve kullanım tablosuna yansısın; sohbet geçmişine girmez
        thread_id=thread.id, profile_id=profile.id, seq=-1, role="health", content_json="[]",
        model=final.model, stop_reason=final.stop_reason, cost_usd=cost,
        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
        cache_read_tokens=final.usage.cache_read_input_tokens, cache_write_tokens=final.usage.cache_creation_input_tokens,
    ))
    if final.stop_reason == "refusal":
        db.commit()
        return {"ok": False, "message": i18n.t(lang, "Claude bu belgeyi yorumlayamadı.")}

    text = "".join(b.text for b in final.content if b.type == "text").strip()
    m = re.search(r"(?:TARİH|DATE):\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            record.record_date = dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    note = re.search(r"(?:KOÇ İÇİN NOT|COACH NOTE):\s*(.+)", text, re.S)
    record.coach_note = note.group(1).strip()[:1000] if note else None
    record.ai_summary = text
    record.cost_usd = cost
    if record.kind in DEFAULT_INTERVALS and record.record_date:
        sched = next(s for s in ensure_schedules(db, profile) if s.kind == record.kind)
        if not sched.last_date or record.record_date > sched.last_date:
            sched.last_date = record.record_date
            sched.last_reminded_at = None
    db.commit()
    return {"ok": True, "summary": text, "cost_usd": cost}
