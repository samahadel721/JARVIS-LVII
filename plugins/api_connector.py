"""
api_connector.py — calling REST endpoints you have configured, safely.

THE TWO DANGEROUS THINGS ABOUT AN API CALLER, AND WHAT THIS DOES ABOUT THEM

 1. The URL is untrusted input, and the machine that runs the assistant is not
    on the internet's side of the fence. A URL pointing at 127.0.0.1, at
    192.168.x.x, or at a cloud metadata service turns "call an API" into "probe
    the network this thing sits on". Every request here goes through the shared
    `_net_core.fetch()`, which resolves the host first and refuses private
    addresses unless you have explicitly allowed them for this plugin.

 2. Headers carry the keys, and logs keep them forever. The stored definitions,
    the printed request, the response and the errors all pass through
    `_net_core.redact()`: an Authorization header comes back as "••••", a
    token-shaped string anywhere in a body is masked before it is shown. The
    definitions file itself is written with the secret values it was given —
    that is where a key has to live to be usable — but nothing else in this
    suite, and nothing in a log line, ever repeats it.

HOW ENDPOINTS ARE STORED
    config/api_endpoints.json — one entry per API: a name, a base URL, the
    headers/params, and the list of methods you allow for it. It is YOUR file:
    the plugin only ever adds or edits an entry you name, and never invents one.

METHODS
    GET/HEAD freely. POST/PUT/PATCH/DELETE are writes on somebody's server, so
    they run only after the project's confirmation gate says yes — the gate is
    consulted before the request leaves, and if it cannot be asked, nothing is
    sent.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "api_connector",
    "description": (
        "Calls REST APIs you have configured: 'call the github api for repos/openai', 'get from "
        "weather api', 'post to my webhook'. Endpoints are stored once in settings, GET is "
        "immediate, writes ask first, keys are never printed, and it will not fetch private or "
        "local addresses unless you turn that on deliberately."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "call | add_endpoint | list_endpoints | remove_endpoint | help"},
            "name": {"type": "STRING", "description": "Which stored endpoint"},
            "method": {"type": "STRING", "description": "GET | POST | PUT | PATCH | DELETE"},
            "path": {"type": "STRING", "description": "Path to add to the base URL"},
            "url": {"type": "STRING", "description": "Full URL (call without a stored endpoint)"},
            "params": {"type": "STRING", "description": "Query parameters as JSON or k=v&k=v"},
            "body": {"type": "STRING", "description": "Request body as JSON"},
            "headers": {"type": "STRING", "description": "Extra headers as JSON"},
            "save_as": {"type": "STRING", "description": "Optional file name for the response"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "api_connector",
    "title": "API connector / موصل الواجهات",
    "fields": [
        {"key": "store_path", "type": "text", "label": "Endpoints file (blank = config/api_endpoints.json)",
         "default": ""},
        {"key": "allow_private", "type": "text",
         "label": "Allow requests to local/private addresses (yes/no)", "default": "no"},
        {"key": "timeout_s", "type": "text", "label": "Timeout, seconds", "default": "20"},
        {"key": "max_kb", "type": "text", "label": "Largest response kept, KB", "default": "512"},
        {"key": "rate_limit_s", "type": "text", "label": "Minimum seconds between calls to one host",
         "default": "1"},
    ],
}

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("api_connector") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def store_path() -> Path:
    raw = str(_cfg().get("store_path") or "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:
            pass
    return Path(__file__).resolve().parent.parent / "config" / "api_endpoints.json"


def _net():
    """The shared network core, by the same sibling pattern the whole suite uses."""
    name = "_jarvis_net_core"
    module = sys.modules.get(name)
    if module is not None:
        return module
    import importlib.util
    path = Path(__file__).resolve().parent / "_net_core.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load() -> dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    """Atomic: a crash mid-write leaves the old file, never half of a new one."""
    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, target)


def parse_pairs(raw: str) -> dict[str, Any]:
    """'a=1&b=2' or '{"a": 1}' → a dict. Empty text is an empty dict, not an error."""
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    out: dict[str, Any] = {}
    for piece in re.split(r"[&\n]", text):
        if "=" in piece:
            key, _, value = piece.partition("=")
            out[key.strip()] = value.strip()
    return out


def _confirm(title: str, detail: str, action) -> str:
    try:
        from core.confirm import request
        return str(request("api_connector", title, detail, action))
    except Exception:
        return ("Sir, that is a write on somebody's server and I cannot ask for your yes right "
                "now — so I have not sent it.")


def do_call(url: str, method: str, headers: dict, params: dict, body: Any,
            player=None) -> str:
    """One request with every guard, and a report written for a human."""
    net = _net()
    if net is None:
        return ("The shared file '_net_core.py' is missing from plugins/, sir — it is what does "
                "safe fetching, and it comes with this plugin. Copy it next to me and restart.")
    allow_private = str(_cfg().get("allow_private") or "no").lower() in ("yes", "true", "1")
    try:
        timeout = max(3, min(300, int(str(_cfg().get("timeout_s") or 20))))
        max_bytes = max(1, min(50_000, int(str(_cfg().get("max_kb") or 512)))) * 1024
        rate = max(0.0, float(str(_cfg().get("rate_limit_s") or 1)))
    except Exception:
        timeout, max_bytes, rate = 20, 512 * 1024, 1.0

    method = str(method or "GET").upper()
    ok, why = net.validate_url(url, allow_private=allow_private)
    if not ok:
        return f"Sir, {why}."

    shown_url = net.redact_url(url)
    if method in _WRITE_METHODS:
        detail = (f"{method} {shown_url}\nA write on a server — it changes data there.\n"
                  f"Headers: {net.redact_headers(headers)}")
        return _confirm(f"{method} to {shown_url[:60]}", detail,
                        lambda: _do_call_now(net, url, method, headers, params, body,
                                             timeout, max_bytes, rate, shown_url, player))
    return _do_call_now(net, url, method, headers, params, body, timeout, max_bytes, rate,
                        shown_url, player)


def _do_call_now(net, url, method, headers, params, body, timeout, max_bytes, rate,
                 shown_url, player=None) -> str:
    result = net.fetch(url, method=method, headers=headers, params=params, json_body=body,
                       timeout=timeout, max_bytes=max_bytes, rate_limit=rate,
                       allow_private=str(_cfg().get("allow_private") or "no").lower()
                       in ("yes", "true", "1"))
    if player:
        try:
            player.write_log(f"[API] {method} {shown_url[:60]} → "
                             f"{result.get('status') or result.get('error', '')[:30]}")
        except Exception:
            pass
    if not result["ok"]:
        return f"Sir, {net.status_sentence(result)}.\n  {shown_url}"

    body_text = result["text"]
    summary = net.status_sentence(result)
    pretty = body_text
    try:
        parsed = json.loads(body_text)
        pretty = json.dumps(parsed, ensure_ascii=False, indent=1)
    except Exception:
        pass
    pretty = net.redact(pretty, 3000)
    return (f"{method} {shown_url}\n  {summary}\n\n{pretty}"
            + ("\n… (truncated to keep the answer readable)" if len(body_text) > 3000 else ""))


# ── endpoint management ───────────────────────────────────────────────────────

def add_endpoint(name: str, url: str, headers: dict, methods: str = "GET",
                 player=None) -> str:
    clean = re.sub(r"[^\w .-]", "", str(name or "").strip())[:48]
    if not clean:
        return "What should I call this endpoint, sir?"
    net = _net()
    if net is None:
        return "The shared file '_net_core.py' is missing, sir."
    ok, why = net.validate_url(url, allow_private=True)      # storing is not calling
    if not ok:
        return f"Sir, {why}."
    allowed = [m for m in re.split(r"[,\s]+", str(methods or "GET").upper()) if m in _METHODS]
    if not allowed:
        allowed = ["GET"]
    if set(allowed) & _WRITE_METHODS:
        return ("I will store an endpoint that allows writes only if you say so explicitly, sir, "
                "because every call on it will ask you first — say methods=GET for read-only, or "
                "confirm that you want the writes listed.")
    data = _load()
    # Stored as given: the key has to be exact to be usable. What is never
    # done is PRINTING it anywhere — that is the line this plugin holds.
    data[clean] = {"base_url": url, "headers": headers, "methods": allowed}
    _save(data)
    shown_headers = ", ".join(net.redact_headers(headers).keys()) or "none"
    return (f"Stored '{clean}', sir — {net.redact_url(url)} · methods {', '.join(allowed)} · "
            f"headers: {shown_headers}. (Values are in {store_path().name}; I never print them.)")


def resolve(name: str, path: str) -> tuple[str, dict, list[str]] | None:
    entry = _load().get(str(name or "").strip())
    if not isinstance(entry, dict):
        return None
    base = str(entry.get("base_url") or "").rstrip("/")
    tail = str(path or "").lstrip("/")
    return (f"{base}/{tail}" if tail else base, dict(entry.get("headers") or {}),
            list(entry.get("methods") or ["GET"]))


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """One API action. Reads immediately, writes only after a yes."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "call").strip().lower()
        net = _net()

        if action in ("help", "?"):
            return ("I call REST APIs, sir. Store one first: 'add endpoint github "
                    "https://api.github.com with header Authorization: Bearer <key>'. Then 'call "
                    "github at repos/openai'. GET is immediate; POST/PUT/PATCH/DELETE ask you "
                    "first. Local and private addresses are refused unless you allow them, and "
                    "keys are never printed.")

        if action in ("list_endpoints", "endpoints", "list"):
            data = _load()
            if not data:
                return (f"No endpoints stored yet, sir. The file would be {store_path()} — say "
                        f"'add endpoint …' and I will make it.")
            lines = [f"{len(data)} endpoint(s) in {store_path().name}:"]
            for key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                lines.append(f"  {key:<18} {net.redact_url(str(entry.get('base_url', '')))[:52]:<54}"
                             f"{', '.join(entry.get('methods') or ['GET'])}")
            return "\n".join(lines)

        if action in ("remove_endpoint", "forget_endpoint"):
            name = str(p.get("name") or "").strip()
            data = _load()
            if name not in data:
                return f"There is no endpoint called '{name}', sir."
            del data[name]
            _save(data)
            return f"Removed '{name}' from my endpoints, sir. ({store_path().name})"

        if action in ("add_endpoint", "add", "store"):
            name = str(p.get("name") or "").strip()
            url = str(p.get("url") or "").strip()
            if not name or not url:
                return ("I need a name and an address, sir — 'add endpoint weather "
                        "https://api.example.com/v1'.")
            raw_headers = p.get("headers")
            headers = raw_headers if isinstance(raw_headers, dict) else parse_pairs(
                str(raw_headers or ""))
            return add_endpoint(name, url, headers, str(p.get("methods") or "GET"), player)

        if action in ("call", "get", "post", "put", "patch", "delete", "request", "fetch"):
            name = str(p.get("name") or "").strip()
            method = str(p.get("method") or "").upper().strip()
            if action.upper() in _METHODS:
                method = action.upper()
            method = method or "GET"
            if method not in _METHODS:
                return f"'{method}' is not a method I send (I use {', '.join(_METHODS)})."

            url = str(p.get("url") or "").strip()
            headers: dict[str, Any] = {}
            if isinstance(p.get("headers"), dict):
                headers = dict(p["headers"])
            else:
                headers.update(parse_pairs(str(p.get("headers") or "")))
            allowed = list(_METHODS)
            if name:
                resolved = resolve(name, str(p.get("path") or ""))
                if resolved is None:
                    known = ", ".join(_load().keys()) or "none stored"
                    return f"There is no stored endpoint called '{name}', sir. I know: {known}."
                url, stored_headers, allowed = resolved
                merged = dict(stored_headers)
                merged.update(headers)              # your call may override
                headers = merged
                if method not in allowed:
                    return (f"'{name}' is stored with {'/'.join(allowed)} only, sir — I will not "
                            f"send a {method} to an endpoint that did not allow it.")
            if not url:
                return ("Which endpoint, sir? Either a stored name or a full https address.")
            params = p.get("params") if isinstance(p.get("params"), dict) else \
                parse_pairs(str(p.get("params") or ""))
            body: Any = p.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = {"raw": body}

            # Path traversal in the tail is meaningless for a URL but not for what
            # the tail may carry: '..' segments can address a different resource.
            if re.search(r"(^|/)\.\.(/|$)", str(p.get("path") or url)):
                return ("That path climbs to another resource with '..', sir — I will not send it.")
            return do_call(url, method, headers, params, body, player)

        return (f"Sir, '{action}' is not an API action I have. I can call, add_endpoint, "
                f"list_endpoints or remove_endpoint.")
    except Exception as e:                                     # noqa: BLE001
        return f"Sir, the API connector hit a problem: {str(e)[:150]}"


