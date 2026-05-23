# KISS License Server

A minimalist license server in the spirit of Keygen.sh / Cryptolens, kept as simple as possible.

- One Python container (FastAPI + Telegram bot in a single process)
- SQLite (file in the `./data` volume), no separate DB server
- License management entirely via a **Telegram bot**
- License validation over HTTP JSON, responses signed with **HMAC-SHA256**
- Usage logs stored in SQLite + push notifications for important events to Telegram

## Table of contents

- [Features](#features)
- [Setup on an Ubuntu VPS](#setup-on-an-ubuntu-vps)
- [`.env` configuration](#env-configuration)
- [Telegram commands](#telegram-commands)
- [HTTPS with Cloudflare proxy + Caddy](#https-with-cloudflare-proxy--caddy)
- [Client API](#client-api)
- [Client examples (Python & Node.js)](#client-examples-python--nodejs)
- [Backup & maintenance](#backup--maintenance)
- [Troubleshooting](#troubleshooting)

## Features

- Multi-product licenses (`product` field), per-license machine quota (`max_machines`), expiry, revoke/unrevoke, extend.
- Activate/validate/deactivate per stable `machine_id` (not MAC address).
- Audit log: every API call is recorded with IP, user-agent, and status. Push notifications to Telegram for important events (new activations, limit reached, expired, revoked, etc.).
- Automatic log retention (default 90 days, configurable to forever).
- No web panel. Management is fully done via Telegram, reducing the attack surface.

## Setup on an Ubuntu VPS

Assumes the VPS already has Docker and Docker Compose installed.

### 1. Prepare your Telegram token

- Chat **@BotFather** → `/newbot` → save the token (`123456:ABC...`).
- Chat **@userinfobot** → save your Telegram ID (a number).

### 2. Clone & configure

```bash
git clone https://github.com/boii/license.git
cd license
cp .env.example .env
nano .env
```

Generate a `SIGNING_KEY` (do not change it after clients are deployed):

```bash
openssl rand -hex 32
```

### 3. Build & start

```bash
docker compose up -d --build
docker compose logs -f license-server
```

Health check (on the VPS):

```bash
curl http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

Send `/start` to your bot in Telegram. If you get "Access denied", `ADMIN_IDS` in `.env` is wrong. Fix it, then `docker compose restart`.

### 4. Create your first license

From Telegram:

```
/new myapp 30 1
```

The bot replies with a key like `VPXNC-YP98C-T4BH9-APW5Q`.

## `.env` configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | – | Token from @BotFather |
| `ADMIN_IDS` | yes | – | Telegram user IDs of admins, comma-separated |
| `SIGNING_KEY` | yes | – | Long random string. `openssl rand -hex 32`. Must match the value embedded in clients |
| `ADMIN_API_TOKEN` | no | – | If set, `/v1/admin/*` requires the `X-Admin-Token` header |
| `EVENT_RETENTION_DAYS` | no | `90` | Usage log retention in days. `0` = keep forever |
| `DB_PATH` | no | `/srv/data/licenses.db` | SQLite path inside the container |
| `API_HOST` | no | `0.0.0.0` | Bind interface |
| `API_PORT` | no | `8080` | Bind port |

The host's `./data` folder is mapped to `/srv/data` in the container. Backing up that folder is enough.

## Telegram commands

License management:

```
/new [product] [days] [machines]   create license (days 0 = lifetime)
/list [n]                          recent licenses
/info <KEY>                        details + activations + log summary
/revoke <KEY>                      disable
/unrevoke <KEY>                    re-enable
/extend <KEY> <days>               extend (0 = lifetime)
/seats <KEY> <n>                   change max_machines
/reset <KEY> [machine_id]          clear activation(s)
/delete <KEY>                      delete permanently
```

Usage logs:

```
/log [n]                  last n global events (default 20)
/log <KEY> [n]            last n events for one license
/errors [n]               only failed events
/stats <KEY> [days]       stats (0 = all-time, default 7 days)
/mute  /unmute            event push notifications
```

Every call to `/v1/validate`, `/v1/activate`, `/v1/deactivate` is recorded to
SQLite. Important events (new activations, limit_reached, expired, revoked, etc.)
are automatically pushed to all admins in `ADMIN_IDS`. Use `/mute` if traffic gets noisy.

## HTTPS with Cloudflare proxy + Caddy

For production, **HTTPS is required**. License keys travel in plaintext without
TLS and can be intercepted on public networks. The setup below is field-tested:

- Cloudflare in front (orange proxy) → hides the VPS IP, DDoS protection.
- Caddy on the VPS as a reverse proxy to the container.
- Certificate via Let's Encrypt **DNS-01** (Cloudflare API). DNS-01 is required because Cloudflare terminates TLS, so TLS-ALPN-01 and HTTP-01 cannot pass through.

### 1. Cloudflare DNS record

- Add an `A` record → name `license`, value = your VPS public IP.
- Proxy status: **Proxied** (orange).
- SSL/TLS → Overview → mode **Full (strict)**. Not Flexible.

### 2. Open ports on the cloud firewall

Many VPS providers have a firewall outside UFW (Tencent Lighthouse, AWS Security Groups,
Alibaba ECS, etc.) that **closes** ports 80 and 443 by default. Open them first:

| Port | Protocol | Source |
|---|---|---|
| 80 | TCP | `0.0.0.0/0` |
| 443 | TCP | `0.0.0.0/0` |

Then on the VPS:

```bash
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443
sudo ufw enable
```

### 3. Bind the container to localhost only

Edit `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

Then `docker compose up -d`. Port 8080 is now reachable from localhost only; Caddy publishes it on 443.

### 4. Install Caddy + the Cloudflare plugin

```bash
sudo apt install -y caddy
sudo caddy add-package github.com/caddy-dns/cloudflare
caddy list-modules | grep cloudflare    # should list: dns.providers.cloudflare
```

### 5. Cloudflare API token

Go to https://dash.cloudflare.com/profile/api-tokens → **Create Token** → template
**Edit zone DNS** → Zone Resources = your domain → Create. Copy the token (shown only once).

Register it with systemd:

```bash
sudo systemctl edit caddy
```

Add:

```
[Service]
Environment=CF_API_TOKEN=your-real-token-no-quotes
```

```bash
sudo systemctl daemon-reload
```

### 6. Caddyfile

`/etc/caddy/Caddyfile`:

```
license.example.com {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Real-IP {http.request.header.CF-Connecting-IP}
    }
}
```

```bash
sudo systemctl restart caddy
sudo journalctl -u caddy -f
```

Wait until you see:

```
trying to solve challenge ... "challenge_type":"dns-01"
authorization finalized
certificate obtained successfully
```

Test:

```bash
curl -I https://license.example.com/healthz
# HTTP/2 200
```

## Client API

Every endpoint returns JSON with a `signature` field (HMAC-SHA256 over the payload
without the signature field, JSON canonical: `sort_keys=True`, `separators=(",", ":")`).
Verify it on the client side so responses cannot be forged.

### `POST /v1/activate`

First time the user enters a key. Binds a new machine to the license.

```json
{
  "key": "VPXNC-YP98C-T4BH9-APW5Q",
  "machine_id": "stable-id-per-machine",
  "fingerprint": "optional app/os info",
  "product": "myapp"
}
```

### `POST /v1/validate`

On every app start. Does not add a machine. If `machine_id` is provided, the server checks that this machine has been activated.

### `POST /v1/deactivate`

Detach a machine from the license (e.g. user moves to a new laptop).

### Response

```json
{
  "valid": true,
  "status": "ok",
  "ts": 1779512345,
  "license": {
    "key": "VPXNC-YP98C-T4BH9-APW5Q",
    "product": "myapp",
    "expires_at": 1782104400,
    "max_machines": 2
  },
  "signature": "hex-hmac-sha256-64-chars"
}
```

Possible status values:

| Status | Meaning |
|---|---|
| `ok` / `activated` / `deactivated` | Success |
| `not_found` | Wrong key |
| `revoked` | Disabled by admin |
| `expired` | Expired |
| `product_mismatch` | Key belongs to a different product |
| `machine_limit_reached` | All seats taken |
| `machine_not_activated` | Machine has not been activated yet |

## Client examples (Python & Node.js)

The [`examples/`](examples/) folder ships ready-to-use clients — single file, no
dependencies. Just copy them into your app:

| File | Language | Usage |
|---|---|---|
| [`examples/python/license_client.py`](examples/python/license_client.py) | Python 3.9+ | `pip install requests`, copy the file, import |
| [`examples/nodejs/license-client.mjs`](examples/nodejs/license-client.mjs) | Node.js 18+ / Bun / Deno | copy and import. No npm install needed |
| [`examples/curl.sh`](examples/curl.sh) | bash + curl + jq | smoke test or server-to-server use |

Quick start (Python):

```python
from license_client import LicenseClient, LicenseError

client = LicenseClient(
    api_url="https://license.kin.my.id",
    signing_key="<same as SIGNING_KEY in .env on the VPS>",
    product="myapp",
)

# First time the user enters a key:
res = client.activate(user_input_key)
if not res["valid"]:
    raise LicenseError(res["status"])

# On every app start (already includes offline grace period):
if not client.check(saved_key):
    sys.exit("Invalid license")
```

Quick start (Node.js):

```js
import { LicenseClient } from "./license-client.mjs";

const client = new LicenseClient({
  apiUrl: "https://license.kin.my.id",
  signingKey: "<same as SIGNING_KEY in .env on the VPS>",
  product: "myapp",
});

const res = await client.activate(userInputKey);
if (!res.valid) throw new Error(res.status);

if (!await client.check(savedKey)) process.exit(1);
```

Quick test from the terminal:

```bash
LICENSE_SIGNING_KEY=xxx python examples/python/license_client.py validate VPXNC-...
LICENSE_SIGNING_KEY=xxx node examples/nodejs/license-client.mjs validate VPXNC-...
```

### Stable `machine_id` patterns

The safest cross-platform option: generate a UUID once and store it in the app's config dir.
Don't use the MAC address (it changes when switching Wi-Fi/Ethernet). For a more
OS-bound value, use:

- Linux: `/etc/machine-id`
- Windows: `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`
- macOS: `IOPlatformUUID` from `ioreg`

### Offline grace period

If the app is sometimes offline, store a `last_ok_at` timestamp on a successful
`validate`. If the next call fails **due to a network error** (not `valid:false`),
allow the app to run while `now - last_ok_at < N days`. After that, force the app online.

## Backup & maintenance

```bash
# Backup
tar czf license-backup-$(date +%F).tgz data/

# View logs
docker compose logs -f license-server

# Restart
docker compose restart license-server

# Update
git pull && docker compose up -d --build
```

The DB schema is handled by `CREATE TABLE IF NOT EXISTS` at boot. No manual migration needed.

## Troubleshooting

**Caddy fails certificate with `Cannot negotiate ALPN protocol "acme-tls/1"`**
Cloudflare's orange proxy is on but Caddy is still using TLS-ALPN-01. Switch to
DNS-01 (see the HTTPS section). After switching, if you hit a Let's Encrypt
rate limit ("too many failed authorizations"), wait an hour or use the staging
endpoint first.

**Cloudflare returns 522**
The origin can't be reached. Check in order:

1. `sudo ss -tlnp | grep -E ':80|:443'` → Caddy must be listening.
2. `sudo ufw status` → 80 and 443 must be ALLOW.
3. **Cloud provider firewall panel** (Tencent Lighthouse / AWS SG / etc.) → open 80 and 443. This is the most common cause of 522.

**Bot says "Access denied"**
`ADMIN_IDS` in `.env` is wrong. Your Telegram ID is shown in the rejection message. Edit `.env`, then `docker compose restart`.

**Signature mismatch on the client**
- The client's `SIGNING_KEY` doesn't exactly match the server's.
- The JSON isn't canonicalized (must use `sort_keys` and no spaces). See the example above.

**License can't be revoked / status stays active**
Make sure the key is exact (case-sensitive). Try `/info <KEY>` first to confirm the key exists.

## Security notes

- Use HTTPS for every public deployment. Plaintext license keys are easy to capture.
- Don't change `SIGNING_KEY` after clients are in the field — every old client will reject responses.
- Rotate `BOT_TOKEN` and `CF_API_TOKEN` if they ever leak (chat, screenshots, logs).
- `ADMIN_API_TOKEN` is optional; it gates HTTP access to `/v1/admin/*`. Primary management is always via Telegram.
- Verify `signature` on the client. TLS protects confidentiality, the signature protects integrity. Use both.
