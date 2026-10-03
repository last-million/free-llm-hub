"""Notice a client that left while the hub was still WAITING, and stop its work.

MEASURED 2026-10-03 on the live hub: `curl -N -m 3` against a streaming
/v1/chat/completions (coding-max) left after 3 s, and the hub's activity row
stayed "in_progress" for 58+ s while the chain walked g4f relay hops for a
client that was gone. werkzeug only learns that a client left when it WRITES
to the socket, and while the hub waits (the first-content peek before a stream
is committed, the chain walk, buffered tool turns, the swarm fan-out, the
pipelines, the stream gate's hold) nothing is written. So nothing stopped:
providers kept generating, quota was spent, Activity showed work running.

Three pieces, all fail-open (any error here = the old behaviour):

* MONITOR -- one shared daemon thread polls every registered client socket
  every POLL_SECONDS with a zero-timeout select(); a socket that reads ready
  is peeked with recv(1, MSG_PEEK). b"" (FIN) or a reset/abort is a client
  that left. Nothing is consumed: the request body has been read in full
  before a socket is registered (app.py), MSG_PEEK leaves bytes where they
  are, and a socket with bytes pending (a pipelined request) is "alive".
  An idle live client is simply not readable. Windows: select() takes
  sockets (<= 512 per call, chunked), MSG_PEEK works, a killed client
  process reads as ConnectionResetError (MEASURED here).
* TOKEN -- one per request, carried in a contextvar (so worker threads that
  run in a copy of the request's context see it). `cancel()` sets the flag,
  runs the registered hooks (the activity row, a subprocess kill) and shuts
  down every upstream connection the request opened.
* URLLIB3 HOOK -- every upstream HTTP call made under a token is tracked
  (HTTPConnectionPool._make_request), and a call that would START after the
  cancel raises ClientGone instead. Cancelling shuts the tracked sockets down
  with the base socket.shutdown(SHUT_RDWR): a read blocked on them returns at
  once (MEASURED on Windows for timeout sockets -- every upstream socket here
  has one), and the upstream sees the connection close. Never resp.close()
  from the monitor: MEASURED, a cross-thread close blocks on the reader's
  buffer lock until the read itself returns.
"""

import contextvars
import logging
import select
import socket
import ssl
import threading
import time
import weakref

_log = logging.getLogger("free-llm-hub")

# How often the monitor looks at the registered sockets. The cost is one
# zero-timeout select() per POLL_SECONDS, plus one peek per READABLE socket.
POLL_SECONDS = 0.5
# Windows' select() handles at most 512 sockets per call (FD_SETSIZE).
SELECT_CHUNK = 500

_CURRENT = contextvars.ContextVar("free_llm_hub_cancel_token", default=None)


class ClientGone(RuntimeError):
    """The client that asked for this work disconnected: no new work for it.

    A RuntimeError on purpose: the chain loops already treat a RuntimeError
    from a hop as "this hop is over, walk on" -- and the walk then stops."""


def current():
    """The cancel token of the request this code runs for, or None."""
    try:
        return _CURRENT.get()
    except Exception:                                            # noqa: BLE001
        return None


def set_current(token):
    """Make `token` (or None) the current request's token in THIS context."""
    _CURRENT.set(token)


def cancelled():
    """True when the current request's client has left."""
    tok = current()
    return tok is not None and tok.cancelled


def bind(fn):
    """`fn` wrapped to run under the CALLER's token -- for a plain
    threading.Thread, which starts with an empty context. No token: `fn`."""
    tok = current()
    if tok is None:
        return fn

    def _bound(*a, **kw):
        _CURRENT.set(tok)
        return fn(*a, **kw)
    return _bound


def join(thread, timeout):
    """thread.join(timeout), returning early once the current request's client
    has left. Returns True when it returned early for that reason."""
    tok = current()
    if tok is None:
        thread.join(timeout)
        return False
    end = time.monotonic() + max(0.0, float(timeout or 0))
    while thread.is_alive():
        if tok.cancelled:
            return True
        rest = end - time.monotonic()
        if rest <= 0:
            return False
        thread.join(min(rest, 0.25))
    return False


def _raw_socket(conn):
    sock = getattr(conn, "sock", None)
    if sock is None:
        return None
    if isinstance(sock, socket.socket):
        return sock
    return getattr(sock, "socket", None)       # urllib3's SSLTransport


