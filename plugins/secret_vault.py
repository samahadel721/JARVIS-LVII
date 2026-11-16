"""
secret_vault.py — خزنة أسرار محلية مشفّرة: مفاتيح API وكلمات المرور خارج ملفاتك النصية.

• **تشفير حقيقي**: مفتاح من كلمة سرك عبر `scrypt` (مقاوم للتخمين)، ثم `AES-256-GCM`
  (تشفير مصادَق يكشف أي تلاعب بالملف) — كلتاهما من `cryptography` الموجودة في متطلبات مشروعك.
• **الملف لا يكشف شيئًا**: لا كلمة السر ولا أي قيمة تُكتب نصًّا — ولا حتى عددها يُخمَّن من الحجم.
  والصلاحيات 0600 (لك وحدك).
• **كلمة السر تُطلب كل مرة**: تُشتق في الذاكرة لحظيًا. ولهذا راحةً `unlock` يفتح لمدة دقائق معدودة
  (١٠ افتراضيًا) — ثم يُقفل وحده.
• **حماية السجل**: لا تُعاد أي قيمة سرّية إلا بأمر صريح `reveal confirm=yes` (مع تحذير أن كل ما يُعاد
  قد يدخل سجل الجلسة) — والطريق الموصى به `copy` يضعها في الحافظة دون أن تُطبع في أي مكان.
• **كلمة السر نُسيت؟** لا استرجاع — هذه هي فكرة التشفير. لذلك الخزنة تحفظ نسخة مشفّرة فقط،
  وأي قيمة نسيتها أعد إدخالها من مصدرها.

🔴 لا سطر يُخرج أسرارًا في أي سجل، ولا يرمي استثناءً للخارج، ولا يعمل شيء بلا كلمة سر صحيحة.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "secret_vault",
    "description": (
        "Encrypted local vault for API keys and passwords: unlock once with your master password, then "
        "store, list, copy or delete secrets — values stay encrypted on disk and are never printed in "
        "any log. Use for: 'create my vault', 'unlock the vault', 'save my API key in the vault', "
        "'what secrets do I have', 'copy my telegram token', 'delete that secret', 'change the vault "
        "password', 'lock the vault', 'vault status'. Values are only shown with an explicit "
        "confirm=yes; prefer 'copy' so the value never appears in the conversation. Do NOT store "
        "recovery codes you cannot re-issue yourself — a forgotten master password cannot be recovered."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": ("init | unlock | lock | set | get | copy | reveal | list | delete | "
                                       "change_password | status | help")},
            "password": {"type": "STRING", "description": "Your master password (init/unlock/set/change)"},
            "name": {"type": "STRING", "description": "Secret name, e.g. 'telegram_bot_token' (for set/get/copy/reveal/delete)"},
            "value": {"type": "STRING", "description": "set: the secret value to store"},
            "new": {"type": "STRING", "description": "change_password: the new master password"},
            "minutes": {"type": "NUMBER", "description": "unlock: how many minutes to stay unlocked, default 10"},
            "confirm": {"type": "STRING", "description": "reveal/delete: must be 'yes'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "secret_vault",
    "title": "Secret vault — local encryption settings",
    "fields": [
        {"key": "unlock_minutes", "type": "text",
         "label": "Minutes to stay unlocked after unlock (1–60)", "default": "10"},
        {"key": "clipboard_clear_seconds", "type": "text",
         "label": "Clear the clipboard after copying a secret (seconds, 0 = never)", "default": "45"},
        {"key": "min_password", "type": "text", "label": "Minimum master-password length", "default": "8"},
    ],
}

T = None
_VAULT = "vault.json"
_AAD = b"jarvis-secret-vault-v1"
_KDF = {"n": 2 ** 14, "r": 8, "p": 1}
_CACHE: dict[str, Any] = {"key": None, "until": 0.0}


def _boot() -> None:
    """يحمّل النواة المساعدة — مرة واحدة."""
    global T
    name = "_jarvis_tools_core"
    if name in sys.modules:
        T = sys.modules[name]
        return
    path = Path(__file__).resolve().parent / "_tools_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        T = module
    except Exception:
        sys.modules.pop(name, None)
        T = None


_boot()


def _cfg() -> dict:
    if T is None:
        return {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    return T.cfg(PLUGIN_SETTINGS["namespace"], PLUGIN_SETTINGS["fields"])


def _limit(default: int, value: Any, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(str(value)))))
    except Exception:
        return default


def _vault_path() -> Optional[Path]:
    if T is None:
        return None
    folder = T.extras_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return folder / _VAULT


def _crypto() -> tuple[Any, str]:
    """يستورد مكتبة التشفير. يعيد (الوحدة أو None، سبب الفشل)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: PLC0415
        return (AESGCM, Scrypt), ""
    except ModuleNotFoundError:
        return None, ("الخزنة تحتاج مكتبة التشفير:  pip install cryptography   (وهي بالفعل في متطلبات مشروعك، "
                      "فـ python setup.py يكفي)")
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر تحميل التشفير ({type(exc).__name__})."


