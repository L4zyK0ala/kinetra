from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from . import i18n

from .config import settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def local_dt(value: dt.datetime | None) -> dt.datetime | None:
    """Naive UTC datetime'ı (Credential.last_synced_at gibi dt.datetime.utcnow() ile üretilen
    alanlar) ekranda göstermeden önce yerel saat dilimine çevirir."""
    if value is None:
        return None
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(settings.local_timezone))


templates.env.filters["local_dt"] = local_dt


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset(path: str) -> str:
    """Statik dosya adresine değişiklik zamanını sürüm olarak ekler (style.css?v=...).

    Mobil Safari gibi tarayıcılar Last-Modified'a bakarak dosyayı sunucuya sormadan
    önbellekten kullanabiliyor; güncelleme sonrası eski CSS'in kalmaması için adres her
    değişiklikte farklılaşır."""
    try:
        version = int((_STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


templates.env.globals["asset"] = asset


def _ctx_lang(ctx) -> str:
    request = ctx.get("request")
    return getattr(getattr(request, "state", None), "lang", i18n.DEFAULT_LANG) if request is not None else i18n.DEFAULT_LANG


@pass_context
def _translate(ctx, text: str, **kwargs) -> str:
    return i18n.t(_ctx_lang(ctx), text, **kwargs)


@pass_context
def _lang(ctx) -> str:
    return _ctx_lang(ctx)


@pass_context
def _month(ctx, month: int) -> str:
    return i18n.month_name(_ctx_lang(ctx), month)


@pass_context
def _weekday(ctx, weekday: int, short: bool = False) -> str:
    return i18n.weekday_name(_ctx_lang(ctx), weekday, short)


@pass_context
def _number(ctx, value, decimals: int = 0) -> str:
    return i18n.number(_ctx_lang(ctx), value, decimals)


templates.env.globals.update(_=_translate, lang=_lang, month_name=_month, weekday_name=_weekday, LANGS=i18n.LANGS)
templates.env.filters["num"] = _number


_MONTHS_SHORT = {
    "tr": ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}


@pass_context
def _ldate(ctx, value, with_time: bool = True, long: bool = False) -> str:
    """Dile göre tarih: tr "01 Eki 2026, 19:24" · en "01 Oct 2026, 19:24" (long: tam ay adı)."""
    if value is None:
        return ""
    lang = _ctx_lang(ctx)
    month = i18n.month_name(lang, value.month) if long else _MONTHS_SHORT[i18n.normalize(lang)][value.month - 1]
    text = f"{value.day:02d} {month} {value.year}"
    if with_time and isinstance(value, dt.datetime):
        text += f", {value:%H:%M}"
    return text


templates.env.filters["ldate"] = _ldate
