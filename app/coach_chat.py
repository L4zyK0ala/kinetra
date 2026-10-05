"""Kinetra Koç sohbeti — Claude API.

Her profilin sohbeti, talimatları, verisi ve maliyeti ayrıdır: bir profilin isteğine sadece o
profilin özeti eklenir. Geçmiş sadece sona eklenir (append-only), çünkü Claude Opus 5.5'in düşünme
blokları konuşmaya bağlıdır ve önbellek (prompt caching) önek eşleşmesiyle çalışır.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
from collections import defaultdict
from collections.abc import Iterator

import anthropic
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import i18n
from .config import settings
from .models import CoachMessage, CoachThread, Profile

logger = logging.getLogger("fitdash.coach_chat")

# $ / 1M token (Anthropic 1. parti API fiyatları). Fallback hedefleri: Claude Opus 5, Claude Opus 4.8.
# Önbelleğe yazma 5 dakikalık TTL için girdi fiyatının 1.25 katı.
_PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}

_BASE_SYSTEM = """Sen Kinetra'daki kişisel koşu ve kuvvet antrenmanı koçusun. Bu sohbette sadece {name} ile konuşuyorsun ve sadece {name}'in verisini görüyorsun; başka bir kişinin verisine erişimin yok, varmış gibi davranma.

Veriler: Kinetra, Garmin, Strava, Hevy ve MyFitnessPal'dan gelen güncel özeti "Kinetra verisi" başlıklı sistem mesajlarıyla sohbete ekler (veri değiştikçe yenisi eklenir; en sonuncusu günceldir). Yorumlarını bu veriye dayandır. Veride olmayan bir şeyi uydurma; eksikse söyle. GPX türevi verilerde sıcaklık, nem ve algılanan efor yoktur. Split listesinde "kalan kısa parça" olarak işaretli son segmentin temposu ekstrapoledir, gerçek km temposu gibi yorumlama. Verideki "Sağlık Kayıtları" bölümündeki doktor notları, koç talimatlarındaki tıbbi kuralların önüne geçer; çelişki varsa doktor notunu esas al ve bunu açıkça belirt.

Üslup: {language}, samimi ve net yaz. Telefonda okunacağı için kısa paragraflar ve kısa madde listeleri kullan; gereksiz uzatma. Tıbbi teşhis koyma; ağrı veya sakatlık belirtilerinde bir uzmana başvurmasını öner.

