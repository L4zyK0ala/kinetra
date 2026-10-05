from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..activity_display import activity_label, format_duration, format_pace, group_activities, is_running
from .. import health, i18n, training
from ..crypto import decrypt_payload
from ..db import get_db
from ..deps import get_current_profile
from ..integrations import garmin as garmin_integration
from ..models import Activity, BodyMeasurement, Credential, DailyMetric, HealthRecord, NutritionLog, PlannedWorkout, Profile, Race
from ..streams import compute_decoupling, compute_splits, find_sibling_with_stream, gpx_metrics, load_stream_data
from ..templating import templates

router = APIRouter()

WORKOUT_TYPE_LABELS = {
    "running": "Koşu",
    "cycling": "Bisiklet",
    "walking": "Yürüyüş",
    "swimming": "Yüzme",
    "strength": "Kuvvet",
    "other": "Diğer",
}

_GARMIN_PUSHABLE_TYPES = {"running", "cycling", "walking", "swimming", "strength"}

_GENDER_LABELS = {"female": "Kadın", "male": "Erkek"}

_TYPE_SYNONYMS = {
    "running": "running", "run": "running", "koşu": "running", "kosu": "running",
    "cycling": "cycling", "ride": "cycling", "bike": "cycling", "bisiklet": "cycling",
    "walking": "walking", "walk": "walking", "yürüyüş": "walking", "yuruyus": "walking",
    "swimming": "swimming", "swim": "swimming", "yüzme": "swimming", "yuzme": "swimming",
    "strength": "strength", "kuvvet": "strength", "güç": "strength", "guc": "strength",
    "other": "other", "diğer": "other", "diger": "other", "dinlenme": "other", "rest": "other", "": "other",
}

_TR_FOLD = str.maketrans("işğüöç", "isguoc")


def _normalize_workout_type(raw: str) -> str:
    key = raw.strip().lower().translate(_TR_FOLD)
    return _TYPE_SYNONYMS.get(key, "other")


def _parse_plan_date(raw: str) -> dt.date | None:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_plan_text(text: str) -> tuple[list[dict], int]:
    """Claude'un pipe-delimited plan yanıtını (TARIH | TIP | BAŞLIK | MESAFE_KM | SURE_DK | NOT)
    PlannedWorkout alanlarına çevirir. (satırlar, atlanan_satır_sayısı) döner."""
    rows: list[dict] = []
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0].upper() in ("TARIH", "TARİH", "DATE") or set(parts[0]) <= {"-", " "}:
            continue  # başlık/ayraç satırı

        parsed_date = _parse_plan_date(parts[0])
        if parsed_date is None:
            skipped += 1
            continue

        workout_type = _normalize_workout_type(parts[1]) if len(parts) > 1 else "other"
        title = parts[2] if len(parts) > 2 and parts[2] else WORKOUT_TYPE_LABELS.get(workout_type, "Antrenman")

        distance_km = None
        if len(parts) > 3 and parts[3]:
            try:
                distance_km = float(parts[3].replace(",", "."))
            except ValueError:
                distance_km = None

        duration_min = None
        if len(parts) > 4 and parts[4]:
            try:
                duration_min = float(parts[4].replace(",", "."))
            except ValueError:
                duration_min = None

        notes = parts[5] if len(parts) > 5 and parts[5] else None

        rows.append(
            {
                "date": parsed_date,
                "workout_type": workout_type,
                "title": title,
                "target_distance_m": distance_km * 1000 if distance_km else None,
                "target_duration_s": duration_min * 60 if duration_min else None,
                "notes": notes,
            }
        )
    return rows, skipped


