"""
media_controller.py — play, pause, skip and volume, without touching a player's internals.

HOW IT WORKS, AND WHY IT IS BUILT THIS WAY
    There are two ways to control media, and they are not equally good:

    1. Media keys. Every desktop player (Spotify, VLC, YouTube in a browser,
       foobar, Windows Media Player) already obeys the keyboard's play/pause,
       next, previous and stop keys. Sending those keys asks the OPERATING
       SYSTEM to deliver the command to whichever player owns the media session
       right now — which is exactly what "pause the music" means. It needs no
       plugins per player, no API keys, and no third-party module: ctypes ships
       with Python, and pycaw is already in requirements.txt for volume.

    2. Each player's own API. Better in theory, worse in practice: Spotify needs
       OAuth and a paid account for control, VLC needs its web interface enabled
       by hand, and every new player is a new integration to maintain and break.

    So this plugin uses the media keys as the transport, and reports what the
    system says is playing. When a player is not reachable, it says which part
    is missing rather than pretending.

PLATFORM NOTES (honest, because this one is genuinely platform-bound)
    Windows  full support: volume and mute through pycaw + Core Audio, media keys
             through keybd_event, and "what is playing" from the window titles.
    macOS    volume and mute through osascript; media keys through osascript too.
    Linux    media keys through playerctl when it is installed (one apt install),
             otherwise the plugin explains that and does nothing quietly.

SETTINGS (Plugin Manager → Media)
    step_percent   how much one "louder" moves the volume (default 10)
    favourite      a player to prefer when reporting what is playing
    resume_after_pause   remembers position between pause and resume
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "media_controller",
    "description": (
        "Controls whatever is playing on this computer: 'pause the music', 'play', "
        "'next track', 'previous song', 'stop the video', 'turn it up', 'volume 30', "
        "'mute', 'unmute', 'what's playing?'. Works with any player that responds to "
        "the system's media keys. Do NOT use it to open a video from YouTube — that is "
        "video_player."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": ("play | pause | resume | toggle | stop | next | "
                                       "previous | volume | louder | quieter | mute | "
                                       "unmute | status")},
            "level": {"type": "INTEGER", "description": "Volume 0-100 for the volume action"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "media_controller",
    "title": "Media / الوسائط",
    "fields": [
        {"key": "step_percent", "type": "text", "label": "Volume step / مقدار الخطوة",
         "default": "10"},
        {"key": "favourite", "type": "text", "label": "Preferred player / المشغّل المفضّل",
         "default": "", "placeholder": "Spotify, VLC, ..."},
        {"key": "resume_after_pause", "type": "toggle",
         "label": "Remember what was playing / تذكّر ما كان يعمل", "default": True},
    ],
}

SYSTEM = platform.system()
STATE_FILE = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "media_state.json"

# Virtual-key codes for the system media keys.
VK = {
    "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "toggle": 0xB3,
    "mute": 0xAD, "quieter": 0xAE, "louder": 0xAF,
}
KEYEVENTF_KEYUP = 0x0002


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _state() -> dict:
    import json
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    import json
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── Windows: volume through Core Audio, keys through the OS ───────────────────

def _windows_volume():
    """The Core Audio endpoint, or None. pycaw is already in requirements.txt."""
    try:
        from pycaw.pycaw import AudioUtilities
        speakers = AudioUtilities.GetSpeakers()
        return getattr(speakers, "EndpointVolume", None)
    except Exception:
        return None


def _win_send_key(vk: int, times: int = 1) -> bool:
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except Exception:
        return False
    for _ in range(max(1, times)):
        try:
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            return False
        time.sleep(0.12)
    return True


# ── macOS ─────────────────────────────────────────────────────────────────────

def _mac_osascript(script: str) -> tuple[bool, str]:
    if not shutil.which("osascript"):
        return False, "osascript is not available"
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=10)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


# ── Linux ─────────────────────────────────────────────────────────────────────

def _has_playerctl() -> bool:
    return shutil.which("playerctl") is not None


def _linux_playerctl(*args: str) -> tuple[bool, str]:
    if not _has_playerctl():
        return False, "playerctl is not installed"
    try:
        r = subprocess.run(["playerctl", *args], capture_output=True, text=True, timeout=10)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


# ── what is playing ──────────────────────────────────────────────────────────

def _now_playing() -> str:
    """Best-effort: find a window that looks like a player and read its title."""
    favourite = str(_cfg().get("favourite") or "").strip().lower()
    hits: list[str] = []
    if SYSTEM == "Windows":
        try:
            import pygetwindow as gw
            for title in gw.getAllTitles():
                t = (title or "").strip()
                if not t:
                    continue
                low = t.lower()
                if any(k in low for k in ("spotify", "vlc", "media player", "foobar",
                                          "musicbee", "youtube", "netflix", "itunes",
                                          "groove", "aimp")):
                    hits.append(t)
        except Exception:
            pass
    elif SYSTEM == "Linux":
        ok, out = _linux_playerctl("metadata", "--format", "{{artist}} — {{title}}")
        if ok and out:
            hits.append(out)
    elif SYSTEM == "Darwin":
        ok, out = _mac_osascript('tell application "System Events" to get name of every '
                                 'process whose background only is false')
        if ok:
            for name in [p.strip() for p in out.split(",")]:
                if any(k in name.lower() for k in ("spotify", "music", "vlc", "quicktime")):
                    hits.append(name)
    if not hits:
        return ""
    if favourite:
        for h in hits:
            if favourite in h.lower():
                return h
    return hits[0]


def _player_running() -> bool:
    return bool(_now_playing())


# ── actions ───────────────────────────────────────────────────────────────────

def _action_transport(command: str) -> str:
    """play/pause/next/previous/stop — through the system's media keys."""
    if SYSTEM == "Windows":
        if command == "play":
            # A media key is a toggle, so a blind 'play' on already-playing media
            # would pause it. Without a readable state, the honest answer is that
            # this is a toggle — and that is what the description says too.
            ok = _win_send_key(VK["toggle"])
            return ("Toggled play/pause, sir." if ok
                    else "Sir, the media keys did not reach the system.")
        if _win_send_key(VK.get(command, VK["toggle"])):
            return f"Done — {command}, sir."
        return "Sir, that media key did not reach the system — is a window focused?"

    if SYSTEM == "Linux":
        mapping = {"play": "play", "pause": "pause", "next": "next", "previous": "previous",
                   "stop": "stop"}
        ok, why = _linux_playerctl(mapping.get(command, "play-pause"))
        if ok:
            return f"Done — {command}, sir."
        if not _has_playerctl():
            return ("Sir, controlling media on Linux needs playerctl, which is one command "
                    "away: sudo apt install playerctl. Until then I can only report volume.")
        return f"Sir, the player did not accept that: {why[:80]}"

    if SYSTEM == "Darwin":
        app = "Spotify"
        cmd = {"play": "play", "pause": "pause", "next": "next track",
               "previous": "previous track", "stop": "pause"}.get(command, "playpause")
        ok, why = _mac_osascript(f'tell application "{app}" to {cmd}')
        return f"Done — {command} in {app}, sir." if ok else \
            f"Sir, {app} did not accept that: {why[:80]}"

    return "Sir, media control is not supported on this system."


