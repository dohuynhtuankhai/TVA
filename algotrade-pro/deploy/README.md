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

## Quick start

```bash
# On your machine: copy the repo up (excludes venv/.env/db automatically via rsync in setup).
scp -r algotrade-pro root@YOUR_VPS_IP:/root/

# On the VPS:
cd /root/algotrade-pro
sudo bash deploy/setup.sh your-domain.com
```

The script installs dependencies, creates an `algopro` service user, builds the
venv under `/opt/algopro`, generates a `.env` with fresh secrets, installs the
systemd + nginx configs, opens the firewall (22/80/443 only), and provisions a
Let's Encrypt certificate.

Then finish the two manual values:

```bash
sudo nano /opt/algopro/algotrade-pro/.env   # set AUTH_USERNAME and AUTH_PASSWORD
sudo systemctl restart algopro
```

Visit `https://your-domain.com/`, log in, add your Binance key, add mappings.

## TradingView webhook

- **URL:** `https://your-domain.com/api/webhook`
- **Header:** `X-Webhook-Secret: <value from .env WEBHOOK_SECRET>`
- **Body:** `{"symbol":"BTCUSDT","action":"LONG","timeframe":"5m","price":65000.0}`

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
sudo systemctl restart algopro           # restart after .env changes
curl -s https://your-domain.com/api/health
sudo certbot renew --dry-run             # cert auto-renews via systemd timer
```

## Updating the app

```bash
# copy new code up, then on the VPS:
sudo rsync -a --exclude venv --exclude .env --exclude '*.db' \
    /root/algotrade-pro/ /opt/algopro/algotrade-pro/
sudo /opt/algopro/algotrade-pro/venv/bin/pip install -r \
    /opt/algopro/algotrade-pro/requirements.txt
sudo chown -R algopro:algopro /opt/algopro
sudo systemctl restart algopro
```
