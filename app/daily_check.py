"""Akşam kontrolü: her Telegram'a bağlı profil için günün değerlendirmesi ve yarının önerisi.

Claude yalnızca değerlendirecek bir şey olduğunda çağrılır (bugün antrenman yapıldı veya yarın
için plan yok); aksi halde ücretsiz bir plan hatırlatması gider. Bugünkü plan için kayıt yoksa
önce ✅/❌/📅 butonlarıyla kullanıcıya sorulur; ❌ sonrası koç isteğe bağlı yeniden planlar.
Hafta, haftalık hedefin (varsayılan 3 koşu + 2 kuvvet) gerisindeyse koç kalan günleri düzenler;
Pazar akşamı haftalık değerlendirme + gelecek haftanın planı onay butonuyla gelir.
Sağlık kontrolü hatırlatmaları kural tabanlıdır, Claude kullanmaz.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import coach_chat, health, telegram_bot, training
from .activity_display import group_activities
from .config import settings
from .db import SessionLocal
from .models import Activity, PlannedWorkout, Profile

logger = logging.getLogger("fitdash.daily_check")


def run_evening_check() -> None:
    db = SessionLocal()
    try:
        for profile in db.query(Profile).filter(Profile.telegram_chat_id.isnot(None)).all():
            if not telegram_bot.token_for(profile):
                continue
            try:
                check_profile(db, profile)
            except Exception:  # noqa: BLE001 - bir profilin hatası diğerini durdurmasın
                logger.exception("Akşam kontrolü başarısız: %s", profile.name)
    finally:
        db.close()


def _plan_line(w: PlannedWorkout, profile: Profile | None = None) -> str:
    bits = [w.title]
    if w.target_distance_m:
        bits.append(f"{w.target_distance_m / 1000:g} km")
    if w.target_duration_s:
        bits.append(f"{int(w.target_duration_s // 60)} " + (telegram_bot.T(profile, "dk") if profile else "dk"))
    return " · ".join(bits) + (" ⌚" if w.garmin_workout_id else "")


def check_profile(db, profile: Profile, today: dt.date | None = None) -> str:
    """Bir profil için akşam kontrolünü yapar; ne yapıldığını ("claude"|"weekly"|"ask"|"reminder") döner."""
    from .routers.coach import active_plans
    from .routers.dashboard import _matches_type

    for text in health.due_reminders(db, profile):
        telegram_bot.send_to_profile(profile, text)

    today = today or dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    day_start = dt.datetime.combine(today, dt.time.min)
    todays = [i["activity"] for i in group_activities(
        db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= day_start,
                                  Activity.start_time < day_start + dt.timedelta(days=1)).all())]
    plans = {
        d: active_plans(db, profile.id).filter(PlannedWorkout.date == d).all()
        for d in (today, tomorrow)
    }
    pending = [w for w in plans[today] if w.status is None and w.workout_type != "other"
               and not any(_matches_type(a, w.workout_type) for a in todays)]
    sunday = today.weekday() == 6
    if pending:
        # Kayıt yoksa önce kullanıcıya sor (ücretsiz); cevaba göre koç ❌ sonrası yeniden planlar.
        for w in pending:
            telegram_bot.send_workout_card(profile, w, intro=telegram_bot.T(profile, "🌙 Bugünkü plan için kayıt görmedim. Durum ne?"))
        if not todays and not sunday:
            if plans[tomorrow]:
                telegram_bot.send_to_profile(profile, f"**{telegram_bot.T(profile, 'Yarın')}:** " + ", ".join(_plan_line(w, profile) for w in plans[tomorrow]))
            return "ask"

    week = training.week_status(db, profile, training.week_start(today), today)
    phase = training.race_phase(db, profile.id, today)
    gap = training.gap_text(week)
    # Hafta hedefin gerisindeyse ve telafi için gün varsa koç kalan günleri düzenlesin
    # (yarış haftası/toparlanmada sayı hedefi kovalanmaz).
    behind = bool(gap) and bool(week["remaining_days"]) and not (phase and phase["phase"] in ("race_week", "recovery"))
    need_claude = sunday or bool(todays) or not plans[tomorrow] or behind

    budget_ok = coach_chat.month_cost(db) < settings.coach_monthly_budget_usd
    if need_claude and coach_chat.api_key_configured() and budget_ok:
        today_plan = ", ".join(_plan_line(w) for w in plans[today]) or "yok"
        tomorrow_plan = ", ".join(_plan_line(w) for w in plans[tomorrow]) or "yok"
        if sunday:
            next_start = today + dt.timedelta(days=1)
            next_plans = active_plans(db, profile.id).filter(
                PlannedWorkout.date >= next_start, PlannedWorkout.date < next_start + dt.timedelta(days=7)
            ).order_by(PlannedWorkout.date).all()
            existing = ", ".join(f"{w.date:%a %d.%m} {w.title}" for w in next_plans) or "yok"
            pending_note = (" Bugünkü plan için durum henüz bildirilmedi: " + ", ".join(w.title for w in pending) + ".") if pending else ""
            prompt = (
                f"[Haftalık plan — {today:%d.%m.%Y} Pazar akşamı]\n"
                f"Bu haftanın özeti verideki 'Haftalık Hedef ve Dönem' bölümünde.{pending_note}\n"
                f"Gelecek hafta ({next_start:%d.%m}–{next_start + dt.timedelta(days=6):%d.%m}) mevcut plan: {existing}\n\n"
                "1) Bu haftayı hedefe göre 3-5 satırda değerlendir (tutan/tutmayan, toparlanma). "
                "2) Gelecek haftayı dönemine göre planla: normal dönemde haftalık düzen hedefi, yarış döneminde fazı esas al. "
                "Her antrenman günü için ayrı plan satırı yaz; mevcut plandaki uygun günler için satır yazma. Telegram'da okunacak, kısa tut."
            )
            telegram_bot.typing(profile)
            result = coach_chat.reply(db, profile, prompt, display_text="🗓️ Haftalık plan", channel="auto")
            telegram_bot.send_coach_reply(db, profile, result)
            return "weekly"
        prompt = (
            f"[Otomatik akşam kontrolü — {today:%d.%m.%Y} {settings.evening_check_hour}:00]\n"
            f"Bugün kaydedilen antrenman: {len(todays)} · Bugünün planı: {today_plan} · Yarının planı: {tomorrow_plan}\n"
            f"Haftalık hedef: {training.week_summary_line(week)} · plan dışı eksik: {gap or 'yok'}\n\n"
            "Bugünü ve toparlanmayı kısaca değerlendir; Telegram'da okunacak, en fazla 10 satır. Plan aksadıysa açıkça belirt; 'Plan Durumu' bölümünü dikkate al. "
            "Yarın için plan yoksa veya mevcut plan değişmeli ise plan satırı öner; haftalık hedefte plan dışı eksik varsa "
            "eksiği haftanın kalan günlerine yerleştiren satırları da yaz (yarış haftası/toparlanmada sayı kovalama). "
            "Mevcut plan uygunsa yeni satır yazma, \"Yarınki plan geçerli\" de."
        )
        telegram_bot.typing(profile)
        result = coach_chat.reply(db, profile, prompt, display_text="🌙 Akşam kontrolü", channel="auto")
        telegram_bot.send_coach_reply(db, profile, result)
        return "claude"

    if plans[tomorrow]:
        text = f"🌙 **{telegram_bot.T(profile, 'Yarın')}:** " + ", ".join(_plan_line(w, profile) for w in plans[tomorrow])
    else:
        text = telegram_bot.T(profile, "🌙 Yarın için plan yok.")
    if need_claude and not budget_ok:
        text += "\n" + telegram_bot.T(profile, "(Bu ayki Claude bütçesi dolduğu için koç değerlendirmesi yapılmadı.)")
    elif need_claude and not coach_chat.api_key_configured():
        text += "\n" + telegram_bot.T(profile, "(Claude API anahtarı tanımlı olmadığı için koç değerlendirmesi yapılmadı.)")
    telegram_bot.send_to_profile(profile, text)
    return "reminder"
