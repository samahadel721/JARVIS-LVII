"""
_mobile_core.py — نواة الموبايل (pairing + tokens + consent)

لا تُحمّل كإضافة — مكتبة مشتركة يستخدمها mobile_sync.
"""

from __future__ import annotations

import hashlib, secrets, time, json
from pathlib import Path
from typing import Optional

ORCH_DIR = Path(__file__).resolve().parent.parent / "config" / "orchestration"
PAIRING_FILE = ORCH_DIR / "mobile_pairing.json"
DEVICES_FILE = ORCH_DIR / "mobile_devices.json"
CONSENT_FILE = ORCH_DIR / "mobile_consents.json"

PAIR_TTL_S = 300
ACCESS_TTL_S = 24 * 3600
REFRESH_TTL_S = 30 * 24 * 3600
UPLOAD_TTL_S = 600

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # بدون O/0/I/1

def now_ts() -> float: return time.time()

def gen_pairing_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))

def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()

def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        import os; os.chmod(path, 0o600)
    except Exception: pass

def load_json(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else default
    except Exception: pass
    return default

def create_pairing() -> str:
    code = gen_pairing_code()
    save_json(PAIRING_FILE, {"code": code, "created": now_ts(), "used": False})
    return code

def verify_pairing(code: str) -> bool:
    data = load_json(PAIRING_FILE, {})
    if not data or data.get("used"): return False
    if time.time() - float(data.get("created", 0)) > PAIR_TTL_S: return False
    if str(data.get("code")).strip().upper() != str(code).strip().upper():
        return False
    data["used"] = True
    save_json(PAIRING_FILE, data)
    return True

def create_device(device_name: str, platform: str = "") -> dict:
    devices = load_json(DEVICES_FILE, {"devices": {}})
    dev_id = "dev_" + secrets.token_hex(6)
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    devices["devices"][dev_id] = {
        "id": dev_id, "name": device_name, "platform": platform,
        "created": now_ts(), "last_seen": now_ts(),
        "access_hash": hash_token(access), "refresh_hash": hash_token(refresh),
        "access_expires": now_ts() + ACCESS_TTL_S, "refresh_expires": now_ts() + REFRESH_TTL_S,
    }
    save_json(DEVICES_FILE, devices)
    return {"device_id": dev_id, "access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL_S}

def verify_token(token: str) -> Optional[dict]:
    devices = load_json(DEVICES_FILE, {"devices": {}})
    h = hash_token(token)
    for dev_id, dev in devices.get("devices", {}).items():
        if dev.get("access_hash") == h and time.time() < float(dev.get("access_expires", 0)):
            return dev
        # دعم X-API-Key بنفس الـ hash
    return None

def refresh_access(refresh_token: str) -> Optional[dict]:
    devices = load_json(DEVICES_FILE, {"devices": {}})
    h = hash_token(refresh_token)
    for dev_id, dev in devices["devices"].items():
        if dev.get("refresh_hash") == h and time.time() < float(dev.get("refresh_expires", 0)):
            new_access = secrets.token_urlsafe(32)
            dev["access_hash"] = hash_token(new_access)
            dev["access_expires"] = now_ts() + ACCESS_TTL_S
            dev["last_seen"] = now_ts()
            save_json(DEVICES_FILE, devices)
            return {"access_token": new_access, "expires_in": ACCESS_TTL_S}
    return None

def list_devices() -> list[dict]:
    devices = load_json(DEVICES_FILE, {"devices": {}})
    out = []
    for d in devices.get("devices", {}).values():
        out.append({"id": d.get("id"), "name": d.get("name"), "platform": d.get("platform"), "created": d.get("created")})
    return out

def revoke_device(device_id: str) -> bool:
    devices = load_json(DEVICES_FILE, {"devices": {}})
    if device_id in devices.get("devices", {}):
        del devices["devices"][device_id]
        save_json(DEVICES_FILE, devices)
        return True
    return False

# file consent (طلب ملف → pending → موافقة → توكن لمرة واحدة)
def request_file(task_id: str, filename: str, reason: str = "") -> str:
    cons = load_json(CONSENT_FILE, {"items": {}})
    cid = "cons_" + secrets.token_hex(4)
    cons["items"][cid] = {"id": cid, "task_id": task_id, "filename": filename, "reason": reason, "status": "pending", "created": now_ts()}
    save_json(CONSENT_FILE, cons)
    return cid

def approve_file(consent_id: str) -> Optional[str]:
    cons = load_json(CONSENT_FILE, {"items": {}})
    item = cons.get("items", {}).get(consent_id)
    if not item or item.get("status") != "pending":
        return None
    token = secrets.token_urlsafe(24)
    item["status"] = "approved"
    item["upload_token"] = hash_token(token)
    item["upload_expires"] = now_ts() + UPLOAD_TTL_S
    save_json(CONSENT_FILE, cons)
    return token

def verify_upload_token(token: str) -> Optional[dict]:
    cons = load_json(CONSENT_FILE, {"items": {}})
    h = hash_token(token)
    for cid, item in cons.get("items", {}).items():
        if item.get("upload_token") == h and time.time() < float(item.get("upload_expires", 0)) and item.get("status") == "approved":
            return item
    return None

def mark_uploaded(consent_id: str, path: str) -> None:
    cons = load_json(CONSENT_FILE, {"items": {}})
    if consent_id in cons.get("items", {}):
        cons["items"][consent_id]["status"] = "uploaded"
        cons["items"][consent_id]["path"] = path
        cons["items"][consent_id]["uploaded_at"] = now_ts()
        # امسح التوكن لمرة واحدة
        cons["items"][consent_id].pop("upload_token", None)
        save_json(CONSENT_FILE, cons)