def extract_plan_lines(text: str) -> list[str]:
    """Koç yanıtındaki geçerli plan satırlarını (TARIH | TIP | ...) aynen döner — sohbette
    "Plana ekle" butonu bu satırları mevcut toplu plan ekleme akışına gönderir."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`")
        rows, _ = parse_plan_text(line)
        if rows:
            lines.append(line)
    return lines

def _pick_last_activity_for_brief(db: Session, profile_id: str) -> Activity | None:
    """En son antrenmanı seçer; aynı gerçek antrenman Garmin/Strava'ya ayrı ayrı senkronize
    olduysa (activity_display.group_activities'teki kardiyo önceliği Garmin'i primary seçer)
    ama GPX zaman serisi sadece Strava kopyasında varsa, GPX verisi kaybolmasın diye stream'i
    olan kopyayı tercih eder."""
    latest = (
        db.query(Activity)
        .filter(Activity.profile_id == profile_id)
        .order_by(Activity.start_time.desc())
        .first()
    )
    if latest is None:
        return None
    return find_sibling_with_stream(db, latest) or latest


def _last_activity_line(a: Activity) -> str:
    parts = [activity_label(a.source, a.activity_type)]
    if a.distance_m:
        parts.append(f"{a.distance_m / 1000:.1f} km")
    duration = format_duration(a.duration_s)
    if duration:
        parts.append(duration)
    if a.distance_m and a.duration_s:
        pace = format_pace(a.distance_m, a.duration_s)
        if pace:
            parts.append(pace)
    if a.avg_hr:
        parts.append(f"ort. nabız {a.avg_hr:.0f} bpm")
    if a.calories:
        parts.append(f"{a.calories:.0f} kcal")
    return ", ".join(parts)


def _fmt_mmss_per_km(seconds_per_km: float) -> str:
    m, s = divmod(round(seconds_per_km), 60)
    return f"{m}:{s:02d}/km"


def _gpx_summary_lines(activity: Activity | None) -> list[str]:
    """Son antrenmanın GPX türevi verilerini (GPS/HR/nokta yoğunluğu kritik, kadans/elevasyon
    nice-to-have, güç bonus) Claude'un daha isabetli yorum yapabilmesi için brief'e ekler.

    Ortalama değerler tek başına split tutarlılığı / aerobik decoupling analizine yetmiyor
    (Claude Coach'tan gelen geri bildirim), bu yüzden km bazlı split tablosu ve ilk yarı/ikinci
    yarı pace+nabız karşılaştırması (decoupling) da eklenir.
    """
    if activity is None:
        return []
    data = load_stream_data(activity.stream)
    metrics = gpx_metrics(data, is_run=is_running(activity.source, activity.activity_type))
    if metrics is None:
        return [
            "## Son Aktivite — GPX Verisi",
            "- Bu aktivite için GPS/nabız zaman serisi verisi yok "
            "(şu an sadece Strava üzerinden senkronize edilen aktivitelerde mevcut).",
            "",
        ]

    lines = ["## Son Aktivite — GPX Verisi"]
    lines.append(f"- GPS: {'var' if metrics['has_gps'] else 'yok'}, {metrics['point_count']} nokta"
                  + (f", ort. örnekleme aralığı {metrics['avg_sample_interval_s']:g} sn" if metrics['avg_sample_interval_s'] else ""))
    if metrics["hr"]:
        lines.append(f"- Nabız: ort. {metrics['hr']['avg']} / maks. {metrics['hr']['max']} / min. {metrics['hr']['min']} bpm")
    if metrics["cadence_avg"] is not None:
        unit = "spm (iki bacak, adım/dk)" if is_running(activity.source, activity.activity_type) else "rpm"
        lines.append(f"- Kadans: ort. {metrics['cadence_avg']} {unit}")
    if metrics["elevation_gain_m"] is not None:
        lines.append(f"- Elevasyon kazancı: {metrics['elevation_gain_m']} m")
    if metrics["power_avg"] is not None:
        lines.append(f"- Güç: ort. {metrics['power_avg']} W")

    splits = compute_splits(data, split_m=1000.0) if data else []
    if splits:
        lines.append("- Km bazlı split (pace / ort. nabız):")
        for s in splits:
            pace_str = _fmt_mmss_per_km(s["pace_s_per_km"])
            hr_str = f"{s['avg_hr']} bpm" if s["avg_hr"] is not None else "—"
            if s["is_partial"]:
                label = f"{s['distance_m']}m (kalan kısa parça, tam km değil)"
                lines.append(f"  {label}: {pace_str} (ekstrapole pace — kısa mesafe için gerçek km temposu gibi yorumlama), {hr_str}")
            else:
                lines.append(f"  {s['index']}. km: {pace_str}, {hr_str}")

    decoupling = compute_decoupling(data) if data else None
    if decoupling:
        f, s = decoupling["first"], decoupling["second"]
        lines.append(
            f"- Aerobik decoupling: ilk yarı {_fmt_mmss_per_km(f['pace_s_per_km'])} / {f['avg_hr']} bpm "
            f"→ ikinci yarı {_fmt_mmss_per_km(s['pace_s_per_km'])} / {s['avg_hr']} bpm "
            f"(decoupling %{decoupling['decoupling_pct']:.1f})"
        )

    lines.append("- Not: Bu veriler GPX kaynaklı; sıcaklık, nem ve algılanan efor (RPE) GPX'te bulunmadığı için yok.")
    lines.append("")
    return lines


def _build_brief_markdown(
    profile: Profile,
    today: dt.date,
    last_activity: Activity | None,
    week_count: int,
    week_distance_km: float,
    week_duration: str | None,
    week_calories: float,
    prev_week_distance_km: float,
    latest_metric: DailyMetric | None,
    today_nutrition: NutritionLog | None,
    latest_measurement: BodyMeasurement | None,
    recent_activities: list[Activity] | None = None,
    upcoming: list[PlannedWorkout] | None = None,
    health_lines: list[str] | None = None,
    include_prompt: bool = True,
) -> str:
    lines: list[str] = [f"# Kinetra Antrenman Özeti — {profile.name} — {today.strftime('%d.%m.%Y')}", ""]

    if profile.gender or profile.height_cm:
        bits = []
        if profile.gender:
            bits.append(_GENDER_LABELS.get(profile.gender, profile.gender))
        if profile.height_cm:
            bits.append(f"{profile.height_cm:g} cm boy")
        lines.append(f"Profil: {', '.join(bits)}")
        lines.append("")

    lines.append("## Son Aktivite")
    if last_activity:
        lines.append(f"- {_last_activity_line(last_activity)}")
        lines.append(f"- Tarih: {last_activity.start_time.strftime('%d.%m.%Y %H:%M')}")
        lines.append(f"- Kaynak: {last_activity.source}")
    else:
        lines.append("- Kayıtlı aktivite yok")
    lines.append("")

    lines.extend(_gpx_summary_lines(last_activity))

    if recent_activities:
        lines.append("## Son 14 Gün Aktiviteleri")
        for a in recent_activities:
            lines.append(f"- {a.start_time.strftime('%d.%m %H:%M')} · {_last_activity_line(a)}")
        lines.append("")

    if upcoming:
        lines.append("## Planlı Antrenmanlar (Kinetra Koç)")
        for w in upcoming:
            bits = [w.date.strftime("%d.%m.%Y"), w.title, WORKOUT_TYPE_LABELS.get(w.workout_type, w.workout_type)]
            if w.target_distance_m:
                bits.append(f"{w.target_distance_m / 1000:g} km")
            if w.target_duration_s:
                bits.append(f"{int(w.target_duration_s // 60)} dk")
            line = " · ".join(bits)
            if w.notes:
                line += f" — {w.notes}"
            lines.append(f"- {line}")
        lines.append("")

    lines.append("## Bu Hafta (son 7 gün)")
    lines.append(
        f"- {week_count} aktivite, {week_distance_km:.1f} km, {week_duration or '0:00'}, "
        f"{week_calories:.0f} aktif kcal"
    )
    if prev_week_distance_km:
        diff_pct = (week_distance_km - prev_week_distance_km) / prev_week_distance_km * 100
        arrow = "↑" if diff_pct >= 0 else "↓"
        lines.append(f"- Önceki haftaya göre mesafe: {arrow} %{abs(diff_pct):.0f}")
    lines.append("")

    lines.append("## Recovery (son ölçüm)")
    if latest_metric:
        lines.append(f"- Tarih: {latest_metric.date.strftime('%d.%m.%Y')}")
        if latest_metric.sleep_score is not None:
            dur = f" ({latest_metric.sleep_duration_s / 3600:.1f} sa)" if latest_metric.sleep_duration_s else ""
            lines.append(f"- Uyku Skoru: {latest_metric.sleep_score}/100{dur}")
        if latest_metric.body_battery_max is not None:
            lines.append(f"- Body Battery: {latest_metric.body_battery_max}/100")
        if latest_metric.resting_hr is not None:
            lines.append(f"- Dinlenik Nabız: {latest_metric.resting_hr} bpm")
        if latest_metric.stress_avg is not None:
            lines.append(f"- Stres: {latest_metric.stress_avg}/100")
        if latest_metric.steps is not None:
            goal = f" / hedef {latest_metric.steps_goal}" if latest_metric.steps_goal else ""
            lines.append(f"- Adım: {latest_metric.steps}{goal}")
    else:
        lines.append("- Veri yok")
    lines.append("")

    lines.append("## Vücut Ölçümleri (son)")
    if latest_measurement:
        lines.append(f"- Tarih: {latest_measurement.date.strftime('%d.%m.%Y')}")
        if latest_measurement.weight_kg is not None:
            goal = f" (hedef {profile.weight_goal_kg:g} kg)" if profile.weight_goal_kg else ""
            lines.append(f"- Kilo: {latest_measurement.weight_kg:.1f} kg{goal}")
        if latest_measurement.fat_percent is not None:
            lines.append(f"- Yağ Oranı: {latest_measurement.fat_percent:.1f}%")
        if latest_measurement.lean_mass_kg is not None:
            lines.append(f"- Yağsız Kütle: {latest_measurement.lean_mass_kg:.1f} kg")
        extra = []
        if latest_measurement.waist_cm is not None:
            extra.append(f"bel {latest_measurement.waist_cm:.0f} cm")
        if latest_measurement.chest_cm is not None:
            extra.append(f"göğüs {latest_measurement.chest_cm:.0f} cm")
        if extra:
            lines.append(f"- {', '.join(extra)}")
    else:
        lines.append("- Ölçüm verisi yok")
    lines.append("")

    lines.append("## Beslenme (bugün)")
    if today_nutrition:
        cal = f"{today_nutrition.calories:.0f}" if today_nutrition.calories is not None else "—"
        cal_goal = f"/{today_nutrition.calories_goal:.0f}" if today_nutrition.calories_goal else ""
        lines.append(f"- Kalori: {cal}{cal_goal} kcal")
        macro_bits = []
        if today_nutrition.protein_g is not None:
            macro_bits.append(f"Protein {today_nutrition.protein_g:.0f}g")
        if today_nutrition.carbs_g is not None:
            macro_bits.append(f"Karbonhidrat {today_nutrition.carbs_g:.0f}g")
        if today_nutrition.fat_g is not None:
            macro_bits.append(f"Yağ {today_nutrition.fat_g:.0f}g")
        if macro_bits:
            lines.append("- " + ", ".join(macro_bits))
    else:
        lines.append("- Bugün için beslenme verisi yok")
    lines.append("")

    if health_lines:
        lines.extend(health_lines)

    if not include_prompt:
        return "\n".join(lines).rstrip() + "\n"

    lines.append("---")
    lines.append("## Yorum ve Sıradaki Antrenman İçin")
    lines.append("")
    lines.append(
        "Önce son antrenmanımı yukarıdaki GPX verileri (GPS/nabız/kadans/elevasyon/güç, nokta "
        "yoğunluğu, km bazlı split tablosu, ilk yarı/ikinci yarı aerobik decoupling) ve toparlanma "
        "verileriyle (uyku, body battery, stres, dinlenik nabız) birlikte kısaca yorumla ve "
        "değerlendir: tempo/nabız dengesi nasıldı, split'ler arasında dikkat çeken bir tutarsızlık "
        "var mı, decoupling yüzdesi ne anlatıyor, toparlanma durumum bir sonraki antrenmanı nasıl "
        "etkilemeli. Bu yorumu serbest metin olarak yaz."
    )
    lines.append("")
    lines.append(
        "Sonra, SADECE bir sonraki antrenmanı öner (tek satır, ileri tarihli çoklu bir plan verme) — "
        "antrenman sonrası tekrar bu sayfaya geleceğim ve güncel verilerle bir sonrakini tekrar "
        "isteyeceğim, bu yüzden çok günlük bir plan eski/çelişkili kalabiliyor. Bu kısmı AŞAĞIDAKİ "
        "FORMATTA ver (alanları \"|\" ile ayır, tarih YYYY-AA-GG formatında olsun):"
    )
    lines.append("")
    lines.append("TARIH | TIP | BAŞLIK | MESAFE_KM | SURE_DK | NOT")
    lines.append("")
    lines.append("- TIP şunlardan biri olmalı: running, cycling, walking, swimming, strength, other")
    lines.append("- MESAFE_KM veya SURE_DK'dan en az biri dolu olsun (ikisi de olabilir), gerekmeyeni boş bırak")
    lines.append("- Dinlenme günü için TIP=other, MESAFE_KM ve SURE_DK boş, NOT'a \"dinlenme\" yaz")
    lines.append(
        "- BAŞLIK kısa ve İngilizce olsun, \"[Weekday] [Intensity]\" kalıbında yaz (antrenmanın "
        "tarihine karşılık gelen İngilizce gün adı + kısa bir yoğunluk ifadesi, ör. Monday Easy, "
        "Wednesday Tempo, Friday Intervals, Sunday Long, Saturday Rest) — uzun açıklayıcı Türkçe "
        "başlık yazma, detaylar zaten NOT alanında olacak"
    )
    lines.append("- Format satırının kendisini veya bu örneği tekrar etme, sadece gerçek tek plan satırını yaz")
    lines.append("")
    lines.append("Örnek:")
    lines.append("2026-08-16 | running | Sunday Tempo | 8 |  | rahat tempo, negatif split")
    lines.append("")
    lines.append(
        "Cevabının tamamını (yorum + plan satırı) kopyalayıp Kinetra'daki Koç sayfasına "
        "yapıştıracağım; Kinetra sadece TARIH ile başlayan satırları otomatik olarak okuyup "
        "plana ekleyecek, yorum kısmı görmezden gelinecek."
    )

    return "\n".join(lines)


def build_profile_brief(db: Session, profile: Profile, include_prompt: bool = True) -> str:
    """Bir profilin Koç özetini üretir: Claude.ai'ye kopyalanan metin ve Kinetra Koç sohbetine
    otomatik eklenen veri aynı fonksiyondan gelir. Sadece o profilin verisi kullanılır."""
    # Aktivite saatleri yerel saatle saklanıyor; pencere de yerel saatle hesaplanmalı (UTC 3 saat kaydırıyordu).
    now = dt.datetime.now()
    today = dt.date.today()
    last_activity = _pick_last_activity_for_brief(db, profile.id)

    def deduped(start: dt.datetime, end: dt.datetime | None = None) -> list[Activity]:
        # Aynı antrenman Garmin + Strava'dan ayrı satır olarak gelir; toplamlarda iki kez sayılmasın.
        q = db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= start)
        if end is not None:
            q = q.filter(Activity.start_time < end)
        return [item["activity"] for item in group_activities(q.all())]

    week_start = now - dt.timedelta(days=7)
    week_acts = deduped(week_start)
    prev_week_acts = deduped(now - dt.timedelta(days=14), week_start)
    recent = sorted(deduped(now - dt.timedelta(days=14)), key=lambda a: a.start_time, reverse=True)

    latest_metric = (
        db.query(DailyMetric).filter(DailyMetric.profile_id == profile.id).order_by(DailyMetric.date.desc()).first()
    )
    latest_measurement = (
        db.query(BodyMeasurement)
        .filter(BodyMeasurement.profile_id == profile.id)
        .order_by(BodyMeasurement.date.desc())
        .first()
    )
    today_nutrition = (
        db.query(NutritionLog).filter(NutritionLog.profile_id == profile.id, NutritionLog.date == today).first()
    )
    upcoming = active_plans(db, profile.id).filter(PlannedWorkout.date >= today).order_by(PlannedWorkout.date.asc()).all()

    return _build_brief_markdown(
        profile,
        today,
        last_activity,
        len(week_acts),
        sum(a.distance_m or 0 for a in week_acts) / 1000,
        format_duration(sum(a.duration_s or 0 for a in week_acts)),
        sum(a.calories or 0 for a in week_acts),
        sum(a.distance_m or 0 for a in prev_week_acts) / 1000,
        latest_metric,
        today_nutrition,
        latest_measurement,
        recent_activities=recent,
        upcoming=upcoming,
        health_lines=training.brief_lines(db, profile) + plan_status_lines(db, profile) + health.brief_lines(db, profile),
        include_prompt=include_prompt,
    )


def _chat_context(db: Session, profile: Profile, lang: str = "tr") -> dict:
    from .. import coach_chat  # döngüsel import olmasın diye geç import

    thread = coach_chat.current_thread(db, profile)
    messages = []
    for m in thread.messages:
        if m.role not in ("user", "system", "assistant"):
            continue
        messages.append({
            "role": m.role,
            "text": i18n.t(lang, m.display_text or "") if m.role != "assistant" else (m.display_text or ""),
            "plan_lines": extract_plan_lines(m.display_text) if m.role == "assistant" else [],
            "cost_usd": m.cost_usd,
            "channel": m.channel,
        })
    return {
        "chat_messages": messages,
        "chat_ready": coach_chat.api_key_configured(),
        "chat_usage": coach_chat.usage_summary(db),
        "chat_month_profile_usd": coach_chat.month_cost(db, profile.id),
        "coach_instructions": profile.coach_instructions or "",
    }

@router.get("/coach-brief")
def coach_brief(
    request: Request,
    plan_added: int | None = None,
    plan_pushed: int | None = None,
    plan_skipped: int | None = None,
    tab: str = "sohbet",
    health_error: str | None = None,
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    today = dt.date.today()
    brief_text = build_profile_brief(db, current)
    upcoming = active_plans(db, current.id).filter(PlannedWorkout.date >= today).order_by(PlannedWorkout.date.asc()).all()
    has_garmin = (
        db.query(Credential)
        .filter(Credential.profile_id == current.id, Credential.source == "garmin")
        .first()
        is not None
    )

    return templates.TemplateResponse(
        request,
        "coach_brief.html",
        {
            "profiles": profiles,
            "active_profile": current,
            "brief_text": brief_text,
            "today": today,
            "upcoming": upcoming,
            "workout_type_labels": WORKOUT_TYPE_LABELS,
            "has_garmin": has_garmin,
            "plan_added": plan_added,
            "plan_pushed": plan_pushed,
            "plan_skipped": plan_skipped,
            **_chat_context(db, current, request.state.lang),
            "tab": tab if tab in ("saglik", "hedefler") else "sohbet",
            **_targets_context(db, current, today, request.state.lang),
            "health_error": health_error,
            "health_items": health.schedule_status(db, current),
            "health_records": db.query(HealthRecord).filter(HealthRecord.profile_id == current.id, HealthRecord.kind.isnot(None)).order_by(HealthRecord.created_at.desc()).all(),
            "health_kinds": health.KIND_LABELS,
        },
    )


def replace_existing_plans(db: Session, profile_id: str, dates: set[dt.date], garmin_payload: dict | None) -> list[str]:
    """Koçun onaylanan önerisi o günün henüz yapılmamış planının yerine geçer: eski plan silinir
    ve Garmin takviminden kaldırılır. Geçmiş günlere dokunulmaz. Kaldırılanların etiketlerini döner."""
    today = dt.date.today()
    removed = []
    for w in active_plans(db, profile_id).filter(PlannedWorkout.date.in_([d for d in dates if d >= today])).all():
        if w.status is not None:
            continue
        if w.garmin_workout_id and garmin_payload is not None:
            garmin_integration.unschedule_planned_workout(garmin_payload, w)
        removed.append(f"{w.date:%d.%m} {w.title}")
        db.delete(w)
    db.flush()
    return removed


def _targets_context(db: Session, profile: Profile, today: dt.date, lang: str = "tr") -> dict:
    start = training.week_start(today)
    week = training.week_status(db, profile, start, today)
    prev_start = start - dt.timedelta(weeks=1)
    phase = training.race_phase(db, profile.id, today)
    return {
        "week": week,
        "week_end": start + dt.timedelta(days=6),
        "prev_week_start": prev_start,
        "prev_week_end": start - dt.timedelta(days=1),
        "prev_week_summary": training.week_summary_line(training.week_status(db, profile, prev_start, today), lang),
        "week_summary": training.week_summary_line(week, lang),
        "week_gap": training.gap_text(week, lang),
        "week_template": training.week_template(profile),
        "week_template_text": training.template_text(training.week_template(profile), lang),
        "day_kinds": training.DAY_KINDS,
        "race_phase": phase,
        "race_phase_text": training.phase_text(phase, lang) if phase else "",
        "races": db.query(Race).filter(Race.profile_id == profile.id, Race.date >= today - dt.timedelta(days=60))
        .order_by(Race.date.asc()).all(),
        "race_priorities": training.PRIORITY_LABELS,
        "default_prep_weeks": training.default_prep_weeks,
    }


@router.post("/coach-brief/{profile_id}/week-template")
async def save_week_template(profile_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    profile = db.get(Profile, profile_id)
    if profile is not None:
        try:
            training.set_week_template(profile, [str(form.get(f"day{i}", "")) for i in range(7)])
            db.commit()
        except ValueError:
            pass
    return RedirectResponse(f"/coach-brief?profile={profile_id}&tab=hedefler", status_code=303)


@router.post("/coach-brief/{profile_id}/race")
def add_race(
    profile_id: str,
    name: str = Form(...),
    date: str = Form(...),
    distance_km: float = Form(...),
    priority: str = Form("A"),
    goal: str = Form(""),
    prep_weeks: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    race_date = _parse_plan_date(date)
    if db.get(Profile, profile_id) is not None and race_date and name.strip() and distance_km > 0:
        db.add(Race(
            profile_id=profile_id, date=race_date, name=name.strip(), distance_km=distance_km,
            priority=priority if priority in training.PRIORITY_LABELS else "A",
            goal=goal.strip() or None, notes=notes.strip() or None,
            prep_weeks=int(prep_weeks) if prep_weeks.strip().isdigit() and 1 <= int(prep_weeks) <= 30 else None,
        ))
        db.commit()
    return RedirectResponse(f"/coach-brief?profile={profile_id}&tab=hedefler", status_code=303)


@router.post("/coach-brief/{profile_id}/race/{race_id}/delete")
def delete_race(profile_id: str, race_id: str, db: Session = Depends(get_db)):
    race = db.get(Race, race_id)
    if race is not None and race.profile_id == profile_id:
        db.delete(race)
        db.commit()
    return RedirectResponse(f"/coach-brief?profile={profile_id}&tab=hedefler", status_code=303)


@router.post("/coach-brief/{profile_id}/plan/bulk")
def add_planned_workouts_bulk(
    profile_id: str,
    plan_text: str = Form(...),
    replace: str | None = Form(None),
    db: Session = Depends(get_db),
):
    rows, skipped = parse_plan_text(plan_text)

    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == "garmin")
        .first()
    )
    payload = decrypt_payload(credential.encrypted_payload) if credential else None
    if replace:  # sohbetteki koç önerisi: o günlerin eski planlarının yerine geçer
        replace_existing_plans(db, profile_id, {row["date"] for row in rows}, payload)

    pushed = 0
    for row in rows:
        workout = PlannedWorkout(profile_id=profile_id, **row)
        if payload is not None and row["workout_type"] in _GARMIN_PUSHABLE_TYPES:
            workout_id, error = garmin_integration.push_planned_workout(payload, workout)
            workout.garmin_workout_id = workout_id
            workout.garmin_error = error
            if workout_id:
                pushed += 1
        db.add(workout)

    db.commit()
    return RedirectResponse(
        f"/coach-brief?profile={profile_id}&plan_added={len(rows)}&plan_pushed={pushed}&plan_skipped={skipped}",
        status_code=303,
    )


@router.post("/coach-brief/{profile_id}/plan/{workout_id}/delete")
def delete_planned_workout(profile_id: str, workout_id: str, db: Session = Depends(get_db)):
    workout = db.get(PlannedWorkout, workout_id)
    if workout is not None and workout.profile_id == profile_id:
        db.delete(workout)
        db.commit()
    return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)


_WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WEEKDAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def _retitle_for_date(title: str, new_date: dt.date) -> str:
    """"Monday Easy" kalıbındaki başlıklarda gün adını yeni tarihe göre değiştirir;
    kalıba uymayan başlıklara dokunmaz."""
    first, sep, rest = title.partition(" ")
    for names in (_WEEKDAYS_EN, _WEEKDAYS_TR):
        if first in names:
            return f"{names[new_date.weekday()]}{sep}{rest}"
    return title


def active_plans(db: Session, profile_id: str):
    """Yapılmadı olarak işaretlenenler hariç planlı antrenmanlar (sorgu)."""
    return db.query(PlannedWorkout).filter(
        PlannedWorkout.profile_id == profile_id,
        or_(PlannedWorkout.status.is_(None), PlannedWorkout.status != "skipped"),
    )


def move_planned_workout(db: Session, workout: PlannedWorkout, target: dt.date) -> None:
    """Antrenmanı yeni güne taşır (başlıktaki gün adı ve Garmin takvimi dahil). Garmin'de hiçbir
    şey değişmediyse Kinetra'da da eski tarihte kalır; sonuç workout.garmin_error'da."""
    old_date = workout.date
    workout.date = target
    workout.title = _retitle_for_date(workout.title, target)

    if workout.garmin_workout_id:
        credential = (
            db.query(Credential)
            .filter(Credential.profile_id == workout.profile_id, Credential.source == "garmin")
            .first()
        )
        if credential is None:
            workout.garmin_error = "Kinetra'da taşındı ama Garmin bağlı olmadığı için Garmin takvimi güncellenemedi."
        else:
            payload = decrypt_payload(credential.encrypted_payload)
            moved, error = garmin_integration.reschedule_planned_workout(payload, workout, old_date)
            if not moved:
                # Garmin'de hiçbir şey değişmedi: Kinetra da eski tarihte kalsın ki tekrar denenebilsin.
                workout.date = old_date
                workout.title = _retitle_for_date(workout.title, old_date)
            workout.garmin_error = error

    db.add(workout)
    db.commit()


def plan_status(db: Session, workout: PlannedWorkout, today: dt.date | None = None) -> str:
    """done | skipped | done_auto (aynı gün eşleşen aktivite var) | unknown (geçmiş, kayıt yok) | planned"""
    from .dashboard import _matches_type

    if workout.status in ("done", "skipped"):
        return workout.status
    day_start = dt.datetime.combine(workout.date, dt.time.min)
    acts = db.query(Activity).filter(
        Activity.profile_id == workout.profile_id,
        Activity.start_time >= day_start,
        Activity.start_time < day_start + dt.timedelta(days=1),
    ).all()
    if workout.workout_type != "other" and any(_matches_type(a, workout.workout_type) for a in acts):
        return "done_auto"
    return "unknown" if workout.date < (today or dt.date.today()) else "planned"


_PLAN_STATUS_TEXT = {
    "done": "✅ yapıldı (kendisi bildirdi)",
    "done_auto": "✅ yapıldı (aktivite kaydı var)",
    "skipped": "❌ yapılmadı (kendisi bildirdi)",
    "unknown": "❔ kayıt yok, durumu bildirilmedi",
}


def plan_status_lines(db: Session, profile: Profile) -> list[str]:
    """Son 7 günün planlarının durumu: koç sonraki önerilerde kaçırılan/taşınan antrenmanları görsün."""
    today = dt.date.today()
    rows = (
        db.query(PlannedWorkout)
        .filter(PlannedWorkout.profile_id == profile.id, PlannedWorkout.date >= today - dt.timedelta(days=7),
                PlannedWorkout.date <= today)
        .order_by(PlannedWorkout.date)
        .all()
    )
    lines = []
    for w in rows:
        status = plan_status(db, w, today)
        if status == "planned":  # bugünün henüz yapılmamış planı: durum metni yok
            continue
        lines.append(f"- {w.date:%d.%m} {w.title} ({WORKOUT_TYPE_LABELS.get(w.workout_type, w.workout_type)}): {_PLAN_STATUS_TEXT[status]}")
    return ["## Plan Durumu (son 7 gün)", *lines, ""] if lines else []


@router.post("/coach-brief/{profile_id}/plan/{workout_id}/reschedule")
def reschedule_planned_workout(
    profile_id: str,
    workout_id: str,
    new_date: str = Form(...),
    db: Session = Depends(get_db),
):
    workout = db.get(PlannedWorkout, workout_id)
    target = _parse_plan_date(new_date)
    if workout is None or workout.profile_id != profile_id or target is None or target == workout.date:
        return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)

    move_planned_workout(db, workout, target)
    db.add(workout)
    db.commit()
    return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)


@router.post("/coach-brief/{profile_id}/plan/{workout_id}/status")
def set_planned_workout_status(
    profile_id: str,
    workout_id: str,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    """Telegram'daki ✅/❌ butonlarının web karşılığı (❌ Garmin takviminden de kaldırır)."""
    from .. import telegram_bot

    workout = db.get(PlannedWorkout, workout_id)
    profile = db.get(Profile, profile_id)
    if workout is not None and profile is not None and workout.profile_id == profile_id and status in ("done", "skip"):
        telegram_bot._workout_action(db, profile, workout, status)
    return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)


@router.post("/coach-brief/{profile_id}/plan/{workout_id}/push-garmin")
def push_planned_workout_to_garmin(profile_id: str, workout_id: str, db: Session = Depends(get_db)):
    workout = db.get(PlannedWorkout, workout_id)
    if workout is None or workout.profile_id != profile_id:
        return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)

    credential = (
        db.query(Credential)
        .filter(Credential.profile_id == profile_id, Credential.source == "garmin")
        .first()
    )
    if credential is None:
        workout.garmin_error = "Garmin bağlı değil. Önce Ayarlar'dan Garmin'e bağlan."
        db.add(workout)
        db.commit()
        return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)

    payload = decrypt_payload(credential.encrypted_payload)
    workout_id_garmin, error = garmin_integration.push_planned_workout(payload, workout)
    workout.garmin_workout_id = workout_id_garmin
    workout.garmin_error = error
    db.add(workout)
    db.commit()
    return RedirectResponse(f"/coach-brief?profile={profile_id}", status_code=303)
