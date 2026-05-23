# KISS integration guide

You only need three values:

```
LICENSE_API  = "https://license.kin.my.id"          # server endpoint
SIGNING_KEY  = "<same as .env on the VPS>"          # used to verify signatures
PRODUCT      = "myapp"                               # must match the license product
```

Embed `SIGNING_KEY` at build time (don't ask the user for it). That's what
makes server responses tamper-proof.

## Pick a language

| File | Language | Best for |
|---|---|---|
| [`python/license_client.py`](python/license_client.py) | Python 3.9+ | desktop apps, CLIs, internal scripts |
| [`nodejs/license-client.mjs`](nodejs/license-client.mjs) | Node.js 18+ / Bun / Deno | Electron, CLIs, web services |
| [`curl.sh`](curl.sh) | shell | smoke tests or server-to-server use |

Each file is intentionally **single-file with no extra dependencies** (except `requests` for the Python client).

## 3-step integration

1. **First time the user enters a license key:**
   ```python
   res = activate(user_input_key)
   if not res["valid"]:
       show_error(res["status"])  # show a message based on status
       return
   save_key_to_local(user_input_key)
   ```

2. **On every app start:**
   ```python
   key = read_key_from_local()
   res = validate(key)
   if not res["valid"]:
       block_app(res["status"])
   ```

3. **When the user moves machines / uninstalls (optional):**
   ```python
   deactivate(key)
   ```

## Response statuses to handle in your UI

| `status`               | Message to user                                      |
|------------------------|------------------------------------------------------|
| `ok` / `activated`     | Continue, no message needed                          |
| `not_found`            | "Invalid license key, please check"                  |
| `revoked`              | "License has been revoked, contact support"          |
| `expired`              | "License expired, please renew"                      |
| `product_mismatch`     | "Key belongs to a different product"                 |
| `machine_limit_reached`| "Machine limit reached, deactivate another device"   |
| `machine_not_activated`| "This machine is not activated"                      |

## Offline grace period (optional, recommended)

Desktop apps go offline sometimes. A simple pattern:

1. On a successful `validate`, store a `last_ok_at` timestamp locally.
2. If `validate` fails **due to a network error** (not `valid:false`),
   allow the app to run while `now - last_ok_at < 7 days`.
3. After 7 days offline, force the user online.

Both clients above already implement this — just use them.