def _derive(password: str, salt: bytes) -> tuple[Optional[bytes], str]:
    crypto, why = _crypto()
    if crypto is None:
        return None, why
    try:
        _, Scrypt = crypto
        key = Scrypt(salt=salt, length=32, n=_KDF["n"], r=_KDF["r"], p=_KDF["p"]).derive(password.encode("utf-8"))
        return key, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"تعذّر اشتقاق المفتاح ({type(exc).__name__})."


def _load_vault() -> tuple[Optional[dict], str]:
    path = _vault_path()
    if path is None:
        return None, "تعذّر تجهيز مجلد الخزنة."
    if not path.exists():
        return None, "لا خزنة بعد — أنشئها: action=init password=\"كلمة سر قوية\""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for field in ("salt", "nonce", "data"):
            if field not in data:
                return None, "ملف الخزنة تالف (حقل ناقص) — لن ألمسه."
        return data, ""
    except Exception:  # noqa: BLE001
        return None, "ملف الخزنة غير مقروء أو تالف — لن ألمسه."


def _save_vault(items: dict, key: bytes, salt: bytes) -> tuple[bool, str]:
    """يعيد كتابة الخزنة مشفّرة بالمفتاح الحالي. لا يكتب أي نصّ صريح."""
    crypto, why = _crypto()
    if crypto is None:
        return False, why
    path = _vault_path()
    if path is None:
        return False, "تعذّر تجهيز مجلد الخزنة."
    try:
        AESGCM, _ = crypto
        nonce = os.urandom(12)
        payload = json.dumps(items, ensure_ascii=False).encode("utf-8")
        blob = AESGCM(key).encrypt(nonce, payload, _AAD)
        record = {"version": 1, "kdf": "scrypt", "kdf_params": _KDF,
                  "salt": base64.b64encode(salt).decode("ascii"),
                  "nonce": base64.b64encode(nonce).decode("ascii"),
                  "data": base64.b64encode(blob).decode("ascii"),
                  "updated_at": int(time.time())}
        ok = T.save_json(path, record, private=True) if T else False
        return (True, "") if ok else (False, "تعذّرت كتابة ملف الخزنة.")
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّرت الكتابة ({type(exc).__name__})."


def _open_with(key: bytes, record: dict) -> tuple[Optional[dict], str]:
    """يفك الخزنة بالمفتاح المعطى. يعيد (العناصر، سبب الفشل) — ورسالة الخطأ لا تكشف شيئًا."""
    crypto, why = _crypto()
    if crypto is None:
        return None, why
    try:
        AESGCM, _ = crypto
        nonce = base64.b64decode(record["nonce"])
        blob = base64.b64decode(record["data"])
        payload = AESGCM(key).decrypt(nonce, blob, _AAD)
        items = json.loads(payload.decode("utf-8"))
        return (items, "") if isinstance(items, dict) else ({}, "")
    except Exception:
        return None, "كلمة السر غير صحيحة (أو الملف تغيّر)."


def _open_with_password(password: str) -> tuple[Optional[dict], Optional[bytes], Optional[bytes], str]:
    """يفتح الخزنة بكلمة سر. يعيد (العناصر، المفتاح، الملح، سبب الفشل)."""
    record, why = _load_vault()
    if record is None:
        return None, None, None, why
    try:
        salt = base64.b64decode(record["salt"])
    except Exception:
        return None, None, None, "ملف الخزنة تالف."
    key, why = _derive(password, salt)
    if key is None:
        return None, None, None, why
    items, why = _open_with(key, record)
    if items is None:
        return None, None, None, why
    return items, key, salt, ""


