# KISS License Server

License server minimalis ala Keygen.sh / Cryptolens, dijaga sesimpel mungkin.

- 1 container Python (FastAPI + Telegram bot dalam satu proses)
- SQLite (file di volume `./data`), tanpa DB server terpisah
- Pengelolaan lisensi sepenuhnya lewat **Telegram bot**
- Validasi lisensi via HTTP JSON, response ditandatangani **HMAC-SHA256**
- Log pemakaian tersimpan di SQLite + push notifikasi event penting ke Telegram

## Daftar isi

- [Fitur](#fitur)
- [Setup di VPS Ubuntu](#setup-di-vps-ubuntu)
- [Konfigurasi `.env`](#konfigurasi-env)
- [Perintah Telegram](#perintah-telegram)
- [HTTPS dengan Cloudflare proxy + Caddy](#https-dengan-cloudflare-proxy--caddy)
- [API untuk client app](#api-untuk-client-app)
- [Contoh client (Python & Node.js)](#contoh-client-python--nodejs)
- [Backup & maintenance](#backup--maintenance)
- [Troubleshooting](#troubleshooting)

## Fitur

- Lisensi multi-produk (`product` field), kuota mesin (`max_machines`), masa berlaku, revoke/unrevoke, extend.
- Activate/validate/deactivate per `machine_id` stabil (bukan MAC address).
- Audit log: setiap call API tercatat lengkap dengan IP, user-agent, status. Push notif Telegram untuk event penting (activation baru, limit_reached, expired, revoked, dll.).
- Retensi log otomatis (default 90 hari, bisa selamanya).
- Tanpa panel web. Manajemen sepenuhnya dari Telegram supaya tidak ada permukaan serang tambahan.

## Setup di VPS Ubuntu

Asumsi VPS sudah punya Docker + Docker Compose.

### 1. Siapkan token Telegram

- Chat **@BotFather** → `/newbot` → catat token (`123456:ABC...`).
- Chat **@userinfobot** → catat ID Telegram kamu (angka).

### 2. Clone & konfigurasi

```bash
git clone https://github.com/boii/license.git
cd license
cp .env.example .env
nano .env
```

Generate `SIGNING_KEY` (jangan diganti setelah ada client di lapangan):

```bash
openssl rand -hex 32
```

### 3. Build & start

```bash
docker compose up -d --build
docker compose logs -f license-server
```

Cek health (di VPS):

```bash
curl http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

Chat `/start` ke bot kamu di Telegram. Kalau dibalas teks "Akses ditolak", `ADMIN_IDS` di `.env` salah — perbaiki, lalu `docker compose restart`.

### 4. Buat lisensi pertama

Dari Telegram:

```
/new myapp 30 1
```

Bot membalas key seperti `VPXNC-YP98C-T4BH9-APW5Q`.

## Konfigurasi `.env`

| Var | Wajib | Default | Keterangan |
|---|---|---|---|
| `BOT_TOKEN` | ya | – | Token dari @BotFather |
| `ADMIN_IDS` | ya | – | Telegram user ID admin, koma sebagai pemisah |
| `SIGNING_KEY` | ya | – | Random panjang. `openssl rand -hex 32`. Sama persis dengan yang di-embed ke client |
| `ADMIN_API_TOKEN` | tidak | – | Jika di-set, endpoint `/v1/admin/*` butuh header `X-Admin-Token` bernilai sama |
| `EVENT_RETENTION_DAYS` | tidak | `90` | Hari retensi log pemakaian. `0` = selamanya |
| `DB_PATH` | tidak | `/srv/data/licenses.db` | Path SQLite di dalam container |
| `API_HOST` | tidak | `0.0.0.0` | Bind interface |
| `API_PORT` | tidak | `8080` | Bind port |

Volume `./data` di host dipetakan ke `/srv/data` di container. Backup cukup folder itu.

## Perintah Telegram

Kelola lisensi:

```
/new [product] [days] [machines]   buat lisensi (days 0 = lifetime)
/list [n]                          daftar lisensi terbaru
/info <KEY>                        detail + activations + ringkasan log
/revoke <KEY>                      matikan
/unrevoke <KEY>                    aktifkan lagi
/extend <KEY> <days>               perpanjang (0 = lifetime)
/seats <KEY> <n>                   ubah max_machines
/reset <KEY> [machine_id]          hapus activation
/delete <KEY>                      hapus permanen
```

Log pemakaian:

```
/log [n]                  n event terakhir global (default 20)
/log <KEY> [n]            n event terakhir untuk 1 lisensi
/errors [n]               hanya event gagal
/stats <KEY> [days]       statistik (0 = all-time, default 7 hari)
/mute  /unmute            push notifikasi event
```

Setiap call ke `/v1/validate`, `/v1/activate`, `/v1/deactivate` direkam ke
SQLite. Event penting (activation baru, limit_reached, expired, revoked, dst.)
otomatis di-push ke semua admin di `ADMIN_IDS`. Pakai `/mute` kalau traffic ramai.

## HTTPS dengan Cloudflare proxy + Caddy

Untuk produksi, **wajib pakai HTTPS**. License key terkirim plaintext kalau tanpa
TLS dan bisa dicuri di jaringan publik. Setup ini sudah teruji:

- Cloudflare di depan (proxy oranye) → hide IP VPS, DDoS protection.
- Caddy di VPS sebagai reverse proxy ke container.
- Sertifikat lewat Let's Encrypt **DNS-01** (Cloudflare API). Wajib DNS-01 karena Cloudflare yang terminate TLS, jadi TLS-ALPN-01 dan HTTP-01 tidak bisa lewat.

### 1. DNS record di Cloudflare

- Add record `A` → name `license`, value = IP publik VPS.
- Proxy status: **Proxied** (oranye).
- SSL/TLS → Overview → mode **Full (strict)**. Jangan Flexible.

### 2. Buka port di firewall provider

Banyak VPS punya firewall di luar UFW (Tencent Lighthouse, AWS Security Group,
Alibaba ECS, dll.) yang **default tutup** port 80 & 443. Buka dulu:

| Port | Protocol | Source |
|---|---|---|
| 80 | TCP | `0.0.0.0/0` |
| 443 | TCP | `0.0.0.0/0` |

Lalu di VPS:

```bash
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443
sudo ufw enable
```

### 3. Bind container hanya ke localhost

Edit `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

Lalu `docker compose up -d`. Sekarang port 8080 hanya bisa diakses dari localhost; Caddy yang akan publish ke 443.

### 4. Pasang Caddy + plugin Cloudflare

```bash
sudo apt install -y caddy
sudo caddy add-package github.com/caddy-dns/cloudflare
caddy list-modules | grep cloudflare    # harus muncul: dns.providers.cloudflare
```

### 5. Cloudflare API token

Di https://dash.cloudflare.com/profile/api-tokens → **Create Token** → template
**Edit zone DNS** → Zone Resources = domain kamu → Create. Copy token (cuma muncul sekali).

Daftarkan ke systemd:

```bash
sudo systemctl edit caddy
```

Tambahkan:

```
[Service]
Environment=CF_API_TOKEN=token-asli-tanpa-kutip
```

```bash
sudo systemctl daemon-reload
```

### 6. Caddyfile

`/etc/caddy/Caddyfile`:

```
license.contoh.com {
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

Tunggu sampai muncul:

```
trying to solve challenge ... "challenge_type":"dns-01"
authorization finalized
certificate obtained successfully
```

Tes:

```bash
curl -I https://license.contoh.com/healthz
# HTTP/2 200
```

## API untuk client app

Semua endpoint membalas JSON dengan field `signature` (HMAC-SHA256 atas payload
selain field signature, JSON canonical: `sort_keys=True`, `separators=(",", ":")`).
Verifikasi di sisi client agar respons tidak bisa dipalsukan.

### `POST /v1/activate`

Pertama kali user input key. Bind mesin baru ke lisensi.

```json
{
  "key": "VPXNC-YP98C-T4BH9-APW5Q",
  "machine_id": "stable-id-per-mesin",
  "fingerprint": "optional info app/os",
  "product": "myapp"
}
```

### `POST /v1/validate`

Setiap app start. Tidak menambah mesin. Kalau `machine_id` dikirim, server cek mesin itu sudah teraktivasi.

### `POST /v1/deactivate`

Lepas mesin dari lisensi (mis. user pindah laptop).

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

Status yang mungkin muncul:

| Status | Arti |
|---|---|
| `ok` / `activated` / `deactivated` | Sukses |
| `not_found` | Key salah ketik |
| `revoked` | Dimatikan admin |
| `expired` | Masa berlaku habis |
| `product_mismatch` | Key untuk produk lain |
| `machine_limit_reached` | Seat penuh |
| `machine_not_activated` | Mesin belum diaktivasi |

## Contoh client (Python & Node.js)

### Python

```python
import hashlib, hmac, json, pathlib, uuid, requests

LICENSE_API = "https://license.contoh.com"
PRODUCT     = "myapp"
SIGNING_KEY = "<sama dengan SIGNING_KEY di server>"

def machine_id() -> str:
    p = pathlib.Path.home() / ".config" / "myapp" / "machine.id"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(uuid.uuid4().hex)
    return p.read_text().strip()

def _verify(resp: dict) -> bool:
    sig = resp.pop("signature", "")
    raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(SIGNING_KEY.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

def _call(path: str, body: dict) -> dict:
    r = requests.post(f"{LICENSE_API}{path}", json=body, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not _verify(dict(data)):  # verify pakai copy karena pop signature
        raise RuntimeError("signature mismatch — koneksi tidak terpercaya")
    return data

def activate(key: str) -> dict:
    return _call("/v1/activate", {
        "key": key, "machine_id": machine_id(), "product": PRODUCT,
    })

def validate(key: str) -> dict:
    return _call("/v1/validate", {
        "key": key, "machine_id": machine_id(), "product": PRODUCT,
    })

# Pakai
res = activate("VPXNC-YP98C-T4BH9-APW5Q")
if not res["valid"]:
    raise SystemExit(f"Aktivasi gagal: {res['status']}")
```

### Node.js

```js
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const LICENSE_API = "https://license.contoh.com";
const PRODUCT     = "myapp";
const SIGNING_KEY = "<sama dengan SIGNING_KEY di server>";

function machineId() {
  const p = path.join(os.homedir(), ".config", "myapp", "machine.id");
  fs.mkdirSync(path.dirname(p), { recursive: true });
  if (!fs.existsSync(p)) fs.writeFileSync(p, crypto.randomUUID());
  return fs.readFileSync(p, "utf8").trim();
}

function sortKeys(o) {
  if (Array.isArray(o)) return o.map(sortKeys);
  if (o && typeof o === "object")
    return Object.keys(o).sort().reduce((a, k) => (a[k] = sortKeys(o[k]), a), {});
  return o;
}

function verify(resp) {
  const { signature, ...rest } = resp;
  const raw = JSON.stringify(sortKeys(rest));
  const expected = crypto.createHmac("sha256", SIGNING_KEY).update(raw).digest("hex");
  return signature.length === expected.length &&
         crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

async function call(p, body) {
  const r = await fetch(`${LICENSE_API}${p}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!verify({ ...data })) throw new Error("signature mismatch");
  return data;
}

export const activate = (key) => call("/v1/activate",
  { key, machine_id: machineId(), product: PRODUCT });
export const validate = (key) => call("/v1/validate",
  { key, machine_id: machineId(), product: PRODUCT });
```

### Pola `machine_id` yang stabil

Cross-platform paling aman: generate UUID sekali, simpan ke config dir aplikasi.
Jangan pakai MAC address (berubah saat ganti Wi-Fi/Ethernet). Kalau mau lebih
"engaged" ke OS, pakai:

- Linux: `/etc/machine-id`
- Windows: `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`
- macOS: `IOPlatformUUID` dari `ioreg`

### Grace period offline

Kalau aplikasi kadang offline, simpan timestamp `last_ok_at` saat `validate`
sukses. Kalau panggilan berikutnya gagal **karena network error** (bukan
`valid:false`), izinkan jalan kalau `now - last_ok_at < N hari`. Setelah itu
paksa online.

## Backup & maintenance

```bash
# Backup
tar czf license-backup-$(date +%F).tgz data/

# Lihat log
docker compose logs -f license-server

# Restart
docker compose restart license-server

# Update
git pull && docker compose up -d --build
```

Skema DB di-handle `CREATE TABLE IF NOT EXISTS` saat boot. Tidak perlu migrasi manual.

## Troubleshooting

**Caddy gagal cert dengan `Cannot negotiate ALPN protocol "acme-tls/1"`**
Berarti Cloudflare proxy oranye aktif tapi Caddy masih pakai TLS-ALPN-01. Pakai
DNS-01 (lihat bagian HTTPS). Setelah ganti, kalau kena rate-limit Let's Encrypt
("too many failed authorizations"), tunggu 1 jam atau pakai staging endpoint dulu.

**Cloudflare balas 522**
Origin tidak bisa dijangkau. Cek berurutan:

1. `sudo ss -tlnp | grep -E ':80|:443'` → Caddy harus listen.
2. `sudo ufw status` → 80 dan 443 harus ALLOW.
3. **Firewall di panel cloud provider** (Tencent Lighthouse / AWS SG / dll.) → buka 80 dan 443. Ini penyebab 522 paling sering.

**Bot bilang "Akses ditolak"**
`ADMIN_IDS` di `.env` salah. ID Telegram kamu ditampilkan di pesan tolakan. Edit `.env`, lalu `docker compose restart`.

**Signature mismatch di client**
- `SIGNING_KEY` di client tidak sama persis dengan di server.
- JSON tidak di-canonicalize (harus `sort_keys` + tanpa spasi). Lihat contoh di atas.

**Lisensi tidak bisa di-revoke / status tetap active**
Pastikan key persis (case-sensitive). Coba `/info <KEY>` dulu untuk konfirmasi key ada.

## Catatan keamanan

- Pakai HTTPS untuk semua deployment publik. License key plaintext mudah disadap.
- `SIGNING_KEY` jangan diganti setelah ada client di lapangan — semua client lama akan tolak respons.
- Rotate `BOT_TOKEN` dan `CF_API_TOKEN` kalau pernah ter-leak (chat, screenshot, log).
- `ADMIN_API_TOKEN` opsional; cukup untuk batasi siapa yang bisa hit `/v1/admin/*` via HTTP. Manajemen utama selalu via Telegram.
- Verifikasi `signature` di client wajib. TLS lindungi confidentiality, signature lindungi integrity. Pakai keduanya.
