"""Make a freshly built project start by itself -- "deploy perfect".

The preview (workspace.py) already knows how to start ONE package.json or ONE
app.py: one install, one start command, one port. Real multi-role apps that the
Build page and Multi runs produce are rarely that simple. The app that prompted
this module was a monorepo: an Express API in ``backend/`` (Postgres + Stripe)
and a SEPARATE Vite/React frontend in ``frontend/`` -- two installs and a
frontend BUILD step, none of which the old ``install()`` ran (it only installs
the root ``node_modules``). It also needed a ``.env`` and a database. So every
Multi run re-did the deployment plumbing by hand (create ``.env``, build the
frontend, patch the server to serve it, start it detached, curl it) and the hub
only ever ADOPTED the hand-started server -- it never started the app on its
own. Five runs in a row rebuilt "the frontend shell" from scratch.

This module is the PURE brain for doing that automatically and generically:

* ``analyze(project_dir)`` -- read the layout (single app, monorepo with
  web+api, npm workspaces, docker compose, python web) and return the extra
  install / build steps the preview must run beyond the root install, which
  directory the server runs from, whether a database is needed and whether a
  local fallback exists, and a one-line human summary.
* ``env_seed_plan(example_text)`` -- turn a ``.env.example`` into a ``.env`` with
  SAFE LOCAL defaults: random values ONLY for the project's own internal signing
  secrets (JWT/session/cookie), real third-party keys left as the example's
  obvious placeholder (never fabricated), a local database default, PORT kept.
* ``deploy_check(url, probe=...)`` -- after the server is up, poll it over HTTP
  until it answers (any status line = alive) or a deadline passes, so the
  conversation gets the URL or the exact error instead of a silent "not
  running".

It is pure and stdlib-only: every filesystem and clock side is an injected
callable with an ``os``/``time`` default, nothing here starts a process or binds
a port (workspace.py owns that, with its PID/port kill rules and timeouts), and
every public function is wrapped so it NEVER raises -- a broken project must
degrade to "nothing extra to do", never crash a preview.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time

# --------------------------------------------------------------------------- #
# Small safe wrappers. Every public entry point funnels its filesystem access
# through these so a weird path or an unreadable file is "absent", never an
# exception out of this module.
# --------------------------------------------------------------------------- #


def _os_isdir(p):
    try:
        return os.path.isdir(p)
    except Exception:                                            # noqa: BLE001
        return False


def _os_isfile(p):
    try:
        return os.path.isfile(p)
    except Exception:                                            # noqa: BLE001
        return False


def _os_listdir(p):
    try:
        return list(os.listdir(p))
    except Exception:                                            # noqa: BLE001
        return []


def _os_read_text(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:                                            # noqa: BLE001
        return None


def _read_json_with(read_text, path):
    raw = read_text(path)
    if not raw:
        return None
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except Exception:                                            # noqa: BLE001
        return None


# Directory names that are never a sub-application to install.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", ".next", ".nuxt", "coverage", "logs", "log",
    ".cache", "tmp", "temp", "public", "static", "assets", "docs", ".idea",
    ".vscode", "test-results", "playwright-report", ".turbo", ".parcel-cache",
}

# A sub-directory whose name says "this is the browser app" / "this is the API".
_FRONTEND_NAMES = ("frontend", "client", "web", "webapp", "ui", "www", "site", "app")
_BACKEND_NAMES = ("backend", "server", "api", "service", "services")

# package.json deps that mean "this is a frontend that must be BUILT before a
# server can serve it".
_FRONTEND_DEP_RE = re.compile(
    r"\b(vite|next|nuxt|react-scripts|@vitejs|@angular|svelte|@sveltejs|"
    r"vue-cli-service|parcel|gatsby|astro|remix|@craco)\b", re.I)

# Where a frontend build lands; if one of these holds files, the build is done.
_BUILD_OUTPUT_DIRS = ("dist", "build", "out", ".next", ".nuxt", "public/build")

# Database drivers in a package.json / requirements and what they mean.
_DB_DRIVERS = (
    (re.compile(r"\b(better-sqlite3|sqlite3|aiosqlite)\b", re.I), "sqlite", True),
    (re.compile(r"\b(pg|postgres|postgresql|psycopg2?|asyncpg)\b", re.I), "postgres", False),
    (re.compile(r"\b(mysql2?|mariadb)\b", re.I), "mysql", False),
    (re.compile(r"\b(mongodb|mongoose|pymongo|motor)\b", re.I), "mongodb", False),
)

# Keys are tokenised on non-alphanumerics (JWT_SECRET -> {jwt, secret}) and
# matched on WHOLE tokens -- `\b` is no boundary across an underscore (both
# sides are word characters), and a substring test would read "ses" inside
# "SESSION". So a token set, not a regex over the raw key.

# An EXTERNAL service: never fabricate a value, keep the example's placeholder
# so the feature stays disabled until the owner fills it in.
_THIRD_PARTY_TOKENS = {
    "stripe", "sendgrid", "twilio", "aws", "ses", "s3", "openai", "anthropic",
    "supabase", "mailgun", "paypal", "smtp", "google", "github", "gitlab",
    "slack", "cloudinary", "firebase", "sentry", "datadog", "mailchimp",
    "recaptcha", "mapbox", "algolia",
}
# A token that says "this is one of the project's OWN signing secrets" --
# safe to generate a random local value for.
_INTERNAL_PREFIX_TOKENS = {
    "jwt", "session", "cookie", "signing", "csrf", "encryption", "refresh",
    "access", "auth", "app",
}
_SECRET_NOUNS = {"secret", "token", "key", "salt", "pepper"}

_DB_PASSWORD_RE = re.compile(r"(db|database|postgres|mysql|mongo)[-_ ]?pass", re.I)


def _key_tokens(key):
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", key or "") if t}


# --------------------------------------------------------------------------- #
# .env seeding
# --------------------------------------------------------------------------- #


def env_seed_plan(example_text, *, secret_factory=None):
    """Turn a ``.env.example`` body into a ``.env`` body with safe local defaults.

    Returns ``{"content": str, "secrets": [key...], "placeholders": [key...],
    "notes": [str...]}`` and never raises. ``secret_factory()`` supplies each
    generated secret (default: a 48-char url-safe random string); inject a
    deterministic one in tests.

    The rule, in one line: generate randomness ONLY for the project's own
    internal signing secrets; keep the example's obvious placeholder for every
    external service; give a local database a working local default; keep
    everything else (PORT, HOST, localhost DB coordinates) exactly as the author
    wrote it.
    """
    if secret_factory is None:
        secret_factory = lambda: secrets.token_urlsafe(36)      # noqa: E731
    text = example_text if isinstance(example_text, str) else ""
    try:
        return _env_seed_plan(text, secret_factory)
    except Exception:                                            # noqa: BLE001
        # Fail closed: an unreadable example yields no file, never a crash.
        return {"content": "", "secrets": [], "placeholders": [], "notes": []}


def _env_seed_plan(text, secret_factory):
    out_lines = []
    gen_secrets = []
    placeholders = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        key, _, value = line.partition("=")
        key_s = key.strip()
        val = value.strip()
        new = _env_value_for(key_s, val, secret_factory, gen_secrets, placeholders)
        out_lines.append("%s=%s" % (key_s, new))
    content = "\n".join(out_lines)
    if content and not content.endswith("\n"):
        content += "\n"
    notes = []
    if gen_secrets:
        notes.append("generated a random local value for %d internal secret(s): %s"
                     % (len(gen_secrets), ", ".join(gen_secrets)))
    if placeholders:
        notes.append("left %d external key(s) as placeholders -- set real values "
                     "to enable them: %s" % (len(placeholders), ", ".join(placeholders)))
    return {"content": content, "secrets": gen_secrets,
            "placeholders": placeholders, "notes": notes}


def _env_value_for(key, example_value, secret_factory, gen_secrets, placeholders):
    toks = _key_tokens(key)
    # An external service key keeps its placeholder, whatever else it matches.
    if toks & _THIRD_PARTY_TOKENS:
        placeholders.append(key)
        return example_value
    # One of the project's own signing secrets: "secret" alone is enough; a bare
    # "key"/"token" needs an internal prefix (so API_KEY stays a placeholder,
    # JWT_SIGNING_KEY does not).
    if toks & _SECRET_NOUNS and (
            "secret" in toks or "salt" in toks or (toks & _INTERNAL_PREFIX_TOKENS)):
        gen_secrets.append(key)
        return secret_factory()
    if _DB_PASSWORD_RE.search(key):
        # A local default that matches the usual local database password so the
        # app connects to a developer's own database if one is running.
        return example_value or "postgres"
    if key.upper() == "NODE_ENV" and not example_value:
        return "development"
    # PORT, HOST, DB_HOST=localhost, feature flags, URLs: keep the author's value.
    return example_value


# --------------------------------------------------------------------------- #
# Layout analysis
# --------------------------------------------------------------------------- #


def analyze(project_dir, *, read_text=None, isfile=None, isdir=None, listdir=None):
    """Describe how to make ``project_dir`` deploy, generically. Never raises.

    Returns a plan dict (see ``_empty_plan`` for the shape). ``steps`` is the
    EXTRA work the preview must do beyond its own root install -- sub-application
    installs and frontend builds, in order -- each an abstract record the caller
    turns into argv (deploy_perfect does not know a platform's npm shim)::

        {"kind": "npm-install"|"npm-build"|"pip-install"|"venv",
         "dir": <abspath>, "label": <str>, "timeout": "install"|"build"|"setup"}
    """
    read_text = read_text or _os_read_text
    isfile = isfile or _os_isfile
    isdir = isdir or _os_isdir
    listdir = listdir or _os_listdir
    try:
        return _analyze(project_dir, read_text, isfile, isdir, listdir)
    except Exception:                                            # noqa: BLE001
        return _empty_plan(project_dir)


def _empty_plan(project_dir):
    return {
        "layout": "single",
        "project_dir": project_dir,
        "server_dir": project_dir,
        "server_kind": None,
        "serves_frontend": True,       # a single app serves itself
        "steps": [],
        "env": {"needs_seed": False, "example_path": None},
        "db": {"required": False, "engine": "none", "local_ok": True, "note": None},
        "frontends": [],
        "notes": [],
        "summary": "",
    }


def _rel(base, path):
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except Exception:                                            # noqa: BLE001
        return path


def _has_build_output(sub, isdir, listdir):
    for name in _BUILD_OUTPUT_DIRS:
        d = os.path.join(sub, *name.split("/"))
        if isdir(d) and listdir(d):
            return True
    return False


def _classify_subapp(sub, read_text, isfile, isdir, listdir):
    """Return a sub-app record or None (not a runnable node/python app)."""
    pkg = _read_json_with(read_text, os.path.join(sub, "package.json"))
    if isinstance(pkg, dict):
        deps = {}
        for field in ("dependencies", "devDependencies"):
            if isinstance(pkg.get(field), dict):
                deps.update(pkg[field])
        depblob = " ".join(deps.keys())
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        is_frontend = bool(_FRONTEND_DEP_RE.search(depblob)) and bool(scripts.get("build"))
        return {
            "dir": sub,
            "stack": "node",
            "deps": depblob,
            "has_node_modules": isdir(os.path.join(sub, "node_modules")),
            "scripts": {k: scripts.get(k) for k in ("dev", "start", "build", "serve", "preview")},
            "is_frontend": is_frontend,
            "built": _has_build_output(sub, isdir, listdir) if is_frontend else True,
            "name": os.path.basename(sub).lower(),
        }
    for entry in ("requirements.txt", "pyproject.toml", "app.py", "main.py",
                  "manage.py", "server.py"):
        if isfile(os.path.join(sub, entry)):
            return {
                "dir": sub, "stack": "python", "deps": "",
                "has_node_modules": False, "scripts": {}, "is_frontend": False,
                "built": True, "name": os.path.basename(sub).lower(),
                "has_requirements": isfile(os.path.join(sub, "requirements.txt")),
            }
    return None


def _analyze(project_dir, read_text, isfile, isdir, listdir):
    project_dir = os.path.abspath(project_dir)
    plan = _empty_plan(project_dir)
    if not isdir(project_dir):
        return plan

    root_pkg = _read_json_with(read_text, os.path.join(project_dir, "package.json"))
    compose = any(isfile(os.path.join(project_dir, n)) for n in
                  ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                   "compose.yaml"))

    # Immediate sub-applications.
    subapps = []
    for name in sorted(listdir(project_dir)):
        if name in _SKIP_DIRS or name.startswith("."):
            continue
        sub = os.path.join(project_dir, name)
        if not isdir(sub):
            continue
        rec = _classify_subapp(sub, read_text, isfile, isdir, listdir)
        if rec:
            subapps.append(rec)

    frontends = [s for s in subapps if s.get("is_frontend")]
    backends = [s for s in subapps if not s.get("is_frontend")]
    workspaces = bool(isinstance(root_pkg, dict) and root_pkg.get("workspaces"))

    # Layout.
    if compose:
        plan["layout"] = "compose"
    elif workspaces:
        plan["layout"] = "workspaces"
    elif frontends and (backends or isinstance(root_pkg, dict)):
        plan["layout"] = "monorepo"
    elif len(subapps) >= 2:
        plan["layout"] = "monorepo"
    else:
        plan["layout"] = "single"

    # Which directory the SERVER runs from (what the preview should launch).
    plan["server_dir"], plan["server_kind"] = _pick_server(
        project_dir, root_pkg, backends, isfile)

    # Extra steps beyond the preview's own root install: install every sub-app
    # that has no deps yet, and BUILD every frontend that is not built. Order:
    # installs first (a build needs its own deps), frontends before backends so
    # a server that serves the built frontend finds it.
    steps = []
    for s in frontends + backends:
        if s["stack"] == "node" and not s["has_node_modules"]:
            steps.append({"kind": "npm-install", "dir": s["dir"],
                          "label": "npm install (%s)" % _rel(project_dir, s["dir"]),
                          "timeout": "install"})
        elif s["stack"] == "python" and s.get("has_requirements"):
            steps.append({"kind": "venv", "dir": s["dir"],
                          "label": "creating the venv (%s)" % _rel(project_dir, s["dir"]),
                          "timeout": "setup"})
            steps.append({"kind": "pip-install", "dir": s["dir"],
                          "label": "pip install -r requirements.txt (%s)"
                          % _rel(project_dir, s["dir"]), "timeout": "install"})
    for s in frontends:
        if not s["built"] and s["scripts"].get("build"):
            steps.append({"kind": "npm-build", "dir": s["dir"],
                          "label": "npm run build (%s)" % _rel(project_dir, s["dir"]),
                          "timeout": "build"})
    plan["steps"] = steps
    plan["frontends"] = [_rel(project_dir, s["dir"]) for s in frontends]

    # Does the server serve the built frontend itself, or is it a separate app?
    # We cannot read arbitrary server code purely, so: a single app serves
    # itself; a monorepo with a separate frontend only "serves" it when the
    # server dir CONTAINS that frontend's build output (e.g. public/). Otherwise
    # we flag it so the conversation knows to serve or run it.
    plan["serves_frontend"] = _server_serves_frontend(
        plan["server_dir"], frontends, isdir, listdir)

    # Environment.
    ex = None
    for cand in (os.path.join(project_dir, ".env.example"),
                 os.path.join(plan["server_dir"], ".env.example")):
        if isfile(cand):
            ex = cand
            break
    has_env = any(isfile(os.path.join(d, ".env"))
                  for d in (project_dir, plan["server_dir"]))
    plan["env"] = {"needs_seed": bool(ex and not has_env), "example_path": ex}

    # Database.
    plan["db"] = _db_plan(project_dir, root_pkg, backends, read_text, isfile)

    plan["notes"] = _plan_notes(plan)
    plan["summary"] = summary(plan)
    return plan


def _pick_server(project_dir, root_pkg, backends, isfile):
    if isinstance(root_pkg, dict):
        scripts = root_pkg.get("scripts") if isinstance(root_pkg.get("scripts"), dict) else {}
        if any(scripts.get(s) for s in ("start", "dev", "serve")):
            return project_dir, "node"
    for s in backends:
        if s["stack"] == "node" and any(s["scripts"].get(x) for x in ("start", "dev", "serve")):
            return s["dir"], "node"
    for s in backends:
        if s["stack"] == "python":
            return s["dir"], "python"
    if backends:
        return backends[0]["dir"], backends[0]["stack"]
    for entry, kind in (("app.py", "python"), ("main.py", "python"),
                        ("server.py", "python"), ("manage.py", "python")):
        if isfile(os.path.join(project_dir, entry)):
            return project_dir, kind
    return project_dir, "node" if isinstance(root_pkg, dict) else None


def _server_serves_frontend(server_dir, frontends, isdir, listdir):
    if not frontends:
        return True
    # Evidence that the server bundles the built UI: a public/ or static/ or
    # dist/ beside the server with files in it.
    for name in ("public", "static", "dist", "build", "www", "client/dist"):
        d = os.path.join(server_dir, *name.split("/"))
        if isdir(d) and listdir(d):
            return True
    return False


def _db_plan(project_dir, root_pkg, backends, read_text, isfile):
    blob = ""
    for pkg in [root_pkg] + [None]:
        if isinstance(pkg, dict):
            for field in ("dependencies", "devDependencies"):
                if isinstance(pkg.get(field), dict):
                    blob += " " + " ".join(pkg[field].keys())
    for s in backends:
        blob += " " + s.get("deps", "")
    req = _os_cat(read_text, os.path.join(project_dir, "requirements.txt"))
    blob += " " + (req or "")
    envex = _os_cat(read_text, os.path.join(project_dir, ".env.example")) or ""
    engine, local_ok = "none", True
    for rx, name, ok in _DB_DRIVERS:
        if rx.search(blob) or rx.search(envex):
            engine, local_ok = name, ok
            break
    if engine == "none":
        # .env hint without a driver listed.
        if re.search(r"\b(DATABASE_URL|DB_HOST|POSTGRES|MONGO_URL|MYSQL)\b", envex, re.I):
            engine, local_ok = "unknown", False
    required = engine not in ("none", "sqlite")
    note = None
    if engine in ("postgres", "mysql", "mongodb", "unknown"):
        dbname = _env_value_of(envex, "DB_NAME") or _env_value_of(envex, "POSTGRES_DB")
        createdb = (" -- e.g. install it locally and `createdb %s`" % dbname) if (
            engine == "postgres" and dbname) else ""
        note = ("needs a %s database; none is provisioned locally, so the app may "
                "run in degraded mode%s, or point DATABASE_URL at a hosted database"
                % (engine if engine != "unknown" else "external", createdb))
    elif engine == "sqlite":
        note = "uses SQLite -- no external database needed"
    return {"required": required, "engine": engine, "local_ok": local_ok, "note": note}


def _os_cat(read_text, path):
    try:
        return read_text(path)
    except Exception:                                            # noqa: BLE001
        return None


def _env_value_of(text, key):
    m = re.search(r"(?mi)^\s*%s\s*=\s*(.+?)\s*$" % re.escape(key), text or "")
    return m.group(1).strip() if m else None


def _plan_notes(plan):
    notes = []
    if plan["layout"] == "compose":
        notes.append("docker-compose.yml present: this app is designed to run "
                     "with `docker compose up` (Docker required)")
    if plan["frontends"] and not plan["serves_frontend"]:
        notes.append("separate frontend (%s): the hub builds it, but the API may "
                     "not serve it -- serve the build output from the API, or run "
                     "the frontend dev server separately"
                     % ", ".join(plan["frontends"]))
    if plan["db"]["note"]:
        notes.append(plan["db"]["note"])
    return notes


def summary(plan, status=None):
    """One plain line for the Build conversation / PROGRESS. Never raises."""
    try:
        return _summary(plan, status)
    except Exception:                                            # noqa: BLE001
        return ""


def _summary(plan, status):
    bits = []
    layout = plan.get("layout")
    if layout == "monorepo":
        fe = ", ".join(plan.get("frontends") or []) or "frontend"
        bits.append("Monorepo: API + separate frontend (%s)" % fe)
    elif layout == "workspaces":
        bits.append("npm workspaces monorepo")
    elif layout == "compose":
        bits.append("Docker Compose app")
    nsteps = len(plan.get("steps") or [])
    if nsteps:
        bits.append("%d extra setup step(s)" % nsteps)
    if plan.get("env", {}).get("needs_seed"):
        bits.append(".env seeded from .env.example (internal secrets random; "
                   "external keys left as placeholders)")
    db = plan.get("db", {})
    if db.get("required"):
        bits.append("DB: %s" % (db.get("note") or db.get("engine")))
    elif db.get("engine") == "sqlite":
        bits.append("DB: SQLite (local, no setup)")
    if status is not None:
        if status.get("running") and status.get("url"):
            bits.append("running at %s" % status["url"])
        elif status.get("error"):
            bits.append("not running: %s" % status["error"])
    return " | ".join(b for b in bits if b)


# --------------------------------------------------------------------------- #
# Deploy check: start happened elsewhere; here we confirm it answers over HTTP.
# --------------------------------------------------------------------------- #


def deploy_check(url, *, probe, now=None, sleep=None, deadline=25.0, interval=1.0):
    """Poll ``url`` until the server answers or ``deadline`` seconds pass.

    ``probe(url)`` returns an HTTP status code (int) when the server answered,
    or None / raises when it is not up yet. ANY status line counts as alive (a
    404 or 500 still means a server is listening -- the same rule the preview
    uses for a bound port). Returns ``{"ok", "status", "url", "waited",
    "error"}`` and never raises.
    """
    now = now or time.monotonic
    sleep = sleep or time.sleep
    start = now()
    last_err = None
    try:
        while True:
            try:
                status = probe(url)
            except Exception as exc:                             # noqa: BLE001
                status = None
                last_err = type(exc).__name__
            waited = round(now() - start, 2)
            if isinstance(status, int):
                return {"ok": True, "status": status, "url": url,
                        "waited": waited, "error": None}
            if now() - start >= deadline:
                return {"ok": False, "status": None, "url": url, "waited": waited,
                        "error": last_err or "no HTTP response before the deadline"}
            sleep(min(interval, max(0.0, deadline - (now() - start))))
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "status": None, "url": url, "waited": 0.0,
                "error": type(exc).__name__}