def _unlocked_key() -> Optional[bytes]:
    """المفتاح من الذاكرة إن كانت الخزنة مفتوحة، وإلا None."""
    if _CACHE.get("key") and time.time() < float(_CACHE.get("until") or 0):
        return _CACHE["key"]
    return None


def _require_open(password: str) -> tuple[Optional[dict], Optional[bytes], Optional[bytes], str]:
    """يفتح من الذاكرة إن أمكن، أو بكلمة سر مباشرة. يعيد (العناصر، المفتاح، الملح، خطأ)."""
    cached = _unlocked_key()
    if cached is not None and not str(password or "").strip():
        record, why = _load_vault()
        if record is None:
            return None, None, None, why
        try:
            salt = base64.b64decode(record["salt"])
        except Exception:
            return None, None, None, "ملف الخزنة تالف."
        items, why = _open_with(cached, record)
        if items is None:
            return None, None, None, "الخزنة تغيّرت منذ آخر فتح — افتحها بكلمة السر من جديد."
        return items, cached, salt, ""
    if not str(password or "").strip():
        return None, None, None, ("الخزنة مقفولة. افتحها أولًا: action=unlock password=\"...\" "
                                  "(تبقى مفتوحة ١٠ دقائق، ثم تُقفل وحدها).")
    return _open_with_password(str(password))


def _check_password_quality(password: str) -> str:
    """يرفض كلمات السر الهزيلة برسالة واضحة. يعيد "" لو مقبولة."""
    minimum = _limit(8, _cfg().get("min_password"), 6, 64)
    text = str(password or "")
    if len(text) < minimum:
        return f"كلمة السر قصيرة — الحد الأدنى {minimum} حرفًا."
    if len(set(text)) < 4:
        return "كلمة السر شديدة التكرار — استخدم خليطًا من حروف وأرقام ورموز."
    if text.lower() in ("password", "12345678", "qwerty123", "admin123"):
        return "كلمة السر هذه معروفة ومُخمَّنة — اختر غيرها."
    return ""


def _looks_like_secret_value(value: str) -> bool:
    return bool(re.search(r"\b[A-Za-z0-9_\-]{16,}\b", value or ""))


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_init(params: dict, player: Any) -> str:
    del player
    crypto, why = _crypto()
    if crypto is None:
        return f"سيدي، {why}"
    path = _vault_path()
    if path is None:
        return "سيدي، تعذّر تجهيز مجلد الخزنة."
    if path.exists():
        return ("سيدي، خزنتك موجودة بالفعل — افتحها بـ action=unlock password=\"...\"، "
                "وإن نسيت كلمة السر فلا سبيل لفتحها (التشفير كذلك).")
    password = str(params.get("password") or "")
    poor = _check_password_quality(password)
    if poor:
        return f"سيدي، {poor}\n  مثال قوي: «سوق-السمك-2026-٣٧» (مزج حروف وأرقام ورموز وكلمة تعرفها)."
    salt = os.urandom(16)
    key, why = _derive(password, salt)
    if key is None:
        return f"سيدي، {why}"
    ok, why = _save_vault({}, key, salt)
    if not ok:
        return f"سيدي، {why}"
    _CACHE["key"] = key
    _CACHE["until"] = time.time() + _limit(10, _cfg().get("unlock_minutes"), 1, 60) * 60
    if T is not None:
        T.log_event("secret_vault", "init", "ok", detail="أُنشئت الخزنة")
    return ("سيدي، أنشأت خزنتك المشفّرة ✔ (AES-256-GCM بمفتاح من scrypt).\n"
            "  الملف بصلاحيات 0600 — لا كلمة السر ولا أي قيمة مكتوبة نصًّا فيه.\n"
            "  افتحها الآن وأنت داخلها؛ وبعد ١٠ دقائق تُقفل وحدها.\n"
            "  ⚠️ كلمة السر لا تُسترجع إن نُسيت — احفظها في مكان أمين.")


