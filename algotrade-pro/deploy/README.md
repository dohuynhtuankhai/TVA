# Deploying AlgoPro Engine to a VPS

This folder contains everything needed to take AlgoPro live as a public,
HTTPS-served, auto-restarting service that can receive TradingView webhooks.

```
deploy/
├── setup.sh             one-shot provisioning script (Ubuntu 24.04/26.04)
├── algopro.service      systemd unit (single uvicorn worker)
├── nginx-algopro.conf   TLS reverse proxy + /ws upgrade + webhook
├── backup-db.sh         daily SQLite snapshot (cron)
└── README.md            this file
```

## Before you start

1. **A VPS** — 1–2 vCPU / 1–2 GB RAM / 20–40 GB SSD is plenty (SQLite + one
   worker). Hetzner, DigitalOcean, Vultr all fine. **Image: Ubuntu 26.04 LTS**
   (or 24.04). `requirements.txt` is pinned to versions with prebuilt CPython
   3.14 wheels, so the system Python on 26.04 is used directly — no uv, no PPA.
2. **Location** — pick a region with low latency to Binance (AWS Tokyo). Tokyo
   or Singapore keeps you inside the PRD's <500 ms webhook→exchange target. A
   US/EU box adds 150–250 ms per order.
3. **A domain** — TradingView only POSTs to ports 80/443 and needs a valid TLS
   cert, so you need a domain (or subdomain) with an A record pointing at the
   VPS's IP. You cannot use a bare `IP:port`.
4. **Static IP** — needed for Binance's API-key IP allowlist.

## Prerequisite: point DNS at the server first

Add an **A record** at your DNS provider (`@` or a subdomain) → the server's
IPv4, and confirm it resolves before running setup, because the script's TLS
step needs it:

```bash
dig +short your-domain.com    # must print the server IP
```

## Quick start

Get the code onto the server, then run the installer. Either transfer method works:

```bash
# Option A — from your machine over SSH:
scp -r algotrade-pro root@YOUR_VPS_IP:/root/
ssh root@YOUR_VPS_IP
cd /root/algotrade-pro

# Option B — clone from GitHub on the server (browser-only / no scp):
apt update && apt install -y git
git clone https://github.com/<you>/<repo>.git
cd <repo>/algotrade-pro

# Then, on the server:
bash deploy/setup.sh your-domain.com
```

The script installs dependencies, creates an `algopro` service user, builds the
venv under `/opt/algopro` (system Python 3.14), generates a `.env` with fresh
secrets, ensures the `static/` dir exists, installs the systemd + nginx configs,
opens the firewall (22/80/443 only), and provisions a Let's Encrypt certificate.

Then finish the two manual values:

```bash
nano /opt/algopro/algotrade-pro/.env   # set AUTH_USERNAME and AUTH_PASSWORD
systemctl restart algopro
```

Visit `https://your-domain.com/`, log in, add your Binance key, add mappings.

> The app refuses to start until `AUTH_USERNAME` and `AUTH_PASSWORD` are set — if
> you hit a **502 Bad Gateway**, that's almost always the cause. Check
> `journalctl -u algopro -n 20`.

## TradingView webhook

TradingView's webhook alerts **cannot send custom HTTP headers** — only a URL
and a JSON message body. The endpoint therefore accepts the `WEBHOOK_SECRET`
either as an `X-Webhook-Secret` header (for non-TradingView callers / curl) or
as a `?secret=` query parameter (the only option TradingView supports).

- **URL (TradingView):** `https://your-domain.com/api/webhook?secret=<WEBHOOK_SECRET>`
- **Header (optional, e.g. curl):** `X-Webhook-Secret: <WEBHOOK_SECRET>`
- **Body:** `{"symbol":"BTCUSDT","action":"LONG","timeframe":"5m","price":65000.0}`

Get the secret with: `grep WEBHOOK_SECRET /opt/algopro/algotrade-pro/.env`

Note: a secret passed in the URL can appear in nginx access logs. If that
matters to you, rotate `WEBHOOK_SECRET` periodically, or send it via the header
for non-TradingView integrations.

## Why a single worker (do not change this)

Three things live in-process and would break under multiple workers:

- **Sessions** are an in-memory dict in `auth.py` → extra workers cause random
  logouts.
- **The utility watcher** (`run_utility_watcher_loop`) starts once per process →
  extra workers send duplicate Telegram alerts.
- **Redis** is optional and falls back to in-memory anyway.

The systemd unit pins `--workers 1`. Scale the box vertically if ever needed,
never horizontally within one host.

## Secrets & data notes

- **`ENCRYPTION_KEY`** must be set before you add any account. Changing it later
  makes already-stored API secrets undecryptable.
- **`SESSION_SECRET`** is set by the script so logins survive restarts.
- **`TESTNET_MODE=false`** means real money on `fapi.binance.com` /
  `api.binance.com`. Leave it `true` first to smoke-test the full path.
- **`algotrade.db`** is SQLite in the working dir — fine for one user. Enable the
  daily backup:

```bash
( crontab -l 2>/dev/null; echo "30 3 * * * /opt/algopro/algotrade-pro/deploy/backup-db.sh" ) | sudo crontab -
```

## Operations

```bash
journalctl -u algopro -f                 # live logs
systemctl restart algopro                # restart after .env changes
systemctl is-active algopro              # quick up/down check
curl -s -o /dev/null -w "%{http_code}\n" https://your-domain.com/login   # expect 200
certbot renew --dry-run                  # cert auto-renews via systemd timer
```

Note: `/api/health` sits behind login and returns `{"detail":"Not authenticated"}`
without a session — that response still means the app is up. Use `/login`
returning `200` for an unauthenticated liveness check.

## Updating the app

If you deployed via `git clone`, pull and re-sync into the live dir:

```bash
cd ~/<repo> && git pull
rsync -a --exclude venv --exclude .env --exclude '*.db' \
    ~/<repo>/algotrade-pro/ /opt/algopro/algotrade-pro/
mkdir -p /opt/algopro/algotrade-pro/static
/opt/algopro/algotrade-pro/venv/bin/pip install -r \
    /opt/algopro/algotrade-pro/requirements.txt
chown -R algopro:algopro /opt/algopro
systemctl restart algopro
```

(Re-running `bash deploy/setup.sh your-domain.com` after a `git pull` does the
same thing idempotently and preserves your existing `.env`.)
