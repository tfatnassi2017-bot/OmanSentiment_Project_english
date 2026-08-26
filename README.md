# Omani Dialect Sentiment Collector — ENGLISH REVIEW COPY

**This is not the live data-collection app.** This is a standalone English
translation of the whole application — consent statement, all 60 questions,
region list, sentiment labels, and the admin dashboard — built specifically
so your supervisor can read and verify the content and flow without needing
Arabic.

A persistent banner ("🔎 SUPERVISOR REVIEW COPY...") is shown on every page
so nobody mistakes this for the real participant-facing site.

## Key differences from the production (Arabic) app

- **Everything is in English**: UI text, all 60 questions, region names,
  consent statement, admin dashboard.
- **The Arabic-language validation on submissions is disabled** here (see
  the comment above `is_mostly_arabic()` in `app.py`), so your supervisor
  can type English test sentences while trying the flow without hitting a
  "please write in Arabic" error. The production app keeps that check.
- **Uses a separate localStorage namespace** (`_en_review` suffix on every
  key) from the production app, so opening both in the same browser never
  causes one to interfere with the other's consent/progress state.
- **Must use a separate database** from production — see below. This is
  not optional: it prevents any test submissions here from ever mixing
  into your real collected dataset.

## Deployment — mirrors your production app's setup exactly

1. **Get a second free Postgres database.** Do not reuse your production
   `DATABASE_URL`. Either create a second database inside your existing
   Neon project, or spin up a second free project at neon.tech — either
   way, copy its connection string.
2. **Push this folder to a new GitHub repository** (or a new folder in an
   existing one) — separate from your production app's repo, to keep
   deployments independent.
3. **Create a second Render Web Service**, same steps as before:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
   - Environment variables: `DATABASE_URL` (the new one from step 1),
     `ADMIN_PASSWORD`, `FLASK_SECRET_KEY` (generate fresh values — don't
     reuse your production app's secrets).
4. Once deployed, you'll get a new `onrender.com` URL — that's the link to
   send your supervisor.

## Local testing (optional, before deploying)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export DATABASE_URL="postgresql://user:pass@host/dbname"   # the NEW, separate one
export ADMIN_PASSWORD="choose-a-strong-password"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

python app.py
```

Then open http://localhost:5000 (form) and http://localhost:5000/admin (dashboard).

## After your supervisor reviews it

This app was built for one purpose (content/flow verification) and isn't
meant to run indefinitely. Once your supervisor has approved everything:
- You can leave it running if useful for future reference, or
- Suspend/delete the Render service and Neon database to avoid any
  ongoing free-tier usage for something no longer needed.

Either way, **no data from this copy should ever be merged into your real
dataset** — treat anything submitted here as test data only.
