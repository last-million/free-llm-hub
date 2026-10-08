"""A short resolver failure must not burn a chain or demote a provider.

OBSERVED 2026-10-08 02:48:27 UTC (hub.log): three unrelated provider hosts
failed to RESOLVE in the same second, the whole chain ended in milliseconds and
the CLI got a 503. Nothing here claims how often that happens; these tests pin
what the hub now does when it does:

  * netresolve.py -- one retry, then the last known good address of a host that
    resolved before (never for a name that never resolved, never older than
    7 days, never cached failures, never touching IP literals / localhost /
    bare names / bind calls), with the original resolver always reachable;
  * app.py -- a hop that failed because THIS computer could not resolve its
    host is filed against nobody, the walk pauses and walks the chain once
    more, and the 503 says whose problem it is.

Hermetic: the resolver is a fake, no socket is opened, no DNS is asked, nothing
sleeps for real.
"""
import copy
import logging
import socket
import threading
import time

import pytest
import requests
import urllib3

import app as A
import netresolve

ADDR1 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))]
ADDR2 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.8", 443))]
HOST = "integrate.api.nvidia.com"


# --------------------------------------------------------------------------- #
# A fake resolver and a fake clock
# --------------------------------------------------------------------------- #

class FakeResolver:
    """Scripted socket.getaddrinfo: per-host queue of outcomes (a list is a
    success, an exception is raised); the last outcome repeats."""

    def __init__(self):
        self.calls = []
        self.script = {}
        self.raised = []

    def set(self, host, *outcomes):
        self.script[host] = list(outcomes)

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls.append((host, port, family, type, proto, flags))
        outcomes = self.script.get(host)
        if not outcomes:
            raise AssertionError("unexpected lookup of %r" % (host,))
        out = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(out, BaseException):
            fresh = out.__class__(*out.args)
            self.raised.append(fresh)
            raise fresh
        return list(out)


def gai(code=socket.EAI_AGAIN, text="Temporary failure in name resolution"):
    return socket.gaierror(code, text)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def nr(monkeypatch):
    """netresolve over a fake resolver, with a recording sleep and a fake clock.
    The wrapper is called directly (netresolve.getaddrinfo): nothing is
    installed process-wide unless a test does it on purpose."""
    fake = FakeResolver()
    sleeps = []
    clock = Clock()
    monkeypatch.setattr(netresolve, "_orig", fake)
    monkeypatch.setattr(netresolve, "_sleep", sleeps.append)
    monkeypatch.setattr(netresolve, "_now", clock)
    monkeypatch.setattr(netresolve, "_flag_on", lambda: True)
    netresolve.reset()
    fake.sleeps = sleeps
    fake.clock = clock
    return fake


# --------------------------------------------------------------------------- #
# netresolve: the stale-address rule
# --------------------------------------------------------------------------- #

def test_a_failure_after_a_success_serves_the_last_known_address(nr, caplog):
    nr.set(HOST, ADDR1, gai())
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1
    nr.clock.t += 90
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        assert netresolve.getaddrinfo(HOST, 443) == ADDR1
    assert nr.sleeps == [netresolve.RETRY_DELAY]          # one retry, ~0.25 s
    assert len(nr.calls) == 3                              # ok, fail, retry
    line = [r.getMessage() for r in caplog.records if "[dns]" in r.getMessage()]
    assert len(line) == 1
    assert HOST in line[0] and "using the last known address from 90s ago" in line[0]
    assert netresolve.stats()["stale_served"] == 1


def test_a_name_that_never_resolved_gets_no_stale_answer(nr):
    original = gai(socket.EAI_NONAME, "Name or service not known")
    nr.set("never-seen.example.com", original)
    with pytest.raises(socket.gaierror) as err:
        netresolve.getaddrinfo("never-seen.example.com", 443)
    assert err.value is nr.raised[0]                       # the ORIGINAL error
    assert err.value.errno == socket.EAI_NONAME
    assert netresolve.stats()["entries"] == 0              # a failure is never cached
    assert netresolve.stats()["stale_served"] == 0


def test_a_failure_is_never_cached_a_later_success_is(nr):
    nr.set(HOST, gai(), gai(), ADDR1)
    with pytest.raises(socket.gaierror):
        netresolve.getaddrinfo(HOST, 443)
    assert netresolve.stats()["entries"] == 0
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1
    assert netresolve.stats()["entries"] == 1


def test_one_retry_recovers_without_serving_anything_stale(nr):
    nr.set(HOST, gai(), ADDR2)
    assert netresolve.getaddrinfo(HOST, 443) == ADDR2
    assert nr.sleeps == [netresolve.RETRY_DELAY]
    assert len(nr.calls) == 2
    assert netresolve.stats()["recovered"] == 1 and netresolve.stats()["stale_served"] == 0


def test_a_success_is_never_delayed(nr):
    nr.set(HOST, ADDR1)
    for _ in range(5):
        netresolve.getaddrinfo(HOST, 443)
    assert nr.sleeps == []
    assert len(nr.calls) == 5


def test_any_errno_counts_including_windows_codes(nr):
    nr.set(HOST, ADDR1, socket.gaierror(11001, "getaddrinfo failed"))
    netresolve.getaddrinfo(HOST, 443)
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1


