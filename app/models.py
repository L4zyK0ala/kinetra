from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    color = Column(String, nullable=False, default="#2563eb")
    weight_goal_kg = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)
    gender = Column(String, nullable=True)  # female | male
    avatar_path = Column(String, nullable=True)  # static/ altına göreli yol, örn. uploads/avatars/<dosya>.jpg
    # Profile özel Strava API uygulaması (Fernet ile şifreli {"client_id", "client_secret"}).
    # Strava yeni uygulamalara 1 bağlı sporcu kapasitesi veriyor; ikinci kişi .env'deki ortak
    # uygulamaya bağlanamadığı için kendi uygulamasını oluşturup burada kullanabiliyor.
    strava_app_encrypted = Column(Text, nullable=True)
    coach_instructions = Column(Text, nullable=True)  # Kinetra Koç sohbeti için profile özel talimatlar
    telegram_chat_id = Column(String, nullable=True)  # bu profile bağlı Telegram grubu
    telegram_link_code = Column(String, nullable=True)  # grupta /bagla KOD ile eşleştirme
    telegram_bot_encrypted = Column(Text, nullable=True)  # profilin kendi botu: şifreli {token, username}
    # Normal dönem haftalık düzeni: Pzt..Paz için "run" | "long" | "strength" | "rest" | "optional" (virgülle).
    # Boşsa training.DEFAULT_WEEK geçerli. Yarış dönemlerinde koç bu düzeni yarış fazına göre değiştirebilir.
    week_template = Column(String, nullable=True)
    language = Column(String, nullable=True)  # arayüz, koç ve Telegram dili: tr | en (boşsa tr)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    credentials = relationship("Credential", back_populates="profile", cascade="all, delete-orphan")
    activities = relationship("Activity", back_populates="profile", cascade="all, delete-orphan")
    daily_metrics = relationship("DailyMetric", back_populates="profile", cascade="all, delete-orphan")
    nutrition_logs = relationship("NutritionLog", back_populates="profile", cascade="all, delete-orphan")
    planned_workouts = relationship("PlannedWorkout", back_populates="profile", cascade="all, delete-orphan")
    body_measurements = relationship("BodyMeasurement", back_populates="profile", cascade="all, delete-orphan")