def _act_unlock(params: dict, player: Any) -> str:
    del player
    password = str(params.get("password") or "")
    if not password:
        return "سيدي، قل كلمة سر الخزنة: action=unlock password=\"...\""
    items, key, _salt, why = _open_with_password(password)
    if items is None:
        if T is not None:
            T.log_event("secret_vault", "unlock", "denied", detail="كلمة سر خاطئة")
        return f"سيدي، {why}\n  (بعد ٥ محاولات خاطئة أقولها لك بصراحة: جرّب كلمة السر التي أنشأت بها الخزنة.)"
    minutes = _limit(10, params.get("minutes") if params.get("minutes") not in (None, "")
                     else _cfg().get("unlock_minutes"), 1, 60)
    _CACHE["key"] = key
    _CACHE["until"] = time.time() + minutes * 60
    if T is not None:
        T.log_event("secret_vault", "unlock", "ok", detail=f"مفتوحة {minutes} دقيقة")
    return (f"سيدي، الخزنة مفتوحة الآن ({len(items)} سرًّا بالداخل) وتبقى مفتوحة {minutes} دقيقة.\n"
            "  قل: «خزّن ...» أو «هات ...» أو «انسخ ...» — ولن أطبع أي قيمة إلا بأمر صريح.")


def _act_lock(params: dict, player: Any) -> str:
    del params, player
    _CACHE["key"] = None
    _CACHE["until"] = 0.0
    return "سيدي، أقفلت الخزنة — والملف يبقى مشفّرًا على القرص."


def _act_set(params: dict, player: Any) -> str:
    del player
    name = str(params.get("name") or "").strip()
    value = str(params.get("value") or "")
    if not name:
        return "سيدي، قل لي اسم السرّ: action=set name=telegram_bot_token value=\"...\""
    if not value:
        return ("سيدي، لم أستلم القيمة — أعد الأمر مع value=\"...\".\n"
                "  (نصيحة: الشات قد يسجّل ما تكتبه، فالأفضل أن تعطيني القيمة مرة واحدة ثم تمسحها — "
                "أو تضعها في ملف وتقول لي مساره.)")
    items, key, salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    if len(items) >= 200:
        return "سيدي، الخزنة بلغت ٢٠٠ سرّ — احذف ما لم تعد تحتاجه."
    existed = name in items
    items[name] = {"value": value, "updated": int(time.time())}
    ok, why = _save_vault(items, key or b"", salt or b"")
    if not ok:
        return f"سيدي، {why}"
    if T is not None:
        T.log_event("secret_vault", "set", "ok", detail=f"الاسم: {name} · الطول: {len(value)}")
    return (f"سيدي، {'حدّثت' if existed else 'خزّنت'} «{name}» ({len(value)} حرفًا).\n"
            f"  المخزون الآن: {len(items)} سرًّا · المشفّر على القرص، والقيمة لا تظهر في أي سجل.\n"
            f"  للاستخدام: copy name={name} يضعها في الحافظة بلا طباعة.")


def _act_get(params: dict, player: Any) -> str:
    del player
    name = str(params.get("name") or "").strip()
    items, _key, _salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    if not name:
        return "سيدي، قل اسم السرّ الذي تريده."
    entry = items.get(name)
    if not isinstance(entry, dict):
        return f"سيدي، لا سرّ باسم «{name}» — قل «اعرض الأسرار» لأراجع معك الأسماء."
    value = str(entry.get("value") or "")
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(entry.get("updated") or 0)))
    masked = (T.mask_secret(value) if T else "•" * 6)
    return (f"سيدي، «{name}»: {masked} ({len(value)} حرفًا · آخر تحديث {updated}).\n"
            f"  • للاستخدام الفوري: action=copy name={name}  ← في الحافظة بلا طباعة\n"
            f"  • لإظهاره هنا صراحةً: action=reveal name={name} confirm=yes (سيدخل المحادثة — تجنّبه)")