def test_a_stale_entry_older_than_seven_days_is_refused(nr):
    nr.set(HOST, ADDR1, gai(), gai())
    netresolve.getaddrinfo(HOST, 443)
    nr.clock.t += netresolve.MAX_STALE + 1
    with pytest.raises(socket.gaierror) as err:
        netresolve.getaddrinfo(HOST, 443)
    assert err.value is nr.raised[0]
    assert netresolve.stats()["stale_refused"] == 1
    assert netresolve.stats()["entries"] == 0


def test_an_entry_just_inside_seven_days_still_serves(nr):
    nr.set(HOST, ADDR1, gai())
    netresolve.getaddrinfo(HOST, 443)
    nr.clock.t += netresolve.MAX_STALE - 5
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1


def test_a_clock_that_stepped_back_is_age_zero_not_an_error(nr):
    nr.set(HOST, ADDR1, gai())
    netresolve.getaddrinfo(HOST, 443)
    nr.clock.t -= 3600
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1


def test_the_flag_off_is_a_pure_passthrough(nr, monkeypatch):
    monkeypatch.setattr(netresolve, "_flag_on", lambda: False)
    nr.set(HOST, ADDR1, gai())
    netresolve.getaddrinfo(HOST, 443)
    with pytest.raises(socket.gaierror) as err:
        netresolve.getaddrinfo(HOST, 443)
    assert err.value is nr.raised[0]
    assert nr.sleeps == []                                 # no retry
    assert len(nr.calls) == 2


def test_the_flag_is_the_dns_stale_cache_setting_default_on(monkeypatch):
    asked = []

    def get_flag(name, default=False):
        asked.append((name, default))
        return default
    monkeypatch.setattr(A.config, "get_flag", get_flag)
    assert netresolve._flag_on() is True
    assert asked == [("dns_stale_cache", True)]
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: False)
    assert netresolve._flag_on() is False


def test_without_config_the_default_is_on(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "config", None)   # import fails
    assert netresolve._flag_on() is True


def test_the_flag_is_not_read_on_a_success(nr, monkeypatch):
    def boom():
        raise AssertionError("a successful lookup must not read the config")
    monkeypatch.setattr(netresolve, "_flag_on", boom)
    nr.set(HOST, ADDR1)
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1


@pytest.mark.parametrize("host,port,flags", [
    ("127.0.0.1", 80, 0),
    ("203.0.113.9", 443, 0),
    ("::1", 80, 0),
    ("fe80::1%eth0", 80, 0),
    ("127.1", 80, 0),                       # numeric spelling, no DNS
    ("localhost", 80, 0),
    ("api.localhost", 80, 0),
    ("LOCALHOST", 80, 0),
    ("mybox", 80, 0),                        # bare name, no dot
    (None, 80, 0),
    ("", 80, 0),
    (HOST, 8787, socket.AI_PASSIVE),         # a bind, not a connect
    (b"\xff\xfe.example", 80, 0),            # bytes that are not ASCII
])
def test_calls_that_are_not_ours_are_untouched(nr, host, port, flags):
    nr.script[host] = [gai()]
    with pytest.raises(socket.gaierror) as err:
        netresolve.getaddrinfo(host, port, 0, 0, 0, flags)
    assert err.value is nr.raised[0]
    assert len(nr.calls) == 1, "no retry for %r" % (host,)
    assert nr.sleeps == []
    assert netresolve.stats()["entries"] == 0
    nr.script[host] = [ADDR1]
    assert netresolve.getaddrinfo(host, port, 0, 0, 0, flags) == ADDR1
    assert netresolve.stats()["entries"] == 0              # and never remembered


def test_the_cache_is_bounded_and_evicts_the_oldest(nr):
    for i in range(netresolve.MAX_ENTRIES + 44):
        host = "h%d.example.com" % i
        nr.set(host, ADDR1)
        netresolve.getaddrinfo(host, 443)
    assert netresolve.stats()["entries"] == netresolve.MAX_ENTRIES
    nr.set("h0.example.com", gai())                          # evicted: no stale
    with pytest.raises(socket.gaierror):
        netresolve.getaddrinfo("h0.example.com", 443)
    last = "h%d.example.com" % (netresolve.MAX_ENTRIES + 43)
    nr.set(last, gai())                                      # kept: stale served
    assert netresolve.getaddrinfo(last, 443) == ADDR1


def test_the_key_includes_family_type_proto_flags_and_port(nr):
    nr.set(HOST, ADDR1, gai(), gai())
    netresolve.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(socket.gaierror):                     # another family
        netresolve.getaddrinfo(HOST, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    with pytest.raises(socket.gaierror):                     # another port
        netresolve.getaddrinfo(HOST, 80, socket.AF_INET, socket.SOCK_STREAM)
    assert netresolve.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM) == ADDR1
    nr.set(HOST.upper(), gai())                              # host case does not matter
    assert netresolve.getaddrinfo(HOST.upper(), 443, socket.AF_INET, socket.SOCK_STREAM) == ADDR1


def test_results_are_copies_so_a_caller_cannot_poison_the_cache(nr):
    nr.set(HOST, ADDR1, gai())
    first = netresolve.getaddrinfo(HOST, 443)
    first.append("junk")
    stale = netresolve.getaddrinfo(HOST, 443)
    assert stale == ADDR1
    stale.clear()
    nr.set(HOST, gai())
    assert netresolve.getaddrinfo(HOST, 443) == ADDR1


