# Calvoun hub on Android

Ship the hub as a side-loaded APK: Chaquopy runs the existing Python, Flask lives in a foreground service, the dashboard is a hardened WebView, and other apps on the phone talk to `http://127.0.0.1:8787` with a per-install key. Battery and security are release gates, not follow-ups. Every hub change below cites the desktop code it touches (`app.py` line numbers are at commit `807a4b3`).

## 1. Goal & scope

**Goal.** Same hub, same `config.json`, on a phone. A third-party app (any OpenAI/Anthropic/Gemini-compatible client) configured with `http://127.0.0.1:8787/v1` + the hub's local API key gets a completion routed across the user's free providers. The in-app dashboard covers Quick chat, Images, Providers, Routing, Quota, Usage, Tracking and Settings.

**In scope (unchanged code paths).** `/v1/*`, `/v1/messages`, `/v1beta/*`, the Ollama-compatible paths (opt-in, see Open questions), the memory summarizer (`_summarize_worker`, app.py:6794), quota/perfstats persistence, secretstore encryption at rest, the model catalog cache.

**Out of scope on the phone, and hard-disabled (not just hidden).** Build page (`#sec-agent`), workspace previews (`workspace.py`), agent CLIs (`agentic_chat.py`, `/api/agent/*`, `/api/clis/*`), swarm/crews (`swarm_windows.py`, `crews.py`, the two spawning MCP tools in `hub_mcp.py:110,156`), playwright MCP (`_start_playwright_mcp`, app.py:11663), codex catalog (app.py:919), subscription providers (`_sub_launcher`, app.py:4216), git/zip auto-update (app.py:22378-22840), desktop shortcut (app.py:11143), LAN/hotspot sharing. Updates ship only as a newly signed APK.

**Non-goals for v1.** Play Store listing, tablets, Android < 8.0, biometric re-auth for key reveal (phase 4), Keystore-wrapped master key (phase 4).

**Definition of done.** Fresh install, toggle "Gateway on", paste one provider key in the dashboard, paste the local key into another app, get a streamed completion; 8 h screen-off with the gateway on costs <= 0.5 % battery and 0 bytes of network.

## 2. Architecture

```
+------------------------------ one Android process ------------------------------+
| HubApp (Application)                                                            |
|   Python.start(AndroidPlatform) once; env vars set BEFORE any hub import        |
|                                                                                 |
| HubService (foreground, type=specialUse)        DashboardActivity               |
|   Thread "hub-serve": android_entry.serve(8787)   WebView -> http://127.0.0.1:8787 |
|   notification: state + Stop action               HttpOnly cookie hub_ct        |
|   PARTIAL_WAKE_LOCK only while a /v1 request      FLAG_SECURE, pause/resume     |
|   is in flight (HubBridge.setBusy from Python)                                  |
|                                                 ConnectActivity                 |
| Python (Chaquopy 3.12): app.py -> werkzeug        base URLs + local key copy    |
|   make_server("127.0.0.1", 8787, threaded=True)   (BiometricPrompt-gated)       |
+---------------------------------------------------------------------------------+
        ^ loopback TCP                                   ^ loopback TCP
   other phone apps: Bearer <local_api_key>        WebView: cookie hub_ct + X-Free-LLM-Hub
```