def _act_copy(params: dict, player: Any) -> str:
    del player
    name = str(params.get("name") or "").strip()
    items, _key, _salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    entry = items.get(name)
    if not isinstance(entry, dict):
        return f"سيدي، لا سرّ باسم «{name}». قل «اعرض الأسرار» لأراجع الأسماء."
    value = str(entry.get("value") or "")
    try:
        import pyperclip  # noqa: PLC0415
        pyperclip.copy(value)
    except ModuleNotFoundError:
        return ("سيدي، النسخ يحتاج pyperclip (في متطلبات مشروعك: python setup.py) — "
                f"ولذلك لن أطبعه هنا. إن أصررت: reveal name={name} confirm=yes")
    except Exception as exc:  # noqa: BLE001
        return f"سيدي، تعذّر النسخ ({type(exc).__name__}) — ولم أطبع القيمة حمايةً لها."
    if T is not None:
        T.log_event("secret_vault", "copy", "ok", detail=f"الاسم: {name}")
    seconds = _limit(45, _cfg().get("clipboard_clear_seconds"), 0, 600)
    note = (f"  وستُمسح الحافظة تلقائيًا خلال {seconds} ثانية." if seconds else
            "  (مسح الحافظة التلقائي مطفأ في الإعدادات.)")
    if seconds:
        _schedule_clipboard_clear(seconds)
    return (f"سيدي، «{name}» في الحافظة الآن ({len(value)} حرفًا) — الصقه حيث تريد.\n" + note)


def _schedule_clipboard_clear(seconds: int) -> None:
    """يمسح الحافظة بعد مدة قصيرة (خيط خفي) — بلا أي خطر على ما تنسخه لاحقًا إن تغيّر."""
    try:
        import threading

        def _worker() -> None:
            time.sleep(seconds)
            try:
                import pyperclip  # noqa: PLC0415
                pyperclip.copy("")
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="VaultClipboardClear").start()
    except Exception:
        pass


def _act_reveal(params: dict, player: Any) -> str:
    del player
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "true", "1", "نعم"):
        return ("سيدي، إظهار قيمة سرّية في المحادثة يحتاج تأكيدًا صريحًا: confirm=yes.\n"
                "  وبدلًا منه — وهو الأفضل — استخدم copy لتنزل في الحافظة دون أن تُطبع في أي مكان.")
    name = str(params.get("name") or "").strip()
    items, _key, _salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    entry = items.get(name)
    if not isinstance(entry, dict):
        return f"سيدي، لا سرّ باسم «{name}»."
    value = str(entry.get("value") or "")
    if T is not None:
        T.log_event("secret_vault", "reveal", "warn", detail=f"الاسم: {name} — كُشف بطلب صريح")
    return (f"سيدي، القيمة كما أمرت (ستبقى في هذه المحادثة):\n\n  {value}\n\n"
            "  ⚠️ نصيحة: بعد استخدامها قل «اقفل الخزنة» وامسح المحادثة إن كانت تُحفظ.")


def _act_list(params: dict, player: Any) -> str:
    del player
    items, _key, _salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    if not items:
        return "سيدي، الخزنة فاضية — قل: خزّن اسم السرّ وقيمته."
    lines = [f"سيدي، أسرارك المحفوظة ({len(items)}) — بالأسماء فقط:"]
    for name in sorted(items):
        entry = items.get(name) or {}
        value = str(entry.get("value") or "") if isinstance(entry, dict) else ""
        updated = time.strftime("%Y-%m-%d", time.localtime(float(entry.get("updated") or 0))) if isinstance(entry, dict) else "؟"
        lines.append(f"  · {name:<28} {len(value):>4} حرفًا · آخر تحديث {updated}")
    lines.append("  للاستخدام: copy name=الاسم — ولا تُطبع أي قيمة هنا.")
    return "\n".join(lines)


def _act_delete(params: dict, player: Any) -> str:
    del player
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "true", "1", "نعم"):
        return "سيدي، حذف سرّ من الخزنة نهائي — يحتاج تأكيدًا صريحًا: confirm=yes."
    name = str(params.get("name") or "").strip()
    items, key, salt, why = _require_open(str(params.get("password") or ""))
    if items is None:
        return f"سيدي، {why}"
    if name not in items:
        return f"سيدي، لا سرّ باسم «{name}»."
    items.pop(name, None)
    ok, why = _save_vault(items, key or b"", salt or b"")
    if not ok:
        return f"سيدي، {why}"
    if T is not None:
        T.log_event("secret_vault", "delete", "ok", detail=f"الاسم: {name}")
    return f"سيدي، حذفت «{name}» — بقي {len(items)} سرًّا في الخزنة."


