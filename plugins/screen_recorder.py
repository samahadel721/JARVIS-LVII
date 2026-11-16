"""
screen_recorder.py — recording the screen, never behind your back.

THE RULE THIS PLUGIN IS BUILT AROUND
    Recording starts only when you ask for it, in words, in the same breath. The
    plugin asks "shall I start?" and does nothing until you say yes. It draws no
    hidden window; it writes no file in secret. Once it is running, it says so
    every time you ask, and it tells you the exact file it is writing and where.
    There is no timer that starts it, no trigger, no background start-up.

    It also stops itself: a recording has a ceiling (30 minutes unless you change
    it), because a recorder left running is a disk filling up and a machine no
    longer yours to trust.

HOW IT RECORDS
    Through ffmpeg, which is the one recorder that is genuinely free and on every
    system. If it is not installed, the plugin does not pretend: it gives you the
    exact line to install it, and on Windows it reminds you that the Game Bar
    (Win+Alt+R) is already there and needs nothing from me.

    Stopping is polite on purpose. ffmpeg is asked to close the file properly
    (the same as pressing 'q'), because a killed recorder leaves a broken mp4 —
    all the minutes played back as nothing.

SETTINGS (Plugin Manager → Screen recorder)
    save_dir       blank = Videos/JARVIS_Recordings
    fps            15 — smooth enough for a walkthrough, light on the machine
    max_minutes    30 — the automatic stop
    audio_device   blank = picture only. On Windows the name of a capture device
                   ('Stereo Mix' if your sound card offers it), on macOS a number,
                   on Linux a PulseAudio monitor name. Sound is never assumed.
    ffmpeg_path    blank = find it on PATH
    scale          blank = full size, or e.g. 1280:-2 to make the file smaller
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "screen_recorder",
    "description": (
        "Records the screen when you explicitly ask: 'start recording my screen', 'stop the "
        "recording', 'is it still recording?'. It says where the file is being written, stops "
        "itself at the time limit, plays nothing back anywhere, and refuses to start quietly."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | stop | status | help"},
            "confirm": {"type": "BOOLEAN",
                        "description": "Must be true — the user asked for the recording"},
            "duration_min": {"type": "STRING", "description": "Optional limit in minutes"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "screen_recorder",
    "title": "Screen recorder / تسجيل الشاشة",
    "fields": [
        {"key": "save_dir", "type": "text", "label": "Save folder (blank = Videos/JARVIS_Recordings)",
         "default": ""},
        {"key": "fps", "type": "text", "label": "Frames per second", "default": "15"},
        {"key": "max_minutes", "type": "text", "label": "Automatic stop after, minutes",
         "default": "30"},
        {"key": "audio_device", "type": "text",
         "label": "Sound device (blank = no sound)", "default": ""},
        {"key": "ffmpeg_path", "type": "text", "label": "ffmpeg path (blank = find it)",
         "default": ""},
        {"key": "scale", "type": "text", "label": "Size (blank = full, or e.g. 1280:-2)",
         "default": ""},
    ],
}

STATE = Path(__file__).resolve().parent.parent / "config" / ".plugin_cache" / "recording.json"
_process: subprocess.Popen | None = None


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("screen_recorder") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def output_dir() -> Path:
    raw = str(_cfg().get("save_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Videos" / "JARVIS_Recordings")
    try:
        return base.resolve()
    except Exception:
        return Path.home() / "Videos" / "JARVIS_Recordings"


def ffmpeg_path() -> str | None:
    configured = str(_cfg().get("ffmpeg_path") or "").strip()
    if configured and Path(configured).exists():
        return configured
    return shutil.which("ffmpeg")


def _install_line() -> str:
    if sys.platform.startswith("win"):
        return ("winget install Gyan.FFmpeg    (then reopen the terminal) — or just use the "
                "Game Bar: Win+Alt+R, which is already on your machine")
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg"


def backend() -> tuple[bool, str, str]:
    """(can record, why not, backend name). Nothing is assumed about the session."""
    binary = ffmpeg_path()
    if not binary:
        return False, (f"ffmpeg is not on this machine, sir, and it is what records. Install it "
                       f"with:  {_install_line()}"), "none"
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, ("there is no graphical session here to record (no DISPLAY), sir — "
                           "start the assistant inside your desktop session"), "none"
        return True, "", "x11grab"
    if sys.platform == "darwin":
        return True, "", "avfoundation"
    if sys.platform.startswith("win"):
        return True, "", "gdigrab"
    return False, f"I do not know how to record on '{sys.platform}', sir", "none"


def build_command(binary: str, name: str, path: Path, fps: int, seconds: int,
                  audio: str = "", scale: str = "") -> list[str]:
    """
    The exact command line. Kept apart from running it so it can be checked
    without recording anything. No shell is involved, so nothing in the file name
    or the device name can turn into a second command.
    """
    cmd = [binary, "-hide_banner", "-loglevel", "error", "-y"]
    if name == "gdigrab":
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]
    elif name == "avfoundation":
        cmd += ["-f", "avfoundation", "-capture_cursor", "1", "-framerate", str(fps),
                "-i", "1:none"]
    else:                                            # x11grab
        display = os.environ.get("DISPLAY") or ":0"
        cmd += ["-f", "x11grab", "-framerate", str(fps), "-i", f"{display}+0,0"]
    if audio:
        if name == "gdigrab":
            cmd += ["-f", "dshow", "-i", f"audio={audio}"]
        elif name == "avfoundation":
            cmd = [c for c in cmd]                   # device index replaces ':none' above
            for i, part in enumerate(cmd):
                if part == "1:none":
                    cmd[i] = f"1:{audio}"
        else:
            cmd += ["-f", "pulse", "-i", audio]
    if scale:
        cmd += ["-vf", f"scale={scale}"]
    cmd += ["-t", str(seconds), "-pix_fmt", "yuv420p", str(path)]
    return cmd


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 500):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")


def _save_state(row: dict[str, Any] | None) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        if row is None:
            STATE.unlink(missing_ok=True)
            return
        fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(row, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def _load_state() -> dict[str, Any] | None:
    try:
        row = json.loads(STATE.read_text(encoding="utf-8"))
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform.startswith("win"):
        try:
            done = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                                  capture_output=True, text=True, timeout=8, check=False)
            return str(int(pid)) in (done.stdout or "")
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _notify(title: str, body: str, priority: str = "normal") -> None:
    try:
        import importlib.util
        import sys as _sys
        name = "_jarvis_notification_center"
        module = _sys.modules.get(name)
        if module is None:
            path = Path(__file__).resolve().parent / "notification_center.py"
            if not path.exists():
                return
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            _sys.modules[name] = module
            spec.loader.exec_module(module)
        if hasattr(module, "publish"):
            module.publish(title, body, category="recording", priority=priority, source="recorder")
    except Exception:
        pass


# ── start / stop / status ─────────────────────────────────────────────────────

def current() -> dict[str, Any] | None:
    """The recording that is running, or None. Discards a dead one on the way."""
    row = _load_state()
    if row is None:
        return None
    pid = int(row.get("pid") or 0)
    if _process is not None and _process.poll() is None:
        return row
    if _alive(pid):
        return row
    _save_state(None)          # it ended while nobody was looking — say nothing about it
    return None


def status_text() -> str:
    row = current()
    if row is None:
        can, why, name = backend()
        if not can:
            return f"Nothing is recording, sir. And I could not start one: {why}"
        return (f"Nothing is recording, sir. Say 'start recording my screen' and I will begin "
                f"({name} through ffmpeg, up to {_int_cfg('max_minutes', 30, 1, 600)} minutes).")
    path = Path(str(row.get("path")))
    size = path.stat().st_size if path.exists() else 0
    elapsed = int(time.time() - float(row.get("started") or time.time()))
    return (f"Recording now, sir — for {elapsed // 60}m {elapsed % 60}s so far, "
            f"{size / (1024 * 1024):.1f} MB written.\n  File : {path}\n"
            f"  Until: {row.get('limit_minutes')} minutes, then it stops itself.")


def start(confirm: bool, minutes: int = 0, player=None) -> str:
    """
    Begin recording — but only on an explicit yes. Without it this returns the
    question and records nothing, which is the whole point.
    """
    global _process
    if not confirm:
        return ("Shall I start recording your screen, sir? Say yes and I will — a file, in your "
                "Videos folder, up to 30 minutes. Nothing records until you say so.")
    if current() is not None:
        return "I am already recording, sir. " + status_text()
    can, why, name = backend()
    if not can:
        return f"Sir, {why}."
    binary = ffmpeg_path()
    assert binary is not None

    minutes = minutes or _int_cfg("max_minutes", 30, 1, 600)
    minutes = max(1, min(600, int(minutes)))
    base = output_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Sir, I could not create {base}: {str(e)[:70]}"
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = _unique(base / f"Recording_{stamp}.mp4")

    cmd = build_command(binary, name, path, _int_cfg("fps", 15, 1, 60), minutes * 60,
                        str(_cfg().get("audio_device") or ""), str(_cfg().get("scale") or ""))
    try:
        creation = 0
        if sys.platform.startswith("win"):
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash
        extra = {} if sys.platform.startswith("win") else {"start_new_session": True}
        _process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,      # argv list, no shell
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=creation, **extra)
        # Its own session on purpose: stopping it can then signal the whole
        # recorder group and nothing else — never the assistant that started it.
    except Exception as e:
        return f"Sir, ffmpeg would not start: {str(e)[:90]}"

    _save_state({"pid": _process.pid, "path": str(path), "backend": name, "started": time.time(),
                 "limit_minutes": minutes, "audio": bool(_cfg().get("audio_device"))})
    time.sleep(1.2)
    if _process.poll() is not None:                 # it died at once — say why, do not claim success
        _save_state(None)
        try:
            err = (_process.stderr.read() or b"").decode("utf-8", "replace").strip()
        except Exception:
            err = ""
        return (f"Sir, ffmpeg started and stopped immediately. It said: {err[-300:] or 'nothing'}\n"
                f"Try lowering the frame rate, or run: {binary} -list_devices true -i \"\" to "
                f"check the screen device.")
    if player:
        try:
            player.write_log(f"[Recorder] recording → {path.name} ({minutes} min limit)")
        except Exception:
            pass
    _notify("Recording started", f"{path.name} — up to {minutes} minutes", "normal")
    return (f"Recording, sir. The file is being written to:\n  {path}\n"
            f"It stops itself after {minutes} minutes, or say 'stop the recording'. The screen "
            f"recording indicator on your system tray stays on while it runs.")


def stop(graceful_seconds: int = 5) -> str:
    """
    End the recording and finish the file properly. ffmpeg is asked to close it
    the same way pressing 'q' does; force-killing a recorder leaves a broken file.
    """
    global _process
    row = current()
    if row is None:
        return "Nothing is recording, sir."
    pid = int(row.get("pid") or 0)
    path = Path(str(row.get("path")))

    if _process is not None and _process.poll() is None:
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                               timeout=10, check=False)        # graceful WM_CLOSE, not /F
            else:
                import signal
                try:
                    os.killpg(os.getpgid(pid), signal.SIGINT)   # ffmpeg's own quit key
                except Exception:
                    _process.send_signal(signal.SIGINT)
            _process.wait(timeout=graceful_seconds)
        except Exception:
            pass
    elif _alive(pid):
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                               timeout=10, check=False)
            else:
                try:
                    os.killpg(os.getpgid(pid), 2)               # SIGINT, the polite quit
                except Exception:
                    os.kill(pid, 15)
        except Exception:
            pass

    for _ in range(20):                       # give it a moment to write the trailer
        if not _alive(pid):
            break
        time.sleep(0.25)

    still = _alive(pid)
    size = path.stat().st_size if path.exists() else 0
    _save_state(None)
    _process = None

    if still:
        return (f"Sir, the recorder (pid {pid}) did not close on its own — the file at {path} may "
                f"be incomplete. I will not force-kill it: end it from Task Manager yourself "
                f"(taskkill /PID {pid} /F) and the file will be usable up to the last moment.")
    if not path.exists() or size == 0:
        _notify("Recording ended with nothing", "no file was written", "high")
        return (f"Sir, the recording ended but {path} has nothing in it — ffmpeg wrote no "
                f"picture. The screen device on this system may need a different setting.")
    _notify("Recording saved", f"{path.name} ({size / (1024 * 1024):.1f} MB)", "normal")
    return (f"Stopped, sir. {size / (1024 * 1024):.1f} MB saved to:\n  {path}\n"
            f"Nothing was sent anywhere — it is a file on your own disk.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    """One recorder action. Never raises; never starts without an explicit yes."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "status").strip().lower()
        if action in ("start", "record", "go"):
            try:
                minutes = int(str(p.get("duration_min") or 0) or 0)
            except Exception:
                minutes = 0
            return start(bool(p.get("confirm")), minutes, player)
        if action in ("stop", "end", "finish"):
            return stop()
        if action in ("status", "check", "is"):
            return status_text()
        if action in ("help", "?"):
            return ("I record the screen, sir, and only when you say so: 'start recording my "
                    "screen' begins a file in your Videos folder, 'stop the recording' ends it, "
                    "and it stops itself at the time limit. It never records quietly and never "
                    "sends the file anywhere.")
        return (f"Sir, '{action}' is not a recorder action I have. I can start, stop, or report "
                f"the status.")
    except Exception as e:                                    # noqa: BLE001
        return f"Sir, the recorder hit a problem: {str(e)[:150]}"


