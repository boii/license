"""KISS license client untuk Python 3.9+.

Cara pakai:
    from license_client import LicenseClient, LicenseError

    client = LicenseClient(
        api_url="https://license.kin.my.id",
        signing_key="<sama dengan SIGNING_KEY di .env VPS>",
        product="myapp",
    )

    # Pertama kali user input key:
    res = client.activate("VPXNC-YP98C-T4BH9-APW5Q")
    if not res["valid"]:
        raise LicenseError(res["status"])

    # Setiap app start:
    if not client.check(saved_key):
        sys.exit("Lisensi tidak valid")

Hanya butuh dependency 'requests'. Install: pip install requests
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import time
import uuid
from typing import Any

import requests


class LicenseError(RuntimeError):
    """Dilempar saat lisensi tidak valid atau respons tidak terpercaya."""


class LicenseClient:
    def __init__(
        self,
        api_url: str,
        signing_key: str,
        product: str,
        *,
        config_dir: pathlib.Path | None = None,
        timeout: float = 10.0,
        offline_grace_days: int = 7,
    ):
        self.api_url = api_url.rstrip("/")
        self.signing_key = signing_key.encode()
        self.product = product
        self.timeout = timeout
        self.offline_grace_seconds = offline_grace_days * 86400
        self.config_dir = config_dir or (pathlib.Path.home() / ".config" / product)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    # ----- public -----
    def activate(self, key: str, fingerprint: str | None = None) -> dict[str, Any]:
        return self._call("/v1/activate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
            "fingerprint": fingerprint,
        })

    def validate(self, key: str) -> dict[str, Any]:
        return self._call("/v1/validate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def deactivate(self, key: str) -> dict[str, Any]:
        return self._call("/v1/deactivate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def check(self, key: str) -> bool:
        """Convenience: validate + grace period offline.

        - Online & valid → True, simpan last_ok.
        - Online & tidak valid → False.
        - Offline / network error → True kalau last_ok < N hari, else False.
        """
        try:
            res = self.validate(key)
        except requests.RequestException:
            return self._within_grace()

        if res.get("valid"):
            self._touch_last_ok()
            return True
        return False

    def machine_id(self) -> str:
        """Stable per-machine ID, disimpan di config dir."""
        p = self.config_dir / "machine.id"
        if not p.exists():
            p.write_text(uuid.uuid4().hex)
        return p.read_text().strip()

    # ----- internal -----
    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in body.items() if v is not None}
        r = requests.post(self.api_url + path, json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not self._verify(dict(data)):
            raise LicenseError("signature mismatch — koneksi tidak terpercaya")
        return data

    def _verify(self, resp: dict[str, Any]) -> bool:
        sig = resp.pop("signature", "")
        raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self.signing_key, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _touch_last_ok(self) -> None:
        (self.config_dir / "last_ok").write_text(str(int(time.time())))

    def _within_grace(self) -> bool:
        p = self.config_dir / "last_ok"
        if not p.exists():
            return False
        try:
            last = int(p.read_text().strip())
        except ValueError:
            return False
        return (int(time.time()) - last) < self.offline_grace_seconds


# --- Quick CLI untuk testing ---
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["activate", "validate", "deactivate", "check"])
    ap.add_argument("key")
    ap.add_argument("--api", default=os.getenv("LICENSE_API", "https://license.kin.my.id"))
    ap.add_argument("--key-secret", dest="signing_key",
                    default=os.getenv("LICENSE_SIGNING_KEY", ""))
    ap.add_argument("--product", default=os.getenv("LICENSE_PRODUCT", "myapp"))
    args = ap.parse_args()

    if not args.signing_key:
        raise SystemExit(
            "Set LICENSE_SIGNING_KEY env var atau --key-secret. "
            "Nilainya harus sama dengan SIGNING_KEY di .env server."
        )

    client = LicenseClient(args.api, args.signing_key, args.product)
    fn = getattr(client, args.action)
    res = fn(args.key)
    if isinstance(res, bool):
        print("OK" if res else "BLOCKED")
    else:
        print(json.dumps(res, indent=2))