def test_the_stale_line_is_logged_once_per_host_per_five_minutes(nr, caplog):
    nr.set(HOST, ADDR1, gai())
    netresolve.getaddrinfo(HOST, 443)
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        for _ in range(4):
            nr.clock.t += 30
            netresolve.getaddrinfo(HOST, 443)
        assert len([r for r in caplog.records if "[dns]" in r.getMessage()]) == 1
        nr.clock.t += netresolve.LOG_EVERY
        netresolve.getaddrinfo(HOST, 443)
        assert len([r for r in caplog.records if "[dns]" in r.getMessage()]) == 2


def test_no_lock_is_held_across_the_os_call(nr):
    """The OS call re-enters netresolve (stats() takes the module lock); if the
    lock were held across it this would deadlock instead of finishing."""
    def reentrant(host, port, family=0, type=0, proto=0, flags=0):
        netresolve.stats()
        return list(ADDR1)
    netresolve._orig = reentrant
    done = []
    t = threading.Thread(target=lambda: done.append(netresolve.getaddrinfo(HOST, 443)),
                         daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and done == [ADDR1]


def test_threads_hammering_it_never_corrupt_it(nr):
    hosts = ["t%d.example.com" % i for i in range(12)]
    for h in hosts:
        nr.set(h, ADDR1, gai(), ADDR1, gai(), gai(), ADDR1)
    errors = []

    def work(seed):
        try:
            for i in range(60):
                h = hosts[(seed + i) % len(hosts)]
                try:
                    out = netresolve.getaddrinfo(h, 443)
                    assert out == ADDR1
                except socket.gaierror:
                    pass
        except BaseException as exc:                        # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors and not any(t.is_alive() for t in threads)
    assert netresolve.stats()["entries"] <= len(hosts)


# --------------------------------------------------------------------------- #
# netresolve: installing it
# --------------------------------------------------------------------------- #

def test_importing_the_app_did_not_install_the_wrapper():
    """tests/conftest.py flips the boot switch before app is imported."""
    assert not netresolve.installed()
    assert socket.getaddrinfo is not netresolve.getaddrinfo


def test_the_app_installs_it_with_one_boot_line():
    src = open("app.py", encoding="utf-8").read()
    assert src.count("netresolve.install_at_boot()") == 1
    assert "import netresolve" in src


def test_install_is_idempotent_and_keeps_the_original_reachable(monkeypatch):
    real = socket.getaddrinfo
    fake = FakeResolver()
    fake.set(HOST, ADDR1)
    monkeypatch.setattr(netresolve, "_orig", fake)
    assert netresolve.install() is True
    assert netresolve.install() is True
    assert socket.getaddrinfo is netresolve.getaddrinfo
    assert netresolve._orig is fake                           # the original stays reachable
    assert socket.getaddrinfo(HOST, 443) == ADDR1
    assert netresolve.uninstall() is True
    assert socket.getaddrinfo is real


def test_installed_wrapper_serves_stale_through_socket_getaddrinfo(monkeypatch):
    fake = FakeResolver()
    fake.set(HOST, ADDR1, gai())
    monkeypatch.setattr(netresolve, "_orig", fake)
    monkeypatch.setattr(netresolve, "_sleep", lambda s: None)
    monkeypatch.setattr(netresolve, "_flag_on", lambda: True)
    netresolve.reset()
    netresolve.install()
    assert socket.getaddrinfo(HOST, 443) == ADDR1
    assert socket.getaddrinfo(HOST, 443) == ADDR1             # stale, through socket.*
    netresolve.uninstall()


def test_uninstall_leaves_somebody_elses_later_patch_alone(monkeypatch):
    netresolve.install()
    other = lambda *a, **k: []                                 # noqa: E731
    monkeypatch.setattr(socket, "getaddrinfo", other)
    assert netresolve.uninstall() is False
    assert socket.getaddrinfo is other


def test_install_wraps_a_resolver_somebody_else_installed(monkeypatch):
    other = FakeResolver()
    other.set(HOST, ADDR1)
    monkeypatch.setattr(socket, "getaddrinfo", other)
    netresolve.install()
    assert netresolve._orig is other
    assert socket.getaddrinfo(HOST, 443) == ADDR1
    netresolve.uninstall()
    assert socket.getaddrinfo is other


def test_the_boot_switch_and_the_environment_switch(monkeypatch):
    monkeypatch.setattr(netresolve, "BOOT_INSTALL", False)
    assert netresolve.install_at_boot() is False
    assert not netresolve.installed()
    monkeypatch.setattr(netresolve, "BOOT_INSTALL", True)
    monkeypatch.setenv("FREE_LLM_HUB_NO_DNS_CACHE", "1")
    assert netresolve.install_at_boot() is False
    assert not netresolve.installed()
    monkeypatch.delenv("FREE_LLM_HUB_NO_DNS_CACHE")
    assert netresolve.install_at_boot() is True
    assert netresolve.installed()


def test_zz_a_test_may_leave_the_wrapper_installed():
    netresolve.install()
    assert socket.getaddrinfo is netresolve.getaddrinfo


def test_zz_the_next_test_sees_the_original_resolver_again():
    """tests/conftest.py puts the real resolver back after every test."""
    assert socket.getaddrinfo is netresolve._import_time
    assert not netresolve.installed()
    assert netresolve._orig is netresolve._import_time


# --------------------------------------------------------------------------- #
# app: what counts as "this computer's network"
# --------------------------------------------------------------------------- #

def _real_resolution_failure(host="api.z.ai"):
    """The exception a hop REALLY raises when the name cannot be resolved:
    requests -> urllib3 -> socket.getaddrinfo (patched to fail; no DNS, no
    socket)."""
    from unittest import mock
    s = requests.Session()
    s.trust_env = False
    with mock.patch.object(socket, "getaddrinfo",
                           side_effect=socket.gaierror(-2, "Name or service not known")):
        try:
            s.get("https://%s/v1/chat/completions" % host, timeout=2)
        except requests.exceptions.RequestException as exc:
            return exc
    raise AssertionError("the patched resolver did not make the request fail")


def test_a_real_requests_connection_error_over_a_name_resolution_error_is_local():
    exc = _real_resolution_failure()
    assert isinstance(exc, requests.exceptions.ConnectionError)
    assert "NameResolutionError" in str(exc)
    assert A._is_local_network_error(exc) is True
    assert A._local_net_host(exc, "x") == "api.z.ai"


def test_a_hand_built_name_resolution_chain_is_local_too():
    pool = urllib3.HTTPSConnectionPool("zenmux.ai", port=443)
    nre = urllib3.exceptions.NameResolutionError(
        "zenmux.ai", None, socket.gaierror(-3, "Temporary failure in name resolution"))
    mre = urllib3.exceptions.MaxRetryError(pool, "/v1/chat/completions", reason=nre)
    assert A._is_local_network_error(requests.exceptions.ConnectionError(mre)) is True


def test_a_plain_gaierror_and_wrapped_ones_are_local():
    assert A._is_local_network_error(socket.gaierror(-2, "Name or service not known"))
    assert A._is_local_network_error(socket.gaierror(11001, "getaddrinfo failed"))
    try:
        try:
            raise socket.gaierror(-3, "x")
        except socket.gaierror as inner:
            raise RuntimeError("hop failed") from inner
    except RuntimeError as wrapped:
        assert A._is_local_network_error(wrapped)
    try:
        try:
            raise socket.gaierror(-3, "x")
        except socket.gaierror:
            raise ValueError("while handling")                # __context__ only
    except ValueError as ctx:
        assert A._is_local_network_error(ctx)


@pytest.mark.parametrize("text", [
    "Failed to resolve 'api.z.ai' ([Errno -2] Name or service not known)",
    "[Errno -3] Temporary failure in name resolution",
    "[Errno -2] Name or service not known",
    "[Errno 11001] getaddrinfo failed",
    "[Errno 101] Network is unreachable",
    "[Errno 113] No route to host",
    "nodename nor servname provided, or not known",
])
def test_the_listed_messages_are_local(text):
    assert A._is_local_network_error(requests.exceptions.ConnectionError(text))
    assert A._is_local_network_error(OSError(text))


def test_provider_side_failures_are_not_local():
    reset = ConnectionResetError(10054, "An existing connection was forcibly closed")
    proto = urllib3.exceptions.ProtocolError("Connection aborted.", reset)
    wrapped = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            urllib3.HTTPSConnectionPool("api.z.ai", port=443), "/x", reason=proto))
    for exc in (
            reset, proto, wrapped,
            ConnectionResetError(104, "Connection reset by peer"),
            ConnectionRefusedError(111, "Connection refused"),
            http_remote_disconnected(),
            requests.exceptions.ReadTimeout("Read timed out. (read timeout=300)"),
            requests.exceptions.ConnectTimeout("Connection to api.z.ai timed out"),
            requests.exceptions.ConnectionError("connection refused"),
            requests.exceptions.ConnectionError("unreachable"),
            requests.exceptions.SSLError("certificate verify failed"),
            RuntimeError("no key"), None):
        assert A._is_local_network_error(exc) is False, repr(exc)