class Credential(Base):
    """Bir profilin bir veri kaynağına (garmin/strava/hevy/mfp) bağlantı bilgisi.

    encrypted_payload, crypto.encrypt_payload() ile şifrelenmiş bir JSON string'idir
    (garth session, strava refresh token, hevy api key, mfp email+şifre gibi).
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("profile_id", "source", name="uq_credential_profile_source"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    encrypted_payload = Column(Text, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)
    last_sync_message = Column(Text, nullable=True)

    profile = relationship("Profile", back_populates="credentials")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_activity_source_external_id"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    activity_type = Column(String, nullable=True)
    start_time = Column(DateTime, nullable=False)
    duration_s = Column(Float, nullable=True)
    distance_m = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)
    avg_hr = Column(Float, nullable=True)
    max_hr = Column(Float, nullable=True)
    elevation_gain_m = Column(Float, nullable=True)
    raw_json = Column(Text, nullable=True)

    profile = relationship("Profile", back_populates="activities")
    stream = relationship("ActivityStream", back_populates="activity", uselist=False, cascade="all, delete-orphan")


class ActivityStream(Base):
    """Bir aktivitenin GPS/HR/kadans zaman serisi verisi (şu an sadece Strava sağlıyor).

    data_json, aynı index'e hizalanmış eş-uzunlukta dizilerden oluşan bir JSON sözlüğüdür:
    {"time": [...], "distance": [...], "latlng": [[lat,lon],...], "altitude": [...],
     "heartrate": [...], "cadence": [...], "watts": [...]}
    """

    __tablename__ = "activity_streams"

    id = Column(String, primary_key=True, default=_uuid)
    activity_id = Column(String, ForeignKey("activities.id"), nullable=False, unique=True)
    point_count = Column(Integer, nullable=True)
    data_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    activity = relationship("Activity", back_populates="stream")


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("profile_id", "date", "source", name="uq_daily_metric_profile_date_source"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    source = Column(String, nullable=False)
    steps = Column(Integer, nullable=True)
    steps_goal = Column(Integer, nullable=True)
    resting_hr = Column(Integer, nullable=True)
    sleep_score = Column(Integer, nullable=True)
    sleep_duration_s = Column(Float, nullable=True)
    body_battery_min = Column(Integer, nullable=True)
    body_battery_max = Column(Integer, nullable=True)
    stress_avg = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="daily_metrics")


class NutritionLog(Base):
    __tablename__ = "nutrition_logs"
    __table_args__ = (
        UniqueConstraint("profile_id", "date", "source", name="uq_nutrition_profile_date_source"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    source = Column(String, nullable=False, default="mfp")
    calories = Column(Float, nullable=True)
    protein_g = Column(Float, nullable=True)
    carbs_g = Column(Float, nullable=True)
    fat_g = Column(Float, nullable=True)
    calories_goal = Column(Float, nullable=True)
    protein_goal_g = Column(Float, nullable=True)
    carbs_goal_g = Column(Float, nullable=True)
    fat_goal_g = Column(Float, nullable=True)
    sodium_mg = Column(Float, nullable=True)
    sodium_goal_mg = Column(Float, nullable=True)
    sugar_g = Column(Float, nullable=True)
    sugar_goal_g = Column(Float, nullable=True)
    fiber_g = Column(Float, nullable=True)
    fiber_goal_g = Column(Float, nullable=True)
    cholesterol_mg = Column(Float, nullable=True)
    cholesterol_goal_mg = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="nutrition_logs")


class BodyMeasurement(Base):
    """Vücut ölçümü (kilo, yağ oranı, çevre ölçümleri) — şu an Hevy'nin
    Measurements bölümünden geliyor, ileride başka kaynaklar da eklenebilir.
    """

    __tablename__ = "body_measurements"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_body_measurement_source_external_id"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    weight_kg = Column(Float, nullable=True)
    lean_mass_kg = Column(Float, nullable=True)
    fat_percent = Column(Float, nullable=True)
    neck_cm = Column(Float, nullable=True)
    shoulder_cm = Column(Float, nullable=True)
    chest_cm = Column(Float, nullable=True)
    left_bicep_cm = Column(Float, nullable=True)
    right_bicep_cm = Column(Float, nullable=True)
    left_forearm_cm = Column(Float, nullable=True)
    right_forearm_cm = Column(Float, nullable=True)
    abdomen_cm = Column(Float, nullable=True)
    waist_cm = Column(Float, nullable=True)
    hips_cm = Column(Float, nullable=True)
    left_thigh_cm = Column(Float, nullable=True)
    right_thigh_cm = Column(Float, nullable=True)
    left_calf_cm = Column(Float, nullable=True)
    right_calf_cm = Column(Float, nullable=True)

    profile = relationship("Profile", back_populates="body_measurements")


class PlannedWorkout(Base):
    """Claude Spor Koçu'nun önerdiği ve kullanıcının elle girdiği yaklaşan antrenman.

    garmin_workout_id doluysa Garmin Connect takvimine gönderilmiş demektir.
    """

    __tablename__ = "planned_workouts"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    title = Column(String, nullable=False)
    workout_type = Column(String, nullable=False)  # running | cycling | walking | swimming | strength | other
    target_distance_m = Column(Float, nullable=True)
    target_duration_s = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    garmin_workout_id = Column(String, nullable=True)
    garmin_error = Column(Text, nullable=True)
    status = Column(String, nullable=True)  # None=planlandı | done | skipped (kullanıcı bildirdi)
    status_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    profile = relationship("Profile", back_populates="planned_workouts")


class CoachThread(Base):
    """Bir profilin Kinetra Koç sohbeti (Claude API). Her profilin sohbetleri ayrıdır."""

    __tablename__ = "coach_threads"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    snapshot_hash = Column(String, nullable=True)  # sohbete en son eklenen veri özetinin özeti
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    messages = relationship(
        "CoachMessage", back_populates="thread", cascade="all, delete-orphan", order_by="CoachMessage.seq"
    )


class CoachMessage(Base):
    """Sohbet mesajı. content_json API'ye aynen geri gönderilen içerik bloklarıdır (düşünme blokları
    dahil — Claude Opus 5.5'te bu bloklar konuşmaya bağlı olduğu için geçmiş hiç düzenlenmez,
    sadece sona eklenir). Asistan mesajlarında token kullanımı ve maliyet de saklanır."""

    __tablename__ = "coach_messages"

    id = Column(String, primary_key=True, default=_uuid)
    thread_id = Column(String, ForeignKey("coach_threads.id"), nullable=False, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    role = Column(String, nullable=False)  # user | system | assistant
    content_json = Column(Text, nullable=False)
    display_text = Column(Text, nullable=True)
    channel = Column(String, nullable=True)  # web | telegram | auto
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    model = Column(String, nullable=True)
    stop_reason = Column(String, nullable=True)
    request_id = Column(String, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)

    thread = relationship("CoachThread", back_populates="messages")


class PlanSuggestion(Base):
    """Koçun önerdiği, Telegram'da onay bekleyen plan satırları. Onaylanınca PlannedWorkout
    olur ve Garmin'e gönderilir; reddedilirse sadece işaretlenir."""

    __tablename__ = "plan_suggestions"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    plan_text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending | accepted | rejected
    result = Column(Text, nullable=True)
    telegram_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class HealthSchedule(Base):
    """Periyodik sağlık kontrolü: kan tahlili (6 ay), kardiyoloji (12 ay)."""

    __tablename__ = "health_schedules"
    __table_args__ = (UniqueConstraint("profile_id", "kind", name="uq_health_schedule_profile_kind"),)

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # blood | cardio
    interval_months = Column(Integer, nullable=False)
    last_date = Column(Date, nullable=True)
    last_reminded_at = Column(DateTime, nullable=True)


class HealthRecord(Base):
    """Tahlil/kardiyoloji belgesi veya doktor notu. Dosyalar static/ dışında saklanır (web'den
    doğrudan erişilemez). ai_summary Claude'un yorumu; coach_note koça giden kısa not."""

    __tablename__ = "health_records"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    kind = Column(String, nullable=True)  # blood | cardio | doctor_note | other | None (Telegram'da tür seçimi bekliyor)
    record_date = Column(Date, nullable=True)
    file_path = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    text_note = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    coach_note = Column(Text, nullable=True)
    cost_usd = Column(Float, nullable=True)
    source = Column(String, nullable=True)  # web | telegram
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Race(Base):
    """Hedef yarış. A önceliği yarış dönemini (hazırlık → taper → toparlanma) başlatır;
    B kısa taper'lı ara yarış, C antrenman koşusu olarak ele alınır."""

    __tablename__ = "races"

    id = Column(String, primary_key=True, default=_uuid)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    name = Column(String, nullable=False)
    distance_km = Column(Float, nullable=False)
    priority = Column(String, nullable=False, default="A")  # A | B | C
    goal = Column(String, nullable=True)  # ör. "1:50 altı" veya "rahat bitir"
    prep_weeks = Column(Integer, nullable=True)  # boşsa mesafeye göre varsayılan
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
