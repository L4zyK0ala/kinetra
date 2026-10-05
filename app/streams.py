from __future__ import annotations

import datetime as dt
import json

from sqlalchemy.orm import Session

from .models import Activity, ActivityStream


def load_stream_data(stream: ActivityStream | None) -> dict[str, list] | None:
    if stream is None:
        return None
    try:
        return json.loads(stream.data_json)
    except (TypeError, ValueError):
        return None


def find_sibling_with_stream(db: Session, activity: Activity) -> Activity | None:
    """Aktivitenin kendi stream'i yoksa, aynı gerçek antrenmanın (yakın başlangıç saatli)
    başka kaynaktan senkronize edilmiş kopyasında GPX stream'i olup olmadığına bakar.

    Feed'te aynı antrenman Garmin/Strava/Hevy'den ayrı satırlar olarak gelip
    activity_display.group_activities() tarafından tek öğede birleştirildiğinde, kardiyo
    için "primary" olarak Garmin seçiliyor — ama stream sadece Strava'dan geliyor. Kullanıcı
    feed'ten Garmin kopyasına tıklarsa detay sayfası/Coach brief GPX verisini kaybetmesin diye
    aynı zaman penceresindeki kardeş aktiviteler arasında stream'i olanı arar."""
    if activity.stream is not None:
        return activity
    from .activity_display import GROUP_TOLERANCE_S

    window = dt.timedelta(seconds=GROUP_TOLERANCE_S)
    candidates = (
        db.query(Activity)
        .filter(
            Activity.profile_id == activity.profile_id,
            Activity.id != activity.id,
            Activity.start_time >= activity.start_time - window,
            Activity.start_time <= activity.start_time + window,
        )
        .all()
    )
    for candidate in candidates:
        if candidate.stream is not None:
            return candidate
    return None


def has_gps(data: dict[str, list] | None) -> bool:
    return bool(data and data.get("latlng"))


def compute_splits(data: dict[str, list], split_m: float = 1000.0) -> list[dict]:
    """Distance + time dizilerinden km bazlı split tablosu üretir (pace, ort. nabız)."""
    distance = data.get("distance") or []
    time = data.get("time") or []
    heartrate = data.get("heartrate") or []
    if not distance or not time or len(distance) != len(time):
        return []

    splits = []
    next_threshold = split_m
    seg_start_idx = 0
    for i, d in enumerate(distance):
        if d >= next_threshold or i == len(distance) - 1:
            seg_distance = d - distance[seg_start_idx]
            seg_duration = time[i] - time[seg_start_idx]
            if seg_distance <= 0 or seg_duration <= 0:
                seg_start_idx = i
                next_threshold += split_m
                continue
            hr_slice = heartrate[seg_start_idx:i + 1] if heartrate else []
            avg_hr = round(sum(hr_slice) / len(hr_slice)) if hr_slice else None
            pace_s_per_km = seg_duration / (seg_distance / 1000)
            # Son segment split_m'e belirgin şekilde ulaşmadıysa (koşunun kalan kısa parçası),
            # tam km imiş gibi etiketlenmesin — pace o parça için ekstrapole edilmiş olur ve
            # gerçek bir km temposu gibi yorumlanırsa yanıltıcı olur.
            is_partial = seg_distance < split_m - 20
            splits.append({
                "index": len(splits) + 1,
                "distance_m": round(seg_distance),
                "duration_s": round(seg_duration),
                "pace_s_per_km": round(pace_s_per_km),
                "avg_hr": avg_hr,
                "is_partial": is_partial,
            })
            seg_start_idx = i
            next_threshold += split_m
    return splits


