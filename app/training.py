"""Haftalık antrenman hedefi ve yarış dönemi.

Normal dönemde koçun tutturmaya çalıştığı çerçeve profilin haftalık düzenidir (varsayılan: 3 koşu,
biri Cumartesi uzun koşu + 2 kuvvet). Gerçekleşen/planlanan sayımlar burada deterministik olarak
hesaplanır ve koç verisine eklenir; koç sadece eksik kalanı nereye yerleştireceğine karar verir.
A önceliğindeki bir yarışın hazırlık penceresine girildiğinde dönem "yarış dönemi" olur ve koç
antrenman yapısını yarışın fazına (hazırlık / taper / yarış haftası / toparlanma) göre değiştirebilir.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from . import i18n
from .activity_display import group_activities, is_running
from .models import Activity, PlannedWorkout, Profile, Race

DAY_KINDS = {
    "run": "koşu",
    "long": "uzun koşu",
    "strength": "kuvvet",
    "rest": "dinlenme",
    "optional": "opsiyonel",
}
DEFAULT_WEEK = ["run", "strength", "run", "strength", "rest", "long", "optional"]
WEEKDAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
WEEKDAYS_LONG = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
PRIORITY_LABELS = {"A": "A — ana hedef", "B": "B — ara yarış", "C": "C — antrenman koşusu"}


# --------------------------------------------------------------------------- haftalık düzen

def week_template(profile: Profile) -> list[str]:
    days = (profile.week_template or "").split(",")
    if len(days) == 7 and all(d in DAY_KINDS for d in days):
        return days
    return list(DEFAULT_WEEK)


def set_week_template(profile: Profile, days: list[str]) -> None:
    if len(days) != 7 or any(d not in DAY_KINDS for d in days):
        raise ValueError("Geçersiz haftalık düzen")
    profile.week_template = None if days == DEFAULT_WEEK else ",".join(days)


def targets(template: list[str]) -> dict:
    return {
        "runs": sum(d in ("run", "long") for d in template),
        "strength": template.count("strength"),
        "long_day": template.index("long") if "long" in template else None,
    }


def template_text(template: list[str], lang: str = "tr") -> str:
    t = targets(template)
    days = " · ".join(f"{i18n.weekday_name(lang, i, short=True)} {i18n.t(lang, DAY_KINDS[k])}" for i, k in enumerate(template))
    if t["long_day"] is not None:
        goal = i18n.t(lang, "{n} koşu (biri {day} uzun koşu)", n=t["runs"], day=i18n.weekday_name(lang, t["long_day"]))
    else:
        goal = i18n.t(lang, "{n} koşu", n=t["runs"])
    return i18n.t(lang, "{days} → hedef {goal} + {n} kuvvet", days=days, goal=goal, n=t["strength"])


# --------------------------------------------------------------------------- yarış dönemi

def default_prep_weeks(distance_km: float) -> int:
    if distance_km >= 40:
        return 16
    if distance_km >= 20:
        return 12
    if distance_km >= 10:
        return 8
    return 6


def taper_days(distance_km: float) -> int:
    if distance_km >= 40:
        return 21
    if distance_km >= 20:
        return 10
    return 7


def recovery_days(distance_km: float) -> int:
    if distance_km >= 40:
        return 14
    if distance_km >= 20:
        return 7
    if distance_km >= 10:
        return 4
    return 3


def race_label(race: Race) -> str:
    bits = [f"{race.date:%d.%m.%Y}", race.name, f"{race.distance_km:g} km", f"öncelik {race.priority}"]
    if race.goal:
        bits.append(f"hedef: {race.goal}")
    return " · ".join(bits)


def race_phase(db: Session, profile_id: str, today: dt.date | None = None) -> dict | None:
    """Bugün bir A yarışının hazırlık/taper/yarış haftası/toparlanma penceresindeyse fazı döner."""
    today = today or dt.date.today()
    races = (
        db.query(Race)
        .filter(Race.profile_id == profile_id, Race.priority == "A",
                Race.date >= today - dt.timedelta(days=21))
        .order_by(Race.date.asc())
        .all()
    )
    # Önce yeni bitmiş bir yarışın toparlanması, sonra en yakın yaklaşan yarışın hazırlığı.
    for race in races:
        since = (today - race.date).days
        if 0 < since <= recovery_days(race.distance_km):
            return {"race": race, "phase": "recovery", "days": since, "recovery_days": recovery_days(race.distance_km),
                    "text": f"yarış sonrası toparlanma ({since}. gün / ~{recovery_days(race.distance_km)} gün)"}
    for race in races:
        days_to = (race.date - today).days
        if days_to < 0:
            continue
        prep = race.prep_weeks or default_prep_weeks(race.distance_km)
        if days_to > prep * 7:
            continue
        if days_to <= 6:
            phase, text = "race_week", f"yarış haftası ({days_to} gün kaldı)" if days_to else "yarış günü"
        elif days_to <= taper_days(race.distance_km):
            phase, text = "taper", f"taper ({days_to} gün kaldı)"
        else:
            week_no = prep - (days_to // 7)
            phase, text = "build", f"hazırlık bloğu, {week_no}. hafta / {prep} (yarışa {days_to // 7} hafta {days_to % 7} gün)"
        return {"race": race, "phase": phase, "days": days_to, "prep": prep, "text": text}
    return None


def upcoming_races(db: Session, profile_id: str, today: dt.date | None = None, weeks: int = 26) -> list[Race]:
    today = today or dt.date.today()
    return (
        db.query(Race)
        .filter(Race.profile_id == profile_id, Race.date >= today, Race.date <= today + dt.timedelta(weeks=weeks))
        .order_by(Race.date.asc())
        .all()
    )


# --------------------------------------------------------------------------- hafta durumu

def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _is_strength(a: Activity) -> bool:
    return a.source == "hevy" or "strength" in (a.activity_type or "").lower()


def _is_long_title(title: str) -> bool:
    t = title.lower()
    return "long" in t or "uzun" in t


def week_status(db: Session, profile: Profile, start: dt.date, today: dt.date | None = None) -> dict:
    """start (Pazartesi) haftası için hedef / gerçekleşen / kalan plan özeti."""
    from .routers.coach import active_plans

    today = today or dt.date.today()
    end = start + dt.timedelta(days=7)
    template = week_template(profile)
    tgt = targets(template)

    acts = [item["activity"] for item in group_activities(
        db.query(Activity).filter(Activity.profile_id == profile.id,
                                  Activity.start_time >= dt.datetime.combine(start, dt.time.min),
                                  Activity.start_time < dt.datetime.combine(end, dt.time.min)).all())]
    plans = active_plans(db, profile.id).filter(PlannedWorkout.date >= start, PlannedWorkout.date < end).all()

    runs = sorted(((a.start_time.date(), (a.distance_m or 0) / 1000) for a in acts if is_running(a.source, a.activity_type)))
    strength = sorted({a.start_time.date() for a in acts if _is_strength(a)})
    # Kayıt düşmemiş ama kullanıcının ✅ ile bildirdiği antrenmanlar da sayılır.
    for w in plans:
        if w.status != "done":
            continue
        if w.workout_type == "running" and not any(d == w.date for d, _ in runs):
            runs.append((w.date, (w.target_distance_m or 0) / 1000))
        elif w.workout_type == "strength" and w.date not in strength:
            strength.append(w.date)
    runs.sort()

    long_dates = {w.date for w in plans if w.workout_type == "running" and _is_long_title(w.title)}
    if tgt["long_day"] is not None:
        long_dates.add(start + dt.timedelta(days=tgt["long_day"]))
    long_done = next(((d, km) for d, km in runs if d in long_dates), None)

    run_days = {d for d, _ in runs}
    # Bugünün planı zaten aynı türde bir kayıtla yapıldıysa "kalan" sayılmaz (çift sayım olmasın).
    pending = [w for w in plans if w.status is None and w.date >= today
               and not (w.workout_type == "running" and w.date in run_days)
               and not (w.workout_type == "strength" and w.date in strength)]
    planned_runs = [w for w in pending if w.workout_type == "running"]
    planned_strength = [w for w in pending if w.workout_type == "strength"]
    long_planned = next((w for w in planned_runs if _is_long_title(w.title) or w.date in long_dates), None)

    remaining_days = [start + dt.timedelta(days=i) for i in range(7)
                      if start + dt.timedelta(days=i) > today]
    return {
        "start": start,
        "targets": tgt,
        "template": template,
        "runs": runs,
        "strength": strength,
        "long_done": long_done,
        "long_planned": long_planned,
        "pending": pending,
        "remaining_days": remaining_days,
        "missing_runs": max(0, tgt["runs"] - len(runs) - len(planned_runs)),
        "missing_strength": max(0, tgt["strength"] - len(strength) - len(planned_strength)),
        "missing_long": tgt["long_day"] is not None and long_done is None and long_planned is None,
    }


def _day(d: dt.date, lang: str = "tr") -> str:
    return i18n.weekday_name(lang, d.weekday(), short=True)


def week_summary_line(st: dict, lang: str = "tr") -> str:
    tgt = st["targets"]
    runs = ", ".join(f"{_day(d, lang)} {km:.1f} km" for d, km in st["runs"])
    text = i18n.t(lang, "koşu {done}/{target}", done=len(st["runs"]), target=tgt["runs"]) + (f" ({runs})" if runs else "")
    if tgt["long_day"] is not None:
        if st["long_done"]:
            text += f" · {i18n.t(lang, 'uzun koşu')} ✓ ({_day(st['long_done'][0], lang)} {st['long_done'][1]:.1f} km)"
        else:
            text += f" · {i18n.t(lang, 'uzun koşu')} ✗"
    text += " · " + i18n.t(lang, "kuvvet {done}/{target}", done=len(st["strength"]), target=tgt["strength"])
    if st["strength"]:
        text += f" ({', '.join(_day(d, lang) for d in st['strength'])})"
    return text


def gap_text(st: dict, lang: str = "tr") -> str:
    gaps = []
    if st["missing_runs"]:
        gaps.append(i18n.t(lang, "{n} koşu", n=st["missing_runs"]))
    if st["missing_long"]:
        gaps.append(i18n.t(lang, "uzun koşu"))
    if st["missing_strength"]:
        gaps.append(i18n.t(lang, "{n} kuvvet", n=st["missing_strength"]))
    return ", ".join(gaps)


def phase_text(phase: dict, lang: str = "tr") -> str:
    """race_phase() sonucunun arayüz/Telegram için dile göre metni."""
    days = phase["days"]
    if phase["phase"] == "recovery":
        return i18n.t(lang, "yarış sonrası toparlanma ({n}. gün / ~{total} gün)", n=days, total=phase["recovery_days"])
    if phase["phase"] == "race_week":
        return i18n.t(lang, "yarış haftası ({n} gün kaldı)", n=days) if days else i18n.t(lang, "yarış günü")
    if phase["phase"] == "taper":
        return i18n.t(lang, "taper ({n} gün kaldı)", n=days)
    prep = phase["prep"]
    return i18n.t(lang, "hazırlık bloğu, {week}. hafta / {prep} (yarışa {w} hafta {d} gün)",
                  week=prep - days // 7, prep=prep, w=days // 7, d=days % 7)


def brief_lines(db: Session, profile: Profile, today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    template = week_template(profile)
    lines = ["## Haftalık Hedef ve Dönem"]

    phase = race_phase(db, profile.id, today)
    if phase:
        lines.append(f"- Dönem: YARIŞ DÖNEMİ — {race_label(phase['race'])} · faz: {phase['text']}")
        if phase["race"].notes:
            lines.append(f"  - Yarış notu: {phase['race'].notes}")
        lines.append("  - Normal haftalık düzen bu dönemde yön göstericidir; antrenman yapısını yarış fazına göre belirle.")
    else:
        lines.append("- Dönem: normal (hazırlık penceresinde A önceliğinde yarış yok) — haftalık hedef geçerli")
    lines.append(f"- Normal haftalık düzen: {template_text(template)}")

    start = week_start(today)
    st = week_status(db, profile, start, today)
    lines.append(f"- Bu hafta ({start:%d.%m}–{start + dt.timedelta(days=6):%d.%m}): {week_summary_line(st)}")
    if st["pending"]:
        lines.append("- Bu haftanın kalan planı: " + ", ".join(
            f"{_day(w.date)} {w.title}" for w in sorted(st["pending"], key=lambda w: w.date)))
    remaining = ", ".join(_day(d) for d in st["remaining_days"]) or "yok"
    gap = gap_text(st)
    lines.append(f"- Yarından itibaren kalan gün: {remaining} · plan dışı eksik: {gap or 'yok (plan hedefi karşılıyor)'}")

    history = []
    for k in range(1, 5):
        ws = start - dt.timedelta(weeks=k)
        history.append(f"{ws:%d.%m} haftası: {week_summary_line(week_status(db, profile, ws, today))}")
    lines.append("- Önceki haftalar: " + " | ".join(history))

    others = [r for r in upcoming_races(db, profile.id, today) if not phase or r.id != phase["race"].id]
    if others:
        lines.append("- Takvimdeki yarışlar: " + "; ".join(race_label(r) for r in others))
    lines.append("")
    return lines


def telegram_week_text(db: Session, profile: Profile, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    lang = i18n.normalize(profile.language)
    start = week_start(today)
    st = week_status(db, profile, start, today)
    phase = race_phase(db, profile.id, today)
    lines = [f"**{i18n.t(lang, 'Bu hafta')}** ({start:%d.%m}–{start + dt.timedelta(days=6):%d.%m})", week_summary_line(st, lang)]
    if st["pending"]:
        lines.append(i18n.t(lang, "Kalan plan") + ": " + ", ".join(f"{_day(w.date, lang)} {w.title}" for w in sorted(st["pending"], key=lambda w: w.date)))
    gap = gap_text(st, lang)
    lines.append(f"{i18n.t(lang, 'Eksik')}: {gap}" if gap else i18n.t(lang, "Plan haftalık hedefi karşılıyor") + " ✓")
    lines.append(f"{i18n.t(lang, 'Düzen')}: {template_text(st['template'], lang)}")
    if phase:
        lines.append(f"🏁 {phase['race'].name}: {phase_text(phase, lang)}")
    return "\n".join(lines)
