"""
remote_gateway.py — a small authenticated door for scripts and other devices.

WHAT THIS IS FOR
    The project already has a remote path for browsers: dashboard/server.py,
    with its own pairing, bearer tokens and a web UI. That is the right thing
    for a phone browser and this plugin does not replace it.

    What was missing is a plain HTTP endpoint for things that are not browsers:
    a cron job on a server, an IFTTT/applet, a Raspberry Pi in another room, a
    shell script on your laptop. They all speak one line of curl and nothing
    else. That is what this is.

    A request is not executed as a command. It is handed to the same
    entry point the dashboard and the desk text box use —
    player.on_text_command(text) — which means the model decides what to do,
    every existing tool guard still applies, and nothing here can run a shell.
    There is no eval, no subprocess, no exec anywhere in this file.

SECURITY, AND WHY THE DEFAULTS ARE WHAT THEY ARE
    OFF until you turn it on. This is a listener; a listener that starts because
    a file was copied into a folder is exactly the wrong default.

    BINDS TO 127.0.0.1 unless you deliberately change it. On localhost only,
    nothing on your network can reach it. Changing it to 0.0.0.0 opens it to
    the LAN and the plugin says so in the log when it starts.

    TOKEN REQUIRED, ALWAYS. There is no "no token" mode. The token lives in the
    settings store, is compared with hmac.compare_digest, and never appears in a
    log line or in a response body.

    RATE LIMITED per client address, and the body is capped — a runaway script
    cannot flood the model, and an oversized body is rejected before it is read.

    REQUESTS ARE NOT ANONYMOUS IN THE LOG: the gateway writes who called and how
    many characters arrived, but never the token.

SETTINGS (Plugin Manager → Remote gateway)
    enabled        off by default; turning it on starts the listener
    token          required, long and random — generate one with:
                   python -c "import secrets;print(secrets.token_urlsafe(32))"
    port           default 8765
    bind_host      127.0.0.1 (this machine only) or 0.0.0.0 (whole network)
    per_minute     requests allowed per client per minute, default 10
    max_chars      longest accepted message, default 1000

USING IT
    curl -X POST http://127.0.0.1:8765/say \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{"text":"what is the weather"}'
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "remote_gateway",
    "description": (
        "An authenticated local HTTP endpoint that lets scripts and other devices send "
        "requests to Mark-LV: 'start the remote gateway', 'stop the remote gateway', 'is "
        "the gateway running'. Requests are handed to the assistant as text, exactly like "
        "typing them — never executed as commands. Do NOT use it to control the dashboard "
        "(that is built in) or to send messages to people."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | stop | status | token"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "remote_gateway",
    "title": "Remote gateway / البوابة الخارجية",
    "fields": [
        {"key": "enabled", "type": "toggle", "label": "Enabled / مُفعّلة",
         "default": False},
        {"key": "token", "type": "password", "label": "Access token / رمز الوصول",
         "default": "", "placeholder": "paste a long random string"},
        {"key": "port", "type": "text", "label": "Port / المنفذ", "default": "8765"},
        {"key": "bind_host", "type": "choice", "label": "Listen on / الاستماع على",
         "default": "127.0.0.1", "choices": ["127.0.0.1", "0.0.0.0"]},
        {"key": "per_minute", "type": "text", "label": "Requests per minute per client",
         "default": "10"},
        {"key": "max_chars", "type": "text", "label": "Max message length / أقصى طول",
         "default": "1000"},
    ],
}

_SERVER: ThreadingHTTPServer | None = None
_THREAD: threading.Thread | None = None
_ON_TEXT: Any = None
_BUCKETS: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(PLUGIN_SETTINGS["namespace"]) or {}
        out.update({k: v for k, v in stored.items() if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_or(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except Exception:
        return default


def _rate_ok(addr: str) -> tuple[bool, str]:
    """Sliding one-minute window per client. Returns (allowed, reason)."""
    limit = _int_or(_cfg().get("per_minute"), 10, 1, 600)
    now = time.time()
    with _LOCK:
        hits = [t for t in _BUCKETS.get(addr, []) if now - t < 60]
        if len(hits) >= limit:
            _BUCKETS[addr] = hits
            return False, f"rate limit ({limit}/min) reached for {addr}"
        hits.append(now)
        _BUCKETS[addr] = hits[-200:]
    return True, ""


def _clean(text: str) -> str:
    """Strip control characters and clamp length before anything sees it."""
    keep = []
    for ch in str(text):
        code = ord(ch)
        if ch in "\n\t":
            keep.append(ch)
        elif code >= 32 and code != 127:
            keep.append(ch)
    out = "".join(keep).strip()
    limit = _int_or(_cfg().get("max_chars"), 1000, 20, 8000)
    return out[:limit]


class _Handler(BaseHTTPRequestHandler):
    """One request = one line of text handed to the assistant. Nothing else."""

    server_version = "MarkLVGateway/1.0"

    def log_message(self, fmt, *args):        # noqa: A003 - silence stderr chatter
        return

    # -- helpers --------------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _authorised(self) -> bool:
        expected = str(_cfg().get("token") or "").strip()
        if not expected:
            return False
        header = self.headers.get("Authorization", "")
        offered = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not offered:
            offered = str(self.headers.get("X-Gateway-Token", "")).strip()
        return bool(offered) and hmac.compare_digest(offered, expected)

    # -- routes ---------------------------------------------------------------
    def do_GET(self):                        # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {"ok": True, "service": "mark-lv-gateway",
                             "time": int(time.time())})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):                       # noqa: N802
        path = self.path.rstrip("/") or "/say"
        if path not in ("/say", "/command", "/message"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        allowed, why = _rate_ok(self.client_address[0])
        if not allowed:
            self._send(429, {"ok": False, "error": why})
            return
        if not self._authorised():
            # Deliberately identical for "no token set" and "wrong token":
            # a caller learns nothing about our configuration.
            self._send(401, {"ok": False, "error": "unauthorised"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._send(413, {"ok": False, "error": "empty or oversized body"})
            return
        try:
            raw = self.rfile.read(length).decode("utf-8", "replace")
        except Exception:
            self._send(400, {"ok": False, "error": "could not read the body"})
            return

        text = ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                text = str(data.get("text") or data.get("message")
                           or data.get("command") or "").strip()
        except Exception:
            text = raw.strip()          # plain-text body is allowed too
        text = _clean(text)
        if not text:
            self._send(400, {"ok": False, "error": "no text supplied"})
            return

        callback = _ON_TEXT
        if not callable(callback):
            self._send(503, {"ok": False,
                             "error": "Mark-LV is not running with a live session"})
            return
        try:
            callback(text)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"delivery failed: {str(e)[:80]}"})
            return
        print(f"[Gateway] delivered {len(text)} char(s) from {self.client_address[0]}")
        self._send(200, {"ok": True, "delivered": len(text)})


# ── lifecycle ─────────────────────────────────────────────────────────────────

def _start(player) -> str:
    global _SERVER, _THREAD, _ON_TEXT
    cfg = _cfg()
    token = str(cfg.get("token") or "").strip()
    if not token:
        return ("Sir, the gateway has no access token set, so I will not open it. Generate "
                "one with python -c \"import secrets;print(secrets.token_urlsafe(32))\" and "
                "paste it into the plugin settings.")
    if len(token) < 16:
        return ("Sir, that token is short enough to guess. Please use at least 24 random "
                "characters.")
    if _SERVER is not None:
        return "The gateway is already running, sir."

    callback = getattr(player, "on_text_command", None) if player else None
    _ON_TEXT = callback
    host = str(cfg.get("bind_host") or "127.0.0.1")
    port = _int_or(cfg.get("port"), 8765, 1024, 65535)
    try:
        _SERVER = ThreadingHTTPServer((host, port), _Handler)
    except OSError as e:
        _SERVER = None
        if "in use" in str(e).lower() or getattr(e, "errno", None) == 98:
            return (f"Sir, port {port} is already taken. Pick another one in the settings.")
        return f"Sir, I could not open the gateway: {str(e)[:100]}"
    _SERVER.daemon_threads = True
    _THREAD = threading.Thread(target=_SERVER.serve_forever, name="RemoteGateway", daemon=True)
    _THREAD.start()
    scope = ("this machine only" if host == "127.0.0.1"
             else "the WHOLE local network — anyone who can reach this port may try")
    if player:
        try:
            player.write_log(f"SYS: Remote gateway listening on {host}:{port} ({scope}).")
        except Exception:
            pass
    note = "" if callback else (" I could not see the live session, so requests will be "
                                "refused until Mark-LV is fully started.")
    return f"Gateway listening on {host}:{port}, reaching {scope}, sir.{note}"


def _stop() -> str:
    global _SERVER, _THREAD
    if _SERVER is None:
        return "The gateway is not running, sir."
    try:
        _SERVER.shutdown()
        _SERVER.server_close()
    except Exception:
        pass
    _SERVER, _THREAD = None, None
    return "Gateway stopped, sir."


def _action_status(parameters: dict, player) -> str:
    cfg = _cfg()
    token = str(cfg.get("token") or "").strip()
    running = _SERVER is not None
    host = str(cfg.get("bind_host") or "127.0.0.1")
    port = _int_or(cfg.get("port"), 8765, 1024, 65535)
    lines = [
        f"Running      : {'yes — ' + host + ':' + str(port) if running else 'no'}",
        f"Enabled      : {'yes' if cfg.get('enabled') else 'no (off by default)'}",
        f"Token        : {'set (' + str(len(token)) + ' chars)' if token else 'NOT set — the gateway will refuse to start'}",
        f"Scope        : {'this machine only' if host == '127.0.0.1' else 'the whole local network'}",
        f"Rate limit   : {_int_or(cfg.get('per_minute'), 10, 1, 600)} requests/minute per client",
        f"Max length   : {_int_or(cfg.get('max_chars'), 1000, 20, 8000)} characters",
        f"Session      : {'linked' if callable(_ON_TEXT) else 'not linked (is Mark-LV fully started?)'}",
        "",
        "Test it with:",
        f'  curl -X POST http://{host}:{port}/say -H "Authorization: Bearer <token>" '
        f'-H "Content-Type: application/json" -d \'{{"text":"hello"}}\'',
    ]
    text = "\n".join(lines)
    if player:
        try:
            player.show_content("Remote gateway", text)
        except Exception:
            pass
    return text


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one gateway action. Never raises."""
    try:
        action = str((parameters or {}).get("action") or "status").strip().lower()
        if action in {"start", "on", "open", "enable"}:
            return _start(player)
        if action in {"stop", "off", "close", "disable"}:
            return _stop()
        if action in {"status", "test", "help"}:
            return _action_status(parameters or {}, player)
        if action in {"token", "new_token"}:
            return ("I will not put a token in the conversation, sir — it would end up in the "
                    "log. Generate one on the machine and paste it into the settings: "
                    "python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
        return (f"Sir, '{action}' is not a gateway action I have. I can start, stop, or "
                f"report status.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the remote gateway hit a problem: {str(e)[:150]}"


# ── self-test: python remote_gateway.py ─────────────────────────────────────

def _selftest() -> int:
    print("remote_gateway — self-test")
    problems = 0
    cfg = _cfg()

    class FakePlayer:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def on_text_command(self, text: str) -> None:
            self.seen.append(text)

        def write_log(self, text: str) -> None:
            pass

        def show_content(self, *a) -> None:
            pass

    print(f"  default enabled      : {cfg.get('enabled')} (must be False)")
    problems += 0 if cfg.get("enabled") in (False, "False", None, "") else 1
    print(f"  refuses without token-> {run({'action': 'start'})[:80]}")
    problems += 0 if "no access token" in run({"action": "start"}) else 1

    # Start it on a high port with a throwaway token, exercise it, then stop it.
    # The settings are written straight into the config file (and restored at the
    # end) so the test does not depend on the app being up.
    cfg_file = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    backup = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else None

    def _force(values: dict) -> None:
        data: dict = {}
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        pc = data.setdefault("plugin_config", {})
        pc.setdefault("remote_gateway", {}).update(values)
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(data, indent=4), encoding="utf-8")

    _force({"token": "x" * 40, "port": "8791", "enabled": True})
    player = FakePlayer()
    print(f"  start                -> {run({'action': 'start'}, player)[:90]}")

    import urllib.error
    import urllib.request

    def call(text: str, token: str | None = "x" * 40, path: str = "/say"):
        req = urllib.request.Request(
            f"http://127.0.0.1:8791{path}",
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:
            return 0, str(e)

    code, body = call("hello from the self-test")
    print(f"  authorised request   -> {code} {body[:60]}")
    problems += 0 if code == 200 and player.seen else 1
    code2, _ = call("no token", token=None)
    code3, _ = call("wrong token", token="y" * 40)
    print(f"  missing token        -> {code2}   wrong token -> {code3}")
    problems += 0 if code2 == 401 and code3 == 401 else 1
    code4, _ = call("", token="x" * 40)
    print(f"  empty text           -> {code4}")
    problems += 0 if code4 == 400 else 1
    ctrl = "bad\x00text\x07with\x1bcontrols"
    call(ctrl)
    print(f"  control chars        : stored as {player.seen[-1]!r}")
    problems += 0 if "\x00" not in player.seen[-1] else 1
    with urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=5) as r:
        print(f"  health               -> {r.status} {r.read().decode()[:50]}")
    print(f"  stop                 -> {run({'action': 'stop'})}")
    try:
        urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=3)
        print("  FAIL: server still answering after stop"); problems += 1
    except Exception:
        print("  port closed after stop: ok")

    if backup is not None:
        cfg_file.write_text(backup, encoding="utf-8")
    else:
        try:
            cfg_file.unlink()
        except Exception:
            pass
    print("  settings restored    : ok")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
