"""Arayüz dili (Türkçe / İngilizce).

Kaynak dil Türkçe: metinler kodda Türkçe yazılır, İngilizce karşılıkları locales/en.json'da
Türkçe metin anahtarıyla durur. Çevirisi olmayan metin Türkçe görünür (uygulama bozulmaz).
Dil profil başına saklanır (Profile.language); profil seçilmemişse kinetra_lang çerezi,
o da yoksa tarayıcının Accept-Language başlığı kullanılır.
"""
from __future__ import annotations

import json
from pathlib import Path

LANGS = {"tr": "Türkçe", "en": "English"}
DEFAULT_LANG = "tr"
LANG_COOKIE = "kinetra_lang"

_CATALOG_PATH = Path(__file__).resolve().parent / "locales" / "en.json"
_catalog: dict[str, str] = {}
_catalog_mtime = 0.0

MONTHS = {
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}
WEEKDAYS = {
    "tr": ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
WEEKDAYS_SHORT = {
    "tr": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}


def normalize(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def _en() -> dict[str, str]:
    """Katalog dosyası değiştiyse yeniden yükler (çeviri eklemek için servis yeniden başlatılmaz)."""
    global _catalog, _catalog_mtime
    try:
        mtime = _CATALOG_PATH.stat().st_mtime
    except OSError:
        return _catalog
    if mtime != _catalog_mtime:
        try:
            _catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
            _catalog_mtime = mtime
        except (OSError, ValueError):
            pass
    return _catalog


def t(lang: str | None, text: str, **kwargs) -> str:
    """text'i lang diline çevirir; kwargs varsa {ad} yer tutucularını doldurur."""
    if lang == "en" and text:
        text = _en().get(text, text)
    return text.format(**kwargs) if kwargs else text


def from_accept_language(header: str | None) -> str:
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return DEFAULT_LANG


def month_name(lang: str, month: int) -> str:
    return MONTHS[normalize(lang)][month - 1]


def weekday_name(lang: str, weekday: int, short: bool = False) -> str:
    return (WEEKDAYS_SHORT if short else WEEKDAYS)[normalize(lang)][weekday]


def number(lang: str, value: float | int | None, decimals: int = 0) -> str:
    """Binlik ayırıcılı sayı: tr 3.671 / 1,5 · en 3,671 / 1.5"""
    if value is None:
        return "—"
    text = f"{value:,.{decimals}f}"
    if normalize(lang) == "tr":
        text = text.replace(",", "§").replace(".", ",").replace("§", ".")
    return text