**Chaquopy.** One interpreter per process, started once in `Application.onCreate()`, never stopped. The hub is imported as flat modules (`import config`, `import app`) so the sources are copied by a Gradle task into `app/src/main/python/calvounhub/` (a package, listed in `extractPackages` so `templates/` and `static/` exist on disk for Flask's loader), and `android_entry.py` puts that folder first on `sys.path` before `import app`. Phase 0 confirms this layout; fallback is copying `templates/static` into `filesDir` at first run and pointing `app.template_folder` at it.

**Entry points the hub must expose** (survey 3, finding 1): the body of `if __name__ == "__main__":` (app.py:22984-23053) becomes `def serve(host=HOST, port=None)` and a `def stop()` that calls `_runtime_server[0].shutdown()` (the same call `_graceful_shutdown_worker` already makes at app.py:10957-10960). `serve()` returns when `serve_forever()` exits; the service then calls `stopSelf()`. Because the interpreter persists, restart-in-process is `stop()` then `serve()` again, so `_claim_single_instance()` (app.py:22921) gets a matching `_release_single_instance()`.

**Foreground service.** Started from the foreground (toggle or notification action) as Android 12+ requires. Type `specialUse`: `dataSync` is capped at 6 h per 24 h on Android 15 for targetSdk 35 and the OS would kill the gateway mid-day; `connectedDevice` needs a real peripheral. `specialUse` needs the `FOREGROUND_SERVICE_SPECIAL_USE` permission and a `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` manifest property; side-loading needs no Play review. The service owns the Python thread, the notification and the wake lock, and is `stopWithTask=false` so swiping the dashboard away does not kill the gateway.

**WebView.** Loads exactly one origin, `http://127.0.0.1:8787/`, only after the service has answered `GET /api/version` (token-exempt, app.py:10317). The control token reaches the page as an HttpOnly cookie set from Kotlin, never as HTML or a URL (section 4).

**Ports.** `HOST` stays the literal `"127.0.0.1"` (app.py:199). `PORT` (app.py:198) is passed as env by the service, default 8787 because that is what other apps will have typed in. On `EADDRINUSE` the service shows the error in the notification and stops; it does not auto-hop ports (clients would silently break).

**Env contract set by Kotlin before `import app`** (Chaquopy sets `HOME=filesDir`, `TMPDIR=cacheDir`; `android_entry.py` overrides `HOME` before importing):

| var | value | read by |
|---|---|---|
| `FREE_LLM_HUB_PLATFORM` | `android` | new `config.PLATFORM` / `config.IS_ANDROID` |
| `FREE_LLM_HUB_CONFIG` | `<noBackupFilesDir>/hub/config.json` | config.py:46-53 `_default_config_path`, secretstore.py:60-61 (secret.key beside it), config.py:1124 `state_dir()` |
| `FREE_LLM_HUB_MEMORY_DIR` | `<noBackupFilesDir>/hub/memory` | memory.py:55 |
| `HOME` | `<noBackupFilesDir>/hub` | every `expanduser("~")` caller (usage_history.py:23, image_history.py:29, agentic_history.py:67, app.py:1359, 3117) |
| `PORT` | `8787` | app.py:198 |
| `FREE_LLM_HUB_BUILD` | Gradle `versionName` | `_detect_hub_version` (app.py:10293) instead of `git rev-parse` |
| `AUTO_UPDATE` | `0` (belt and braces) | app.py:22378 |

## 3. Battery

Principle: with the gateway on and nobody talking to it, the Python side has **zero** timer wake-ups; the process blocks in `accept()`. Everything periodic on desktop is either removed, made lazy on the request path, or (if ever needed) moved to WorkManager. No wake lock is held except while a request is in flight.

### 3.1 What runs when the screen is off

| component | screen off, idle | in-flight request |
|---|---|---|
| werkzeug `serve_forever` thread | blocked on accept, 0 CPU | one thread per connection |
| Python background loops | none (all 4 idle loops removed, see 3.2) | none |
| WebView | `onPause()` + `pauseTimers()` freeze all JS timers | same (dashboard is not visible) |
| wake lock | none | `PARTIAL_WAKE_LOCK` tag `calvoun:inflight`, timeout `CHAT_READ_TIMEOUT` (300 s, app.py:202) + 30 s |
| network | 0 bytes | provider HTTPS only |
| notification | static "on, idle" | text "1 request in flight" (throttled to 1 update/s) |

### 3.2 What is removed or made lazy (survey 1)

| desktop behaviour | where | Android |
|---|---|---|
| auto-update loop, 30 s wake-ups, 5 h git/zip pull, `os.execv` | app.py:22811-22836, 22566, 22714, 23001 | never started; `_auto_update_enabled()` returns False |
| AA benchmark loop, 60 s wake-ups | app.py:1642-1673 | never started; `_aa_refresh_once` (app.py:1633) runs lazily from `/api/tracking` when `fetched_at` > 6 h **and** an AA key is set **and** a request just arrived |
| preview reaper, 60 s wake-ups + boot port sweep | workspace.py:1404-1420, app.py:23004-23013 | never started |
| vision heartbeat, 45 min | vision_status.py:131, app.py:23031 | never started; `status()` already recomputes on read (vision_status.py:97) |
| catalog warm-up: 47 HTTPS calls at boot, 48 threads | app.py:1010-1030, 811, 23032 | deferred until the first `GET /` or `/v1/models`; pool capped at 8 |
| `/api/tracking` re-sweeps 47 providers whenever the 600 s cache expires | app.py:9804-9822 via `_prefetch_auto_models` (1066), `MODEL_CACHE_TTL` (251) | read-only from the warm cache; `?refresh=1` behind an explicit button re-sweeps |
| swarm resume `Timer(20)`, CLI autoinstall (30 MB Node tarball), playwright, codex catalog, opencode repair, desktop shortcut | app.py:23012, 11792, 11663, 919, 985, 11112 | never started |

Result: after `serve()` starts on Android, `threading.enumerate()` shows the main thread, the serve thread and nothing else. A test asserts this (section 8, phase 1).

### 3.3 Dashboard timers (survey 1, client side)

Hard backstop: `DashboardActivity.onPause()` calls `webView.onPause(); webView.pauseTimers()`; `onResume()` calls `resumeTimers(); onResume()` and the page's existing `visibilitychange` handler does the catch-up fetch. On top of that, in `templates/index.html` when `HUB_PLATFORM === 'android'`:

- `setInterval(resync, 15000)` (12235) -> 30 000; `pollActivity` (12236) 3000 -> 10 000; `pollTracking` (12237) 8000 -> 30 000.
- `tick()` (4815) and `tickActivity()` (10600): start only while a quota countdown or an in-flight row exists, stop when none (a `startTicker()/stopTicker()` pair instead of two unconditional 1 s intervals).
- `refreshCount()` (8491): add `if (document.hidden) return;` and skip the `/api/workspace/running` fetch on Android.
- Boot pollers `/api/clis` (7145), `/api/mcp` (7502), `/api/workspace/running` (8494): early return on Android so their timers are never created.

### 3.4 Doze, App Standby, battery-optimisation exemption

- A foreground service keeps the process out of the cached/frozen state (Android 14 freezes cached apps) and out of App Standby buckets. That is all the gateway needs: loopback traffic only happens when **another app on the same phone is running**, which means the phone is awake.
- Therefore the app does **not** request `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` by default. One settings toggle, "Answer requests while the screen is off" (for Tasker-style background clients), explains why and only then fires `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`. Off by default.
- Start at boot: off by default. When on, `BootReceiver` starts the FGS (`specialUse` is on the list of types allowed from `BOOT_COMPLETED`).

### 3.5 WorkManager vs timers

Nothing periodic ships in v1. Rule for later: no `threading.Timer`, no `time.sleep` loops in Python on Android. Periodic work (an AA score refresh, a catalog re-sweep, cache pruning) goes through a `PeriodicWorkRequest` (>= 15 min) with `UNMETERED` + `BATTERY_NOT_LOW` constraints whose worker calls one idempotent Python function (`app.aa_refresh_once()` etc.) and returns.

### 3.6 Network wake-ups

- Boot: 1 request (`/api/version` from the service), 0 provider calls.
- First dashboard open: the deferred catalog warm-up (8 parallel connections max).
- Per chat turn: the provider call(s) + at most one memory-summarizer call (`_summarize_worker`, app.py:6794). Make the summarizer opt-out in Settings (default on, see Open questions for cellular).
- Idle: 0.

### 3.7 Budgets to measure (Battery Historian: `adb shell dumpsys batterystats --reset`, 8 h screen-off run, then `dumpsys batterystats`, `dumpsys netstats detail`)

| metric | target |
|---|---|
| partial wake locks held while `_runtime_active[0] == 0` | 0 |
| app-attributed wake-ups / alarms per hour, idle | 0 from Python; <= 2 total (system/WebView noise) |
| CPU time, 8 h idle | <= 60 s |
| battery, 8 h idle screen off, gateway on | <= 0.5 % |
| battery, dashboard open, screen on | <= 3 % / h (screen dominates) |
| network bytes, 8 h idle | 0 |
| RSS idle (Flask + requests + cryptography + Pillow + 23k-line module) | <= 120 MB |
| cold start to `/api/version` 200 | <= 4 s on a 2022 mid-range phone |

## 4. Security

### 4.1 Threat model

| id | threat | mitigation |
|---|---|---|
| T1 | another app on the phone silently uses the gateway (burns the user's quota, reads/sends prompts under the user's identity) | mandatory `local_api_key` on every `/v1`, `/v1/messages`, `/v1beta`, Ollama path (4.2) |
| T2 | another app drives the control API (`/api/*`), dumps provider keys via reveal/export | control token never rendered; HttpOnly cookie for the WebView; token rotated at every service start; reveal/export off on Android until re-auth ships (4.3) |
| T3 | lost/stolen phone, ADB/cloud backup restore onto another device | app-private `noBackupFilesDir`, `allowBackup=false`, extraction rules, `FLAG_SECURE`, no logcat banner (4.4, 4.6) |
| T4 | Wi-Fi / hotspot exposure, DNS rebinding | literal `127.0.0.1` bind, loopback Host/Origin checks kept, cleartext only to loopback via NSC, no LAN mode (4.7) |
| T5 | provider output rendered in the dashboard tries to script the page | server CSP nonce + `frame-ancestors 'none'` (app.py:8073-8115) unchanged; no JS bridge; HttpOnly token; single-origin WebView (4.5) |
| T6 | token holder reaches code-execution routes (CLI `Popen`, workspace spawns, self-update writing `_REPO_DIR`) | those routes 404 on Android at `before_request`; the threads are never started (section 5) |
| T7 | leakage through screenshots, recents thumbnail, clipboard, logs | `FLAG_SECURE`, `ClipData` marked sensitive + auto-clear, banner skipped, werkzeug access log at WARNING (4.6) |

### 4.2 Loopback exposure: decision (a), every request authenticated

**Decision: (a).** On Android every `/v1*` and Ollama request requires `Authorization: Bearer <local_api_key>` (or `x-api-key` / `x-goog-api-key` / `?key=` as `_guard_v1` already accepts, app.py:8198-8222), and every `/api/*` request requires the control token. Both secrets are generated at service start, before `make_server`; the Android build refuses to start if either is missing (fail closed).

**Why not (b), a per-app allowlist.** The server cannot learn which app opened a loopback TCP connection: `ConnectivityManager.getConnectionOwnerUid()` is restricted to the active VPN app and system-privileged callers, `/proc/net/tcp` (what `psutil.net_connections()` reads, workspace.py:1225-1273) is SELinux-denied to apps since Android 10, and TCP has no `SO_PEERCRED`. An allowlist would be enforced by nothing. A Unix domain socket would give peer UIDs but no OpenAI-compatible client can dial one. (a) is also the shape every LLM client already has: a base URL field and an API key field.

**Why (a) keeps the "other apps can use it" goal.** The user does exactly what they would do for any hosted API: paste a base URL and a key. **Pairing UX (ConnectActivity):**

1. Rows generated from `_connect_snippets()` (app.py:10270): OpenAI `http://127.0.0.1:8787/v1`, Anthropic `http://127.0.0.1:8787`, Gemini `http://127.0.0.1:8787/v1beta`, Ollama `http://127.0.0.1:8787` (each with a Copy button, no auth needed to copy a URL).
2. "Local API key" shown masked. **Copy key** first runs `BiometricPrompt` with `BIOMETRIC_STRONG | DEVICE_CREDENTIAL` (skipped only if the device has no lock screen at all, with a warning), then puts the key on the clipboard as `ClipData` with `EXTRA_IS_SENSITIVE` (Android 13+ hides it from the clipboard preview) and clears the clipboard after 60 s.
3. **Rotate key** calls the new `config.rotate_local_api_key()`; the screen explains that every paired app must be re-pasted.
4. A short "how to paste" hint per popular client type (OpenAI-compatible: base URL + key; Anthropic SDK: `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`).

Third-party apps never receive the control token; it is internal to the app and rotated at every service start.

### 4.3 Control token handling for the WebView

Today `index()` renders the token into the page (app.py:8710-8714 -> `var HUB_CONTROL_TOKEN = {{ control_token | tojson }}`, index.html:4224), contradicting config.py:1016-1024 and the comment at app.py:8058-8062. On Android any app can `GET /` and scrape it (survey 2, finding 1).

**Fix: HttpOnly, SameSite=Strict cookie set from Kotlin, never from the server.**

- `HubService`, after `serve()` is up: `token = py.getModule("config").callAttr("get_control_token").toString()`; `CookieManager.getInstance().setCookie("http://127.0.0.1:8787", "hub_ct=$token; Path=/; HttpOnly; SameSite=Strict"); flush()`. The WebView then sends it on every same-origin request, including `fetch` and `<img src>`.
- Server: new `_supplied_control_token()` used by both `_has_control_token` (app.py:8022-8030) and `_local_control_guard` (8065): header `X-Free-LLM-Hub-Token` first, then cookie `hub_ct` **only when `config.IS_ANDROID`**; `request.args.get("token")` accepted **only when not Android**. Everything else in the guard (loopback Host, loopback Origin, `X-Free-LLM-Hub: dashboard` on writes, app.py:8045-8056) stays, so the cookie cannot be replayed cross-site: the custom header forces a preflight the hub never grants.
- `index()` passes `control_token=None` on Android; the page's `api()` helper already only adds the header when `HUB_CONTROL_TOKEN` is truthy (index.html:4235), and the `?token=` on `/api/images/history/<id>` (6968) collapses to an empty string, so no JS change is needed for the token itself.
- Why not `shouldInterceptRequest` header injection: `WebResourceRequest` exposes no POST body and returning a `WebResourceResponse` breaks streamed `ReadableStream` chat. Why not `evaluateJavascript` after load: race with the page's first fetches, and the token stays readable by page JS (T5). The cookie wins on both.
- Rotate at every service start (`config.rotate_control_token()`), so anything scraped from a previous run is dead. The WebView cookie is reset at the same moment.
- `_has_control_token`'s "no token configured -> True" branch (app.py:8026) is unreachable on Android because `serve()` calls `ensure_control_token()` first, and a test asserts it.

**Provider-key reveal and export.** `api_provider_reveal_key` (app.py:9134) and `api_settings_export?sections=api_keys` (13643, plaintext because `load_config` decrypts, config.py:295-303) return `403 {"code":"reauth_required"}` on Android in v1, and the eye icon (index.html:5559-5575) plus the api_keys export option are hidden. Phase 4 adds the re-auth flow (page navigates to `calvoun://reauth`, `shouldOverrideUrlLoading` intercepts, `BiometricPrompt`, one-time 60 s nonce from `app.issue_sensitive_nonce()` handed back through `evaluateJavascript` as a `CustomEvent`, page retries with `X-Free-LLM-Hub-Sensitive`). Settings export without keys keeps working.

### 4.4 Storage

- All state under `Context.noBackupFilesDir/hub/`: `config.json` (0600, atomic writes config.py:645/674 now effective on ext4), `secret.key` (secretstore.py:60-73), `memory/`, histories, `aa_scores.json`, `test_cache.json`, `instance.lock`, quota state. Never `getExternalFilesDir`, never shared storage.
- Fix the two constants that ignore `FREE_LLM_HUB_CONFIG`: `AA_SCORE_CACHE_PATH` (app.py:1359) and `TEST_CACHE_PATH` (3117) -> `os.path.join(config.state_dir(), ...)`. `HOME` is also pointed at the same dir so the remaining `expanduser("~")` callers follow.
- `secretstore`: on Android a missing `cryptography` wheel must be fatal at import (`if config.IS_ANDROID and not _HAVE_CRYPTO: raise RuntimeError`), not the silent "every key unreadable" that the guarded import gives today.
- Phase 4: master key generated in Kotlin, wrapped by an `AndroidKeyStore` AES key, handed to Python as `FREE_LLM_HUB_SECRET_KEY_B64`; `secretstore.load_or_create_key` honours the env before touching `secret.key`. Rooted extraction then also needs the device.

### 4.5 WebView hardening

`setJavaScriptEnabled(true)`, `setDomStorageEnabled(true)` (the dashboard keeps only UI prefs in localStorage, index.html:18, 3769+), `setAllowFileAccess(false)`, `setAllowContentAccess(false)`, `setAllowFileAccessFromFileURLs(false)`, `setAllowUniversalAccessFromFileURLs(false)`, `setMixedContentMode(MIXED_CONTENT_NEVER_ALLOW)`, `setGeolocationEnabled(false)`, `setSupportMultipleWindows(false)`, no `addJavascriptInterface` anywhere, `setWebContentsDebuggingEnabled(BuildConfig.DEBUG)`. `shouldOverrideUrlLoading`: `http://127.0.0.1:<PORT>/...` loads in place, external `http(s)` opens via `Intent.ACTION_VIEW`, everything else is dropped; `onReceivedHttpAuthRequest` cancels; `setDownloadListener` drops downloads in v1. `CookieManager.setAcceptThirdPartyCookies(false)`. Clear the WebView cache on service stop. Server-side CSP, `X-Frame-Options DENY`, `no-store` on `/api` (app.py:8073-8115) stay untouched.

### 4.6 Backups, screenshots, logs, clipboard

- Manifest: `android:allowBackup="false"`, `android:dataExtractionRules="@xml/data_extraction_rules"` (API 31+, exclude `file` domain `.` for both `cloud-backup` and `device-transfer`), `android:fullBackupContent="@xml/backup_rules"` (API <= 30, same exclusion). `noBackupFilesDir` is excluded anyway; the rules are the second lock.
- `FLAG_SECURE` on `DashboardActivity` and `ConnectActivity` (no screenshots, blank recents thumbnail).
- `_print_banner` (app.py:22863) prints the control token; under Chaquopy stdout is logcat. Skip it on Android. `logging.getLogger("werkzeug").setLevel(WARNING)` in `android_entry.py` so per-poll access lines never reach logcat.
- Clipboard: `EXTRA_IS_SENSITIVE`, cleared after 60 s (4.2).

### 4.7 Network security config and LAN rules

`res/xml/network_security_config.xml`: `<base-config cleartextTrafficPermitted="false">` with default trust anchors, plus `<domain-config cleartextTrafficPermitted="true"><domain>127.0.0.1</domain><domain>localhost</domain></domain-config>`. This governs the WebView and any Android HTTP client; Python `requests` is outside it, so an Android test asserts every provider `base_url` in `providers.py` is `https://` (true today: zero `http://` in the file).

LAN mode rules: **none in v1.** `HOST` stays the compile-time literal (app.py:199), `serve()` asserts `host == "127.0.0.1"` on Android, no env var, no toggle. If a "share with my laptop" feature is ever wanted it is a separate project with TLS (self-signed, pinned per device), mandatory keys, per-device pairing, and still no token in HTML.

### 4.8 Key management summary

| secret | where | lifetime | rotation |
|---|---|---|---|
| control token | `config.json` (0600, app-private) + WebView cookie jar | one service run | every service start (`config.rotate_control_token()`) |
| local API key | `config.json` | until user rotates | user action in ConnectActivity |
| provider keys | `config.json`, AES-256-GCM via `secret.key` | user-managed | user edits; reveal/export off until phase 4 re-auth |
| `secret.key` | beside config, 0600 | install | phase 4: Keystore-wrapped |

## 5. Hub changes needed

| file | change | why |
|---|---|---|
| `config.py` (next to `_default_config_path`, 46-53) | `PLATFORM = (os.environ.get("FREE_LLM_HUB_PLATFORM") or "desktop").strip().lower()`; `IS_ANDROID = PLATFORM == "android"`; add `rotate_control_token()` and `rotate_local_api_key()` (clear then `ensure_*`) | single platform switch read once; rotation for 4.2/4.3 |
| `app.py` 22984-23053 | move the `__main__` body into `def serve(host=HOST, port=None)`; add `def stop()` (`_runtime_server[0].shutdown()`) and `_release_single_instance()`; keep `if __name__ == "__main__": serve()` | Chaquopy calls a function; stop must come from another thread; restart-in-process |
| `app.py` `serve()` start | on Android: `assert host == "127.0.0.1"`; `config.rotate_control_token()`; `config.ensure_local_api_key()`; raise if either empty | fail closed (T1/T2/T4) |
| `app.py` 8022-8030 `_has_control_token`, 8065 `_local_control_guard` | new `_supplied_control_token()`: header, then cookie `hub_ct` on Android; `args.get("token")` only off Android | token via cookie, never URL (4.3) |
| `app.py` 8198-8203 `_guard_v1` | `if not local_key: return 401 on Android` (defence in depth; `serve()` already ensured it) | no "open on localhost" on a phone (T1) |
| `app.py` 8710-8714 `index()` | `control_token=None if config.IS_ANDROID else config.ensure_control_token()`, add `platform=config.PLATFORM` | token out of HTML; dashboard hides desktop-only UI |
| `app.py` 22863 `_print_banner` | skip on Android | token in logcat (T7) |
| `app.py` 22378 `_auto_update_enabled`, 22830 `_start_auto_update`, 22840 `api_auto_update`, `_recover_interrupted_hub_transition` (called at 22987) | return False / return / 400 `unsupported_platform` / skip on Android | no git, no `os.execv`, no writes into the asset tree; updates = signed APK |
| `app.py` 11792 `_start_agent_cli_autoinstall`, 3905 `_ensure_npm` | early return on Android regardless of the `agent_cli_autoinstall` flag | 30 MB Node download that can never exec (W^X); a settings import can flip the flag back |
| `app.py` boot calls 23001 `_start_aa_refresh`, 23004-23013 `workspace.sweep_own_range()`/`start_reaper()`, 22999-23012 `swarm_windows.load()` + `Timer(20, _resume_interrupted_swarms)`, 23029 `_maybe_auto_create_desktop_shortcut`, 23031 `vision_status.start_heartbeat()`, 23033 `_repair_opencode_config`, 23034-23037 playwright + codex threads | wrap in `if not config.IS_ANDROID:` | zero idle threads (3.2) |
| `app.py` 1010 `_warm_catalogs_async`, 811 `_CATALOG_FETCH_WORKERS` | on Android do not call at boot; call once from `index()` / `/v1/models` guarded by a flag; `8 if config.IS_ANDROID else 48` | no 47-request burst on the radio at service start |
| `app.py` 9804 `api_tracking` | never trigger fetches (`no_fetch=True` through `_prefetch_auto_models`, 1066); `?refresh=1` re-sweeps | biggest hidden radio wake-up |
| `app.py` 1366 `_load_aa_cache`, 1633 `_aa_refresh_once` | lazy refresh from `/api/tracking` when stale > 6 h and key set | replaces the 60 s loop |
| `app.py` 1359 `AA_SCORE_CACHE_PATH`, 3117 `TEST_CACHE_PATH` | derive from `config.state_dir()` | follow `FREE_LLM_HUB_CONFIG` |
| `app.py` new `before_request` (register after `_local_control_guard`) | on Android return 404 for `/api/agent/`, `/api/workspace/`, `/api/swarm`, `/api/crews`, `/api/clis/`, `/api/mcp` writes, `/api/auto-update`, `/api/hub/desktop-shortcut` (11143), `/api/subscriptions`; 403 `reauth_required` for 9134 reveal and 13643 export with `api_keys` | routes must refuse, not just hide (T6) |
| `app.py` 360 `_enabled_keyed` | drop subscription providers (`_sub_launcher`, 4216) on Android | never advertise models that 404 |
| `app.py` 8125-8141 `_runtime_before` / `_runtime_request_done` | call `_activity_listener[0](count)` on 0->1 and 1->0; add `set_activity_listener(fn)` | wake lock only while a request is in flight |
| `app.py` 10293 `_detect_hub_version`, 10317 `api_version` | use `FREE_LLM_HUB_BUILD` when set; add `platform` and `features: {build, previews, clis, subscriptions, auto_update, desktop_shortcut}` all false on Android | no git on the phone; capabilities come from the server, not UA sniffing |
| `hub_mcp.py` 110, 156 | do not register `crew_run` / `swarm_windows_start` (and `crew_start`, 361) on Android | spawning tools |
| `workspace.py` `start_project` | return `{"ok": false, "error": "previews are not available on Android"}` on Android | belt and braces behind the 404 |
| `secretstore.py` guarded import | raise on Android when `cryptography` is missing; phase 4: honour `FREE_LLM_HUB_SECRET_KEY_B64` | silent key loss is worse than a crash |
| `templates/index.html` 2 | `<html lang="en" data-theme="light" data-platform="{{ platform }}">` | CSS hiding without a fork |
| `templates/index.html` 4224 | `var HUB_PLATFORM = {{ platform|tojson }};` beside `HUB_CONTROL_TOKEN` | JS gates |
| `templates/index.html` CSS | `html[data-platform="android"] [data-view="sec-agent"], [data-view="sec-subs"], #cx-sidebar-update, #hub-mode-switch, #hub-stop, #lifecycle-tools, #antigravity-card, #ollama-card, #mcp-list, #drawer-hub-mode-switch, #settings-update-check, #settings-update-status, .key-reveal-eye { display:none !important }` (ids from survey 3: 2607, 2614, 2640, 2691, 2703, 2721, 2736, 2740, 2779, 3503, 3623-3625, 5559) | hide Build, Subscriptions, update, CLI cards, reveal |
| `templates/index.html` JS | early return on Android in the pollers at 7145, 7502, 8494; redirect `/agent` and `/subscriptions` to `/chat` in the router; timers per 3.3; hide the desktop-shortcut checkbox in the stop modal (4571-4619) | dead ends on a 400 px screen; battery |
| `requirements-android.txt` (new) | pure-Python pins from `requirements.txt` (flask 3.0.3, requests 2.34.2, werkzeug, jinja2) + `tzdata`; native pins (`cryptography`, `cffi`, `Pillow`, `psutil`) set to versions present in the Chaquopy wheel index for the pinned Chaquopy release; no PyYAML | quota.py:270 `ZoneInfo` needs tzdata; a missing native pin fails the Gradle build loudly |
| `tests/` (new, run with `FREE_LLM_HUB_PLATFORM=android` on desktop CI) | `test_android_never_renders_the_token.py`, `test_android_v1_needs_a_key.py`, `test_android_has_no_idle_threads.py`, `test_android_hides_desktop_routes.py`, `test_android_reads_the_cookie_not_the_url.py`, `test_the_desktop_build_is_unchanged.py`, `test_every_provider_is_https.py` | release gates that run without a phone |

## 6. Android app skeleton

```
android/
  settings.gradle.kts            plugins: com.android.application, org.jetbrains.kotlin.android, com.chaquo.python
  build.gradle.kts
  gradle.properties              android.useAndroidX=true
  app/
    build.gradle.kts             minSdk 26, targetSdk 35, compileSdk 35; abiFilters arm64-v8a (+x86_64 in debug for the emulator)
                                 chaquopy { defaultConfig { version = "3.12"; pip { install("-r", "../../requirements-android.txt") }
                                            extractPackages("calvounhub") } }
                                 task syncHub(Copy): repo *.py allowlist + templates/ + static/ + free_models.txt -> src/main/python/calvounhub/ (+ empty __init__.py); preBuild dependsOn syncHub
    src/main/AndroidManifest.xml
    src/main/res/xml/network_security_config.xml, data_extraction_rules.xml, backup_rules.xml
    src/main/python/android_entry.py
    src/main/java/com/calvoun/hub/
      HubApp.kt                  Application: Os.setenv(...) for the env table in section 2, then Python.start(AndroidPlatform(this))
      HubService.kt              foreground service (specialUse): start python thread, notification, wake lock, stop
      HubBridge.kt               object with @JvmStatic setBusy(busy: Boolean) called from Python via the activity listener
      DashboardActivity.kt       WebView (4.5), cookie set before loadUrl, FLAG_SECURE, onPause/onResume timers
      ConnectActivity.kt         base URLs + key copy with BiometricPrompt (4.2)
      SettingsActivity.kt        start at boot, answer while screen off (exemption prompt), rotate keys, stop gateway, version
      BootReceiver.kt            RECEIVE_BOOT_COMPLETED -> HubService only if the toggle is on
```

**Manifest.** Permissions: `INTERNET`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `POST_NOTIFICATIONS`, `USE_BIOMETRIC`, `RECEIVE_BOOT_COMPLETED`; add `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` only if the screen-off toggle ships in that build. No storage, no camera. `<application android:allowBackup="false" android:dataExtractionRules=... android:fullBackupContent=... android:networkSecurityConfig="@xml/network_security_config">`. Service: `<service android:name=".HubService" android:exported="false" android:stopWithTask="false" android:foregroundServiceType="specialUse"><property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" android:value="Local LLM gateway serving an HTTP API on 127.0.0.1 to other apps while the user keeps it switched on"/></service>`. Activities `exported="false"` except the launcher.

**`android_entry.py`** (the only Python the app adds):

```python
import os, sys, logging, threading
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "calvounhub"))
os.environ["HOME"] = os.path.dirname(os.environ["FREE_LLM_HUB_CONFIG"])   # before any hub import
logging.getLogger("werkzeug").setLevel(logging.WARNING)
import config, app                                                        # noqa: E402
assert config.IS_ANDROID

def serve(port: int):
    from java import jclass
    bridge = jclass("com.calvoun.hub.HubBridge")
    app.set_activity_listener(lambda n: bridge.setBusy(n > 0))
    app.serve(port=port)                                                  # blocks until stop()

def stop():
    app.stop()

def control_token():
    return config.get_control_token()
```

**`HubService.kt` essentials.**

```kotlin
override fun onStartCommand(i: Intent?, f: Int, id: Int): Int {
    startForeground(NOTIF_ID, buildNotification("starting"), FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
    thread(name = "hub-serve") {
        try { Python.getInstance().getModule("android_entry").callAttr("serve", PORT) }
        finally { stopSelf() }                                   // serve() returned: dashboard Stop or stop()
    }
    waitForHealth("http://127.0.0.1:$PORT/api/version") { setCookieAndNotify() }
    return START_STICKY
}
override fun onDestroy() { Python.getInstance().getModule("android_entry").callAttr("stop"); releaseWakeLock() }

// HubBridge.setBusy(true)  -> wakeLock.acquire((300 + 30) * 1000L)   // CHAT_READ_TIMEOUT + margin
// HubBridge.setBusy(false) -> if (wakeLock.isHeld) wakeLock.release()
```

**Notification.** Channel `gateway`, `IMPORTANCE_LOW` (silent), ongoing. Title "Calvoun hub is on"; text "127.0.0.1:8787, idle" / "1 request in flight" / "port 8787 busy, tap to fix"; actions **Open** (DashboardActivity) and **Stop** (`stop()` then `stopSelf()`). Tapping the notification opens the dashboard.

## 7. Build & release

- **Gradle.** AGP 8.x, Kotlin 2.x, Chaquopy pinned to one release (16.x line); pin `python.version = "3.12"` to match the desktop interpreter. `buildPython` = the host's Python 3.12. Release build: `minifyEnabled false` (Chaquopy needs no R8 rules but keeps the build boring), `isDebuggable false`, `abiFilters arm64-v8a` only (drops APK size by half; add `x86_64` for emulator debug builds).
- **CI: `.github/workflows/android.yml`** (the repo has no workflows yet). Triggers: push to `main` (build + tests, upload unsigned debug APK as an artifact), tags `android-v*` (signed release attached to a GitHub Release). Jobs: (1) `pytest tests/test_android_*.py tests/test_the_desktop_build_is_unchanged.py` on ubuntu with `FREE_LLM_HUB_PLATFORM=android`, no phone needed; (2) `actions/setup-java` 17, `actions/setup-python` 3.12, `android-actions/setup-android`, gradle cache, `./gradlew :app:assembleRelease`, `actions/upload-artifact`. Secrets: `ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`; the workflow decodes the keystore to `$RUNNER_TEMP` and passes the path through `signingConfigs.release` env lookups. Keystore never committed.
- **Signing.** One release keystore generated once (`keytool -genkeypair -keyalg RSA -keysize 4096 -validity 10000`), stored offline plus in Actions secrets. Android only installs an update signed with the same key, so the APK signature is the whole update channel, which is why the in-app updater is off.
- **Side-load.** Users download the APK from the GitHub Release page, allow "Install unknown apps" for their browser once, install. Updates: install the newer APK over it (higher `versionCode`, same key). Later option: publish a static Obtainium/F-Droid-style repo for auto-checking, still signed by us.
- **Versioning.** `versionName = "<pyproject version>-android.<build>"` (today `0.5.7-android.1`), `versionCode = <run number>` from CI (monotonic). Gradle injects `versionName` into `BuildConfig` and the service passes it as `FREE_LLM_HUB_BUILD`, so `/api/version` and the dashboard's "what's new" show the APK build, not a git hash. Tag format `android-v0.5.7-1`.

## 8. Phased delivery

**Phase 0: spike (2-3 days).** Empty Android project + Chaquopy; `syncHub` copy task; `android_entry.serve()` on a thread; `adb forward tcp:8787 tcp:8787` then `curl 127.0.0.1:8787/api/version` from the PC. Acceptance: 200 on a real arm64 phone; list of import errors and missing wheels; measured cold start and idle RSS; answer to the `extractPackages`/templates question.

**Phase 1: hub refactor, desktop-safe (3-4 days).** Everything in section 5 except UI polish: platform switch, `serve()/stop()`, gating, fail-closed auth, cookie token, state paths, `/api/version` features, 404 routes, workers cap, tracking no-fetch, lazy warm/AA. Acceptance: full existing `pytest` green with the platform unset; the seven new tests green with `FREE_LLM_HUB_PLATFORM=android`; `threading.enumerate()` after `serve()` on a thread shows only main + server; `GET /` body does not contain the control token; `/v1/models` 401 without the key; `/api/agent/settings` 404.

**Phase 2: service + WebView (3-4 days).** `HubService`, notification, cookie, `DashboardActivity` hardening, pause/resume, `FLAG_SECURE`. Acceptance: dashboard loads over the WebView; a provider key can be added and a Quick chat streams; screenshot attempt is blocked; **Stop** from the notification ends the process's server thread within 5 s and `adb shell dumpsys activity services` shows the service gone; `dumpsys activity services` while on shows `foregroundServiceType=specialUse`; Build/Subscriptions/update UI not visible at 400 px.

**Phase 3: other apps + battery (3-4 days).** ConnectActivity with BiometricPrompt copy and rotate, wake lock via `HubBridge`, dashboard timer changes, tracking refresh button. Acceptance: a third-party OpenAI-compatible client with the pasted key gets a completion and gets 401 after "Rotate key"; 8 h screen-off Battery Historian run meets every budget in 3.7 (0 wake locks outside requests, 0 bytes, <= 0.5 %); wake lock appears in `dumpsys power` only during a long non-streaming request and is gone afterwards.

**Phase 4: hardening + release (3-5 days).** Re-auth nonce for reveal/export, Keystore-wrapped master key, boot-start toggle, screen-off exemption toggle, signed CI release, versioning. Acceptance: security checklist (4.1 table) walked item by item on a device; `adb backup` of the app yields no `config.json`/`secret.key`; a non-developer goes from install to first completion in a third-party app in under 3 minutes with only the ConnectActivity as guidance.

## 9. Open questions

1. **Chaquopy wheel index vs pins.** Which exact `cryptography`/`cffi`/`Pillow`/`psutil` versions exist for Python 3.12 in the pinned Chaquopy release; whether Pillow is needed at all on Android (if the Images view only proxies provider output, drop the largest wheel).
2. **Templates on disk.** Whether `extractPackages("calvounhub")` is enough for Flask's template/static loader, or whether the first-run copy fallback is needed (phase 0 answers).
3. **Ollama-compatible clients.** `_skips_control_gate` (app.py:8192) exempts Ollama-only paths from the control token and they then hit `_guard_v1`; most Ollama clients cannot send a bearer key. Default: Ollama wire off on Android; an explicit "allow keyless Ollama clients" toggle with a red warning is the only alternative, and it re-opens T1 for those paths.
4. **Memory summarizer on cellular.** One extra LLM call per turn (`_summarize_worker`, app.py:6794); keep default-on, or off when `ConnectivityManager` reports metered? Needs a hint from Kotlin to Python either way.
5. **Werkzeug vs waitress.** The dev server is fine for one user on loopback; if long streams or many parallel clients misbehave, `waitress` is pure Python and a drop-in.
6. **Desktop parity for the token.** The same HttpOnly-cookie design would fix the desktop's "never rendered" docstring lie too, but a desktop browser is not controlled by us; a separate decision.
7. **Play Store later.** `specialUse` needs a written justification at review; if refused, the fallback is `dataSync` with the 6 h/24 h cap on Android 15 and a re-start prompt, or a redesign around a bound service that only runs while a client app is bound.
8. **16 KB page-size native libraries** (required for new devices on Android 15+): confirm the Chaquopy release ships 16 KB-aligned wheels for the native pins.