def _shutdown_conn(conn, token):
    """Shut `conn`'s socket down if `token` still owns it. Never raises.

    The socket is the one remembered at connect time: once a response says
    "Connection: close", http.client hands the socket to the response and
    sets conn.sock to None (MEASURED: a streamed reply was then uncuttable).
    A socket whose response is fully closed has fileno() -1 and is skipped."""
    try:
        if getattr(conn, "_hub_cancel_owner", None) is not token:
            return False
        raw = _raw_socket(conn) or getattr(conn, "_hub_raw_sock", None)
        if raw is None or raw.fileno() < 0:
            return False
        # The BASE shutdown: SSLSocket.shutdown would also drop its SSL object
        # under a reader that is still inside it.
        socket.socket.shutdown(raw, socket.SHUT_RDWR)
        return True
    except Exception:                                            # noqa: BLE001
        return False


class Token:
    """One request's cancel flag. Thread-safe; cancel() runs once."""

    def __init__(self, label=""):
        self.label = str(label or "")
        self.started = time.monotonic()
        self.reason = None
        self.cancelled_at = None         # time.time() of the cancel
        self.after = None                # seconds from the request's start
        self.closed_upstream = 0         # upstream connections shut down
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._conns = weakref.WeakSet()
        self._hooks = {}
        self._seq = 0

    @property
    def cancelled(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def add_hook(self, fn):
        """Run `fn()` on cancel (at once when already cancelled). Returns a
        key for remove_hook."""
        with self._lock:
            if not self._event.is_set():
                self._seq += 1
                self._hooks[self._seq] = fn
                return self._seq
        try:
            fn()
        except Exception:                                        # noqa: BLE001
            pass
        return None

    def remove_hook(self, key):
        if key is None:
            return
        with self._lock:
            self._hooks.pop(key, None)

    def track_conn(self, conn):
        """An upstream connection this request is about to use."""
        try:
            conn._hub_cancel_owner = self
            with self._lock:
                self._conns.add(conn)
        except Exception:                                        # noqa: BLE001
            pass

    def cancel(self, reason="client disconnected"):
        """Flag set FIRST (so whatever a shutdown makes fail already knows it
        was a cancel, not a provider fault), then the hooks, then every
        upstream connection is shut down. True on the first call only."""
        with self._lock:
            if self._event.is_set():
                return False
            self.reason = str(reason or "client disconnected")
            self.cancelled_at = time.time()
            self.after = time.monotonic() - self.started
            self._event.set()
            hooks = list(self._hooks.values())
            self._hooks.clear()
            conns = list(self._conns)
        for fn in hooks:
            try:
                fn()
            except Exception:                                    # noqa: BLE001
                pass
        n = 0
        for conn in conns:
            if _shutdown_conn(conn, self):
                n += 1
        self.closed_upstream = n
        try:
            _log.warning("[cancel] %s after %.1fs: %s -- stopped its work "
                         "(%d upstream call(s) cut)", self.label or "request",
                         self.after, self.reason, n)
        except Exception:                                        # noqa: BLE001
            pass
        return True


def peer_closed(sock):
    """True when a READ-READY client socket says its peer is gone (FIN or
    reset). Bytes pending = alive. Never consumes anything."""
    try:
        data = sock.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, InterruptedError, socket.timeout):
        return False
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return True
    except OSError:
        return False             # closed under us: the request is over, not cancelled
    return data == b""


