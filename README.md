# TISCODE 2FA — Proof of Concept

A web login demo that uses **TISCODE** (a short sound code) as the second authentication factor.

Developed during an Erasmus+ internship at the University of Padova (UNIPD), 2026.

## How it works

1. The user logs in with e-mail and password.
2. The user chooses **TISCODE** as the second factor.
3. The browser plays a short TISCODE sound: an opening marker (OM) followed by an infocore.
4. The TISCODE app on the user's phone recognises the sound and shows a notification.
5. The user taps the notification. The phone opens the `/ack` link, which confirms the login on the website.

The sound only reaches phones in the same room, so a successful login also proves that the user's phone is physically close to the computer.

For comparison, two classic one-time-code methods are also implemented:

- **OTP code**: a 6-digit code is delivered to the phone (`/inbox` page). This simulates SMS or e-mail delivery without network delay.
- **Authenticator app**: a standard TOTP code (RFC 6238), compatible with Google or Microsoft Authenticator (`/totp-setup` to scan the QR code).

## Features

- Selectable TISCODE length (5 to 1 s). Only the infocore is shortened; the opening marker is kept.
- Login time is measured for every method.
- **Experiment mode** for user studies: phone selector, a balanced fixed sound list, retry and "not recognised" buttons, and automatic logging to `results.csv`.
- `analyze.py` computes recognition rates with 95% Wilson confidence intervals and timing statistics with Mann-Whitney U tests, and draws the charts.

## Run locally

```bash
pip install -r requirements.txt
printf 'you@example.com\nyour-password\n' > .demo_login   # demo account (not committed)
python3 app.py                                           # http://localhost:5002
```

Phones must be on the same network and open `http://<computer-IP>:5002/...`.

## Deploy (Render)

Settings: start command `gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app`.

Use a **single worker**, because the demo keeps the login state in memory.

Environment variables:

| Variable | Purpose |
|---|---|
| `DEMO_EMAIL`, `DEMO_PASSWORD` | demo account |
| `EXPERIMENT_MODE` | `false` = public demo (experiment tools hidden) |
| `FLASK_SECRET` | random string (session cookies) |
| `TOTP_SECRET` | base32 key for the Authenticator demo |

The TISCODE links on the platform must point to `https://<your-app>.onrender.com/ack`.

## Limitations

- The demo serves one login at a time (in-memory state). It is not meant for production.
- The current TISCODE app needs one tap on the notification. A zero-touch flow, where the app confirms automatically, is future work.