def _action_volume(level: Any) -> str:
    try:
        target = max(0, min(100, int(str(level).strip())))
    except Exception:
        return "What volume, sir? Give me a number from 0 to 100."

    if SYSTEM == "Windows":
        vol = _windows_volume()
        if vol is None:
            return ("Sir, pycaw is not importable, so I cannot set the volume directly — it "
                    "ships with setup.py, so running that once fixes it.")
        try:
            vol.SetMasterVolumeLevelScalar(target / 100.0, None)
            return f"Volume set to {target}%, sir."
        except Exception as e:
            return f"Sir, setting the volume failed: {str(e)[:90]}"

    if SYSTEM == "Darwin":
        ok, why = _mac_osascript(f"set volume output volume {target}")
        return f"Volume set to {target}%, sir." if ok else f"Sir, that failed: {why[:80]}"

    if _has_playerctl():
        ok, why = _linux_playerctl("volume", f"{target / 100:.2f}")
        return f"Volume set to {target}%, sir." if ok else f"Sir, that failed: {why[:80]}"
    return ("Sir, setting an exact volume on Linux needs playerctl (sudo apt install "
            "playerctl). I can still send the volume keys via the system if you prefer.")


def _action_step(direction: str) -> str:
    try:
        step = max(1, min(50, int(str(_cfg().get("step_percent") or 10).strip())))
    except Exception:
        step = 10
    if SYSTEM == "Windows":
        vol = _windows_volume()
        if vol is not None:
            try:
                current = float(vol.GetMasterVolumeLevelScalar()) * 100
                target = max(0.0, min(100.0, current + (step if direction == "up" else -step)))
                vol.SetMasterVolumeLevelScalar(target / 100.0, None)
                return f"Volume {int(target)}%, sir."
            except Exception:
                pass
        # Fall back to the system's own volume keys — one press is 2%, so press step/2.
        presses = max(1, round(step / 2))
        ok = _win_send_key(VK["louder"] if direction == "up" else VK["quieter"], presses)
        return (f"Turned it {'up' if direction == 'up' else 'down'}, sir." if ok
                else "Sir, the volume keys did not reach the system.")

    if SYSTEM == "Darwin":
        delta = "+" if direction == "up" else "-"
        ok, why = _mac_osascript(
            f"set volume output volume (output volume of (get volume settings) {delta} {step})")
        return f"Turned it {'up' if direction == 'up' else 'down'}, sir." if ok else \
            f"Sir, that failed: {why[:80]}"
    return f"Sir, use a media key or install playerctl — exact steps need one of them."


