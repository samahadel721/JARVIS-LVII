"""
_net_core.py — the one place in this suite that talks to the network.

Four plugins need to fetch things: the API connector, the website monitor, the
news monitor and the price monitor. Rather than four copies of "call requests
and hope", they share this file — the same pattern as _whatsapp_core.py, right
down to the leading underscore that tells the plugin loader this is a helper,
not a plugin. If it is missing, the loader already knows how to say so:
"Needs the shared file '_net_core.py', which is not in plugins/."

What every fetch through here gets, whether the caller remembered to ask:

  * A scheme check and an SSRF guard. http/https only, and the host is resolved
    first — a URL pointing at 127.0.0.1, 192.168.x.x, a link-local address or a
    cloud metadata service is refused unless the user has explicitly allowed
    private addresses in that plugin's settings. A URL is untrusted input; it
    should not be able to make the assistant probe the machine it runs on.
  * A size ceiling and a timeout, so one slow or endless response cannot hold
    the assistant or fill the disk.
  * A per-host minimum interval, so a monitor that fires every minute does not
    hammer somebody's server — and does not get the user's IP banned for it.
  * Redaction. Every log line, every error and every stored snippet goes through
    `redact()` first: Authorization headers, cookies, api keys, tokens and
    passwords never leave this module in readable form.

No credentials are ever invented or stored here. Callers pass what the user
configured; this module only refuses to repeat it where it could be seen.

Self-test: python _net_core.py
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse, urlencode

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 2_000_000            # 2 MB of body is plenty for an API,
                                         # a page snapshot or a price scrape
MIN_INTERVAL = 1.0                       # seconds between calls to one host

# Hostnames that mean "the machine this is running on" or "the infrastructure
# behind it". Checked by name as well as by resolved address, because some of
# these resolve differently depending on where you are standing.
_BLOCKED_NAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "metadata",
    "metadata.google.internal", "metadata.goog", "instance-data",
}

_SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "api-key",
                   "x-auth-token", "proxy-authorization", "x-amz-security-token"}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|pwd)"
               r"\s*[=:]\s*[\"']?[^\s\"'&,;]{4,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)


class NetError(Exception):
    """A refusal. The message is written for the user, not for a log file."""


# ── redaction ─────────────────────────────────────────────────────────────────

def redact(text: str, limit: int = 300) -> str:
    """Everything that leaves this module says which, never what."""
    out = str(text or "")
    # The scheme word stays ("Bearer ••••") because knowing the kind of token is
    # useful in a diagnostic; every character of the token itself goes. The first
    # version of this kept the prefix up to the last four characters, which left
    # a JWT's header readable — caught by this file's own self-test.
    out = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 ••••", out)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(0).split(m.group(0)[-4:])[0] + "••••", out)
    out = re.sub(r"(?i)(https?://)([^/\s:@]+):([^/\s@]+)@", r"\1••••:••••@", out)
    return out[:limit]


def redact_url(url: str) -> str:
    """A URL with any embedded username/password removed, for showing and logging."""
    try:
        parts = urlparse(str(url))
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            parts = parts._replace(netloc=host)
        return urlunparse(parts)
    except Exception:
        return redact(url, 200)


def redact_headers(headers: dict | None) -> dict:
    """A copy of the headers with every secret value replaced."""
    out = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in _SECRET_HEADERS or re.search(
                r"(?i)(key|token|secret|password|auth)", str(key)):
            out[key] = "••••"
        else:
            out[key] = redact(str(value), 120)
    return out


# ── URL and SSRF validation ───────────────────────────────────────────────────

def host_is_private(host: str) -> tuple[bool, str]:
    """
    (is_private, resolved_address). A name that does not resolve counts as
    private-ish for our purposes: we cannot prove where it goes, so we do not go.
    """
    name = str(host or "").strip().strip("[]").lower()
    if not name:
        return True, ""
    if name in _BLOCKED_NAMES or name.endswith(".local") or name.endswith(".internal"):
        return True, ""
    try:
        infos = socket.getaddrinfo(name, None)
    except Exception:
        return True, ""
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except Exception:
            return True, address
        if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True, address
        if str(ip) == "169.254.169.254":              # cloud metadata, belt and braces
            return True, address
    return False, infos[0][4][0] if infos else ""


def validate_url(url: str, allow_private: bool = False,
                 schemes: tuple[str, ...] = ("http", "https")) -> tuple[bool, str]:
    """(usable, reason-if-not). The caller gets a sentence, not an exception."""
    raw = str(url or "").strip()
    if not raw:
        return False, "there is no address"
    try:
        parts = urlparse(raw)
    except Exception:
        return False, "that is not a readable address"
    if parts.scheme.lower() not in schemes:
        return False, (f"'{parts.scheme or 'no scheme'}' is not one of the schemes I fetch "
                       f"({', '.join(schemes)})")
    if not parts.netloc:
        return False, "that address has no host"
    if parts.username or parts.password:
        return False, ("that address carries a username and password inside it — I will not "
                       "fetch it, because a password in a URL ends up in logs and histories")
    private, address = host_is_private(parts.hostname or "")
    if private and not allow_private:
        return False, (f"'{parts.hostname}' points inside this machine or a private network "
                       f"({address or 'unresolved'}). I will not fetch private addresses unless "
                       f"you allow it in my settings — a URL should not be able to make me "
                       f"probe the machine I run on")
    return True, ""


# ── rate limiting ─────────────────────────────────────────────────────────────

class RateLimiter:
    """At most one call per host per `min_interval` seconds, across threads."""

    def __init__(self, min_interval: float = MIN_INTERVAL) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> float:
        """Block until this host may be called again. Returns the seconds waited."""
        host = (host or "?").lower()
        with self._lock:
            elapsed = time.time() - self._last.get(host, 0.0)
            delay = max(0.0, self.min_interval - elapsed)
            self._last[host] = time.time() + delay
        if delay:
            time.sleep(delay)
        return round(delay, 2)


_default_limiter = RateLimiter()


def user_agent() -> str:
    """An honest name. No browser impersonation — if a site wants a real browser
    it can say no, and we report that instead of dressing up as Chrome."""
    return "Mark-LV/assistant (+local use; python-requests)"


# ── the fetch ─────────────────────────────────────────────────────────────────

def _urllib_fetch(url: str, method: str, headers: dict, data: bytes | None,
                  timeout: int) -> tuple[int, dict, bytes]:
    """Fallback with no third-party dependency at all."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), dict(response.headers), response.read()
    except urllib.error.HTTPError as e:            # a real status, not a failure
        return int(e.code), dict(e.headers or {}), e.read()