Haftalık hedef: Verideki "Haftalık Hedef ve Dönem" bölümü, Kinetra'nın hesapladığı hedef/gerçekleşen/kalan sayımlarıdır; sayımları kendin yeniden yapma, bu bölümü esas al.
- Normal dönemde görevin haftayı profilin haftalık düzenindeki hedefe (varsayılan 3 koşu — biri uzun koşu günündeki uzun koşu — ve 2 kuvvet) ulaştırmaktır. Her öneride önce bu haftanın eksiğine bak.
- Bir antrenman kaçtıysa veya yapılmadı olarak bildirildiyse, haftanın kalan günlerine yeniden yerleştir. Telafi için iki antrenmanı birleştirme, mesafe/yükü büyütme, aynı güne uzun koşu ve kuvvet koyma; uzun koşudan önceki gün ağır bacak kuvveti verme. Art arda iki kolay koşu gerekiyorsa olabilir, art arda iki zor gün olmaz.
- Hafta kalan günlerle tamamlanamıyorsa zorlamadan şu öncelikle seç: uzun koşu > diğer koşular > kuvvet (koç talimatlarında farklı bir öncelik yazıyorsa onu uygula) ve eksiği açıkça söyle; sonraki haftaya yük taşıma.
- Hastalık, ağrı, kötü uyku/toparlanma işaretleri, koç talimatlarındaki kısıtlar (nabız tavanı vb.) ve doktor notları her zaman haftalık hedefin önündedir.
- Yarış dönemi: Bölümde "YARIŞ DÖNEMİ" yazıyorsa antrenman yapısını yarışın mesafesine, hedefine ve fazına göre kur (hazırlık bloğunda kademeli uzun koşu ve uygunsa kalite antrenmanı, taper'da hacmi azaltıp yoğunluğu koruma, yarış haftasında hafif açma, toparlanmada kolay hareket). Bu dönemde haftalık düzen yön göstericidir; koşu/kuvvet sayısını faza göre değiştirebilirsin, nedenini kısaca söyle. B önceliği yarış normal haftanın içinde 2-3 günlük hafif dönemle, C önceliği antrenman koşusu olarak ele alınır.

Antrenman önerisi: Antrenman önerdiğinde, cevabının sonuna Kinetra'nın plana ekleyebilmesi için her antrenman için tam olarak şu formatta bir satır yaz (alanları "|" ile ayır, tarih YYYY-AA-GG):
TARIH | TIP | BAŞLIK | MESAFE_KM | SURE_DK | NOT
- TIP: running, cycling, walking, swimming, strength veya other
- BAŞLIK kısa ve İngilizce, "[Weekday] [Intensity]" kalıbında (ör. Wednesday Tempo, Saturday Long, Tuesday Strength); gün adı tarihe karşılık gelmeli; uzun koşu başlığında "Long" geçsin
- MESAFE_KM veya SURE_DK'dan en az biri dolu olsun; günün planını iptal etmek için TIP=other ve NOT'a "dinlenme"
- Onaylanan satır, o tarihteki henüz yapılmamış mevcut planın yerine geçer; mevcut plan uygunsa o gün için satır yazma. Aynı güne iki antrenman istiyorsan ikisini de yaz.
- Normalde sadece bir sonraki antrenman için tek satır yaz. Haftalık plan istendiğinde ("[Haftalık plan]" mesajı veya kullanıcı açıkça istediğinde) ya da bir kaçırma sonrası haftanın kalanını yeniden düzenlemen gerektiğinde, değişen her gün için ayrı satır yaz (dinlenme günleri hariç). Kullanıcı istemedikçe veya konu antrenman önerisi değilse bu satırları ekleme."""

_DEFAULT_INSTRUCTIONS = "Ek talimat yok."

# Profil dili: veri özeti ve talimatlar Türkçe kalsa da koç cevabı bu dilde yazar.
_LANGUAGE_RULE = {
    "tr": "Türkçe",
    "en": "Cevaplarını her zaman İngilizce (English) yaz — kullanıcı Türkçe yazsa, veri ve talimatlar Türkçe olsa bile",
}


class CoachChatError(Exception):
    pass


def api_key_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _month_start() -> dt.datetime:
    now = dt.datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_cost(db: Session, profile_id: str | None = None) -> float:
    q = db.query(func.coalesce(func.sum(CoachMessage.cost_usd), 0.0)).filter(CoachMessage.created_at >= _month_start())
    if profile_id:
        q = q.filter(CoachMessage.profile_id == profile_id)
    return float(q.scalar() or 0.0)


def usage_summary(db: Session) -> dict:
    """Bu ayın profil bazlı kullanım/maliyet özeti (Ayarlar ve Koç sayfası için)."""
    rows = (
        db.query(
            CoachMessage.profile_id,
            func.count(CoachMessage.id),
            func.coalesce(func.sum(CoachMessage.input_tokens), 0),
            func.coalesce(func.sum(CoachMessage.cache_read_tokens), 0),
            func.coalesce(func.sum(CoachMessage.cache_write_tokens), 0),
            func.coalesce(func.sum(CoachMessage.output_tokens), 0),
            func.coalesce(func.sum(CoachMessage.cost_usd), 0.0),
        )
        .filter(CoachMessage.role.in_(("assistant", "refused", "health")), CoachMessage.created_at >= _month_start())
        .group_by(CoachMessage.profile_id)
        .all()
    )
    names = {p.id: p.name for p in db.query(Profile).all()}
    per_profile = [
        {
            "profile_id": pid, "name": names.get(pid, "?"), "replies": n,
            "input_tokens": int(inp), "cache_read_tokens": int(cr), "cache_write_tokens": int(cw),
            "output_tokens": int(out), "cost_usd": float(cost),
        }
        for pid, n, inp, cr, cw, out, cost in rows
    ]
    total = sum(p["cost_usd"] for p in per_profile)
    return {
        "per_profile": sorted(per_profile, key=lambda p: p["name"]),
        "total_usd": total,
        "budget_usd": settings.coach_monthly_budget_usd,
        "model": settings.coach_model,
        "month_label": _month_start().strftime("%m.%Y"),
    }


def current_thread(db: Session, profile: Profile) -> CoachThread:
    thread = (
        db.query(CoachThread)
        .filter(CoachThread.profile_id == profile.id)
        .order_by(CoachThread.created_at.desc())
        .first()
    )
    if thread is None:
        thread = CoachThread(profile_id=profile.id)
        db.add(thread)
        db.commit()
    return thread


def start_new_thread(db: Session, profile: Profile) -> CoachThread:
    thread = CoachThread(profile_id=profile.id)
    db.add(thread)
    db.commit()
    return thread


def _cost(usage, model: str) -> float:
    """Kullanımı model fiyatıyla dolara çevirir. Fallback çalıştıysa her adım kendi modelinin fiyatıyla."""
    steps = usage.iterations or [usage]
    total = 0.0
    for step in steps:
        price = _PRICES.get(getattr(step, "model", None) or model, _PRICES["claude-opus-5-5"])
        total += (
            (step.input_tokens or 0) * price["input"]
            + (step.output_tokens or 0) * price["output"]
            + (step.cache_read_input_tokens or 0) * price["cache_read"]
            + (step.cache_creation_input_tokens or 0) * price["cache_write"]
        ) / 1_000_000
    return total


def _system_blocks(profile: Profile) -> list[dict]:
    # Sabit kalmalı (tarih/saat yok): her istekte aynı byte'lar → önbellekten okunur.
    return [
        {"type": "text", "text": _BASE_SYSTEM.format(name=profile.name, language=_LANGUAGE_RULE[i18n.normalize(profile.language)])},
        {
            "type": "text",
            "text": "Koç talimatları (kullanıcının kendi notları):\n" + ((profile.coach_instructions or "").strip() or _DEFAULT_INSTRUCTIONS),
            "cache_control": {"type": "ephemeral"},
        },
    ]


# Aynı profilin sohbetine web, Telegram ve akşam kontrolü aynı anda yazabilir; turlar karışmasın
# (sıra numarası ve append-only geçmiş) diye profil başına tek tur.
_turn_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def stream_reply(
    db: Session,
    profile: Profile,
    thread: CoachThread,
    user_content: str | list[dict],
    snapshot: str,
    display_text: str | None = None,
    channel: str = "web",
) -> Iterator[dict]:
    """Kullanıcı mesajını gönderir, yanıtı parça parça döndürür ({"t": "delta"|"done"|"error", ...}).

    user_content metin ya da içerik blokları (ör. Telegram'dan gelen fotoğraf + metin) olabilir.
    Tur sadece başarılı yanıttan sonra kaydedilir: yarıda kalan bir istek geçmişte cevapsız
    mesaj bırakmaz (yarım kalan bir sistem mesajı sonraki isteği geçersiz kılardı)."""
    lock = _turn_locks[profile.id]
    if not lock.acquire(timeout=300):
        yield {"t": "error", "message": i18n.t(profile.language, "Koç şu an başka bir mesajı yanıtlıyor, biraz sonra tekrar dene.")}
        return
    try:
        db.expire(thread)  # kilit beklerken başka bir kanal tur eklemiş olabilir; mesajlar yeniden yüklensin
        yield from _stream_reply_locked(db, profile, thread, user_content, snapshot, display_text, channel)
    finally:
        lock.release()


def reply(db: Session, profile: Profile, user_content: str | list[dict], display_text: str | None = None,
          channel: str = "web") -> dict:
    """Akış gerektirmeyen kanallar (Telegram, akşam kontrolü) için: tam cevabı tek seferde döner."""
    from .routers.coach import build_profile_brief  # döngüsel import olmasın diye geç import

    thread = current_thread(db, profile)
    snapshot = build_profile_brief(db, profile, include_prompt=False)
    result = {"t": "error", "message": i18n.t(profile.language, "Yanıt alınamadı.")}
    for event in stream_reply(db, profile, thread, user_content, snapshot, display_text, channel):
        if event["t"] in ("done", "error"):
            result = event
    return result


def _stream_reply_locked(db, profile, thread, user_content, snapshot, display_text, channel) -> Iterator[dict]:
    lang = i18n.normalize(profile.language)
    if not api_key_configured():
        yield {"t": "error", "message": i18n.t(lang, "Claude API anahtarı tanımlı değil (.env içinde ANTHROPIC_API_KEY).")}
        return
    spent = month_cost(db)
    if spent >= settings.coach_monthly_budget_usd:
        yield {"t": "error", "message": i18n.t(lang, "Bu ayki Koç bütçesi doldu (${spent} / ${budget}). Bütçeyi .env'de FITDASH_COACH_MONTHLY_BUDGET_USD ile artırabilirsin.",
                                                spent=f"{spent:.2f}", budget=f"{settings.coach_monthly_budget_usd:.2f}")}
        return

    history = [
        {"role": m.role, "content": json.loads(m.content_json)}
        for m in thread.messages
        if m.role in ("user", "system", "assistant")
    ]
    if display_text is None:
        display_text = user_content if isinstance(user_content, str) else "(ek)"
    new_turn: list[dict] = [{"role": "user", "content": user_content, "_display": display_text}]
    snapshot_hash = hashlib.sha256(snapshot.encode()).hexdigest()
    if snapshot_hash != thread.snapshot_hash:
        stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
        new_turn.append({
            "role": "system",
            "content": f"Kinetra verisi ({stamp} itibarıyla güncel):\n\n{snapshot}",
            "_display": "📊 Güncel Kinetra verileri koça iletildi",
        })
    request_messages = history + [{"role": m["role"], "content": m["content"]} for m in new_turn]

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=settings.coach_model,
            max_tokens=32000,
            system=_system_blocks(profile),
            messages=request_messages,
            output_config={"effort": settings.coach_effort},
            cache_control={"type": "ephemeral"},  # büyüyen sohbet kuyruğu da önbelleğe alınsın
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # güvenlik reddinde sunucu tarafı yedek model
        ) as stream:
            for text in stream.text_stream:
                yield {"t": "delta", "text": text}
            final = stream.get_final_message()
    except anthropic.AuthenticationError:
        yield {"t": "error", "message": i18n.t(lang, "Claude API anahtarı geçersiz. .env içindeki ANTHROPIC_API_KEY'i kontrol et.")}
        return
    except anthropic.PermissionDeniedError:
        yield {"t": "error", "message": i18n.t(lang, "API anahtarının bu modele erişim izni yok.")}
        return
    except anthropic.RateLimitError:
        yield {"t": "error", "message": i18n.t(lang, "Claude API hız sınırına takıldı, biraz sonra tekrar dene.")}
        return
    except anthropic.BadRequestError as exc:
        logger.warning("Koç isteği reddedildi: %s", exc)
        msg = str(getattr(exc, "message", exc))
        if "credit" in msg.lower() or "billing" in msg.lower():
            msg = i18n.t(lang, "Anthropic hesabında kredi yok veya faturalandırma ayarlı değil (Console → Billing).")
        yield {"t": "error", "message": i18n.t(lang, "İstek reddedildi: {msg}", msg=msg)}
        return
    except anthropic.APIStatusError as exc:
        yield {"t": "error", "message": i18n.t(lang, "Claude API hatası ({code}). Biraz sonra tekrar dene.", code=exc.status_code)}
        return
    except anthropic.APIConnectionError:
        yield {"t": "error", "message": i18n.t(lang, "Claude API'ye bağlanılamadı (internet bağlantısını kontrol et).")}
        return

    cost = _cost(final.usage, final.model)
    if final.stop_reason == "refusal":
        # Yedek model de reddettiyse turu kaydetmiyoruz ama ücretlendirilmiş olabilir: maliyeti kayda geçir.
        _record_cost_only(db, profile, thread, final, cost)
        yield {"t": "error", "message": i18n.t(lang, "Koç bu mesaja yanıt veremedi. Sorunu farklı ifade etmeyi dene.")}
        return

    # Turu kaydet: kullanıcı mesajı, (varsa) veri mesajı, asistan yanıtı — tam içerik blokları ile.
    seq = (thread.messages[-1].seq + 1) if thread.messages else 0
    for m in new_turn:
        db.add(CoachMessage(
            thread_id=thread.id, profile_id=profile.id, seq=seq, role=m["role"],
            content_json=json.dumps(m["content"], ensure_ascii=False), display_text=m["_display"], channel=channel,
        ))
        seq += 1
    reply_text = "".join(b.text for b in final.content if b.type == "text")
    usage = final.usage
    steps = usage.iterations or [usage]
    db.add(CoachMessage(
        thread_id=thread.id, profile_id=profile.id, seq=seq, role="assistant",
        content_json=json.dumps([b.to_dict(mode="json") for b in final.content], ensure_ascii=False),
        display_text=reply_text, channel=channel, model=final.model, stop_reason=final.stop_reason,
        request_id=getattr(final, "_request_id", None),
        input_tokens=sum(s.input_tokens or 0 for s in steps),
        output_tokens=sum(s.output_tokens or 0 for s in steps),
        cache_read_tokens=sum(s.cache_read_input_tokens or 0 for s in steps),
        cache_write_tokens=sum(s.cache_creation_input_tokens or 0 for s in steps),
        cost_usd=cost,
    ))
    thread.snapshot_hash = snapshot_hash
    db.add(thread)
    db.commit()

    yield {
        "t": "done",
        "text": reply_text,
        "truncated": final.stop_reason == "max_tokens",
        "cost_usd": cost,
        "month_profile_usd": month_cost(db, profile.id),
        "month_total_usd": month_cost(db),
        "model": final.model,
    }


def _record_cost_only(db: Session, profile: Profile, thread: CoachThread, final, cost: float) -> None:
    """Reddedilen bir turun maliyetini bütçeye yansıtır; sohbet geçmişine girmez (role=refused)."""
    seq = (thread.messages[-1].seq + 1) if thread.messages else 0
    db.add(CoachMessage(
        thread_id=thread.id, profile_id=profile.id, seq=-1 - seq, role="refused",
        content_json="[]", model=final.model, stop_reason=final.stop_reason, cost_usd=cost,
        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
    ))
    db.commit()