def http_remote_disconnected():
    import http.client
    return http.client.RemoteDisconnected("Remote end closed connection without response")


def test_a_cycle_in_the_exception_chain_does_not_loop():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert A._is_local_network_error(a) is False


def test_the_last_error_class_is_dns_for_a_local_failure_only():
    assert A._classify_hop_error(exc=_real_resolution_failure()) == "dns"
    assert A._classify_hop_error(exc=requests.exceptions.ConnectionError("connection refused")) == "conn"
    assert A._classify_hop_error(exc=requests.exceptions.ReadTimeout("slow")) == "timeout"
    assert A._classify_hop_error(exc=RuntimeError("no key")) == "error"


# --------------------------------------------------------------------------- #
# app: filed against nobody
# --------------------------------------------------------------------------- #

def _snapshot():
    with A._outcome_lock:
        outcomes = copy.deepcopy(A._outcomes)
    return {
        "outcomes": outcomes,
        "recent": dict(A._recent_hop_fail),
        "tool_outcomes": copy.deepcopy(A._tool_outcomes),
        "relay": copy.deepcopy(A._relay_tool_fail),
        "swarm": dict(A._swarm_member_fail),
        "team": copy.deepcopy(A._team_stats),
        "verifier": copy.deepcopy(A._verifier_stats),
        "hop_model": copy.deepcopy(A._hop_model_fail),
        "timeouts": dict(A._provider_timeout_fail),
        "consec": dict(A._provider_consec_fail),
        "junk": copy.deepcopy(A._junk_bench),
        "dead": dict(A._dead_models),
    }


@pytest.fixture
def throttles(monkeypatch):
    calls = []
    for name in ("mark_throttled", "mark_model_throttled", "mark_key_exhausted"):
        monkeypatch.setattr(A.quota, name,
                            lambda *a, _n=name, **k: calls.append((_n, a)))
    return calls