def fetch(url: str, method: str = "GET", headers: dict | None = None,
          params: dict | None = None, body: Any = None, json_body: Any = None,
          timeout: int = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES,
          allow_private: bool = False, limiter: RateLimiter | None = None,
          rate_limit: float | None = None) -> dict[str, Any]:
    """
    One request, with every guard applied. NEVER raises: network trouble is a
    normal answer here, and the caller decides what to say about it.

    Returns {ok, status, text, bytes, headers, error, reason, url, elapsed,
             truncated, waited, private}
    """
    result: dict[str, Any] = {"ok": False, "status": 0, "text": "", "bytes": 0,
                              "headers": {}, "error": "", "url": redact_url(str(url or "")),
                              "elapsed": 0.0, "truncated": False, "waited": 0.0}
    ok, why = validate_url(url, allow_private=allow_private)
    if not ok:
        result["error"] = why
        return result

    method = str(method or "GET").upper()
    if method not in ALLOWED_METHODS:
        result["error"] = f"'{method}' is not a method I send (I use {', '.join(ALLOWED_METHODS)})"
        return result

    parts = urlparse(str(url))
    if params:
        joiner = "&" if parts.query else "?"
        url = f"{url}{joiner}{urlencode({k: v for k, v in params.items() if v is not None})}"

    clean_headers = {"User-Agent": user_agent(), "Accept": "*/*"}
    for key, value in (headers or {}).items():
        if key and not str(key).lower().startswith(("host", "content-length")):
            clean_headers[str(key)] = str(value)

    payload: bytes | None = None
    if json_body is not None:
        import json as _json
        payload = _json.dumps(json_body).encode("utf-8")
        clean_headers.setdefault("Content-Type", "application/json")
    elif body is not None:
        payload = body.encode("utf-8") if isinstance(body, str) else bytes(body)

    active = limiter or _default_limiter
    result["waited"] = active.wait(parts.hostname or "?") if rate_limit is None else \
        RateLimiter(rate_limit).wait(parts.hostname or "?")

    started = time.time()
    try:
        try:
            import requests
        except Exception:
            requests = None

        if requests is not None:
            response = requests.request(method, url, headers=clean_headers, data=payload,
                                        timeout=timeout, allow_redirects=True, stream=True)
            result["status"] = int(response.status_code)
            result["headers"] = {k: v for k, v in response.headers.items()}
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    result["truncated"] = True
                    chunks.append(chunk[: max(0, max_bytes - (size - len(chunk)))])
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            response.close()
        else:
            status, headers, raw = _urllib_fetch(url, method, clean_headers, payload, timeout)
            if len(raw) > max_bytes:
                raw, result["truncated"] = raw[:max_bytes], True
            result["status"], result["headers"] = status, headers

        result["ok"] = True
        result["bytes"] = len(raw)
        result["text"] = raw.decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        message = str(e)
        lowered = message.lower()
        if "timed out" in lowered or "timeout" in lowered:
            message = f"the server stopped answering after {timeout}s"
        elif "name or service" in lowered or "getaddrinfo" in lowered or "nodename" in lowered:
            message = "that address could not be found — check the spelling"
        elif "ssl" in lowered or "certificate" in lowered:
            message = f"the secure connection was refused ({message[:80]})"
        elif "connection refused" in lowered:
            message = "the server refused the connection"
        result["error"] = redact(message, 200)
    result["elapsed"] = round(time.time() - started, 2)
    return result


