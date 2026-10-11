"""Calvoun Free LLM Hub -- show a preview inside the Build page despite its
framing headers.

WHY THIS EXISTS (2026-10-11)
----------------------------
The Build page frames the running preview (http://127.0.0.1:5800) from the
dashboard (http://127.0.0.1:8787). Another port is another ORIGIN, so an app
that sends `X-Frame-Options: SAMEORIGIN` or a CSP `frame-ancestors 'self'` --
helmet does both by default, and many generated Express apps use helmet -- is
refused by the browser ("127.0.0.1 refused to connect.") while it works fine in
its own tab. MEASURED live on the owner's app.

WHAT IT DOES
------------
A tiny HTTP relay on 127.0.0.1 (a free port, never the hub's) in front of ONE
preview port. Every request goes to the real port unchanged (Host included:
the app sees the address the browser used, so the absolute links it builds
point back here). Every response comes back unchanged EXCEPT:

  * `X-Frame-Options` is removed;
  * only the `frame-ancestors` directive is removed from
    `Content-Security-Policy` and `Content-Security-Policy-Report-Only`
    (every other directive is kept byte for byte; a policy left empty is
    dropped);
  * an absolute `Location` that points at the real port is pointed at the
    proxy instead (a relative one is left alone);
  * ONE extra header `Content-Security-Policy: frame-ancestors 'self'
    http://127.0.0.1:<hub> http://localhost:<hub>` is added to every final
    response, so the proxy's address can be framed by the hub's dashboard and
    by the app itself -- not by any other website the user has open (the app
    asked not to be framed; the exception is the hub only).

Bodies are streamed, never buffered or edited: Content-Length, chunked
(relayed as chunks, so a streamed answer stays streamed) and read-until-close
all pass through, keep-alive included. An `Upgrade` request (a websocket, e.g.
Vite / Next HMR) is tunnelled raw both ways after the 101.

stdlib only (socket + threading). Never raises out of its public functions
except FrameProxy.start(), whose OSError the caller turns into a plain message.
"""
import http.client
import re
import socket
import threading

BIND_HOST = "127.0.0.1"
HEAD_MAX = 64 * 1024          # request/response head size limit
MAX_CONNS = 128               # concurrent client connections per proxy
CONNECT_TIMEOUT = 5.0         # reaching the app on loopback
HEAD_TIMEOUT = 300.0          # waiting for the app's answer (a dev server's
                              # first compile can take a while)
BODY_TIMEOUT = 900.0          # silence inside a streamed body
IDLE_TIMEOUT = 120.0          # a keep-alive client that sends nothing more
_BIND_TRIES = 8

_FRAME_HEADERS = ("content-security-policy", "content-security-policy-report-only")


class _ProtocolError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Header helpers (pure)
# --------------------------------------------------------------------------- #

def _directive_name(segment):
    parts = segment.strip().split(None, 1)
    return parts[0].lower() if parts else ""


def strip_frame_ancestors(value):
    """`value` of a CSP header without its `frame-ancestors` directive(s).
    Every other directive keeps its exact text. Returns None when nothing is
    left (the header should then be dropped); the SAME string when there was
    no frame-ancestors to remove."""
    value = value or ""
    changed = False
    kept_policies = []
    for policy in value.split(","):
        segs = policy.split(";")
        kept = [s for s in segs if _directive_name(s) != "frame-ancestors"]
        if len(kept) != len(segs):
            changed = True
        if any(s.strip() for s in kept):
            kept_policies.append(";".join(kept))
    if not changed:
        return value
    if not kept_policies:
        return None
    return ",".join(kept_policies).strip()


def _source_allows(token, scheme, host, port):
    """Does one frame-ancestors source expression match http://host:port?"""
    low = token.strip().lower()
    if not low or low in ("'none'", "'self'"):
        return False          # 'self' is the APP's origin, another port
    if low == "*":
        return True
    if re.fullmatch(r"[a-z][a-z0-9+.\-]*:", low):
        # A scheme-source: "http:" matches http (and https); "https:" does not
        # match an http ancestor.
        return low == scheme + ":"
    m = re.fullmatch(r"(?:([a-z][a-z0-9+.\-]*)://)?(\*|\*\.[^:/\s]+|[^:/\s]+)"
                     r"(?::(\*|\d+))?(?:/\S*)?", low)
    if not m:
        return False
    s, h, p = m.groups()
    if s and s != scheme:
        return False
    if h == "*":
        pass
    elif h.startswith("*."):
        if not host.endswith(h[1:]):
            return False
    elif h != host:
        return False
    if p is None:
        return port == 80 and scheme == "http"
    return p == "*" or int(p) == port


def _frame_ancestors_allows(sources, origins):
    for scheme, host, port in origins:
        if not any(_source_allows(t, scheme, host, port) for t in sources):
            return False
    return True


