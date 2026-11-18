import base64
import collections
import http.client
import ipaddress
import json
import logging
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from werkzeug.wrappers import Request, Response

from .apppasswords import AppPasswords

PREFIX = "/dav"
WELL_KNOWN = "/.well-known/caldav"
REALM = "Calendar"
ALLOWED_METHODS = ("OPTIONS", "GET", "HEAD", "PUT", "DELETE", "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "MKCALENDAR", "MOVE")
FORWARDED_HEADERS = ("Content-Type", "Depth", "If-Match", "If-None-Match", "Overwrite", "Prefer", "Brief", "Accept", "User-Agent")
DROPPED_ANSWER_HEADERS = frozenset(name.lower() for name in (
    "Connection", "Keep-Alive", "Transfer-Encoding", "Proxy-Authenticate", "Proxy-Authorization", "TE", "Trailer",
    "Upgrade", "Set-Cookie", "WWW-Authenticate", "Content-Length", "Server", "Date"))
ENCODED_TRICKS = ("%2f", "%5c", "%00", "%2e")
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_STORE_ANSWER_BYTES = 64 * 1024 * 1024
STORE_TIMEOUT_SECONDS = 60
FAILURE_WINDOW_SECONDS = 15 * 60
FAILURES_PER_ADDRESS = 5
FAILURES_BEFORE_STRICT = 100
STRICT_FAILURES_PER_ADDRESS = 1
MAX_TRACKED_ADDRESSES = 10000
MAX_CF_VISITOR_LENGTH = 200
IPV6_NETWORK_BITS = 64
SWITCHED_OFF_TEXT = "CalDAV sync is switched off on this server.\n"
NOT_HTTPS_TEXT = "CalDAV only works over https through Cloudflare.\n"
TOO_MANY_TEXT = "Too many wrong passwords from this address; wait and try again.\n"
SIGN_IN_TEXT = "Sign in with your user name and an app password made in the calendar's Settings.\n"

log = logging.getLogger("calendar.gate")


class FailureLimiter:
    def __init__(self, clock=time.monotonic, window: float = FAILURE_WINDOW_SECONDS, per_address: int = FAILURES_PER_ADDRESS,
                 before_strict: int = FAILURES_BEFORE_STRICT):
        self.clock = clock
        self.window = window
        self.per_address = per_address
        self.before_strict = before_strict
        self.lock = threading.Lock()
        self.by_address: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
        self.everyone: collections.deque = collections.deque()

    def _prune(self) -> None:
        cutoff = self.clock() - self.window
        while self.everyone and self.everyone[0] < cutoff:
            self.everyone.popleft()
        for address in list(self.by_address):
            moments = self.by_address[address]
            while moments and moments[0] < cutoff:
                moments.popleft()
            if not moments:
                del self.by_address[address]

    def _limit(self) -> int:
        return STRICT_FAILURES_PER_ADDRESS if len(self.everyone) >= self.before_strict else self.per_address

    def seconds_blocked(self, address: str) -> int:
        with self.lock:
            self._prune()
            moments = self.by_address.get(address)
            if not moments or len(moments) < self._limit():
                return 0
            return max(1, int(moments[0] + self.window - self.clock()) + 1)

    def failed(self, address: str) -> None:
        with self.lock:
            now = self.clock()
            self.everyone.append(now)
            moments = self.by_address.pop(address, None) or collections.deque()
            moments.append(now)
            self.by_address[address] = moments
            while len(self.by_address) > MAX_TRACKED_ADDRESSES:
                self.by_address.popitem(last=False)


def _plain(text: str, status: int, headers: dict | None = None) -> Response:
    response = Response(text, status=status, mimetype="text/plain")
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def _challenge() -> Response:
    return _plain(SIGN_IN_TEXT, 401, {"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})


def came_over_https(request: Request) -> bool:
    raw = request.headers.get("Cf-Visitor", "")
    if len(raw) > MAX_CF_VISITOR_LENGTH:
        return False
    try:
        visitor = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return isinstance(visitor, dict) and visitor.get("scheme") == "https"


def failure_bucket(address: str) -> str:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 4:
        return str(parsed)
    if parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(ipaddress.ip_network(f"{parsed}/{IPV6_NETWORK_BITS}", strict=False))


def is_tricky_path(path: str, raw_uri: str) -> bool:
    if "\\" in path or "\x00" in path or "//" in path:
        return True
    if any(segment in (".", "..") for segment in path.split("/")):
        return True
    raw_path = raw_uri.split("?", 1)[0].lower()
    return any(trick in raw_path for trick in ENCODED_TRICKS)


def is_under_prefix(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def is_own_path(inner: str, user: str) -> bool:
    return inner in ("", "/") or inner == f"/{user}" or inner.startswith(f"/{user}/")


class CalDavGate:
    def __init__(self, passwords: AppPasswords, user: str, store_port: int, store_password: str, enabled: bool,
                 require_cloudflare_https: bool, trusted_proxies=("127.0.0.1", "::1"), limiter: FailureLimiter | None = None,
                 store_host: str = "127.0.0.1"):
        self.passwords = passwords
        self.user = user
        self.store_host = store_host
        self.store_port = store_port
        self.store_authorization = "Basic " + _basic(user, store_password)
        self.enabled = enabled
        self.require_cloudflare_https = require_cloudflare_https
        self.trusted = tuple(ipaddress.ip_network(entry, strict=False) for entry in trusted_proxies)
        self.limiter = limiter or FailureLimiter()

    def client_address(self, request: Request) -> str:
        remote = request.remote_addr or ""
        try:
            is_trusted = any(ipaddress.ip_address(remote) in network for network in self.trusted)
        except ValueError:
            is_trusted = False
        forwarded = (request.headers.get("Cf-Connecting-Ip") or "").strip()
        if is_trusted and forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                return remote
        return remote

    def __call__(self, environ, start_response):
        request = Request(environ)
        response = self.answer(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response(environ, start_response)

    def answer(self, request: Request) -> Response:
        if not self.enabled:
            return _plain(SWITCHED_OFF_TEXT, 404)
        if self.require_cloudflare_https and not came_over_https(request):
            return _plain(NOT_HTTPS_TEXT, 403)
        path = request.path
        if path.rstrip("/") == WELL_KNOWN:
            return _plain("", 301, {"Location": PREFIX + "/"})
        if not is_under_prefix(path) or is_tricky_path(path, request.environ.get("REQUEST_URI", path)):
            return _plain("Not found.\n", 404)
        address = self.client_address(request)
        bucket = failure_bucket(address)
        blocked_for = self.limiter.seconds_blocked(bucket)
        if blocked_for:
            return _plain(TOO_MANY_TEXT, 429, {"Retry-After": str(blocked_for)})
        credentials = request.authorization
        if credentials is None or credentials.type != "basic":
            return _challenge()
        if credentials.username != self.user or not self.passwords.check(credentials.password or "", address):
            self.limiter.failed(bucket)
            log.warning("CalDAV sign-in refused for user %r from %s", (credentials.username or "")[:40], address)
            return _challenge()
        inner = path[len(PREFIX):]
        if not is_own_path(inner, self.user):
            return _plain("That is not your calendar.\n", 403)
        if request.method not in ALLOWED_METHODS:
            return _plain("That method is not allowed here.\n", 405, {"Allow": ", ".join(ALLOWED_METHODS)})
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return _plain("That request is too large.\n", 413)
        headers = self._forwarded_headers(request)
        if headers is None:
            return _plain("That destination is not one of your calendars.\n", 403)
        return self._forward(request, inner or "/", headers)

    def _forwarded_headers(self, request: Request) -> dict | None:
        headers = {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers}
        headers["Authorization"] = self.store_authorization
        headers["X-Script-Name"] = PREFIX
        destination = request.headers.get("Destination")
        if destination is not None:
            parts = urlsplit(destination)
            target = unquote(parts.path)
            if (parts.netloc and parts.netloc != request.host) or not is_under_prefix(target) \
                    or is_tricky_path(target, parts.path) or not is_own_path(target[len(PREFIX):], self.user):
                return None
            headers["Destination"] = parts.path
        return headers

    def _forward(self, request: Request, inner: str, headers: dict) -> Response:
        body = request.get_data(cache=False) if request.method not in ("GET", "HEAD", "OPTIONS", "DELETE") else None
        if body is not None and len(body) > MAX_BODY_BYTES:
            return _plain("That request is too large.\n", 413)
        query = request.environ.get("QUERY_STRING", "")
        raw_path = request.environ.get("REQUEST_URI", "").split("?", 1)[0]
        target = raw_path[len(PREFIX):] if is_under_prefix(raw_path) else quote(inner, safe="/@:+,;=")
        target = target or "/"
        connection = http.client.HTTPConnection(self.store_host, self.store_port, timeout=STORE_TIMEOUT_SECONDS)
        try:
            connection.request(request.method, target + (f"?{query}" if query else ""), body=body, headers=headers)
            answer = connection.getresponse()
            data = answer.read(MAX_STORE_ANSWER_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            log.error("the calendar store did not answer a CalDAV request: %s", error.__class__.__name__)
            return _plain("The calendar store is not answering.\n", 503)
        finally:
            connection.close()
        if len(data) > MAX_STORE_ANSWER_BYTES or answer.status == 401:
            log.error("the calendar store answered %s with %d bytes", answer.status, len(data))
            return _plain("The calendar store gave an answer this server will not pass on.\n", 502)
        response = Response(data, status=answer.status)
        response.headers.clear()
        for name, value in answer.getheaders():
            if name.lower() not in DROPPED_ANSWER_HEADERS:
                response.headers.add(name, value)
        return response


def _basic(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()