def gpx_metrics(data: dict[str, list] | None, is_run: bool = False) -> dict | None:
    """Coach brief'e eklenecek GPX türevi metrikler: Claude'un antrenman yorumu için
    öncelik sırasına göre (GPS/HR/nokta yoğunluğu kritik, kadans/elevasyon nice-to-have,
    güç bonus) özetlenmiş veriler. Sıcaklık/nem/algılanan efor GPX'te bulunmadığı için
    hiç eklenmez."""
    if not data:
        return None
    time = data.get("time") or []
    if not time:
        return None

    point_count = len(time)
    avg_interval = round((time[-1] - time[0]) / (len(time) - 1), 1) if len(time) >= 2 else None

    hr = data.get("heartrate") or []
    hr_stats = {"avg": round(sum(hr) / len(hr)), "max": round(max(hr)), "min": round(min(hr))} if hr else None

    cadence = data.get("cadence") or []
    # Strava koşu kadansını tek bacak için tutar (Garmin'in 176 spm'i Strava'da 88); koşuda ikiyle çarp.
    # Bisiklette kadans pedal devridir (rpm), olduğu gibi kalır.
    cadence = [c for c in cadence if c]
    cadence_avg = round(sum(cadence) / len(cadence) * (2 if is_run else 1)) if cadence else None

    altitude = data.get("altitude") or []
    elevation_gain_m = None
    if len(altitude) >= 2:
        gain = sum(max(0.0, altitude[i] - altitude[i - 1]) for i in range(1, len(altitude)))
        elevation_gain_m = round(gain)

    watts = data.get("watts") or []
    power_avg = round(sum(watts) / len(watts)) if watts else None

    return {
        "has_gps": has_gps(data),
        "point_count": point_count,
        "avg_sample_interval_s": avg_interval,
        "hr": hr_stats,
        "cadence_avg": cadence_avg,
        "elevation_gain_m": elevation_gain_m,
        "power_avg": power_avg,
    }


def compute_decoupling(data: dict[str, list] | None) -> dict | None:
    """İlk yarı / ikinci yarı pace+nabız karşılaştırması (aerobik decoupling).

    Efficiency factor (EF) = hız / nabız. İkinci yarıda EF düşüyorsa (decoupling_pct > 0)
    aynı efor için nabız yükseliyor demektir — yorgunluk/aerobik dayanıklılık sinyali."""
    if not data:
        return None
    distance = data.get("distance") or []
    time = data.get("time") or []
    heartrate = data.get("heartrate") or []
    if not distance or not time or not heartrate or len(distance) != len(time) or len(heartrate) != len(time):
        return None
    total_distance = distance[-1]
    if total_distance <= 0:
        return None

    half_distance = total_distance / 2
    split_idx = next((i for i, d in enumerate(distance) if d >= half_distance), len(distance) // 2)
    if split_idx <= 0 or split_idx >= len(distance) - 1:
        return None

    def half_stats(lo: int, hi: int) -> dict | None:
        d_seg = distance[hi] - distance[lo]
        t_seg = time[hi] - time[lo]
        hr_seg = heartrate[lo:hi + 1]
        if d_seg <= 0 or t_seg <= 0 or not hr_seg:
            return None
        avg_hr = sum(hr_seg) / len(hr_seg)
        if avg_hr <= 0:
            return None
        pace_s_per_km = t_seg / (d_seg / 1000)
        ef = (d_seg / t_seg) / avg_hr
        return {"avg_hr": round(avg_hr), "pace_s_per_km": round(pace_s_per_km), "ef": ef}

    first = half_stats(0, split_idx)
    second = half_stats(split_idx, len(distance) - 1)
    if not first or not second:
        return None

    decoupling_pct = (first["ef"] - second["ef"]) / first["ef"] * 100
    return {"first": first, "second": second, "decoupling_pct": round(decoupling_pct, 1)}


def stream_summary(data: dict[str, list] | None) -> dict:
    """Coach brief ve detay sayfası için GPX türevi özet: nokta yoğunluğu, HR/kadans/elevasyon/güç varlığı."""
    if not data:
        return {
            "has_gps": False, "point_count": 0, "has_hr": False, "has_cadence": False,
            "has_elevation": False, "has_power": False, "avg_sample_interval_s": None,
        }
    time = data.get("time") or []
    point_count = len(time) or len(data.get("distance") or [])
    avg_interval = None
    if len(time) >= 2:
        avg_interval = round((time[-1] - time[0]) / (len(time) - 1), 1)
    return {
        "has_gps": has_gps(data),
        "point_count": point_count,
        "has_hr": bool(data.get("heartrate")),
        "has_cadence": bool(data.get("cadence")),
        "has_elevation": bool(data.get("altitude")),
        "has_power": bool(data.get("watts")),
        "avg_sample_interval_s": avg_interval,
    }