@pytest.fixture
def plain_chain(monkeypatch):
    """Hop plumbing minus disk and readiness; the LEDGERS stay real."""
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_save_perf_stats", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    monkeypatch.setattr(A, "_CHAIN_RETRY_DELAY", 0)
    yield


def _dispatch_failing_locally(pid="nvidia", model="kimi", tools=False):
    """Drive the REAL _dispatch_chat; only the HTTP call underneath is fake."""
    def upstream(p, payload, stream, **kw):
        raise _real_resolution_failure({"nvidia": "integrate.api.nvidia.com",
                                        "glm": "api.z.ai", "zenmux": "zenmux.ai"}.get(p, p + ".example"))
    from unittest import mock
    with mock.patch.object(A, "_upstream_chat", upstream):
        with pytest.raises(requests.exceptions.ConnectionError) as err:
            A._dispatch_chat(pid, {"model": model, "messages": [{"role": "user", "content": "hi"}]}, False)
    return err.value


def test_a_real_dispatch_failure_marks_the_hop_and_files_nothing(plain_chain, throttles):
    before = _snapshot()
    with A.app.test_request_context():
        exc = _dispatch_failing_locally("nvidia", "kimi")
        assert A._local_net_failed("nvidia", "kimi")
        assert A._local_net_failed("nvidia")                  # provider-level read
        assert not A._local_net_failed("glm", "kimi")
        # every ledger the client-gone pattern lists, called the way the loops call them
        A._record_outcome("nvidia", "kimi", False)
        A._record_outcome("nvidia", "kimi", False, junk=True)
        A._note_recent_hop_failure("nvidia", "kimi", "timeout")
        A._throttle_failed_hop("nvidia", "kimi", exc=exc)
        A._throttle_failed_hop("nvidia", "kimi")
        A._note_provider_timeout("nvidia", exc)
        A._note_provider_result("nvidia", ok=False, hard_fail=True)
        A._note_nonanswer("nvidia", "kimi", kind=None)
        A._note_relay_tool_fail("nvidia", "kimi")
        A._note_tool_turn_outcome("nvidia", "kimi", False)
        A._note_quality_strike("nvidia", "kimi", "echoed_decimal")
        A._record_stream_outcome("nvidia", "kimi", "some streamed text " * 20)
        A._note_swarm_member_fail("nvidia", "kimi", "ConnectionError")
        A._swarm_note_member_exc("nvidia", "kimi", exc)
        A._team_stats_note("nvidia", "kimi", "scout", False, 0.1)
        A._verifier_stats_note("nvidia", "kimi", False)
    assert _snapshot() == before
    assert throttles == []


def test_the_same_calls_do_file_when_the_failure_was_not_local(plain_chain, throttles):
    """The gate is the local-network mark, not a blanket silence."""
    before = _snapshot()
    with A.app.test_request_context():
        A._record_outcome("nvidia", "kimi", False)
        A._note_recent_hop_failure("nvidia", "kimi", "timeout")
        A._swarm_note_member_exc("nvidia", "kimi", requests.exceptions.ReadTimeout("slow"))
        A._throttle_failed_hop("nvidia", "kimi", exc=requests.exceptions.ReadTimeout("slow"))
    after = _snapshot()
    assert after["outcomes"] != before["outcomes"]
    assert ("nvidia", "kimi") in after["recent"]
    assert ("nvidia", "kimi") in after["swarm"]
    assert throttles, "a model-scoped cooldown is still filed for a real read timeout"


def test_a_real_answer_clears_the_mark_so_a_later_failure_is_filed(plain_chain):
    from unittest import mock
    with A.app.test_request_context():
        _dispatch_failing_locally("nvidia", "kimi")
        assert A._local_net_failed("nvidia", "kimi")

        class R:
            status_code = 200
            headers = {}
        with mock.patch.object(A, "_upstream_chat", lambda *a, **k: R()):
            A._dispatch_chat("nvidia", {"model": "kimi", "messages": []}, False)
        assert not A._local_net_failed("nvidia", "kimi")
        A._record_outcome("nvidia", "kimi", False)
        assert A._outcomes.get(("nvidia", "kimi"), {}).get("fail", 0) >= 1


def test_a_mark_expires(plain_chain, monkeypatch):
    with A.app.test_request_context():
        _dispatch_failing_locally("nvidia", "kimi")
        assert A._local_net_failed("nvidia", "kimi")
        monkeypatch.setattr(A, "_LOCAL_NET_MARK_TTL", -1.0)   # every mark is now old
        assert not A._local_net_failed("nvidia", "kimi")


def test_the_role_actor_judge_files_nothing_for_a_local_failure(plain_chain, throttles):
    before = _snapshot()
    with A.app.test_request_context():
        exc = _dispatch_failing_locally("nvidia", "kimi")
        verdict = A._role_judge("nvidia", "kimi", None, exc,
                                {"tools": [{"type": "function", "function": {"name": "x"}}]},
                                {}, 1000, "coding|hard|tools|m")
    assert verdict["ok"] is False and verdict["fail"] == "exc"
    assert _snapshot() == before
    assert throttles == []


# --------------------------------------------------------------------------- #
# app: the chain clock pauses and walks once more
# --------------------------------------------------------------------------- #

CHAIN3 = [("nvidia", "kimi"), ("glm", "glm-5"), ("zenmux", "z1")]


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    monkeypatch.setattr(A, "_LOCAL_NET_SLEEP", rec.append)
    return rec