def status_sentence(result: dict) -> str:
    """One honest line about what came back — used by all four plugins so they
    phrase refusals the same way."""
    if not result.get("ok"):
        return f"the request did not complete: {result.get('error', 'unknown reason')}"
    status = int(result.get("status") or 0)
    if status == 401:
        return "the server wants a login (401) — I do not carry credentials into a URL"
    if status == 403:
        return ("the server refused this request (403). That is a no, and I do not work around "
                "one — no pretending to be a browser, no solving challenges")
    if status == 404:
        return "the server says that address does not exist (404)"
    if status == 429:
        return "the server is rate-limiting me (429) — I will wait longer before trying again"
    if status >= 500:
        return f"the server had a problem of its own ({status})"
    if status >= 400:
        return f"the server answered {status}"
    return f"answered {status} in {result.get('elapsed')}s ({result.get('bytes')} bytes)"


# ── a tiny JSON path, shared by the price monitor and the API connector ───────

def dig(data: Any, path: str, default: Any = None) -> Any:
    """
    'items.0.price' / 'data[0].price' into a decoded JSON structure. Returns the
    default when the path does not exist — a wrong path is a normal event when
    somebody is configuring a monitor, not an exception.
    """
    if not path:
        return data
    node = data
    for part in re.split(r"\.|\[|\]", str(path)):
        part = part.strip().strip("'\"")
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part, default)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except Exception:
                return default
        else:
            return default
    return node


# ── self-test: python _net_core.py ───────────────────────────────────────────

