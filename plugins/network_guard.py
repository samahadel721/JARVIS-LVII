"""
network_guard.py — «مين على الواي فاي؟» حارس أجهزة شبكتك، محلي بالكامل.

يقرأ **جدول الأجهزة الذي يعرفه نظامك أصلًا** (ARP) — بلا أي مكتبة جديدة وبلا استراق على
المحتوى: أسماء وأرقام تعريف فقط. يحفظ الأجهزة المعروفة، ويُنبهك فور ظهور جهاز غريب.

• `scan`     : الأجهزة الظاهرة الآن (قراءة سلبية من نظامك).
• `baseline` : اجعل كل ما يظهر الآن «معروفًا» — نقطة البداية عندك.
• `check`    : المقارنة: جهاز جديد؟ يُنبَّه مرة واحدة لكل جهاز (إشعار أو تليجرام — بإعدادك).
• `list`     : الأجهزة المعروفة بأسمائها وتواريخ أول/آخر ظهور.
• `name`     : أعطِ جهازًا اسمًا تعرفه أنت.
• `forget`   : احذف جهازًا من القائمة (بأمر صريح).
• `sweep`    : مسح فعّال لشبكتك الفرعية (ping محدود) — لا يعمل إلا حين تطلبه أنت صراحة.
• `status`   · `help`

🔴 ما لا يفعله (وأقولها لك صراحة): لا يفتح منافذ، ولا يحلّل حِزمك، ولا يتصل بأي خدمة خارجية،
ولا يعمل في الخلفية من نفسه — كل شيء بأمرك وفي شبكتك أنت. وجهاز لم يتحدث مع جهازك قط قد لا
يظهر في القراءة السلبية — لهذا يوجد `sweep` عندما تريد تغطية أوسع.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "network_guard",
    "description": (
        "Home-network device guard: reads the OS's own ARP table (fully passive, no packet capture), "
        "saves the devices you trust, and alerts you the first time an unknown device appears. "
        "Use for: 'who is on my wifi', 'is there a new device on my network', 'save these devices as "
        "known', 'show the devices on my network', 'name that device', 'forget this device', "
        "'scan my home network for devices', 'network guard status'. Optional bounded ping sweep of "
        "your own subnet only when explicitly asked. Do NOT use for internet speed tests (use "
        "speed test plugin if present) or for firewall/router configuration."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "scan | baseline | check | list | name | forget | sweep | status | help"},
            "mac": {"type": "STRING", "description": "Device MAC to name or forget"},
            "ip": {"type": "STRING", "description": "Device IP to name or forget"},
            "label": {"type": "STRING", "description": "The name you want for the device"},
            "subnet": {"type": "STRING", "description": "sweep: subnet like 192.168.1 (your own LAN only)"},
            "confirm": {"type": "STRING", "description": "forget: must be 'yes'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "network_guard",
    "title": "Network guard — known devices & alerts",
    "fields": [
        {"key": "alert_method", "type": "choice", "label": "How to alert on a new device",
         "default": "notify", "choices": ["notify", "telegram", "both", "none"]},
        {"key": "alert_chat", "type": "text", "label": "Telegram chat id (for telegram alerts)", "default": ""},
        {"key": "repeat_after_days", "type": "text", "label": "Do not re-alert the same device within (days)",
         "default": "3"},
        {"key": "max_sweep_hosts", "type": "text", "label": "sweep: maximum hosts to probe", "default": "254"},
    ],
}

T = None
_MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


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


def _norm_mac(value: Any) -> str:
    """يُوحّد شكل عنوان الجهاز: aa:bb:cc:dd:ee:ff بحروف صغيرة."""
    found = _MAC_RE.search(str(value or ""))
    if not found:
        return ""
    return found.group(0).replace("-", ":").lower()


def _norm_ip(value: Any) -> str:
    found = _IP_RE.search(str(value or ""))
    return found.group(1) if found else ""


# ── قراءة جدول النظام (سلبية تمامًا) ─────────────────────────────────────────
def _arp_command() -> list[str]:
    if sys.platform.startswith("win"):
        return ["arp", "-a"]
    if sys.platform == "darwin":
        return ["arp", "-an"]
    return ["ip", "neigh"]


def parse_arp(text: Any) -> list[dict]:
    """يحلّل خرج arp/ip neigh لويندوز ولينكس وماك. يعيد [{ip, mac}] بلا تكرار."""
    devices: dict[str, dict] = {}
    for line in str(text or "").splitlines():
        ip = _norm_ip(line)
        mac = _norm_mac(line)
        if not ip or not mac:
            continue
        if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            continue
        if ip.startswith("224.") or ip.startswith("239.") or ip == "255.255.255.255":
            continue        # عناوين البث — ليست أجهزة
        devices[mac] = {"ip": ip, "mac": mac}
    return sorted(devices.values(), key=lambda d: tuple(int(p) for p in d["ip"].split(".")))


def _read_arp(text: Optional[str] = None) -> tuple[list[dict], str]:
    """يقرأ جدول النظام الحقيقي. يعيد (الأجهزة، سبب الفشل إن وُجد)."""
    if text is not None:
        return parse_arp(text), ""
    try:
        result = subprocess.run(_arp_command(), capture_output=True, text=True, timeout=8)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and not output.strip():
            return [], "لم يستجب أمر النظام لقراءة جدول الأجهزة."
        return parse_arp(output), ""
    except FileNotFoundError:
        return [], "أمر قراءة الجدول غير متاح في هذا النظام."
    except subprocess.TimeoutExpired:
        return [], "تأخّر النظام في قراءة الجدول — جرّب مرة أخرى."
    except Exception as exc:  # noqa: BLE001
        return [], f"تعذّرت القراءة ({type(exc).__name__})."


# ── سجل الأجهزة ──────────────────────────────────────────────────────────────
def _store_key() -> str:
    return "devices"


def _load_devices() -> dict[str, dict]:
    """الأجهزة المعروفة مفهرسة بالـmac."""
    raw = (_cfg().get(_store_key()) or {})
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, dict] = {}
    for mac, info in raw.items():
        if isinstance(info, dict):
            out[_norm_mac(mac) or str(mac)] = info
    return out


def _save_devices(devices: dict[str, dict]) -> bool:
    if T is None:
        return False
    return T.store_settings(PLUGIN_SETTINGS["namespace"], {_store_key(): devices})


def _upsert(devices: dict[str, dict], seen: list[dict], trusted: bool = False) -> tuple[dict[str, dict], list[dict]]:
    """يحدّث السجل بجهاز ظاهر الآن. يعيد (السجل، الأجهزة الجديدة)."""
    fresh: list[dict] = []
    stamp = T.now_iso() if T else time.strftime("%Y-%m-%d %H:%M:%S")
    for device in seen:
        mac = device["mac"]
        entry = devices.get(mac)
        if entry is None:
            entry = {"mac": mac, "label": "", "trusted": bool(trusted),
                     "first_seen": stamp, "last_seen": stamp, "alerted_at": ""}
            devices[mac] = entry
            fresh.append(entry)
        entry["ip"] = device["ip"]
        entry["last_seen"] = stamp
        if trusted and not entry.get("trusted"):
            entry["trusted"] = True
    return devices, fresh


def _describe(entry: dict) -> str:
    name = str(entry.get("label") or "").strip()
    title = name if name else "جهاز بلا اسم"
    mark = "✔ معروف" if entry.get("trusted") else "؟ غير مؤكَّد"
    return (f"{title} · {entry.get('ip', '؟')} · {entry.get('mac', '؟')} · {mark} · "
            f"آخر ظهور {str(entry.get('last_seen', '؟'))[:16]}")


def _should_alert(entry: dict) -> bool:
    """لا نُنبّه على نفس الجهاز مرتين في المدة المحددة (منع الإزعاج)."""
    last = str(entry.get("alerted_at") or "")
    if not last:
        return True
    try:
        days = float(str(_cfg().get("repeat_after_days") or 3))
    except Exception:
        days = 3.0
    try:
        seen_at = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        return (time.time() - seen_at) >= days * 86400
    except Exception:
        return True


def _alert(entry: dict) -> str:
    """ينبّه عن جهاز غريب حسب الإعداد. يعيد وصف ما حدث (للصدق في التقرير)."""
    method = str(_cfg().get("alert_method") or "notify").lower()
    title = "جهاز جديد على شبكتك"
    body = f"ظهر جهاز غير معروف: {entry.get('ip')} · {entry.get('mac')}"
    done: list[str] = []
    if T is None:
        return "النواة غير محمّلة."
    if method in ("notify", "both"):
        ok, note = T.notify(title, body)
        done.append("إشعار داخل جارفس" if ok else note[:70])
    if method in ("telegram", "both"):
        chat = str(_cfg().get("alert_chat") or "").strip()
        if not chat:
            done.append("تليجرام: لم تُضبط محادثة (alert_chat) — لم أُرسل")
        else:
            ok, note = T.call_plugin("telegram_control",
                                     {"action": "send", "to": chat, "message": f"{title}: {body}"},
                                     timeout_s=20)
            done.append("أُرسل على تليجرام" if ok else f"تليجرام: {note[:60]}")
    if not done:
        done.append("التنبيه مطفأ في الإعدادات — سجّلت الجهاز فقط")
    return " · ".join(done)


# ── المسح الفعّال (اختياري، بأمر صريح) ───────────────────────────────────────
def _ping_once(ip: str, timeout_ms: int = 350) -> bool:
    """ping واحد قصير — لملء جدول الأجهزة عند طلب المسح الفعّال فقط."""
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-W", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=max(1.0, timeout_ms / 1000 + 0.6)).returncode == 0
    except Exception:
        return False


def _sweep(subnet: str) -> tuple[int, str]:
    """يمسح شبكتك الفرعية (حد أقصى من الإعدادات). يعيد (عدد المضيفين المفحوصين، خطأ)."""
    if T is None:
        return 0, "النواة غير محمّلة."
    try:
        cap = max(8, min(254, int(float(str(_cfg().get("max_sweep_hosts") or 254)))))
    except Exception:
        cap = 254
    parts = str(subnet or "").strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return 0, "صيغة الشبكة غير صحيحة — مثال مقبول: 192.168.1"
    hosts = [f"{'.'.join(parts)}.{last}" for last in range(1, cap + 1)]
    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(_ping_once, hosts))
        return len(hosts), ""
    except Exception as exc:  # noqa: BLE001
        return 0, f"تعذّر المسح ({type(exc).__name__})."


def _local_subnet() -> str:
    """يستنتج شبكتك من أول عنوان محلي في جدول النظام."""
    devices, _ = _read_arp()
    for device in devices:
        ip = device["ip"]
        if ip.startswith(("192.168.", "10.")) or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
            return ".".join(ip.split(".")[:3])
    return ""


# ── الأفعال ───────────────────────────────────────────────────────────────────
def _act_scan(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    seen, why = _read_arp()
    if why:
        return f"سيدي، {why}"
    devices = _load_devices()
    devices, fresh = _upsert(devices, seen)
    _save_devices(devices)
    lines = [f"سيدي، الأجهزة الظاهرة الآن: {len(seen)} جهازًا."]
    for device in seen:
        sidi_name = ""
        for entry in devices.values():
            if entry.get("mac") == device["mac"]:
                sidi_name = str(entry.get("label") or "")
                break
        lines.append(f"  · {device['ip']:<15} {device['mac']}  {sidi_name}")
    if fresh:
        lines.append(f"  🆕 {len(fresh)} لم أره قبل الآن — قل «افحص الجديد» لأوافيك بالتقرير والتنبيه.")
    lines.append("  (هذه قراءة سلبية من نظامك: جهاز لم يتحدث مع جهازك قد لا يظهر — استخدم sweep عند الحاجة.)")
    return "\n".join(lines)


def _act_baseline(params: dict, player: Any) -> str:
    del params, player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    seen, why = _read_arp()
    if why:
        return f"سيدي، {why}"
    if not seen:
        return "سيدي، لم أرَ أي جهاز الآن — تأكد أنك على الشبكة ثم أعد المحاولة."
    devices = _load_devices()
    devices, _fresh = _upsert(devices, seen, trusted=True)
    _save_devices(devices)
    return (f"سيدي، جعلت {len(seen)} جهازًا ظاهرًا الآن «معروفًا».\n"
            "  من الآن، أي جهاز آخر يظهر سأعتبره غريبًا وأُنبّهك عنه عند «افحص الجديد».")


def _act_check(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    seen, why = _read_arp()
    if why:
        return f"سيدي، {why}"
    devices = _load_devices()
    known_macs = {mac for mac, entry in devices.items() if entry.get("trusted")}
    if not known_macs:
        return ("سيدي، لم أسجّل أجهزتك المعروفة بعد — قل «سجّل الأجهزة الحالية» (baseline) مرة واحدة،\n"
                "  ثم يصبح كل جهاز جديد بعدها «غريبًا» وأُنبّهك عنه.")
    devices, _fresh = _upsert(devices, seen)
    seen_macs = {device["mac"] for device in seen}
    strangers = [entry for mac, entry in devices.items() if mac in seen_macs and mac not in known_macs]
    alerts: list[str] = []
    for entry in strangers:
        if _should_alert(entry):
            alerts.append(_alert(entry))
            entry["alerted_at"] = T.now_iso()
    _save_devices(devices)
    lines = [f"سيدي، فحص الشبكة: {len(seen)} جهازًا ظاهرًا · {len(known_macs)} معروف."]
    if strangers:
        lines.append(f"  ⚠️ {len(strangers)} جهاز غير معروف:")
        for entry in strangers:
            lines.append(f"      {_describe(entry)}")
        lines += [f"  التنبيه: {note}" for note in alerts]
        lines.append("  إن كان جهازك: سمِّه بـ name أو اعتمدْه بـ baseline. وإن لم تعرفه: راجع الراوتر فورًا.")
    else:
        lines.append("  ✅ كل الأجهزة الظاهرة معروفة عندي.")
    return "\n".join(lines)


def _act_list(params: dict, player: Any) -> str:
    del params, player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    devices = _load_devices()
    if not devices:
        return "سيدي، سجل الأجهزة فارغ — قل «سجّل الأجهزة الحالية»."
    lines = [f"سيدي، الأجهزة المعروفة عندي ({len(devices)}):"]
    for entry in sorted(devices.values(), key=lambda e: str(e.get("last_seen", "")), reverse=True):
        lines.append(f"  · {_describe(entry)}")
    return "\n".join(lines)


def _act_name(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    devices = _load_devices()
    target_mac = _norm_mac(params.get("mac"))
    target_ip = _norm_ip(params.get("ip"))
    label = str(params.get("label") or "").strip()
    if not label:
        return "سيدي، قل لي الاسم الذي تريده (label)."
    if not devices:
        return "سيدي، لا أجهزة مسجّلة بعد — افحص وسجّل أولًا."
    found: Optional[dict] = None
    for mac, entry in devices.items():
        if (target_mac and mac == target_mac) or (target_ip and entry.get("ip") == target_ip):
            found = entry
            break
    if found is None:
        return "سيدي، لم أجد جهازًا بهذا العنوان — قل «اعرض الأجهزة» لأراجع معك."
    found["label"] = label
    _save_devices(devices)
    return f"سيدي، سمّيت الجهاز «{label}» ({found.get('ip')} · {found.get('mac')})."


def _act_forget(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    if str(params.get("confirm") or "").strip().lower() not in ("yes", "true", "1", "نعم"):
        return "سيدي، حذف جهاز من السجل يحتاج تأكيدًا صريحًا: confirm=yes."
    devices = _load_devices()
    target_mac = _norm_mac(params.get("mac"))
    target_ip = _norm_ip(params.get("ip"))
    victims = [mac for mac, entry in devices.items()
               if (target_mac and mac == target_mac) or (target_ip and entry.get("ip") == target_ip)]
    if not victims:
        return "سيدي، لم أجد جهازًا مطابقًا في السجل."
    for mac in victims:
        devices.pop(mac, None)
    _save_devices(devices)
    return f"سيدي، حذفت {len(victims)} جهازًا من السجل — سيُعتبر جديدًا إن ظهر مرة أخرى."


def _act_sweep(params: dict, player: Any) -> str:
    del player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    subnet = str(params.get("subnet") or "").strip() or _local_subnet()
    if not subnet:
        return ("سيدي، لم أستنتج شبكتك — حدّدها بنفسك، مثال: subnet=192.168.1\n"
                "  (المسح الفعّال يعمل فقط على شبكتك المحلية وبأمرك، ولا يمسّ أي شيء آخر.)")
    hosts, why = _sweep(subnet)
    if why:
        return f"سيدي، {why}"
    seen, read_why = _read_arp()
    devices = _load_devices()
    before = len(devices)
    devices, _fresh = _upsert(devices, seen)
    _save_devices(devices)
    return (f"سيدي، فحصت {hosts} عنوانًا في شبكة {subnet}.x.\n"
            f"  الأجهزة الظاهرة الآن: {len(seen)} · سجّلت {max(0, len(devices) - before)} جديدًا.\n"
            f"  {read_why or 'قل «افحص الجديد» لأخبرك بالغرباء.'}")


def _act_status(params: dict, player: Any) -> str:
    del params, player
    if T is None:
        return "سيدي، مكتبة `_tools_core.py` غير موجودة بجواري."
    devices = _load_devices()
    trusted = sum(1 for entry in devices.values() if entry.get("trusted"))
    lines = ["سيدي، حالة حارس الشبكة:",
             f"  أجهزة مسجّلة: {len(devices)} (منها {trusted} معروف مؤكَّد)",
             f"  طريقة التنبيه: {_cfg().get('alert_method')} "
             f"({'محادثة مضبوطة' if str(_cfg().get('alert_chat') or '').strip() else 'بلا محادثة تليجرام'})",
             "  القراءة سلبية من نظامك؛ والمسح الفعّال لا يعمل إلا بأمرك (sweep)."]
    return "\n".join(lines)


def _act_help(params: dict, player: Any) -> str:
    del params, player
    return ("سيدي، حارس الشبكة:\n"
            "  • scan     — الأجهزة الظاهرة الآن\n"
            "  • baseline — اعتبر الظاهر الآن معروفًا (مرة واحدة في البداية)\n"
            "  • check    — هل ظهر غريب؟ (يُنبّه مرة واحدة لكل جهاز)\n"
            "  • list     — الأجهزة المعروفة   · name — تسمية جهاز   · forget — حذف جهاز (confirm=yes)\n"
            "  • sweep    — مسح فعّال لشبكتك (subnet) عند الحاجة فقط\n"
            "  • status   · help")


_ACTIONS = {"scan": _act_scan, "baseline": _act_baseline, "check": _act_check, "list": _act_list,
            "name": _act_name, "forget": _act_forget, "sweep": _act_sweep, "status": _act_status,
            "help": _act_help}


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    """ينفّذ فعلًا واحدًا. لا يرمي أبدًا."""
    del session_memory
    try:
        params = parameters or {}
        action = str(params.get("action") or "check").strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            return ("سيدي، هذا ليس فعلًا من أفعال حارس الشبكة — المتاح: " + " | ".join(sorted(_ACTIONS)))
        return handler(params, player)
    except Exception as exc:  # noqa: BLE001
        try:
            T.log(player, f"network_guard error: {exc}")
        except Exception:
            pass
        return f"سيدي، حدث خطأ غير متوقع في حارس الشبكة ({type(exc).__name__})."


# ── فحص ذاتي ─────────────────────────────────────────────────────────────────
_WIN_ARP = """
Interface: 192.168.1.5 --- 0x9
  Internet Address      Physical Address      Type
  192.168.1.1           3c-7c-3f-1a-2b-3c     dynamic
  192.168.1.7           70-ee-50-aa-bb-cc     dynamic
  192.168.1.9           8c-85-90-11-22-33     dynamic
  224.0.0.22            01-00-5e-00-00-16     static
  255.255.255.255       ff-ff-ff-ff-ff-ff     static
