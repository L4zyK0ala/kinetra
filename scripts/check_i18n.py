"""Çeviri kataloğu kontrolü: şablonlarda ve kodda kullanılan metinlerin app/locales/en.json'da
karşılığı var mı, katalogda artık kullanılmayan anahtar kaldı mı, yer tutucular ({ad}) ve HTML
etiketleri iki dilde aynı mı. Sorun bulursa 1 ile çıkar.

    python scripts/check_i18n.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FITDASH_ENCRYPTION_KEY", "x" * 43 + "=")  # sadece import için; anahtar kullanılmaz

keys: dict[str, str] = {}


def add(text: str, where: str) -> None:
    if text and text.strip():
        keys.setdefault(text, where)


# Şablonlar: _('...') ve _("...")
JINJA_STR = r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)\""""
for path in (APP / "templates").glob("*.html"):
    for m in re.finditer(r"(?<![\w.])_\(\s*(?:" + JINJA_STR + ")", path.read_text(encoding="utf-8")):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        add(raw.replace("\\'", "'").replace('\\"', '"'), path.name)


# Python: i18n.t(lang, "..."), t(lang, "..."), T(profile, "...") ve _HELP
class Calls(ast.NodeVisitor):
    def __init__(self, name: str, help_text: str | None) -> None:
        self.name, self.help_text = name, help_text

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if fname in ("t", "T") and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                add(arg.value, self.name)
            elif isinstance(arg, ast.Name) and arg.id == "_HELP" and self.help_text:
                add(self.help_text, self.name)
        self.generic_visit(node)


for path in APP.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    help_text = next((n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(getattr(t, "id", "") == "_HELP" for t in n.targets)), None)
    Calls(path.name, help_text).visit(tree)

# Şablonda _(değişken) ile çevrilen sabit sözlük değerleri ve sabit metinler
from app import activity_display, health, measurements, training  # noqa: E402
from app.routers import coach, dashboard  # noqa: E402

for mapping in (training.DAY_KINDS, training.PRIORITY_LABELS, health.KIND_LABELS, coach.WORKOUT_TYPE_LABELS,
                activity_display._LABELS, dashboard._PLAN_TYPE_LABELS):
    for value in mapping.values():
        add(value, "dynamic")
for _field, label, _unit in measurements.MEASUREMENT_FIELDS:
    add(label, "measurements")
for title, _unit, fields in measurements._CHART_GROUPS:
    add(title, "measurements")
    for _f, name in fields:
        if name:
            add(name, "measurements")
for text in ("Boy", "Dinlenik nabız", "Body Battery (zirve)", "Stres", "değişim yok", "İyi geceler", "Günaydın",
             "İyi günler", "İyi akşamlar", "Bugün", "Yarın", "📊 Güncel Kinetra verileri koça iletildi",
             "🌙 Akşam kontrolü", "🗓️ Haftalık plan", "Bağlandı, ilk senkronizasyon bekleniyor.", "Profil bulunamadı.",
             "Desteklenmeyen dosya türü. PDF veya fotoğraf (JPEG/PNG) gönder.", "Dosya çok büyük (maks. 20 MB).",
             "Desteklenmeyen dosya türü. JPEG, PNG, WEBP veya GIF yükle.", "Dosya çok büyük (maks. 8 MB).", "Boş dosya.",
             "Kinetra'da taşındı ama Garmin bağlı olmadığı için Garmin takvimi güncellenemedi."):
    add(text, "extra")

catalog = json.loads((APP / "locales" / "en.json").read_text(encoding="utf-8"))
missing = [(k, w) for k, w in keys.items() if k not in catalog]
unused = [k for k in catalog if k not in keys]
mismatched = [
    k for k, v in catalog.items()
    if sorted(re.findall(r"\{(\w*)\}", k)) != sorted(re.findall(r"\{(\w*)\}", v))
    or sorted(re.findall(r"</?(?:strong|code|b|br|p)\b", k)) != sorted(re.findall(r"</?(?:strong|code|b|br|p)\b", v))
]

for k, where in missing:
    print(f"EKSİK   [{where}] {k!r}")
for k in unused:
    print(f"FAZLA   {k!r}")
for k in mismatched:
    print(f"UYUMSUZ {k!r} → {catalog[k]!r}")
print(f"{len(keys)} metin, {len(catalog)} çeviri · eksik {len(missing)}, fazla {len(unused)}, uyumsuz {len(mismatched)}")
sys.exit(1 if missing or unused or mismatched else 0)