def hub_origins(hub_port):
    """The origins the dashboard is opened on."""
    if not hub_port:
        return []
    return [("http", "127.0.0.1", int(hub_port)), ("http", "localhost", int(hub_port))]


def framing_block(headers, hub_port):
    """Why a browser would refuse to show a response with these headers inside
    the hub's dashboard, or None when it would show it. `headers` is a list of
    (name, value) pairs (duplicates kept). Report-Only policies never block."""
    origins = hub_origins(hub_port) or [("http", "127.0.0.1", 8787)]
    saw_fa = False
    for name, value in headers or ():
        if (name or "").strip().lower() != "content-security-policy":
            continue
        for policy in (value or "").split(","):
            for seg in policy.split(";"):
                if _directive_name(seg) != "frame-ancestors":
                    continue
                saw_fa = True
                sources = seg.strip().split()[1:]
                if not _frame_ancestors_allows(sources, origins):
                    return "Content-Security-Policy frame-ancestors"
                break         # only the first one in a policy counts
    if saw_fa:
        return None           # frame-ancestors present: X-Frame-Options is ignored
    for name, value in headers or ():
        if (name or "").strip().lower() != "x-frame-options":
            continue
        toks = [t.strip().lower() for t in (value or "").split(",")]
        if any(t in ("deny", "sameorigin") for t in toks):
            return "X-Frame-Options"
    return None


def fetch_headers(port, timeout=3.0, path="/"):
    """GET `path` on 127.0.0.1:port the way the iframe would, without
    following a redirect (a 3xx is an answer). Returns the response headers as
    a list of (name, value), or None when nothing answered."""
    conn = None
    try:
        conn = http.client.HTTPConnection(BIND_HOST, int(port), timeout=timeout)
        conn.request("GET", path, headers={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate",
            "Connection": "close"})
        resp = conn.getresponse()
        headers = list(resp.getheaders())
        resp.close()
        return headers
    except Exception:                                            # noqa: BLE001
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                                    # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

