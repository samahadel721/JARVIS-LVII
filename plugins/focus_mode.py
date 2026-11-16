"""
plugins/focus_mode.py — "do not let me open these sites for an hour".

Drop this single file into `plugins/`. Standard library only. It works by
editing the hosts file, which is the one blocking mechanism that every browser
and every app honours without an extension, a driver, or a background service.

WHAT IT IS HONEST ABOUT
    A hosts entry redirects a name to nowhere. Apps that resolve DNS themselves
    over HTTPS (some Chrome configurations) walk past it, and nothing here can
    stop that without installing a filter driver. So the plugin says what it
    did and what it cannot do, instead of implying total enforcement.

    Editing the hosts file needs administrator rights. When they are missing the
    reply says exactly which command or setting fixes it — it never fails
    silently, and it never rewrites a file it could not first read.

SAFETY
    Only the lines between its own two markers are ever touched. The untouched
    original is copied next to the config once, so a crashed machine can be
    restored by hand. Turning focus mode off — or letting the timer run out —
    removes the block completely.

SETTINGS (Plugin Manager → Focus Mode)
    Default session length, whether sites are blocked at all, and extra domains.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

PLUGIN = {
    "name": "focus_mode",
    "description": (
        "Enables or disables a distraction-free focus session: blocks a list of "
        "social and video sites system-wide for a set time, then releases them. "
        "Use for: 'start focus mode', 'focus mode for 90 minutes', 'block "
        "facebook and instagram', 'stop focus mode', 'am I in focus mode', "
        "'how long left in focus mode'. This is a timed block with a status and "
        "an off switch — do NOT use browser_control or computer_settings for it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'on'/'start' to begin (default), 'off'/'stop' to release "
                    "everything immediately, 'status' for time remaining and what "
                    "is blocked."
                ),
            },
            "minutes": {
                "type": "INTEGER",
                "description": "Session length in minutes (default from settings, usually 50).",
            },
            "sites": {
                "type": "STRING",
                "description": (
                    "Optional comma-separated domains to block instead of the "
                    "default list, e.g. 'facebook.com, tiktok.com'. Domains only, "
                    "no full URLs."
                ),
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "focus_mode",
    "title": "Focus Mode / وضع التركيز",
    "fields": [
        {"key": "default_minutes", "type": "text", "label": "Default length (min) / المدة الافتراضية",
         "default": "50"},
        {"key": "block_sites", "type": "toggle", "label": "Block websites / حجب المواقع",
         "default": True},
        {"key": "extra_sites", "type": "text", "label": "Extra domains / مواقع إضافية",
         "default": "", "placeholder": "example.com, another.com"},
        {"key": "auto_release", "type": "toggle", "label": "Release automatically when time is up",
         "default": True},
    ],
}

#: The default distraction list. Editing this file is the supported way to
#: change it permanently; the Extra domains setting is for additions.
_DEFAULT_SITES = (
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "tiktok.com", "www.tiktok.com", "reddit.com", "www.reddit.com",
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "netflix.com", "www.netflix.com", "twitch.tv", "www.twitch.tv",
    "pinterest.com", "www.pinterest.com", "snapchat.com", "www.snapchat.com",
)

_BEGIN = "# >>> JARVIS focus mode - managed block, do not edit >>>"
_END = "# <<< JARVIS focus mode - end >>>"

_lock = threading.Lock()
_timer: threading.Timer | None = None
_player = None            # held only so the expiry notice can be spoken


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _state_path() -> Path:
    folder = _base_dir() / "config" / ".plugin_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "focus_mode.json"


def _hosts_path() -> Path:
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def _config() -> dict:
    values = {"default_minutes": "50", "block_sites": True, "extra_sites": "",
              "auto_release": True}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("focus_mode") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    try:
        values["default_minutes"] = max(1, int(float(values["default_minutes"])))
    except (TypeError, ValueError):
        values["default_minutes"] = 50
    return values


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _is_active(state: dict) -> bool:
    return bool(state) and float(state.get("until", 0)) > time.time()


def _remaining_minutes(state: dict) -> float:
    return max(0.0, (float(state.get("until", 0)) - time.time()) / 60.0)


# ---------------------------------------------------------------------------
# The hosts file
# ---------------------------------------------------------------------------

def _clean_hosts_text(existing: str) -> str:
    """
    Remove our previous block, and nothing else.

    Everything outside the two markers is preserved byte for byte — this file
    often matters to other software, and a focus timer is not a good reason to
    reformat somebody's machine configuration.
    """
    lines = existing.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == _BEGIN:
            inside = True
            continue
        if line.strip() == _END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def _apply_block(domains: list[str]) -> tuple[bool, str]:
    """
    (applied, detail). Writes the block, or explains exactly why it could not.
    """
    hosts = _hosts_path()
    try:
        existing = hosts.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return False, f"I could not read {hosts}: {exc}"

    backup = _base_dir() / "config" / ".plugin_cache" / "hosts.backup"
    if not backup.exists():
        try:
            backup.write_text(existing, encoding="utf-8")
        except Exception:
            pass

    block = [_BEGIN]
    for domain in domains:
        block.append(f"127.0.0.1 {domain}")
        block.append(f"::1 {domain}")
    block.append(_END)
    new_text = _clean_hosts_text(existing) + "\n".join(block) + "\n"

    try:
        hosts.write_text(new_text, encoding="utf-8")
    except PermissionError:
        if os.name == "nt":
            return False, ("blocking sites needs administrator rights — close "
                           "JARVIS, reopen it with 'Run as administrator', and "
                           "ask again. The timer itself would have worked.")
        return False, ("blocking sites needs root — start JARVIS with sudo, or "
                       "use the timer without site blocking.")
    except Exception as exc:
        return False, f"I could not write {hosts}: {exc}"

    _flush_dns()
    return True, f"{len(domains)} domains blocked"


def _remove_block() -> tuple[bool, str]:
    hosts = _hosts_path()
    try:
        existing = hosts.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return False, f"I could not read {hosts}: {exc}"
    if _BEGIN not in existing:
        return True, "nothing was blocked"
    try:
        hosts.write_text(_clean_hosts_text(existing), encoding="utf-8")
    except PermissionError:
        return False, ("releasing sites needs administrator rights — reopen "
                       "JARVIS as administrator and say 'stop focus mode'.")
    except Exception as exc:
        return False, f"I could not write {hosts}: {exc}"
    _flush_dns()
    return True, "sites released"


def _flush_dns() -> None:
    """
    Ask the OS to forget its DNS cache so the change takes effect at once.

    Best effort by design: failing to flush does not mean the block failed, and
    an error here should never reach the user's answer.
    """
    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["ipconfig", "/flushdns"])
    elif shutil.which("dscacheutil"):
        commands.append(["dscacheutil", "-flushcache"])
        commands.append(["killall", "-HUP", "mDNSResponder"])
    elif shutil.which("resolvectl"):
        commands.append(["resolvectl", "flush-caches"])
    for command in commands:
        try:
            subprocess.run(command, timeout=8, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Session control
# ---------------------------------------------------------------------------

def _domains_from_text(text: str) -> list[str]:
    out: list[str] = []
    for chunk in str(text or "").replace(";", ",").split(","):
        item = chunk.strip().lower()
        if not item:
            continue
        item = item.split("//")[-1].split("/")[0].split("?")[0].strip()
        if item and " " not in item and "." in item:
            out.append(item)
    return out


def _blocked_domains(parameters: dict, config: dict) -> list[str]:
    """Explicit request > extra settings > the built-in list, deduplicated."""
    requested = _domains_from_text(parameters.get("sites", ""))
    extras = _domains_from_text(config.get("extra_sites", ""))
    if requested:
        chosen = requested
    elif config.get("block_sites", True):
        chosen = list(_DEFAULT_SITES) + extras
    else:
        chosen = list(extras)
    seen: list[str] = []
    for domain in chosen:
        if domain not in seen:
            seen.append(domain)
    return seen


def _schedule_release(minutes: float) -> None:
    """Arm (or re-arm) the one timer that ends the session on its own."""
    global _timer
    if _timer is not None:
        try:
            _timer.cancel()
        except Exception:
            pass
        _timer = None
    if minutes <= 0:
        return

    def _release() -> None:
        with _lock:
            ok, _detail = _remove_block()
            _write_state({})
        if ok and _player is not None:
            try:
                # Same channel the timer plugins use: one short instruction the
                # model phrases in the user's own language, spoken mid-session.
                _player.request_say("Focus session is over. Tell the user the "
                                    "blocked sites are available again, in one "
                                    "short sentence.")
            except Exception:
                pass

    _timer = threading.Timer(minutes * 60.0, _release)
    _timer.daemon = True
    _timer.start()


def _action_on(parameters: dict, config: dict, player) -> str:
    global _player
    _player = player
    minutes = parameters.get("minutes")
    try:
        minutes = float(minutes) if minutes not in (None, "") else float(config["default_minutes"])
    except (TypeError, ValueError):
        minutes = float(config["default_minutes"])
    minutes = max(1.0, min(600.0, minutes))

    domains = _blocked_domains(parameters, config)
    applied, detail = _apply_block(domains) if domains else (True, "no sites configured")

    state = _read_state()
    previous = _is_active(state)
    _write_state({"until": time.time() + minutes * 60.0,
                  "minutes": minutes,
                  "domains": domains,
                  "blocked": bool(applied and domains),
                  "started": state.get("started") or time.time()})
    if applied and config.get("auto_release", True):
        _schedule_release(minutes)

    count = int(minutes) if minutes == int(minutes) else minutes
    length = f"{count:g} minute" + ("" if count == 1 else "s")
    if applied:
        head = "Focus mode extended" if previous else "Focus mode is on"
        return (f"{head} for {length}, sir — {detail}. "
                f"I will release them when the time is up.")
    return (f"Focus timer is running for {length}, sir, but I could not block "
            f"the sites: {detail}")


def _action_off() -> str:
    ok, detail = _remove_block()
    _write_state({})
    _schedule_release(0)
    if ok:
        return f"Focus mode is off — {detail}."
    return f"I could not leave focus mode cleanly: {detail}"


def _action_status() -> str:
    state = _read_state()
    if not _is_active(state):
        if state:                                  # timer expired while the app was closed
            _remove_block()
            _write_state({})
        return "Focus mode is off — nothing is blocked."
    left = _remaining_minutes(state)
    domains = state.get("domains") or []
    blocked = "sites are blocked" if state.get("blocked") else "no sites are blocked (timer only)"
    left_txt = f"{int(left)} minute" + ("" if int(left) == 1 else "s")
    return (f"Focus mode is on, sir: {left_txt} left, {blocked}"
            + (f", {len(domains)} domains on the list." if domains else "."))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        action = str(parameters.get("action", "on") or "on").strip().lower()
        with _lock:
            if action in ("off", "stop", "end", "cancel"):
                result = _action_off()
            elif action in ("status", "state", "left"):
                result = _action_status()
            else:
                result = _action_on(parameters, config, player)
    except Exception as exc:
        return f"Sir, focus_mode failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    print(run({"action": "status"}))
    print(run({"action": "on", "minutes": 1, "sites": "example.com"}))
    print(run({"action": "status"}))
    print(run({"action": "off"}))
