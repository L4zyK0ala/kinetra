# Security Policy

## Supported versions

Kinetra has no versioned releases. Only the latest commit on `main` gets security fixes; please update to it before reporting.

## Security model

Kinetra is built to run on a home network or behind a VPN. Please keep this in mind when reporting:

- **No login by design.** Anyone who can reach the app can open any profile. Exposing Kinetra directly to the internet is unsupported, so "no authentication" on its own is not a vulnerability.
- **Credentials** for Garmin, Strava, Hevy, MyFitnessPal and the Telegram bots are stored encrypted with `FITDASH_ENCRYPTION_KEY`.
- **Uploaded health documents** are stored outside the static web folder.

## In scope

For example:

- Ways to read or decrypt stored credentials or tokens
- Cross-site scripting through synced data (activity names, coach replies, notes) or uploads
- Path traversal or access to files outside the intended folders (health documents, avatars)
- A Telegram group or bot controlling or reading a profile it isn't linked to
- Secrets or personal data leaking into logs, error pages or the repository

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead: on the repository page, go to **Security → Report a vulnerability**.

Include the steps to reproduce it, the affected file or page, and the impact you expect.

You'll get a first reply within 7 days. If the report is confirmed, a fix goes to `main`, and you'll be credited in the commit unless you prefer otherwise. If it's declined, you'll get an explanation. This is a personal project, so timelines are best effort.
