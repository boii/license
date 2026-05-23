/**
 * KISS license client untuk Node.js 18+, Bun, Deno.
 * Satu file, tanpa dependency. Pakai built-in `fetch`, `crypto`, `fs`.
 *
 * Cara pakai:
 *   import { LicenseClient } from "./license-client.mjs";
 *
 *   const client = new LicenseClient({
 *     apiUrl: "https://license.kin.my.id",
 *     signingKey: "<sama dengan SIGNING_KEY di .env VPS>",
 *     product: "myapp",
 *   });
 *
 *   // Pertama kali user input key:
 *   const res = await client.activate("VPXNC-YP98C-T4BH9-APW5Q");
 *   if (!res.valid) throw new Error(res.status);
 *
 *   // Setiap app start:
 *   if (!await client.check(savedKey)) process.exit(1);
 */
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export class LicenseError extends Error {}

export class LicenseClient {
  constructor({
    apiUrl,
    signingKey,
    product,
    configDir,
    timeoutMs = 10_000,
    offlineGraceDays = 7,
  }) {
    this.apiUrl = apiUrl.replace(/\/$/, "");
    this.signingKey = signingKey;
    this.product = product;
    this.timeoutMs = timeoutMs;
    this.offlineGraceMs = offlineGraceDays * 86_400_000;
    this.configDir = configDir ?? path.join(os.homedir(), ".config", product);
    fs.mkdirSync(this.configDir, { recursive: true });
  }

  // ----- public -----
  activate(key, fingerprint = undefined) {
    return this._call("/v1/activate", {
      key, machine_id: this.machineId(), product: this.product, fingerprint,
    });
  }

  validate(key) {
    return this._call("/v1/validate", {
      key, machine_id: this.machineId(), product: this.product,
    });
  }

  deactivate(key) {
    return this._call("/v1/deactivate", {
      key, machine_id: this.machineId(), product: this.product,
    });
  }

  /**
   * Convenience: validate + grace period offline.
   * - Online & valid → true, simpan last_ok.
   * - Online & tidak valid → false.
   * - Offline / error jaringan → true kalau last_ok < N hari, else false.
   */
  async check(key) {
    try {
      const res = await this.validate(key);
      if (res.valid) {
        this._touchLastOk();
        return true;
      }
      return false;
    } catch (err) {
      if (err instanceof LicenseError) throw err;     // signature mismatch tetap fatal
      return this._withinGrace();
    }
  }

  machineId() {
    const p = path.join(this.configDir, "machine.id");
    if (!fs.existsSync(p)) fs.writeFileSync(p, crypto.randomUUID());
    return fs.readFileSync(p, "utf8").trim();
  }

  // ----- internal -----
  async _call(p, body) {
    const clean = Object.fromEntries(
      Object.entries(body).filter(([, v]) => v !== undefined && v !== null)
    );
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
    let data;
    try {
      const r = await fetch(this.apiUrl + p, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(clean),
        signal: ctl.signal,
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
    } finally {
      clearTimeout(timer);
    }
    if (!this._verify({ ...data })) {
      throw new LicenseError("signature mismatch — koneksi tidak terpercaya");
    }
    return data;
  }

  _verify(resp) {
    const sig = resp.signature ?? "";
    delete resp.signature;
    const raw = JSON.stringify(sortKeys(resp));
    const expected = crypto.createHmac("sha256", this.signingKey)
      .update(raw).digest("hex");
    if (sig.length !== expected.length) return false;
    return crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
  }

  _touchLastOk() {
    fs.writeFileSync(path.join(this.configDir, "last_ok"), String(Date.now()));
  }

  _withinGrace() {
    const p = path.join(this.configDir, "last_ok");
    if (!fs.existsSync(p)) return false;
    const last = parseInt(fs.readFileSync(p, "utf8").trim(), 10);
    if (!Number.isFinite(last)) return false;
    return (Date.now() - last) < this.offlineGraceMs;
  }
}

function sortKeys(o) {
  if (Array.isArray(o)) return o.map(sortKeys);
  if (o && typeof o === "object") {
    return Object.keys(o).sort().reduce((acc, k) => {
      acc[k] = sortKeys(o[k]);
      return acc;
    }, {});
  }
  return o;
}

// --- Quick CLI ---
if (import.meta.url === `file://${process.argv[1]}`) {
  const [, , action, key] = process.argv;
  if (!["activate", "validate", "deactivate", "check"].includes(action) || !key) {
    console.error("Usage: node license-client.mjs {activate|validate|deactivate|check} <KEY>");
    process.exit(1);
  }
  const client = new LicenseClient({
    apiUrl: process.env.LICENSE_API ?? "https://license.kin.my.id",
    signingKey: process.env.LICENSE_SIGNING_KEY ?? "",
    product: process.env.LICENSE_PRODUCT ?? "myapp",
  });
  if (!client.signingKey) {
    console.error("Set LICENSE_SIGNING_KEY env var (sama dengan SIGNING_KEY server).");
    process.exit(1);
  }
  const res = await client[action](key);
  console.log(typeof res === "boolean" ? (res ? "OK" : "BLOCKED") : JSON.stringify(res, null, 2));
}
