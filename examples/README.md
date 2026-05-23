# Cara KISS integrasi ke aplikasi kamu

Ada 3 hal yang kamu butuhkan:

```
LICENSE_API  = "https://license.kin.my.id"          # endpoint server
SIGNING_KEY  = "<isi sama dengan .env di VPS>"      # untuk verifikasi signature
PRODUCT      = "myapp"                               # cocok dengan nama produk lisensi
```

`SIGNING_KEY` di-embed saat build aplikasi (bukan di-input user). Itu yang
bikin respons server tidak bisa dipalsukan.

## Pilih bahasa

| File | Bahasa | Cocok untuk |
|---|---|---|
| [`python/license_client.py`](python/license_client.py) | Python 3.9+ | desktop app, CLI, script internal |
| [`nodejs/license-client.mjs`](nodejs/license-client.mjs) | Node.js 18+ / Bun / Deno | Electron, CLI, web service |
| [`curl.sh`](curl.sh) | shell | smoke test atau pemakaian server-to-server |

Setiap file sengaja **single-file, tanpa dependency tambahan** (kecuali `requests` di Python).

## Pola integrasi 3 langkah

1. **Saat user pertama kali install & input license key:**
   ```python
   res = activate(user_input_key)
   if not res["valid"]:
       show_error(res["status"])  # tampilkan pesan sesuai status
       return
   save_key_to_local(user_input_key)
   ```

2. **Setiap kali aplikasi start:**
   ```python
   key = read_key_from_local()
   res = validate(key)
   if not res["valid"]:
       block_app(res["status"])
   ```

3. **Saat user mau pindah laptop / uninstall (opsional):**
   ```python
   deactivate(key)
   ```

## Status response yang harus kamu handle di UI

| `status`               | Pesan ke user                         |
|------------------------|---------------------------------------|
| `ok` / `activated`     | Lanjut, jangan tampilkan apa-apa      |
| `not_found`            | "License key salah, periksa kembali"  |
| `revoked`              | "License dimatikan, hubungi support"  |
| `expired`              | "License kedaluwarsa, perpanjang"     |
| `product_mismatch`     | "Key untuk produk lain"               |
| `machine_limit_reached`| "Batas mesin tercapai, deactivate dulu di mesin lain" |
| `machine_not_activated`| "Mesin belum diaktivasi"              |

## Grace period offline (opsional, recommended)

Aplikasi desktop kadang nggak online. Pola sederhana:

1. Saat `validate` sukses, simpan timestamp `last_ok_at` ke file lokal.
2. Saat `validate` gagal **karena network error** (bukan `valid:false`),
   izinkan jalan kalau `now - last_ok_at < 7 hari`.
3. Setelah 7 hari offline, paksa user online.

Implementasi sudah ada di kedua client di atas — tinggal pakai.