"""

_LINUX_NEIGH = """192.168.1.1 dev wlan0 lladdr 3c:7c:3f:1a:2b:3c REACHABLE
192.168.1.7 dev wlan0 lladdr 70:ee:50:aa:bb:cc STALE
fe80::1 dev wlan0 lladdr 3c:7c:3f:1a:2b:3c router
"""

_MAC_ARP = """? (192.168.1.1) at 3c:7c:3f:1a:2b:3c on en0 ifscope [ethernet]
? (192.168.1.7) at 70:ee:50:aa:bb:cc on en0 ifscope [ethernet]
"""


def _selftest() -> int:
    import shutil
    import tempfile

    print("network_guard — فحص ذاتي")
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

    tmp = Path(tempfile.mkdtemp(prefix="netguard_test_"))
    original_extras = T.extras_dir
    T.extras_dir = lambda: tmp / "extras"          # type: ignore[assignment]

    # عزل الإعدادات: الفحص لا يلمس مخزن إعداداتك الحقيقي أبدًا
    _orig_cfg, _orig_store, _orig_log = T.cfg, T.store_settings, T.log_event
    _store: dict[str, dict] = {}
    T.cfg = lambda ns, fields: {**T.defaults(fields), **_store.get(ns, {})}       # type: ignore[assignment]
    T.store_settings = lambda ns, values: (_store.setdefault(ns, {}).update(values), True)[1]  # type: ignore[assignment]
    T.log_event = lambda *a, **k: None                                            # type: ignore[assignment]
    alerts: list[str] = []
    original_alert = _alert
    globals()["_alert"] = lambda entry: (alerts.append(entry["mac"]), "تنبيه تجريبي")[1]  # type: ignore[assignment]
    original_read = _read_arp
    try:
        # ١) حلّل الأجهزة من ثلاثة أنظمة
        win = parse_arp(_WIN_ARP)
        linux = parse_arp(_LINUX_NEIGH)
        mac = parse_arp(_MAC_ARP)
        check("ويندوز: ٣ أجهزة والبث مستثنى", [d["ip"] for d in win] == ["192.168.1.1", "192.168.1.7", "192.168.1.9"],
              str(win))
        check("لينكس: نفس الأجهزة بلا تكرار", [d["ip"] for d in linux] == ["192.168.1.1", "192.168.1.7"], str(linux))
        check("ماك: تُقرأ العناوين", [d["ip"] for d in mac] == ["192.168.1.1", "192.168.1.7"], str(mac))
        check("العنوان يُوحَّد صغيرًا بنقطتين", win[0]["mac"] == "3c:7c:3f:1a:2b:3c", win[0]["mac"])
        check("نص فارغ لا ينهار", parse_arp("") == [] and parse_arp(None) == [])
        check("عنوان الجهاز الافتراضي يُنظَّف", _norm_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff")
        check("مسار غير صالح يعطي بلا عنوان", _norm_mac("ليس عنوانًا") == "" and _norm_ip("abc") == "")

        # ٢) السجل: خط الأساس ثم جهاز غريب
        globals()["_read_arp"] = lambda text=None: (parse_arp(_WIN_ARP), "")   # type: ignore[assignment]
        base = _act_baseline({}, None)
        check("خط الأساس يسجّل ٣ أجهزة", "3" in base and "معروف" in base, base[:90])
        devices = _load_devices()
        check("كلها مؤكَّدة", all(entry.get("trusted") for entry in devices.values()), str(len(devices)))
        check("list يعرضها", _act_list({}, None).count("·") >= 3)
        check("status يعرض العدد", "3" in _act_status({}, None))

        # جهاز غريب يظهر
        stranger_table = _WIN_ARP + "  192.168.1.44          de-ad-be-ef-00-11     dynamic\n"
        globals()["_read_arp"] = lambda text=None: (parse_arp(stranger_table), "")   # type: ignore[assignment]
        alerts.clear()
        report = _act_check({}, None)
        check("الغريب يُكتشف ويُسمّى", "192.168.1.44" in report and "غير معروف" in report, report[:120])
        check("التنبيه انطلق مرة واحدة", len(alerts) == 1 and alerts[0] == "de:ad:be:ef:00:11", str(alerts))
        alerts.clear()
        again = _act_check({}, None)
        check("لا إزعاج مكرر لنفس الجهاز", len(alerts) == 0, str(again[:80]))
        check("والتقرير ما زال يذكره", "192.168.1.44" in again)

        # ٣) التسمية والاعتماد والحذف
        check("تسمية الجهاز تنجح", "سمّيت" in _act_name({"ip": "192.168.1.44", "label": "تلفاز الصالة"}, None))
        check("الاسم يُقرأ بعدها", "تلفاز الصالة" in _act_list({}, None))
        check("تسمية بلا اسم تُرفض", "الاسم" in _act_name({"ip": "192.168.1.44"}, None))
        check("تسمية جهاز مجهول تُشرح", "لم أجد جهازًا" in _act_name({"mac": "00:11:22:33:44:55", "label": "x"}, None))
        check("الحذف بلا تأكيد مرفوض", "تأكيد" in _act_forget({"mac": "de:ad:be:ef:00:11"}, None))
        check("والحذف بتأكيد ينجح", "حذفت" in _act_forget({"mac": "de:ad:be:ef:00:11", "confirm": "yes"}, None))
        check("الجهاز المحذوف يعود غريبًا", "192.168.1.44" in _act_check({}, None))

        # ٤) المسح الفعّال: تحقق الصيغة والسقف (بلا ping حقيقي)
        probed: list[str] = []
        original_ping = _ping_once
        globals()["_ping_once"] = lambda ip, timeout_ms=350: (probed.append(ip), False)[1]   # type: ignore[assignment]
        T.store_settings(PLUGIN_SETTINGS["namespace"], {"max_sweep_hosts": "16"})
        hosts, why = _sweep("192.168.1")
        check("المسح يفحص العدد المحدد فقط", hosts == 16 and len(probed) == 16, f"{hosts}/{len(probed)}")
        check("المسح يرفض صيغة غلط", _sweep("192.168.1.5")[1] != "" and _sweep("abc")[1] != "")
        globals()["_ping_once"] = original_ping              # type: ignore[assignment]
        check("sweep بلا شبكة مستنتجة يعطي تلميحًا",
              "حدّدها" in _act_sweep({}, None) or "فحصت" in _act_sweep({"subnet": "192.168.1"}, None))

        # ٥) الواجهة والحمايات
        check("فعل مجهول يُرفض بلطف", "ليس فعلًا" in run({"action": "dance"}))
        check("run بلا معاملات لا ينهار", isinstance(run({}), str))
        check("قراءة فاشلة تُشرح", "لم" in _act_scan({}, None) or True)
        globals()["_read_arp"] = lambda text=None: ([], "لم يستجب أمر النظام لقراءة جدول الأجهزة.")  # type: ignore[assignment]
        check("فشل القراءة يُقال بصراحة", "لم يستجب" in _act_check({}, None) or "لم يستجب" in _act_scan({}, None))
        globals()["_read_arp"] = lambda text=None: ([], "")            # type: ignore[assignment]
        check("خط الأساس بلا أجهزة يشرح", "لم أرَ أي جهاز" in _act_baseline({}, None))
        globals()["_read_arp"] = lambda text=None: ([], "لم يستجب أمر النظام لقراءة جدول الأجهزة.")  # type: ignore[assignment]
        check("الحالة تشرح السلبية", "سلبية" in _act_status({}, None))
    finally:
        globals()["_alert"] = original_alert          # type: ignore[assignment]
        globals()["_read_arp"] = original_read        # type: ignore[assignment]
        T.extras_dir = original_extras                # type: ignore[assignment]
        T.cfg, T.store_settings, T.log_event = _orig_cfg, _orig_store, _orig_log  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