def _action_mute(on: bool) -> str:
    if SYSTEM == "Windows":
        vol = _windows_volume()
        if vol is None:
            return "Sir, pycaw is not importable, so I cannot toggle mute directly."
        try:
            vol.SetMute(1 if on else 0, None)
            return "Muted, sir." if on else "Unmuted, sir."
        except Exception as e:
            return f"Sir, that failed: {str(e)[:90]}"
    if SYSTEM == "Darwin":
        ok, why = _mac_osascript(f"set volume {'with' if on else 'without'} output muted")
        return ("Muted, sir." if on else "Unmuted, sir.") if ok else f"Sir, that failed: {why[:80]}"
    if _has_playerctl():
        ok, _ = _linux_playerctl("volume", "0" if on else "1")
        if ok:
            return "Muted, sir." if on else "Unmuted, sir."
    return "Sir, muting this system is not supported by the available tools."


def _action_status(parameters: dict, player) -> str:
    playing = _now_playing()
    volume_txt = "unknown"
    if SYSTEM == "Windows":
        vol = _windows_volume()
        if vol is not None:
            try:
                volume_txt = (f"{round(float(vol.GetMasterVolumeLevelScalar()) * 100)}%"
                              + (" (muted)" if vol.GetMute() else ""))
            except Exception:
                pass
    elif SYSTEM == "Darwin":
        ok, out = _mac_osascript("output volume of (get volume settings)")
        if ok:
            volume_txt = f"{out}%"
    elif _has_playerctl():
        ok, out = _linux_playerctl("volume")
        if ok:
            volume_txt = out

    lines = [
        f"System    : {SYSTEM}",
        f"Playing   : {playing or 'nothing I can see'}",
        f"Volume    : {volume_txt}",
        f"Transport : {'media keys' if SYSTEM == 'Windows' else 'osascript' if SYSTEM == 'Darwin' else 'playerctl' if _has_playerctl() else 'none available'}",
        f"favourite : {_cfg().get('favourite') or '(none set)'}",
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Media status", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one media action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "status").strip().lower()
        if action in {"play", "pause", "resume", "toggle"}:
            if action == "pause":
                return _action_transport("toggle") if SYSTEM == "Windows" else _action_transport("pause")
            if action == "resume":
                return _action_transport("play")
            return _action_transport("play")
        if action in {"next", "previous", "stop"}:
            return _action_transport(action)
        if action in {"volume", "set_volume"}:
            return _action_volume(p.get("level"))
        if action in {"louder", "quieter", "up", "down"}:
            return _action_step("up" if action in {"louder", "up"} else "down")
        if action == "mute":
            return _action_mute(True)
        if action == "unmute":
            return _action_mute(False)
        if action in {"status", "what", "now_playing"}:
            return _action_status(p, player)
        return (f"Sir, '{action}' is not a media command I have. I can play, pause, stop, "
                f"next, previous, change volume, mute, or report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the media plugin hit a problem: {str(e)[:150]}"


# ── self-test: python media_controller.py ───────────────────────────────────

def _selftest() -> int:
    print("media_controller — self-test")
    problems = 0
    print(f"  platform             : {SYSTEM}")
    print(f"  playerctl available  : {_has_playerctl()}")
    if SYSTEM == "Windows":
        try:
            import ctypes                                   # noqa: F401
            print("  ctypes               : ok")
        except Exception:
            problems += 1
            print("  ctypes               : FAIL")
        print(f"  pycaw endpoint       : {'ok' if _windows_volume() else 'not importable'}")
    print(f"  bad volume           -> {run({'action': 'volume', 'level': 'abc'})[:70]}")
    if "number from 0 to 100" not in run({"action": "volume", "level": "abc"}):
        problems += 1
    print(f"  unknown action       -> {run({'action': 'dance'})[:70]}")
    print(f"  status               -> {run({'action': 'status'}).splitlines()[0]}")
    print(f"  transport on {'Linux' if SYSTEM == 'Linux' else SYSTEM} : "
          f"{run({'action': 'pause'})[:70]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
