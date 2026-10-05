from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _ensure_columns() -> None:
    """Alembic yok - SQLite'a yeni eklenen nullable kolonları ADD COLUMN ile tamamlar.

    Var olan bir fitdash.db üzerinde modele yeni kolon eklendiğinde
    create_all() bunu farketmez (sadece eksik tabloları oluşturur);
    bu fonksiyon eksik kolonları geriye dönük olarak ekler.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        def add_missing(table: str, columns: dict[str, str]) -> None:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, coltype in columns.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

        add_missing(
            "nutrition_logs",
            {
                "calories_goal": "FLOAT", "protein_goal_g": "FLOAT", "carbs_goal_g": "FLOAT", "fat_goal_g": "FLOAT",
                "sodium_mg": "FLOAT", "sodium_goal_mg": "FLOAT", "sugar_g": "FLOAT", "sugar_goal_g": "FLOAT",
                "fiber_g": "FLOAT", "fiber_goal_g": "FLOAT", "cholesterol_mg": "FLOAT", "cholesterol_goal_mg": "FLOAT",
            },
        )
        add_missing("daily_metrics", {"steps_goal": "INTEGER"})
        add_missing("profiles", {"weight_goal_kg": "FLOAT", "height_cm": "FLOAT", "gender": "TEXT", "avatar_path": "TEXT", "strava_app_encrypted": "TEXT", "coach_instructions": "TEXT",
                                  "telegram_chat_id": "TEXT", "telegram_link_code": "TEXT",
                                  "telegram_bot_encrypted": "TEXT", "week_template": "TEXT",
                                  "language": "TEXT"})
        add_missing("coach_messages", {"channel": "TEXT"})
        add_missing("planned_workouts", {"status": "TEXT", "status_at": "DATETIME"})


def init_db() -> None:
    from . import models  # noqa: F401  ensures models are registered before create_all

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