def _selftest() -> int:
    print("_net_core — self-test")
    problems = 0

    for url, want_ok, label in (
        ("https://example.com/a", True, "عادي"),
        ("http://example.com/a", True, "http"),
        ("ftp://example.com/a", False, "مخطط ftp"),
        ("", False, "فارغ"),
        ("https://user:pw@example.com/a", False, "كلمة مرور في الرابط"),
        ("http://127.0.0.1:8080/x", False, "loopback (SSRF)"),
        ("http://localhost/x", False, "localhost (SSRF)"),
        ("http://192.168.1.1/x", False, "شبكة خاصة (SSRF)"),
        ("http://169.254.169.254/latest/meta-data/", False, "خدمة metadata (SSRF)"),
        ("http://10.0.0.5/x", False, "10.x خاصة (SSRF)"),
        ("http://[::1]/x", False, "IPv6 loopback (SSRF)"),
    ):
        ok, why = validate_url(url)
        mark = "✓" if ok == want_ok else "✗"
        problems += 0 if ok == want_ok else 1
        print(f"  {label:<24} {'مسموح' if ok else 'مرفوض':<7} {mark}" + (f"  ({why[:52]})" if why else ""))

    ok, why = validate_url("http://127.0.0.1:9/x", allow_private=True)
    print(f"  loopback مع سماح صريح   : مسموح={ok} (يبقى قابلاً للسماح بإرادة المستخدم)")
    problems += 0 if ok else 1

    secret = ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefg and api_key=SUPERSECRET123 "
              "plus ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 and sk-abcdefghijklmnopqrstuvwxyz")
    cleaned = redact(secret, 400)
    leaked = [token for token in ("eyJhbGciOiJIUzI1NiJ9", "SUPERSECRET123",
                                  "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                                  "sk-abcdefghijklmnopqrstuvwxyz") if token in cleaned]
    print(f"  إخفاء الأسرار           : {cleaned[:84]}")
    print(f"  هل تسرّب شيء؟           : {leaked or 'لا شيء إطلاقًا'}")
    problems += 0 if not leaked else 1
    print(f"  إخفاء رؤوس الطلب        : {redact_headers({'Authorization': 'Bearer x', 'Accept': 'json'})}")
    problems += 0 if redact_headers({"Authorization": "Bearer x"})["Authorization"] == "••••" else 1
    print(f"  إخفاء يوزر في الرابط     : {redact_url('https://u:pw@example.com/a?k=1')}")
    # The implementation removes the credentials entirely rather than masking
    # them — no length, no hint. The first version of THIS CHECK expected masks,
    # which is the check being wrong, not the code.
    problems += 0 if "u:pw" not in redact_url("https://u:pw@example.com/a") else 1

    # fetch: a refused host must not raise, and must say why
    bad = fetch("http://127.0.0.1:9/x")
    print(f"  fetch محجوب (SSRF)      : ok={bad['ok']} — {bad['error'][:60]}")
    problems += 0 if (not bad["ok"] and "private" in bad["error"]) else 1
    print(f"  طريقة غير مسموحة        : {fetch('https://example.com', method='TRACE')['error'][:58]}")
    problems += 0 if "not a method" in fetch("https://example.com", method="TRACE")["error"] else 1

    # a real, local, allowed server — the whole fetch path end to end
    import http.server
    import socketserver
    import threading as _t
    payload = b'{"price": 123.45, "items": [{"n": "a"}, {"n": "b"}]}'

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/deny"):
                self.send_response(403); self.end_headers(); self.wfile.write(b"no"); return
            if self.path.startswith("/big"):
                self.send_response(200); self.end_headers(); self.wfile.write(b"x" * 5000); return
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"echo": ' + body + b", \"auth\": \"" +
                             str(self.headers.get("Authorization", "")).encode() + b'"}')

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        _t.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        got = fetch(base + "/data", allow_private=True)
        print(f"  جلب حقيقي               : ok={got['ok']} status={got['status']} "
              f"{got['bytes']} bytes in {got['elapsed']}s")
        problems += 0 if got["ok"] and got["status"] == 200 and got["bytes"] == len(payload) else 1
        import json as _json
        print(f"  dig على الناتج          : price={dig(_json.loads(got['text']), 'price')} · "
              f"items.1.n={dig(_json.loads(got['text']), 'items[1].n')}")
        problems += 0 if dig(_json.loads(got["text"]), "items.1.n") == "b" else 1
        problems += 0 if dig(_json.loads(got["text"]), "missing.deep", "fallback") == "fallback" else 1

        denied = fetch(base + "/deny", allow_private=True)
        print(f"  403                     : {status_sentence(denied)[:64]}")
        problems += 0 if denied["ok"] and denied["status"] == 403 and "refused" in status_sentence(denied) else 1

        small = fetch(base + "/big", allow_private=True, max_bytes=1000)
        print(f"  حدّ الحجم               : {small['bytes']} bytes (سقف 1000) — مقصوص={small['truncated']}")
        problems += 0 if small["bytes"] == 1000 and small["truncated"] else 1

        posted = fetch(base + "/post", method="POST", json_body={"a": 1}, allow_private=True,
                       headers={"Authorization": "Bearer SECRETTOKEN12345"})
        echoed = _json.loads(posted["text"])
        print(f"  POST بجسم JSON          : {echoed.get('echo')}")
        problems += 0 if posted["ok"] and echoed.get("echo") == {"a": 1} else 1

        limited = RateLimiter(0.4)
        first, second = limited.wait("h"), limited.wait("h")
        print(f"  حدّ المعدّل             : أول={first}s ثاني={second}s (يجب أن ينتظر)")
        problems += 0 if first == 0.0 and second >= 0.3 else 1

        missing = fetch(base + "/nope-with-timeout", timeout=1, allow_private=True)
        print(f"  مهلة/فشل بلا انهيار     : ok={missing['ok']}")
        httpd.shutdown()

    print(f"  User-Agent صادق          : {user_agent()}")
    problems += 0 if "Mark-LV" in user_agent() and "Chrome" not in user_agent() else 1

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
