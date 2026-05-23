"""KISS license client for Python 3.9+.

Usage:
    from license_client import LicenseClient, LicenseError

    client = LicenseClient(
        api_url="https://license.kin.my.id",
        signing_key="<same as SIGNING_KEY in .env on the VPS>",
        product="myapp",
    )

    # First time the user enters a key:
    res = client.activate("VPXNC-YP98C-T4BH9-APW5Q")
    if not res["valid"]:
        raise LicenseError(res["status"])

    # On every app start:
    if not client.check(saved_key):
        sys.exit("Invalid license")

Only depends on `requests`. Install: pip install requests
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
    """Raised when the license is invalid or the response cannot be trusted."""


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
        """Convenience: validate + offline grace period.

        - Online & valid → True, store last_ok.
        - Online & invalid → False.
        - Offline / network error → True if last_ok < N days, else False.
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
        """Stable per-machine ID, stored in the config dir."""
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
            raise LicenseError("signature mismatch — untrusted response")
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


# --- Quick CLI for testing ---
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
            "Set the LICENSE_SIGNING_KEY env var or use --key-secret. "
            "It must match the SIGNING_KEY in the server's .env file."
        )

    client = LicenseClient(args.api, args.signing_key, args.product)
    fn = getattr(client, args.action)
    res = fn(args.key)
    if isinstance(res, bool):
        print("OK" if res else "BLOCKED")
    else:
        print(json.dumps(res, indent=2))