def _walk(chain, fails, clock=None):
    """Run the real walk + dispatch with a fake upstream. `fails(pid, n)` says
    whether the n-th call to that provider fails to resolve. Returns
    (served pair or None, [calls in order], clock)."""
    from unittest import mock
    calls, count = [], {}

    class R:
        status_code = 200
        headers = {}

    def upstream(pid, payload, stream, **kw):
        n = count[pid] = count.get(pid, 0) + 1
        calls.append(pid)
        if fails(pid, n):
            raise _real_resolution_failure(pid + ".example.com")
        return R()
    served = None
    clock = clock or A._ChainClock()
    with mock.patch.object(A, "_upstream_chat", upstream):
        for pid, model in clock.walk(chain):
            try:
                clock.dispatch(pid, {"model": model, "messages": []}, False)
            except requests.exceptions.RequestException:
                continue
            served = (pid, model)
            break
    return served, calls, clock


def test_two_hosts_in_a_row_pause_one_second_then_the_next_hop_is_tried(plain_chain, sleeps):
    with A.app.test_request_context():
        served, calls, clock = _walk(CHAIN3, lambda pid, n: pid in ("nvidia", "glm"))
        assert served == ("zenmux", "z1")
        assert calls == ["nvidia", "glm", "zenmux"]
        assert sum(sleeps) == pytest.approx(1.0) and max(sleeps) <= 0.5   # 1 s, in slices
        assert clock._net_pauses == 1


def test_one_host_alone_does_not_pause(plain_chain, sleeps):
    with A.app.test_request_context():
        served, calls, _ = _walk(CHAIN3, lambda pid, n: pid == "nvidia")
        assert served == ("glm", "glm-5")
    assert sleeps == []


def test_a_non_local_failure_in_between_resets_the_streak(plain_chain, sleeps):
    from unittest import mock
    with A.app.test_request_context():
        calls = []

        class R:
            status_code = 200
            headers = {}

        def upstream(pid, payload, stream, **kw):
            calls.append(pid)
            if pid == "glm":
                raise requests.exceptions.ConnectionError("connection refused")
            if pid in ("nvidia", "zenmux"):
                raise _real_resolution_failure(pid + ".example.com")
            return R()
        chain = CHAIN3 + [("g4f", "m")]
        clock = A._ChainClock()
        with mock.patch.object(A, "_upstream_chat", upstream):
            for pid, model in clock.walk(chain):
                try:
                    clock.dispatch(pid, {"model": model, "messages": []}, False)
                except requests.exceptions.RequestException:
                    continue
                break
        assert calls == ["nvidia", "glm", "zenmux", "g4f"]
    assert sleeps == []                       # never 2 local hosts back to back


def test_a_chain_that_ends_only_on_local_failures_is_walked_once_more(plain_chain, sleeps):
    """Down for the whole first pass, back for the second: served on the re-walk
    after the 1 s + 3 s pauses, and nothing was filed against anybody."""
    before = _snapshot()
    with A.app.test_request_context():
        served, calls, clock = _walk(CHAIN3, lambda pid, n: n == 1)
        assert served == ("nvidia", "kimi")
        assert calls == ["nvidia", "glm", "zenmux", "nvidia"]
        assert sum(sleeps) == pytest.approx(4.0)
        assert clock._net_rewalked is True and clock._net_pauses == 2
    assert _snapshot() == before


def test_a_dead_network_walks_the_chain_exactly_twice_then_says_so(plain_chain, sleeps):
    with A.app.test_request_context():
        served, calls, clock = _walk(CHAIN3, lambda pid, n: True)
        assert served is None
        assert len(calls) == 6                # once + ONE re-walk, never a third
        assert sum(sleeps) == pytest.approx(4.0)       # 1 s + 3 s, no more
        assert clock._net_only() is True
        assert A._net_only_now() is True
        text = A._chain_exhausted_text(["nvidia: ConnectionError", "glm: ConnectionError"])
        assert "this computer could not resolve provider hostnames" in text
        assert "a DNS/network problem on this machine, not a provider outage" in text
        assert "check the connection or DNS, then retry" in text


def test_a_client_that_left_stops_the_pause(plain_chain, monkeypatch, sleeps):
    with A.app.test_request_context():
        monkeypatch.setattr(A, "_client_gone", lambda: True)
        clock = A._ChainClock()
        clock._net_streak = ["a.example.com", "b.example.com"]
        assert clock._net_pause() is False
    assert sleeps == []


def test_the_pause_never_goes_past_the_request_deadline(plain_chain, sleeps, monkeypatch):
    with A.app.test_request_context():
        clock = A._ChainClock()
        monkeypatch.setattr(clock, "left", lambda: 1.4)
        assert clock._net_pause() is True
        assert sum(sleeps) == pytest.approx(0.4)          # left - 1.0
        monkeypatch.setattr(clock, "left", lambda: 0.9)
        before = list(sleeps)
        assert clock._net_pause() is False                  # no time at all
        assert sleeps == before


def test_a_clock_built_without_init_still_walks():
    """The class comment promises it: tests build clocks without __init__."""
    clock = A._ChainClock.__new__(A._ChainClock)
    clock.trivial = False
    assert list(clock.walk([("p1", "m1"), ("p2", "m2")])) == [("p1", "m1"), ("p2", "m2")]
    assert clock._net_only() is False and clock._net_pauses == 0


