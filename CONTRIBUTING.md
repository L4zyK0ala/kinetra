# Contributing to Kinetra

Thanks for helping out. Bug reports, fixes, translations and new integrations are all welcome.

## Before you start

- **Security issues:** don't open a public issue. Follow [SECURITY.md](SECURITY.md) instead.
- **Bigger changes** (a new integration, a new page, a schema change): open an issue first, so we can agree on the approach before you write the code.
- **Small fixes** (typos, small bugs, translations): a pull request is enough.

## Keep personal data out

Kinetra handles health and training data, so anything posted here is public. **Never include real personal data** in issues, pull requests, screenshots or test files:
- names, emails, GPS routes, heart-rate or lab values, health notes,
- API keys, Telegram bot tokens, cookies, `.env` contents.

Use the demo data for screenshots and examples.

## Development setup

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env    # put a key in FITDASH_ENCRYPTION_KEY (the command is in the file)
FITDASH_DATABASE_URL=sqlite:///demo.db venv/bin/python scripts/seed_demo.py --lang en
FITDASH_DATABASE_URL=sqlite:///demo.db venv/bin/uvicorn app.main:app --reload --port 8010
```

Open `http://127.0.0.1:8010` and pick a demo profile. You don't need any Garmin, Strava, Hevy, MyFitnessPal, Claude or Telegram account for UI work. `ANTHROPIC_API_KEY` and `TELEGRAM_BOT_TOKEN` can stay empty.

The SQLite schema is created on startup. When you add a column to a model, also add it to `_ensure_columns()` in `app/db.py`, so that existing databases get it too.

## Code style

- **Match the surrounding code:** naming, structure and comment density. Comments and docstrings in the code base are mostly Turkish; English is fine for new code.
- **Keep it simple:** no new frameworks or build steps. The UI is server-rendered Jinja2 with small inline scripts and Chart.js.
- **Colors:** in CSS, use the tokens in `app/static/css/style.css` (light and dark mode are defined there) rather than hard-coded colors.

## Translations

Turkish is the source language. Every user-facing text is written in Turkish in the code and translated through `app/locales/en.json`.

- **In templates:** `{{ _('Metin') }}`, or with placeholders: `{{ _('{n} koşu', n=count) }}`
- **In Python:** `i18n.t(lang, "Metin")`, or in Telegram code: `T(profile, "Metin")`

When you add or change a text, add its English version to `en.json`, then run:

```bash
venv/bin/python scripts/check_i18n.py
```

It reports missing, unused or mismatched entries (placeholders and HTML tags must match).

Adding a new language means a new catalog in `app/locales/` plus an entry in `LANGS` in `app/i18n.py`. Open an issue first and we'll sort out the details.

## Pull requests

- Keep each pull request to one topic.
- Describe what changed and how you tested it.
- For UI changes, add a screenshot made with demo data.
- Make sure `scripts/check_i18n.py` passes and the pages you touched load in both languages.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