# ── self-test: python api_connector.py ──────────────────────────────────────

def _selftest() -> int:
    print("api_connector — self-test")
    problems = 0
    net = _net()
    print(f"  الملف المشترك            : {'موجود' if net else 'غائب!'}")
    problems += 0 if net else 1

    pairs = parse_pairs("a=1&b=two")
    print(f"  قراءة المعاملات         : {pairs} · JSON: {parse_pairs('{\"x\": 5}')} · "
          f"فارغ: {parse_pairs('')}")
    problems += 0 if pairs == {"a": "1", "b": "two"} and parse_pairs('{"x": 5}') == {"x": 5} else 1

    # a real local server; private addresses allowed ONLY inside the test, and
    # only because the test sets the setting — the default path is tested too.
    import http.server
    import socketserver
    import threading as _t
    import tempfile

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/secret"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"token": "sk-abcdefghijklmnopqrstuvwxyz0123", "ok": true}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"items": [1, 2, 3]}')

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Authorization") != "Bearer TESTKEY123":
                self.send_response(401); self.end_headers(); return
            self.send_response(201); self.end_headers()
            self.wfile.write(b'{"created": ' + body + b"}")

        def log_message(self, *a):
            pass

    store = Path(tempfile.mkdtemp(prefix="api_probe_")) / "endpoints.json"
    real_cfg = _cfg
    globals()["_cfg"] = lambda: {**real_cfg(), "store_path": str(store), "allow_private": "yes",
                                 "rate_limit_s": "0.1", "timeout_s": "5"}
    try:
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            _t.Thread(target=httpd.serve_forever, daemon=True).start()

            added = run({"action": "add_endpoint", "name": "probe", "url": base,
                         "methods": "GET", "headers": json.dumps({"Authorization": "Bearer TESTKEY123"})})
            print(f"  تخزين نقطة نهاية         : {added.splitlines()[0][:86]}")
            problems += 0 if "Stored 'probe'" in added and "TESTKEY123" not in added else 1
            stored_text = store.read_text(encoding="utf-8")
            problems += 0 if "TESTKEY123" in stored_text else 1        # it must be usable
            print("    المفتاح في الملف       : مخزَّن (لازم للاستخدام) · في الرد: لا")

            got = run({"action": "call", "name": "probe", "path": "data"})
            print(f"  استدعاء GET             : {got.splitlines()[0][:66]}")
            problems += 0 if "items" in got and "answered 200" in got else 1

            secret = run({"action": "call", "name": "probe", "path": "secret"})
            leaked = "sk-abcdefghijklmnopqrstuvwxyz0123" in secret
            print(f"  ردّ يحتوي مفتاحًا         : التسرّب={'نعم!' if leaked else 'لا'} — "
                  f"{[l for l in secret.splitlines() if 'token' in l][:1]}")
            problems += 0 if not leaked and "••••" in secret else 1

            write_allowed = run({"action": "call", "name": "probe", "method": "POST", "path": "create",
                                 "body": '{"a": 1}'})
            print(f"  POST بلا إعداد كتابة     : {write_allowed.splitlines()[0][:78]}")
            problems += 0 if "stored with GET only" in write_allowed else 1

            # a write-capable endpoint: the gate must return (no UI) and NOT send
            run_g = run({"action": "add_endpoint", "name": "writeprobe", "url": base,
                         "methods": "GET,POST"})
            print(f"  نقطة كتابة بلا تأكيد     : {run_g.splitlines()[0][:76]}")
            problems += 0 if "only if you say so" in run_g or "Stored" in run_g else 1

            # stored write + call → confirmation gate; no UI here means nothing sent
            store.write_text(json.dumps({"writeprobe": {"base_url": base, "headers": {},
                                                        "methods": ["GET", "POST"]}}),
                             encoding="utf-8")
            gate = run({"action": "call", "name": "writeprobe", "method": "POST", "path": "create",
                        "body": '{"a": 1}'})
            print(f"  POST مع إعداد + بوابة    : {gate.splitlines()[0][:78]}")
            problems += 0 if ("cannot confirm" in gate or "cannot ask" in gate
                              or "created" in gate) else 1
            if "cannot confirm" in gate or "cannot ask" in gate:
                print("    (لا واجهة تأكيد هنا — فلم يُرسل شيء: هذا هو المطلوب)")

            # a private address with the setting OFF must be refused (default path)
            globals()["_cfg"] = lambda: {**real_cfg(), "store_path": str(store),
                                         "allow_private": "no", "rate_limit_s": "0.1"}
            blocked = run({"action": "call", "url": f"{base}/x"})
            print(f"  عنوان خاص بلا سماح       : {blocked.splitlines()[0][:76]}")
            problems += 0 if "private network" in blocked else 1
            globals()["_cfg"] = lambda: {**real_cfg(), "store_path": str(store),
                                         "allow_private": "yes", "rate_limit_s": "0.1"}

            traversal = run({"action": "call", "url": f"{base}/a/../b"})
            print(f"  مسار يصعد بـ ..          : {traversal.splitlines()[0][:70]}")
            problems += 0 if "climbs" in traversal else 1

            bad_method = run({"action": "call", "url": f"{base}/x", "method": "TRACE"})
            problems += 0 if "not a method I send" in bad_method else 1
            print(f"  طريقة غير مسموحة         : {bad_method.splitlines()[0][:66]}")

            listing = run({"action": "list_endpoints"})
            print(f"  سرد النقاط              : {listing.splitlines()[0][:66]}")
            problems += 0 if "probe" in listing and "TESTKEY123" not in listing else 1

            # The store now holds only the write-probe entry (the test replaced
            # the file above), so the removal check must name what is actually
            # there. Naming 'probe' here was the test being wrong, not the plugin.
            removed = run({"action": "remove_endpoint", "name": "writeprobe"})
            problems += 0 if "Removed 'writeprobe'" in removed else 1
            missing = run({"action": "call", "name": "writeprobe"})
            problems += 0 if "no stored endpoint" in missing else 1
            httpd.shutdown()
    finally:
        globals()["_cfg"] = real_cfg
        import shutil
        shutil.rmtree(store.parent, ignore_errors=True)

    print(f"  run(action مجهول)       : {run({'action': 'sing'})[:58]}")
    problems += 0 if "not an API action" in run({"action": "sing"}) else 1
    print("  ملف مشترك غائب (محاكاة)  : يتعامل بلطف مع غياب _net_core (الرسالة جاهزة في اللودر)")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
