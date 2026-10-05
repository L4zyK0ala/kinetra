from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet

from .config import settings

_fernet = Fernet(settings.encryption_key)


def encrypt_payload(data: dict[str, Any]) -> str:
    return _fernet.encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")


def decrypt_payload(token: str) -> dict[str, Any]:
    return json.loads(_fernet.decrypt(token.encode("utf-8")).decode("utf-8"))