def test_a_locally_failed_hop_is_not_charged_to_the_provider_on_a_tool_turn(plain_chain, sleeps):
    with A.app.test_request_context():
        clock = A._ChainClock(tools=True)
        served, calls, clock = _walk(CHAIN3, lambda pid, n: n == 1, clock=clock)
        assert served is not None
        assert clock._prov_hops.get("nvidia", 0) <= 1
        assert clock._prov_secs.get("nvidia", 0.0) == 0.0
        assert not clock._relay_bad


# --------------------------------------------------------------------------- #
# app: the three routes
# --------------------------------------------------------------------------- #

def _wire_routes(monkeypatch, chain, calls):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (chain[0][0], chain[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(chain))

    def upstream(pid, payload, stream, **kw):
        calls.append(pid)
        raise _real_resolution_failure(pid + ".example.com")
    monkeypatch.setattr(A, "_upstream_chat", upstream)


def test_chat_route_a_dead_network_is_a_plain_503_that_names_this_computer(
        plain_chain, sleeps, monkeypatch, throttles):
    calls = []
    _wire_routes(monkeypatch, CHAIN3, calls)
    before = _snapshot()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "explain the design of this repo"}]})
    assert r.status_code == 503
    assert r.headers["X-Free-LLM-Hub-Last-Error"] == "dns"
    msg = r.get_json()["error"]["message"]
    assert "this computer could not resolve provider hostnames" in msg
    assert "not a provider outage" in msg
    assert len(calls) == 6
    assert sum(sleeps) == pytest.approx(4.0)
    assert _snapshot() == before and throttles == []


def test_messages_route_says_the_same(plain_chain, sleeps, monkeypatch, throttles):
    calls = []
    _wire_routes(monkeypatch, CHAIN3, calls)
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": "explain the design of this repo"}]})
    assert r.status_code == 503
    assert r.headers["X-Free-LLM-Hub-Last-Error"] == "dns"
    assert "this computer could not resolve provider hostnames" in r.get_data(as_text=True)
    assert len(calls) == 6 and throttles == []


def test_responses_route_adds_only_its_one_existing_retry(plain_chain, sleeps, monkeypatch, throttles):
    """/v1/responses already re-runs a transient-only chain once (6 s in
    production, 0 here); a local-only failure must not loop beyond that single
    retry plus the walk's own single re-walk per pass."""
    calls = []
    _wire_routes(monkeypatch, CHAIN3, calls)
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": False, "input": "explain the design of this repo"})
    assert r.status_code == 503
    assert r.headers["X-Free-LLM-Hub-Last-Error"] == "dns"
    assert "this computer could not resolve provider hostnames" in r.get_data(as_text=True)
    assert len(calls) == 12                  # (1 walk + 1 re-walk) x (1 pass + 1 retry)
    assert throttles == []


def test_the_activity_row_carries_a_network_note(plain_chain, sleeps, monkeypatch):
    calls = []
    _wire_routes(monkeypatch, CHAIN3, calls)
    A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "explain the design of this repo"}]})
    with A._activity_lock:
        rows = list(A._activity)
    assert rows and max(rows, key=lambda r: r["id"]).get("net") == "network"
    html = open("templates/index.html", encoding="utf-8").read()
    assert "a.net ?" in html and "could not resolve provider hostnames" in html


def test_a_normal_failure_keeps_the_old_text_and_header(plain_chain, sleeps, monkeypatch):
    """Every other path stays byte-identical: provider-side failures read as
    before, with no pause and no re-walk."""
    calls = []
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("nvidia", "kimi", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(CHAIN3))

    def upstream(pid, payload, stream, **kw):
        calls.append(pid)
        raise requests.exceptions.ConnectionError("connection refused")
    monkeypatch.setattr(A, "_upstream_chat", upstream)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "explain the design of this repo"}]})
    assert r.status_code == 503
    assert r.headers["X-Free-LLM-Hub-Last-Error"] == "conn"
    assert r.get_json()["error"]["message"].startswith(
        "All providers failed: nvidia: connection error; glm: connection error; "
        "zenmux: connection error")
    assert "this computer" not in r.get_data(as_text=True)
    assert calls == ["nvidia", "glm", "zenmux"] and sleeps == []


def test_the_text_is_unchanged_outside_a_network_failure():
    assert A._chain_exhausted_text(["p1: ConnectionError", "p2: HTTP 429"]) == \
        "All providers failed: p1: connection error; p2: HTTP 429"
    with A.app.test_request_context():
        assert A._chain_exhausted_text(["p1: ConnectionError"]) == \
            "All providers failed: p1: connection error"


def test_a_hard_upstream_error_keeps_its_own_text_even_when_hops_failed_locally():
    with A.app.test_request_context():
        A.g.hub_net_only = True
        text = A._chain_exhausted_text(["p1: ConnectionError", "p2: HTTP 404"],
                                       {"pid": "p2", "status": 404})
        assert "this computer" not in text and "last hard error: HTTP 404" in text


# --------------------------------------------------------------------------- #
# app: each ledger on its own, a relay on a tool turn, a stale verdict
# --------------------------------------------------------------------------- #

RELAY = ("g4f", "srv_a:some/model")          # a relay pair: _relay_server_id knows it