class Monitor:
    """The one thread that watches every registered client socket."""

    def __init__(self, poll=POLL_SECONDS):
        self.poll = poll
        self._lock = threading.Lock()
        self._entries = {}               # token -> socket
        self._wake = threading.Event()
        self._thread = None

    def watch(self, sock, token):
        """Watch `sock` for `token`'s request. False when it cannot be
        watched (no socket, TLS, already closed)."""
        try:
            if sock is None or isinstance(sock, ssl.SSLSocket) or sock.fileno() < 0:
                return False
        except Exception:                                        # noqa: BLE001
            return False
        with self._lock:
            self._entries[token] = sock
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="client-disconnect-monitor")
                self._thread.start()
        self._wake.set()
        return True

    def forget(self, token):
        with self._lock:
            self._entries.pop(token, None)

    def watching(self, token):
        with self._lock:
            return token in self._entries

    def count(self):
        with self._lock:
            return len(self._entries)

    def _take(self, token, sock):
        """Unregister `token` if it is still watched on `sock`; True if so."""
        with self._lock:
            if self._entries.get(token) is sock:
                del self._entries[token]
                return True
        return False

    def _run(self):
        while True:
            try:
                self._wake.clear()
                with self._lock:
                    items = list(self._entries.items())
                if not items:
                    self._wake.wait(30.0)
                    continue
                self.check(items)
            except Exception as exc:                             # noqa: BLE001
                _log.debug("client-disconnect monitor: %s", exc)
            time.sleep(self.poll)

    def check(self, items=None):
        """One pass over `items` ((token, socket) pairs; default: all).
        Returns the tokens it cancelled."""
        if items is None:
            with self._lock:
                items = list(self._entries.items())
        live = []
        for tok, sock in items:
            try:
                fd = sock.fileno()
            except Exception:                                    # noqa: BLE001
                fd = -1
            if fd < 0:
                self._take(tok, sock)    # closed by the server: request over
                continue
            live.append((tok, sock))
        gone = []
        for i in range(0, len(live), SELECT_CHUNK):
            part = live[i:i + SELECT_CHUNK]
            ready = self._readable([s for _t, s in part])
            for tok, sock in part:
                if id(sock) not in ready:
                    continue
                with self._lock:
                    if self._entries.get(tok) is not sock:
                        continue
                if peer_closed(sock) and self._take(tok, sock):
                    gone.append(tok)
        for tok in gone:
            tok.cancel("client disconnected")
        return gone

    @staticmethod
    def _readable(socks):
        try:
            r, _w, _x = select.select(socks, [], [], 0)
            return {id(s) for s in r}
        except (OSError, ValueError):
            # One of them was closed between fileno() and select(): look at
            # each on its own so the rest are still checked.
            out = set()
            for s in socks:
                try:
                    r, _w, _x = select.select([s], [], [], 0)
                    if r:
                        out.add(id(s))
                except (OSError, ValueError):
                    pass
            return out


MONITOR = Monitor()


_HOOKED = {"done": False}


def install_urllib3_hook():
    """Track every upstream connection used under a token, and refuse to START
    one for a cancelled request. Idempotent; False when urllib3's internals
    are not the ones this was written against (then cancel only flags)."""
    if _HOOKED["done"]:
        return True
    try:
        from urllib3 import connection as _u3conn
        from urllib3 import connectionpool as _u3pool
        pool_cls = _u3pool.HTTPConnectionPool
        orig_make = pool_cls._make_request
    except Exception:                                            # noqa: BLE001
        return False

    def _make_request(self, conn, *a, **kw):
        tok = current()
        if tok is not None:
            if tok.cancelled:
                raise ClientGone("the client disconnected: no new upstream call")
            try:
                raw = _raw_socket(conn)
                if raw is not None:          # a pooled, already-connected one
                    conn._hub_raw_sock = raw
            except Exception:                                    # noqa: BLE001
                pass
            tok.track_conn(conn)
        return orig_make(self, conn, *a, **kw)

    def _wrap_connect(cls):
        orig = cls.__dict__.get("connect")
        if orig is None:
            return

        def connect(self, *a, **kw):
            out = orig(self, *a, **kw)
            try:
                # Remembered: conn.sock goes None once a response takes it.
                self._hub_raw_sock = _raw_socket(self)
            except Exception:                                    # noqa: BLE001
                pass
            tok = getattr(self, "_hub_cancel_owner", None)
            if tok is not None and tok.cancelled:
                # Cancelled while this connection was being opened: the
                # shutdown pass found no socket yet. Cut it now.
                _shutdown_conn(self, tok)
                raise ClientGone("the client disconnected while connecting upstream")
            return out
        connect._hub_cancel_hook = True
        cls.connect = connect

    try:
        _make_request._hub_cancel_hook = True
        pool_cls._make_request = _make_request
        for cls in (getattr(_u3conn, "HTTPConnection", None),
                    getattr(_u3conn, "HTTPSConnection", None)):
            if cls is not None:
                _wrap_connect(cls)
    except Exception:                                            # noqa: BLE001
        return False
    _HOOKED["done"] = True
    return True