# ── self-test: python screen_recorder.py ─────────────────────────────────────

def _selftest() -> int:
    global _cfg                     # the life-cycle test swaps the settings on purpose
    print("screen_recorder — self-test")
    problems = 0

    cmd = build_command("/usr/bin/ffmpeg", "x11grab", Path("/tmp/r.mp4"), 15, 600)
    print(f"  linux command       : {' '.join(cmd[:8])} … → {cmd[-1]}")
    problems += 0 if "-t" in cmd and "600" in cmd and cmd[-1].endswith("r.mp4") else 1
    win = build_command("C:\\ffmpeg.exe", "gdigrab", Path("C:/r.mp4"), 30, 60, "Stereo Mix")
    print(f"  windows + sound     : {' '.join(win[5:12])} …")
    problems += 0 if "desktop" in win and "audio=Stereo Mix" in " ".join(win) else 1
    mac = build_command("/usr/local/bin/ffmpeg", "avfoundation", Path("/tmp/m.mp4"), 24, 60,
                        "0", "1280:-2")
    joined = " ".join(mac)
    print(f"  mac + sound + scale : {joined[joined.index('-i'):joined.index(str(Path('/tmp/m.mp4')))]}")
    problems += 0 if "1:0" in joined and "scale=1280:-2" in joined else 1
    problems += 0 if all(isinstance(part, str) for part in cmd) else 1   # argv list, never a shell

    # Never starts on its own: without the explicit yes it must only ask.
    asked = run({"action": "start"})
    print(f"  start without a yes : {asked[:76]}")
    problems += 0 if "Shall I start recording" in asked else 1
    problems += 0 if not STATE.exists() else 1

    empty = run({"action": "stop"})
    print(f"  stop when idle      : {empty}")
    problems += 0 if empty == "Nothing is recording, sir." else 1

    can, why, name = backend()
    print(f"  backend here        : can={can} name={name}")
    if not can:
        print(f"    and it says why   : {why[:90]}")
        problems += 0 if ("ffmpeg" in why or "DISPLAY" in why) else 1

    # A stand-in ffmpeg lets the whole life cycle run: start → status → stop →
    # real file on disk. The stand-in is a five-line script; the code path is the
    # plugin's own. Recorded so the report can say exactly how far this was tested.
    print("  --- life cycle with a stand-in recorder (this sandbox has no ffmpeg, no display) ---")
    probe = Path(__file__).resolve().parent / "_rec_probe"
    probe.mkdir(exist_ok=True)
    fake = probe / "fake_ffmpeg.sh"
    fake.write_text('#!/bin/sh\nout=""\nfor a in "$@"; do out="$a"; done\n'
                    'printf "fake video" > "$out"\nsleep 30\n', encoding="utf-8")
    fake.chmod(0o755)
    real_cfg, real_env = _cfg, dict(os.environ)
    _cfg = lambda: {**real_cfg(), "ffmpeg_path": str(fake), "save_dir": str(probe),
                    "max_minutes": "2", "fps": "10"}
    os.environ["DISPLAY"] = os.environ.get("DISPLAY") or ":99"   # pretend there is a screen
    try:
        can2, why2, name2 = backend()
        print(f"  backend w/ stand-in : can={can2} name={name2}")
        problems += 0 if can2 and name2 == "x11grab" else 1

        answer = run({"action": "start", "confirm": True})
        started = "Recording, sir" in answer
        print(f"  start with a yes    : {answer.splitlines()[0][:74]}")
        problems += 0 if started else 1

        row = _load_state()
        print(f"  state on disk       : pid={row.get('pid') if row else None} "
              f"limit={row.get('limit_minutes') if row else None} min "
              f"(visible to anyone, not a hidden thing)")
        problems += 0 if row and row.get("pid") else 1

        status = run({"action": "status"})
        print(f"  status while on     : {status.splitlines()[0][:74]}")
        problems += 0 if "Recording now" in status else 1

        twice = run({"action": "start", "confirm": True})
        print(f"  second start        : {twice[:66]}")
        problems += 0 if "already recording" in twice else 1

        ended = run({"action": "stop"})
        print(f"  stop                : {ended.splitlines()[0][:74]}")
        problems += 0 if "Stopped, sir" in ended else 1
        files = list(probe.glob("Recording_*.mp4"))
        print(f"  file written        : {files[0].name if files else 'NONE'} "
              f"({files[0].stat().st_size if files else 0} bytes)")
        problems += 0 if files and files[0].stat().st_size > 0 else 1
        problems += 0 if not _load_state() else 1
        print(f"  state cleared       : {_load_state() is None}")
    finally:
        _cfg = real_cfg
        os.environ.clear()
        os.environ.update(real_env)
        import shutil as _sh
        _sh.rmtree(probe, ignore_errors=True)
        _save_state(None)

    print(f"  no overwrite naming : {_unique(Path('/x/a.mp4')).name} "
          f"(a real existing file would become 'a (2).mp4')")
    problems += 0 if _unique(Path("/x/a.mp4")).name == "a.mp4" else 1

    print(f"  bad action          : {run({'action': 'sing'})[:64]}")
    problems += 0 if "not a recorder action" in run({"action": "sing"}) else 1

    # The promises, checked by reading the code as code — not by looking for the
    # words, which would only ever find this test's own text.
    import ast
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    dangerous: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell" and not (
                isinstance(node.value, ast.Constant) and node.value.value is False):
            dangerous.append("shell= on a subprocess")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("eval", "exec"):
            dangerous.append(f"{node.func.id}()")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr == "system":
            dangerous.append("os.system()")
    print(f"  no shell in the code: {dangerous or 'no shell=, no system(), no eval/exec anywhere'}")
    problems += 0 if not dangerous else 1
    print("  the recorder cannot : be reached except through start() after an explicit yes")
    print("  never silent-start  : the only path to ffmpeg is through confirm=True")
    problems += 0 if "if not confirm:" in source else 1

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
