# KISS License Server

License server minimalis ala Keygen.sh / Cryptolens, sengaja se-simpel mungkin:

- 1 container Python (FastAPI + Telegram bot dalam satu proses)
- SQLite (file di volume `./data`), tanpa DB server terpisah
- Pengelolaan lisensi sepenuhnya lewat Telegram bot
- Validasi lisensi via HTTP JSON, response ditandatangani HMAC-SHA256

## Setup di VPS Ubuntu

```bash
git clone <repo-ini> license && cd license
cp .env.example .env
nano .env        # isi BOT_TOKEN, ADMIN_IDS, SIGNING_KEY
docker compose up -d --build
docker compose logs -f
```

Cek health:

```bash
curl http://localhost:8080/healthz
```

Bot akan langsung online. Kirim `/start` ke bot untuk lihat perintah.

## Konfigurasi `.env`

| Var               | Wajib | Keterangan |
|-------------------|-------|-----------|
| `BOT_TOKEN`       | ya    | Token dari @BotFather |
| `ADMIN_IDS`       | ya    | Telegram user ID admin, koma. Cari via @userinfobot |
| `SIGNING_KEY`     | ya    | Random panjang, untuk HMAC. `openssl rand -hex 32` |
| `ADMIN_API_TOKEN` | tidak | Kalau di-set, endpoint `/v1/admin/*` butuh header `X-Admin-Token` |
| `EVENT_RETENTION_DAYS` | tidak | Hari retensi log pemakaian. Default 90, 0 = selamanya |
| `DB_PATH`         | -     | Default `/srv/data/licenses.db` |

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
/log <KEY> [n]            n event terakhir 1 lisensi
/errors [n]               hanya event gagal
/stats <KEY> [days]       statistik (0 = all-time, default 7 hari)
/mute  /unmute            push notifikasi event
```

Setiap call ke `/v1/validate`, `/v1/activate`, `/v1/deactivate` direkam ke
SQLite (`usage_events`) lengkap dengan IP, user-agent, machine_id, status.
Event penting (activation baru, limit_reached, expired, revoked, dst.)
otomatis di-push ke admin Telegram. Pakai `/mute` saat traffic ramai.

Retensi diatur lewat `EVENT_RETENTION_DAYS` (default 90 hari, set 0 untuk
simpan selamanya).

Contoh:

```
/new myapp 30 2
```

→ lisensi produk `myapp`, valid 30 hari, max 2 mesin.

## API untuk Client App

Semua endpoint balas JSON dengan field `signature` (HMAC-SHA256 dari payload
selain field signature, JSON canonical). Verifikasi di sisi client supaya
respon tidak bisa dipalsukan.

### `POST /v1/activate`

```json
{
  "key": "ABCDE-FGHIJ-KLMNO-PQRST",
  "machine_id": "stable-id-per-mesin",
  "fingerprint": "optional info",
  "product": "myapp"
}
```

Pertama kali dipanggil dari mesin baru, lisensi akan ter-bind ke mesin itu.
Kalau jumlah mesin sudah penuh, balasan `valid: false`, `status: machine_limit_reached`.

### `POST /v1/validate`

Sama bodynya dengan `/v1/activate` tapi tidak menambah mesin baru. Cocok dipanggil
saat app start. Kalau `machine_id` dikirim, server cek mesin itu sudah ter-aktivasi.

### `POST /v1/deactivate`

Lepas mesin dari lisensi (mis. user pindah laptop).

### Response field

```json
{
  "valid": true,
  "status": "ok",          // ok | not_found | revoked | expired | product_mismatch
                            // | machine_not_activated | machine_limit_reached
  "ts": 1700000000,
  "license": {
    "key": "...",
    "product": "myapp",
    "expires_at": 1701000000,
    "max_machines": 2
  },
  "signature": "hex-hmac-sha256"
}
```

### Verifikasi signature (Python contoh)

```python
import hashlib, hmac, json

def verify(resp: dict, signing_key: str) -> bool:
    sig = resp.pop("signature", "")
    raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(signing_key.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
```

## Backup

Cukup backup folder `./data/` (ada `licenses.db`).

## Catatan keamanan

- Pasang Nginx/Caddy di depan port 8080 untuk TLS sebelum produksi.
- Jaga `SIGNING_KEY` tetap rahasia. Jangan ganti setelah ada client di lapangan, karena signature akan invalid.
- `ADMIN_API_TOKEN` opsional; cukup untuk batasi siapa yang bisa list lisensi via HTTP. Manajemen utama tetap via Telegram.
