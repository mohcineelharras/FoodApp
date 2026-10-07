# FoodApp

Counter pickup ordering. Browse the menu, keep a cart, and place an order under your name. The app does not collect card numbers.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m foodapp
```

Open http://127.0.0.1:8000. The server binds to loopback unless `FOODAPP_HOST` is set. Copy `.env.example` to `.env` only as a reminder of the variables; a process manager or the shell should provide them. Do not commit `.env`.

In development, an empty `FOODAPP_SECRET` becomes an ephemeral secret for that process, so sessions end when the process does. Production refuses to start until `FOODAPP_SECRET` is a unique value of at least 32 characters and `FOODAPP_ALLOWED_HOSTS` is set. Production cookies are always marked `Secure`.

Set `FOODAPP_TRUST_PROXY=1` only when a reverse proxy on loopback appends the client address to `X-Forwarded-For`.

The database file and its directory are readable only by the account that runs the app. A checkout page is tied to the cart it showed, so a change in another tab has to be reviewed again. Anonymous sessions expire after two hours, and new anonymous sessions are limited per address.

## Checks

```bash
pip install -r requirements.txt -r requirements-dev.txt
make lint
make test
make audit
```

GitHub Actions runs the same lint, tests, and dependency audit. This repository does not deploy from that workflow.
