"""HTTP API for client apps to validate / activate licenses.

Endpoints:
    POST /v1/validate
    POST /v1/activate
    POST /v1/deactivate
    GET  /v1/admin/licenses              (requires ADMIN_API_TOKEN if set)
    GET  /v1/admin/events                (same)
    GET  /healthz
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings
from .db import DB
from .notifier import Notifier


def _sign(payload: dict[str, Any], key: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _client_ip(request: Request) -> str | None:
    # Honor common reverse proxies (Nginx/Caddy) if present.
    for h in ("x-forwarded-for", "x-real-ip"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else None


class ValidateBody(BaseModel):
    key: str = Field(..., min_length=4)
    machine_id: str | None = None
    product: str | None = None


class ActivateBody(BaseModel):
    key: str = Field(..., min_length=4)
    machine_id: str = Field(..., min_length=1, max_length=128)
    fingerprint: str | None = Field(None, max_length=512)
    product: str | None = None


def _check_license(lic: dict[str, Any], product: str | None) -> tuple[bool, str]:
    if lic["status"] != "active":
        return False, "revoked"
    if product and lic["product"] != product:
        return False, "product_mismatch"
    if lic["expires_at"] and int(time.time()) > lic["expires_at"]:
        return False, "expired"
    return True, "ok"


def build_app(settings: Settings, db: DB, notifier: Notifier | None) -> FastAPI:
    app = FastAPI(title="KISS License Server", version="1.1.0")

    def _require_admin(token: str | None) -> None:
        if settings.admin_api_token and token != settings.admin_api_token:
            raise HTTPException(status_code=401, detail="unauthorized")

    def _response(ok: bool, status: str, lic: dict[str, Any] | None = None,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "valid": ok,
            "status": status,
            "ts": int(time.time()),
        }
        if lic is not None:
            payload["license"] = {
                "key": lic["key"],
                "product": lic["product"],
                "expires_at": lic["expires_at"],
                "max_machines": lic["max_machines"],
            }
        if extra:
            payload.update(extra)
        payload["signature"] = _sign(payload, settings.signing_key)
        return payload

    async def _record(
        request: Request, *, event: str, status: str,
        body: ValidateBody | ActivateBody,
    ) -> None:
        ip = _client_ip(request)
        ua = request.headers.get("user-agent")
        machine_id = getattr(body, "machine_id", None)
        await db.log_event(
            event=event, status=status,
            license_key=body.key, machine_id=machine_id,
            product=getattr(body, "product", None),
            ip=ip, user_agent=ua,
        )
        if notifier:
            notifier.fire(
                event=event, status=status,
                license_key=body.key, machine_id=machine_id, ip=ip,
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/validate")
    async def validate(body: ValidateBody, request: Request) -> dict[str, Any]:
        lic = await db.get_license(body.key)
        if not lic:
            await _record(request, event="validate", status="not_found", body=body)
            return _response(False, "not_found")
        ok, status = _check_license(lic, body.product)
        if not ok:
            await _record(request, event="validate", status=status, body=body)
            return _response(False, status, lic)
        if body.machine_id:
            acts = await db.list_activations(body.key)
            if not any(a["machine_id"] == body.machine_id for a in acts):
                await _record(request, event="validate",
                              status="machine_not_activated", body=body)
                return _response(False, "machine_not_activated", lic)
            await db.upsert_activation(body.key, body.machine_id, None)
        await _record(request, event="validate", status="ok", body=body)
        return _response(True, "ok", lic)

    @app.post("/v1/activate")
    async def activate(body: ActivateBody, request: Request) -> dict[str, Any]:
        lic = await db.get_license(body.key)
        if not lic:
            await _record(request, event="activate", status="not_found", body=body)
            return _response(False, "not_found")
        ok, status = _check_license(lic, body.product)
        if not ok:
            await _record(request, event="activate", status=status, body=body)
            return _response(False, status, lic)

        acts = await db.list_activations(body.key)
        already = any(a["machine_id"] == body.machine_id for a in acts)
        if not already and len(acts) >= lic["max_machines"]:
            await _record(request, event="activate",
                          status="machine_limit_reached", body=body)
            return _response(False, "machine_limit_reached", lic,
                             {"activations": len(acts)})

        is_new, total = await db.upsert_activation(
            body.key, body.machine_id, body.fingerprint
        )
        result = "activated" if is_new else "ok"
        await _record(request, event="activate", status=result, body=body)
        return _response(True, result, lic, {"activations": total})

    @app.post("/v1/deactivate")
    async def deactivate(body: ActivateBody, request: Request) -> dict[str, Any]:
        lic = await db.get_license(body.key)
        if not lic:
            await _record(request, event="deactivate", status="not_found", body=body)
            return _response(False, "not_found")
        removed = await db.remove_activation(body.key, body.machine_id)
        status = "deactivated" if removed else "machine_not_activated"
        await _record(request, event="deactivate", status=status, body=body)
        return _response(removed, status, lic)

    @app.get("/v1/admin/licenses")
    async def admin_list(
        x_admin_token: str | None = Header(default=None),
        limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        _require_admin(x_admin_token)
        rows = await db.list_licenses(limit=limit, offset=offset)
        return {"items": rows, "limit": limit, "offset": offset}

    @app.get("/v1/admin/events")
    async def admin_events(
        x_admin_token: str | None = Header(default=None),
        limit: int = 100, key: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        _require_admin(x_admin_token)
        rows = await db.recent_events(
            limit=max(1, min(limit, 500)),
            license_key=key, status=status,
        )
        return {"items": rows, "limit": limit}

    return app
