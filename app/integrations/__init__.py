from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from ..models import Credential, Profile

SOURCE_LABELS = {
    "garmin": "Garmin Connect",
    "strava": "Strava",
    "hevy": "Hevy",
    "mfp": "MyFitnessPal",
}


@dataclass
class SyncResult:
    status: str  # "ok" | "error"
    message: str


# source -> (profile, db, credential, decrypted_payload) -> SyncResult
# credential.encrypted_payload çağrılan fonksiyon tarafından güncellenebilir
# (ör. yenilenmiş oturum/token'ı geri yazmak için) - commit çağıran tarafta yapılır.
SYNC_FUNCS: dict[str, Callable[[Profile, Session, Credential, dict], SyncResult]] = {}

# source -> (raw form data) -> dict payload to encrypt and store (raises ValueError on bad input)
CONNECT_FUNCS: dict[str, Callable[[dict], dict]] = {}


def register_sync(source: str) -> Callable:
    def deco(fn: Callable[[Profile, Session, dict], SyncResult]) -> Callable:
        SYNC_FUNCS[source] = fn
        return fn

    return deco


def register_connect(source: str) -> Callable:
    def deco(fn: Callable[[dict], dict]) -> Callable:
        CONNECT_FUNCS[source] = fn
        return fn

    return deco