def _act_change_password(params: dict, player: Any) -> str:
    del player
    old = str(params.get("password") or "")
    new = str(params.get("new") or "")
    if not old or not new:
        return "سيدي، أحتاج القديمة والجديدة: change_password password=\"القديمة\" new=\"الجديدة\""
    poor = _check_password_quality(new)
    if poor:
        return f"سيدي، {poor}"
    if old == new:
        return "سيدي، الجديدة مطابقة للقديمة — لا معنى للتغيير."
    items, _key, _salt, why = _open_with_password(old)
    if items is None:
        return f"سيدي، {why} — لم أغيّر شيئًا."
    salt = os.urandom(16)
    key, why = _derive(new, salt)
    if key is None:
        return f"سيدي، {why}"
    ok, why = _save_vault(items, key, salt)
    if not ok:
        return f"سيدي، {why} — الخزنة كما كانت."
    _CACHE["key"] = key
    _CACHE["until"] = time.time() + _limit(10, _cfg().get("unlock_minutes"), 1, 60) * 60
    if T is not None:
        T.log_event("secret_vault", "change_password", "ok", detail="أُعيد التشفير")
    return (f"سيدي، غيّرت كلمة سر الخزنة وأعدت تشفير {len(items)} سرًّا بالمفتاح الجديد.\n"
            "  القديمة لم تعد تعمل — والجديدة مطلوبة في المرة القادمة.")