class _Reader:
    """Buffered reads over a socket; what is read past a head stays buffered."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.eof = False

    def _fill(self):
        if self.eof:
            return False
        data = self.sock.recv(65536)
        if not data:
            self.eof = True
            return False
        self.buf += data
        return True

    def read_head(self, skip_blank=False):
        """The bytes up to and including the blank line ending a head, or None
        at a clean EOF before any byte."""
        while True:
            if skip_blank:
                while self.buf[:2] == b"\r\n" or self.buf[:1] == b"\n":
                    del self.buf[:2 if self.buf[:2] == b"\r\n" else 1]
            ends = [i + n for i, n in ((self.buf.find(b"\r\n\r\n"), 4),
                                       (self.buf.find(b"\n\n"), 2)) if i >= 0]
            if ends:
                end = min(ends)
                head = bytes(self.buf[:end])
                del self.buf[:end]
                return head
            if len(self.buf) > HEAD_MAX:
                raise _ProtocolError("head too large")
            if not self._fill():
                if not self.buf:
                    return None
                raise _ProtocolError("closed in the middle of a head")

    def read_line(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = bytes(self.buf[:i + 1])
                del self.buf[:i + 1]
                return line
            if len(self.buf) > HEAD_MAX:
                raise _ProtocolError("line too long")
            if not self._fill():
                raise _ProtocolError("closed in the middle of a line")

    def read_some(self, n=65536):
        if self.buf:
            data = bytes(self.buf[:n])
            del self.buf[:n]
            return data
        if self.eof:
            return b""
        data = self.sock.recv(n)
        if not data:
            self.eof = True
        return data

    def take(self):
        data = bytes(self.buf)
        self.buf.clear()
        return data


def _parse_head(head):
    """(start line, [(name, value, raw)]) -- raw keeps an untouched header's
    exact text (continuation lines included)."""
    text = head.decode("latin-1")
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise _ProtocolError("empty head")
    entries = []
    for ln in lines[1:]:
        if ln[:1] in (" ", "\t") and entries:
            name, value, raw = entries[-1]
            entries[-1] = (name, value + " " + ln.strip(), raw + "\r\n" + ln)
            continue
        name, sep, value = ln.partition(":")
        if not sep:
            continue
        entries.append((name.strip(), value.strip(), ln))
    return lines[0], entries


def _values(entries, name):
    name = name.lower()
    return [v for n, v, _r in entries if n.lower() == name]


def _tokens(entries, name):
    out = []
    for v in _values(entries, name):
        out += [t.strip().lower() for t in v.split(",") if t.strip()]
    return out


def _body_framing(entries):
    """("none"|"chunked"|"close"|"length", n) from the headers alone."""
    te = _tokens(entries, "transfer-encoding")
    if te:
        if te[-1] == "chunked":
            return ("chunked", 0)
        return ("close", 0)
    lens = _values(entries, "content-length")
    if lens:
        vals = {v.strip() for v in ",".join(lens).split(",")}
        if len(vals) != 1:
            raise _ProtocolError("conflicting Content-Length")
        n = int(vals.pop())
        if n < 0:
            raise _ProtocolError("negative Content-Length")
        return ("length", n)
    return ("none", 0)


def _relay_exact(src, dst, n):
    while n > 0:
        data = src.read_some(min(n, 65536))
        if not data:
            raise _ProtocolError("body ended early")
        dst.sendall(data)
        n -= len(data)


def _relay_chunked(src, dst):
    while True:
        line = src.read_line()
        dst.sendall(line)
        size = int(line.split(b";", 1)[0].strip() or b"x", 16)
        if size == 0:
            while True:
                trailer = src.read_line()
                dst.sendall(trailer)
                if trailer in (b"\r\n", b"\n"):
                    return
        _relay_exact(src, dst, size)
        dst.sendall(src.read_line())          # the CRLF after the chunk data


def _relay_until_close(src, dst):
    while True:
        data = src.read_some()
        if not data:
            return
        dst.sendall(data)


def _relay_body(src, dst, framing):
    kind, n = framing
    if kind == "length":
        _relay_exact(src, dst, n)
    elif kind == "chunked":
        _relay_chunked(src, dst)
    elif kind == "close":
        _relay_until_close(src, dst)


def _simple_reply(sock, code, reason, text):
    body = text.encode("utf-8")
    head = ("HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n" % (code, reason, len(body)))
    try:
        sock.sendall(head.encode("latin-1") + body)
    except OSError:
        pass


def _close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# The proxy
# --------------------------------------------------------------------------- #

class FrameProxy:
    """One relay in front of one preview port. start() binds and returns;
    stop() closes the listener and every open connection (idempotent)."""

    def __init__(self, target_port, hub_port=None, avoid=None, host=BIND_HOST):
        self.target_port = int(target_port)
        self.hub_port = int(hub_port) if hub_port else None
        self.host = host
        self._avoid = avoid
        self.port = None
        self._sock = None
        self._stopping = False
        self._lock = threading.Lock()
        self._socks = set()
        self._active = 0
        self._thread = None
        self._loc_re = re.compile(
            r"^(\s*[A-Za-z][A-Za-z0-9+.\-]*://)(?:127\.0\.0\.1|localhost|\[::1\]):%d"
            r"(?=[/?#]|$)" % self.target_port, re.I)

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port) if self.port else None

    @property
    def running(self):
        return self._sock is not None and not self._stopping

    def _avoided(self, port):
        if port == self.target_port or (self.hub_port and port == self.hub_port):
            return True
        try:
            return bool(self._avoid(port)) if self._avoid else False
        except Exception:                                        # noqa: BLE001
            return False

    def start(self):
        last = None
        for _ in range(_BIND_TRIES):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((self.host, 0))
                port = s.getsockname()[1]
                if self._avoided(port):
                    s.close()
                    continue
                s.listen(64)
                s.settimeout(0.5)
            except OSError as exc:
                last = exc
                s.close()
                continue
            self._sock, self.port = s, port
            break
        if self._sock is None:
            raise last or OSError("no free port for the frame proxy")
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="frame-proxy-%d" % self.port)
        self._thread.start()
        return self

    def stop(self):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            socks = list(self._socks)
            self._socks.clear()
            listener = self._sock
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for s in socks:
            _close(s)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    # -- bookkeeping ---------------------------------------------------------
    def _track(self, s):
        with self._lock:
            if self._stopping:
                return False
            self._socks.add(s)
            return True

    def _untrack(self, s):
        with self._lock:
            self._socks.discard(s)
        _close(s)

    def _accept_loop(self):
        while not self._stopping:
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                full = self._active >= MAX_CONNS
                if not full and not self._stopping:
                    self._active += 1
                    self._socks.add(client)
                    ok = True
                else:
                    ok = False
            if not ok:
                if full:
                    _simple_reply(client, 503, "Service Unavailable",
                                  "Too many connections to the preview.")
                _close(client)
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True,
                             name="frame-proxy-conn").start()

    def _serve(self, client):
        reader = _Reader(client)
        try:
            while not self._stopping:
                client.settimeout(IDLE_TIMEOUT)
                head = reader.read_head(skip_blank=True)
                if head is None:
                    return
                client.settimeout(BODY_TIMEOUT)
                if not self._exchange(client, reader, head):
                    return
        except (OSError, _ProtocolError, ValueError):
            return
        finally:
            with self._lock:
                self._active -= 1
            self._untrack(client)

    # -- one request/response ------------------------------------------------
    def _exchange(self, client, reader, head):
        start, entries = _parse_head(head)
        parts = start.split()
        if len(parts) != 3 or not parts[2].upper().startswith("HTTP/"):
            _simple_reply(client, 400, "Bad Request", "Bad request.")
            return False
        method, version = parts[0].upper(), parts[2].upper()
        try:
            req_framing = _body_framing(entries)
        except (_ProtocolError, ValueError):
            _simple_reply(client, 400, "Bad Request", "Bad request.")
            return False
        if req_framing[0] == "close":
            _simple_reply(client, 400, "Bad Request", "Unsupported request body.")
            return False
        conn_toks = _tokens(entries, "connection")
        upgrade = bool(_values(entries, "upgrade")) and "upgrade" in conn_toks
        try:
            up = socket.create_connection((BIND_HOST, self.target_port),
                                          timeout=CONNECT_TIMEOUT)
        except OSError:
            _simple_reply(client, 502, "Bad Gateway",
                          "The app on port %d is not answering." % self.target_port)
            return False
        if not self._track(up):
            _close(up)
            return False
        tunnelled = False
        try:
            up.settimeout(HEAD_TIMEOUT)
            up.sendall(head)
            body_thread = None
            if req_framing[0] != "none":
                if "100-continue" in _tokens(entries, "expect"):
                    # The client waits for "100 Continue" before it sends the
                    # body: relay it alongside reading the answer.
                    body_thread = threading.Thread(
                        target=self._relay_quiet, args=(reader, up, req_framing),
                        daemon=True)
                    body_thread.start()
                else:
                    _relay_body(reader, up, req_framing)
            upr = _Reader(up)
            sent_any = False
            while True:
                try:
                    rhead = upr.read_head()
                except (_ProtocolError, OSError):
                    rhead = None
                if rhead is None:
                    if not sent_any:
                        _simple_reply(client, 502, "Bad Gateway",
                                      "The app closed the connection without answering.")
                    return False
                rstart, rentries = _parse_head(rhead)
                rparts = rstart.split(None, 2)
                status = int(rparts[1]) if len(rparts) > 1 and rparts[1].isdigit() else 0
                if 100 <= status < 200 and status != 101:
                    client.sendall(rhead)            # interim (100 Continue...)
                    sent_any = True
                    continue
                break
            client.sendall(self.rewrite_head(rstart, rentries, final=status != 101))
            if status == 101:
                if upgrade:
                    tunnelled = True
                    self._tunnel(client, reader, up, upr)
                return False
            up.settimeout(BODY_TIMEOUT)
            if method == "HEAD" or status in (204, 304):
                framing = ("none", 0)
            else:
                framing = _body_framing(rentries)
                if framing[0] == "none":
                    framing = ("close", 0)       # no length, not chunked: until close
            _relay_body(upr, client, framing)
            if body_thread is not None:
                body_thread.join(timeout=5.0)
            if framing[0] == "close":
                return False
            rtoks = _tokens(rentries, "connection")
            if "close" in rtoks or "close" in conn_toks:
                return False
            if rstart.upper().startswith("HTTP/1.0") and "keep-alive" not in rtoks:
                return False
            if version == "HTTP/1.0" and "keep-alive" not in conn_toks:
                return False
            return True
        finally:
            if not tunnelled:
                self._untrack(up)

    def _relay_quiet(self, reader, up, framing):
        try:
            _relay_body(reader, up, framing)
        except (OSError, _ProtocolError, ValueError):
            pass

    def rewrite_head(self, start, entries, final=True):
        out = [start]
        for name, value, raw in entries:
            low = name.lower()
            if low == "x-frame-options":
                continue
            if low in _FRAME_HEADERS:
                new = strip_frame_ancestors(value)
                if new is None:
                    continue
                if new != value:
                    out.append("%s: %s" % (name, new))
                    continue
            elif low == "location":
                new = self._loc_re.sub(
                    lambda m: m.group(1) + "127.0.0.1:%d" % self.port, value, count=1)
                if new != value:
                    out.append("%s: %s" % (name, new))
                    continue
            out.append(raw)
        if final and self.hub_port:
            out.append("Content-Security-Policy: frame-ancestors 'self' "
                       "http://127.0.0.1:%d http://localhost:%d"
                       % (self.hub_port, self.hub_port))
        return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")

    def _tunnel(self, client, creader, up, upr):
        """Raw bytes both ways until either side closes."""
        client.settimeout(None)
        up.settimeout(None)
        pending = creader.take()
        if pending:
            up.sendall(pending)
        pending = upr.take()
        if pending:
            client.sendall(pending)

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(up, client), daemon=True,
                             name="frame-proxy-tunnel")
        t.start()
        try:
            pump(client, up)
            t.join(timeout=5.0)
        finally:
            self._untrack(up)