_LEDGER_CALLS = {
    "record_outcome": lambda exc: A._record_outcome(*RELAY, False),
    "record_outcome_junk": lambda exc: A._record_outcome(*RELAY, False, junk=True),
    "recent_hop_failure": lambda exc: A._note_recent_hop_failure(*RELAY, "timeout"),
    "throttle_failed_hop": lambda exc: A._throttle_failed_hop(*RELAY, exc=exc),
    "provider_result": lambda exc: A._note_provider_result(RELAY[0], ok=False, hard_fail=True),
    "nonanswer": lambda exc: A._note_nonanswer(*RELAY, kind=None),
    "relay_tool_fail": lambda exc: A._note_relay_tool_fail(*RELAY),
    "tool_turn_outcome": lambda exc: A._note_tool_turn_outcome(*RELAY, False),
    "quality_strike": lambda exc: A._note_quality_strike(*RELAY, "echoed_decimal"),
    "stream_outcome": lambda exc: A._record_stream_outcome(*RELAY, "a streamed answer " * 20),
    "swarm_member_fail": lambda exc: A._note_swarm_member_fail(*RELAY, "ConnectionError"),
    "swarm_member_exc": lambda exc: A._swarm_note_member_exc(*RELAY, exc),
    "team_stats": lambda exc: A._team_stats_note(*RELAY, "scout", False, 0.1),
    "verifier_stats": lambda exc: A._verifier_stats_note(*RELAY, False),
    "role_judge": lambda exc: A._role_judge(*RELAY, None, exc, {"tools": [
        {"type": "function", "function": {"name": "x"}}]}, {}, 1000, "k"),
}


@pytest.mark.parametrize("name", sorted(_LEDGER_CALLS))
def test_each_ledger_files_nothing_for_a_local_failure(name, plain_chain, throttles):
    """One ledger per test, so removing ONE gate fails exactly its own case."""
    with A.app.test_request_context():
        A.g.hub_tool_turn = True                 # _in_tool_turn(): the tool ledgers are live
        exc = _dispatch_failing_locally(*RELAY)
        before = _snapshot()
        _LEDGER_CALLS[name](exc)
        assert _snapshot() == before, name
    assert throttles == []


@pytest.mark.parametrize("name", sorted(set(_LEDGER_CALLS) - {"throttle_failed_hop"}))
def test_each_ledger_still_files_for_a_failure_that_is_not_local(name, plain_chain, throttles):
    """The control: with no mark the same call DOES change its ledger, so the
    test above can only pass because of the gate."""
    with A.app.test_request_context():
        A.g.hub_tool_turn = True
        exc = requests.exceptions.ConnectionError("connection refused")
        before = _snapshot()
        _LEDGER_CALLS[name](exc)
        after = _snapshot()
    assert after != before, name


def test_a_relay_on_a_tool_turn_gets_no_strike_for_this_computers_dns(plain_chain, sleeps):
    """_ChainClock._plain files a plain ConnectionError against a relay server
    on a tool turn (_note_relay_tool_fail): not when the cause was local."""
    chain = [("g4f", "srv_a:m1"), ("g4f", "srv_b:m2"), ("nvidia", "kimi")]
    with A.app.test_request_context():
        clock = A._ChainClock(tools=True)
        served, calls, _ = _walk(chain, lambda pid, n: pid == "g4f", clock=clock)
        assert served == ("nvidia", "kimi") and calls == ["g4f", "g4f", "nvidia"]
    assert A._relay_tool_fail == {}
    assert not A._relay_tool_sick("g4f", "srv_a:m1")
    assert not A._relay_tool_sick("g4f", "srv_b:m2")


def test_the_same_relay_failure_without_a_local_cause_is_struck(plain_chain, sleeps):
    from unittest import mock
    chain = [("g4f", "srv_a:m1"), ("nvidia", "kimi")]

    class R:
        status_code = 200
        headers = {}

    def upstream(pid, payload, stream, **kw):
        if pid == "g4f":
            raise requests.exceptions.ConnectionError("connection refused")
        return R()
    with A.app.test_request_context():
        clock = A._ChainClock(tools=True)
        with mock.patch.object(A, "_upstream_chat", upstream):
            for pid, model in clock.walk(chain):
                try:
                    clock.dispatch(pid, {"model": model, "messages": []}, False)
                except requests.exceptions.RequestException:
                    continue
                break
    assert A._relay_tool_fail, "a refusal from a relay server is still a strike"


def test_a_second_clock_does_not_inherit_the_first_ones_network_verdict(plain_chain, sleeps):
    """The roles walk and its `best` fallback, or /v1/responses and its storm
    retry, are two clocks in ONE request: the 503 text follows the LAST walk."""
    from unittest import mock
    with A.app.test_request_context():
        _walk(CHAIN3, lambda pid, n: True)                    # all-local, 2+ hosts
        assert A._net_only_now() is True
        assert "this computer could not resolve" in A._chain_exhausted_text(["a: ConnectionError"])

        class Down:
            status_code = 503
            headers = {}
            text = ""

            def close(self):
                pass
        clock = A._ChainClock()
        assert A._net_only_now() is False                      # cleared at construction
        with mock.patch.object(A, "_upstream_chat", lambda *a, **k: Down()):
            for pid, model in clock.walk(CHAIN3):
                clock.dispatch(pid, {"model": model, "messages": []}, False)
        assert clock._net_local == 0 and A._net_only_now() is False
        assert A._chain_exhausted_text(["nvidia: HTTP 503", "glm: HTTP 503"]) == \
            "All providers failed: nvidia: HTTP 503; glm: HTTP 503"