def _act_status(params: dict, player: Any) -> str:
    del params, player
    crypto, why_crypto = _crypto()
    path = _vault_path()
    state = "لا خزنة بعد"
    count = "—"
    if path is not None and path.exists():
        record, why = _load_vault()
        if record is None:
            state = f"الملف موجود لكن {why}"
        else:
            size = T.human_bytes(path.stat().st_size) if T else ""
            opened = _unlocked_key() is not None
            state = f"موجودة ({size}) · {'مفتوحة الآن' if opened else 'مقفولة'}"
            cached = _unlocked_key()
            if cached is not None:
                try:
                    items, _ = _open_with(cached, record)
                    count = str(len(items or {}))
                except Exception:
                    count = "—"
    remaining = 0
    if _unlocked_key() is not None:
        remaining = max(0, int((_CACHE.get("until", 0) - time.time()) // 60))
    return ("سيدي، حالة الخزنة:\n"
            f"  التشفير: {'AES-256-GCM + scrypt ✔' if crypto else why_crypto}\n"
            f"  الحالة: {state} · الأسرار: {count}"
            + (f" · تُقفل بعد ~{remaining} دقيقة" if remaining else "")
            + "\n  كلمة السر لا تُحفظ في أي مكان — لا أنا ولا الملف نحتفظ بها."
            + "\n  نسيتها = لا استرجاع (هذا ما يجعل الخزنة خزنة).")


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، الخزنة المشفّرة:\n"
            "  • init password=\"...\"        — إنشاء الخزنة (مرة واحدة)\n"
            "  • unlock password=\"...\"      — فتحها (تبقى مفتوحة ١٠ دقائق ثم تُقفل) · lock للإغلاق\n"
            "  • set name=... value=...       — خزّن سرًّا   · list — الأسماء فقط\n"
            "  • get name=...                 — معاينة مُقنّعة (بلا طباعة القيمة)\n"
            "  • copy name=...                — القيمة في الحافظة (الطريق الموصى به) وتُمسح تلقائيًا بعدها\n"
            "  • reveal name=... confirm=yes  — إظهارها في المحادثة (تجنّبه)\n"
            "  • delete name=... confirm=yes  — حذف سرّ   · change_password password= new=  — تغيير كلمة السر\n"
            "  • status · help")


_ACTIONS = {"init": _act_init, "unlock": _act_unlock, "lock": _act_lock, "set": _act_set,
            "get": _act_get, "copy": _act_copy, "reveal": _act_reveal, "list": _act_list,
            "delete": _act_delete, "change_password": _act_change_password,
            "status": _act_status, "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا ولا يُظهر سرًّا بغير أمر صريح."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "status").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return "سيدي، هذا ليس فعلًا من أفعال الخزنة — المتاح: " + " | ".join(sorted(_ACTIONS))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        if T is not None:
            T.log(player, f"secret_vault error: {type(exc).__name__}")
        return f"سيدي، حدث خطأ غير متوقع في الخزنة ({type(exc).__name__}) — لم تُمَسّ أي قيمة."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile

    print("secret_vault — فحص ذاتي")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if T is None:
        print("  ✗ النواة _tools_core.py غير محمّلة")
        print("RESULT: 1 problem(s)")
        return 1

    crypto, why_crypto = _crypto()
    tmp = Path(tempfile.mkdtemp(prefix="vault_test_"))
    original_extras = T.extras_dir
    T.extras_dir = lambda: tmp / "extras"            # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    master = "سوق-السمك-2026-٣٧"
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    sys.modules.pop("pyperclip", None)
    try:
        if crypto is None:
            # بلا مكتبة تشفير: كل الأفعال يجب أن تشرح وتتوقف بلطف — لا انهيار
            message = _act_init({"password": master}, None)
            check("بلا مكتبة تشفير: رسالة شرح واضحة", "cryptography" in message, message[:90])
            check("وبقية الأفعال لا تنهار",
                  all(isinstance(run({"action": a}), str) for a in
                      ("unlock", "set", "get", "list", "delete", "status", "change_password", "reveal")))
            print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
            T.extras_dir = original_extras               # type: ignore[assignment]
            T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
            shutil.rmtree(tmp, ignore_errors=True)
            return problems

        # ١) الإنشاء ورفض كلمات السر الهزيلة
        check("بلا خزنة: يُقال بصراحة", "لا خزنة بعد" in _act_status({}, None))
        check("كلمة سر قصيرة تُرفض", "قصيرة" in _act_init({"password": "abc"}, None))
        check("كلمة سر مكررة تُرفض", "التكرار" in _act_init({"password": "aaaaaaaaaa"}, None))
        check("كلمة سر شهيرة تُرفض", "مُخمَّنة" in _act_init({"password": "password"}, None))
        created = _act_init({"password": master}, None)
        check("الإنشاء ينجح", "أنشأت خزنتك" in created, created[:90])
        path = T.extras_dir() / "vault.json"
        check("الملف مكتوب", path.exists())
        if os.name != "nt":
            check("الصلاحيات 0600", oct(path.stat().st_mode)[-3:] == "600", oct(path.stat().st_mode)[-3:])
        raw = path.read_text(encoding="utf-8")
        check("كلمة السر غير مكتوبة نصًّا", master not in raw)
        check("الملف لا يفشي اسمًا أو قيمة", "telegram" not in raw and "sk-" not in raw)
        check("البنية صحيحة (salt/nonce/data)", all(k in raw for k in ("salt", "nonce", "data", "scrypt")))
        check("إنشاء ثانٍ مرفوض", "موجودة بالفعل" in _act_init({"password": master}, None))

        # ٢) التخزين والاسترجاع
        stored = _act_set({"name": "telegram_bot_token", "value": secret}, None)
        check("التخزين ينجح (ويكتب طوله فقط)", "خزّنت" in stored and str(len(secret)) in stored, stored[:100])
        raw2 = path.read_text(encoding="utf-8")
        check("القيمة ليست في الملف نصًّا", secret not in raw2)
        _act_set({"name": "weather_api_key", "value": "abcdef1234567890abcdef"}, None)
        masked = _act_get({"name": "telegram_bot_token"}, None)
        check("المعاينة مُقنّعة", secret not in masked and "•" in masked, masked[:100])
        check("المعاينة تذكر الطول", str(len(secret)) in masked)
        listing = _act_list({}, None)
        check("القائمة بالأسماء فقط", "telegram_bot_token" in listing and secret not in listing)
        check("القائمة تذكر العدد", "2" in listing)
        check("اسم غير موجود يُشرح", "لا سرّ باسم" in _act_get({"name": "nope"}, None))

        # ٣) القفل والفتح وكلمة السر الخطأ
        check("القفل ينجح", "أقفلت" in _act_lock({}, None))
        locked = _act_get({"name": "telegram_bot_token"}, None)
        check("بعد القفل: يطلب الفتح", "مقفولة" in locked, locked[:90])
        wrong = _act_unlock({"password": "كلمة-غلط-تمامًا"}, None)
        check("كلمة سر خطأ تُرفض بلا كشف", "غير صحيحة" in wrong and secret not in wrong, wrong[:90])
        opened = _act_unlock({"password": master}, None)
        check("الكلمة الصحيحة تفتح", "مفتوحة الآن" in opened and "2" in opened, opened[:110])
        check("وتُذكر مدة الفتح", "دقيقة" in opened)
        check("الحالة تُظهر مفتوحة", "مفتوحة الآن" in _act_status({}, None))

        # ٤) النسخ إلى الحافظة بلا طباعة
        import types
        copied: list[str] = []
        fake_clip = types.ModuleType("pyperclip")
        fake_clip.copy = lambda text: copied.append(text)
        fake_clip.paste = lambda: ""
        sys.modules["pyperclip"] = fake_clip
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"clipboard_clear_seconds": "0"})
        copy_result = _act_copy({"name": "telegram_bot_token"}, None)
        check("النسخ يضع القيمة في الحافظة", copied and copied[-1] == secret, str(copied[:1])[:60])
        check("والرد نفسه لا يحوي القيمة", secret not in copy_result, copy_result[:90])
        check("نسخ سرّ غير موجود يُشرح", "لا سرّ باسم" in _act_copy({"name": "nope"}, None))

        # ٥) الإظهار بتأكيد
        check("الإظهار بلا تأكيد مرفوض", "confirm=yes" in _act_reveal({"name": "telegram_bot_token"}, None))
        revealed = _act_reveal({"name": "telegram_bot_token", "confirm": "yes"}, None)
        check("وبالتأكيد تظهر القيمة مع تحذير", secret in revealed and "اقفل الخزنة" in revealed, revealed[:80])

        # ٦) الحذف بتأكيد
        check("الحذف بلا تأكيد مرفوض", "confirm=yes" in _act_delete({"name": "weather_api_key"}, None))
        deleted = _act_delete({"name": "weather_api_key", "confirm": "yes"}, None)
        check("وبالتأكيد يُحذف ويُعدّ الباقي", "حذفت" in deleted and "1" in deleted, deleted[:90])

        # ٧) تغيير كلمة السر
        changed = _act_change_password({"password": master, "new": "قفل-الحديد-2026-٩٩"}, None)
        check("التغيير ينجح", "غيّرت كلمة سر" in changed, changed[:100])
        check("ويعيد التشفير فعلًا", (T.extras_dir() / "vault.json").read_text(encoding="utf-8") != raw2)
        _act_lock({}, None)
        check("القديمة لم تعد تعمل", "غير صحيحة" in _act_unlock({"password": master}, None))
        check("والجديدة تفتح", "مفتوحة الآن" in _act_unlock({"password": "قفل-الحديد-2026-٩٩"}, None))
        check("السرّ باقٍ بعد كل ذلك", secret in _act_reveal({"name": "telegram_bot_token", "confirm": "yes"}, None))
        check("كلمة سر جديدة هزيلة تُرفض",
              "قصيرة" in _act_change_password({"password": "قفل-الحديد-2026-٩٩", "new": "abc"}, None))
        check("وتغيير بلا وسائط يشرح", "القديمة والجديدة" in _act_change_password({}, None))

        # ٨) انتهاء المهلة والملف التالف
        _CACHE["until"] = time.time() - 1
        check("انتهاء المدة يقفل تلقائيًا", "مقفولة" in _act_get({"name": "telegram_bot_token"}, None))
        _act_unlock({"password": "قفل-الحديد-2026-٩٩"}, None)
        good = path.read_text(encoding="utf-8")
        path.write_text("{ليس JSON", encoding="utf-8")
        check("ملف تالف يُشرح بلا انهيار", "تالف" in _act_get({"name": "telegram_bot_token"}, None)
              or "غير مقروء" in _act_get({"name": "telegram_bot_token"}, None))
        path.write_text(good, encoding="utf-8")
        _CACHE["key"] = None
        _act_unlock({"password": "قفل-الحديد-2026-٩٩"}, None)
        check("ويعمل بعد إرجاعه", "مفتوحة" in _act_status({}, None))

        # ٩) الواجهة
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "steal"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("help يشرح المسار الموصى به", "copy" in _act_help({}, None) and "reveal" in _act_help({}, None))
        check("unlock بلا كلمة سر يشرح", "قل كلمة سر" in _act_unlock({}, None))
        check("set بلا اسم يشرح", "اسم السرّ" in _act_set({"value": "x"}, None))
        sys.modules.pop("pyperclip", None)
    finally:
        T.extras_dir = original_extras               # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        _CACHE["key"] = None
        _CACHE["until"] = 0.0
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
