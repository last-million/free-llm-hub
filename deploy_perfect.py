"""Make a freshly built project start by itself -- "deploy perfect".

The preview (workspace.py) already knows how to start ONE package.json or ONE
app.py: one install, one start command, one port. Real multi-role apps that the
Build page and Multi runs produce are rarely that simple. The app that prompted
this module was a monorepo: an Express API (Postgres + Stripe) started from the
root and a SEPARATE Vite/React frontend in ``frontend/`` -- a frontend install
and BUILD the old preview never ran (it installs the root ``node_modules``
only). It also needed a ``.env`` and a database. So every Multi run re-did the
deployment plumbing by hand and the hub only ever ADOPTED a hand-started server.

This module is the PURE brain for doing that automatically:

* ``analyze(project_dir)`` -- the layout (single app, npm/pnpm workspaces, a
  conventional frontend+backend pair, docker compose), the EXTRA steps the
  preview must run after its own root install (a declared sub-project's install
  with lifecycle scripts OFF, a frontend build), the server directory, whether
  a database is needed and a one-line summary.
* ``env_seed_plan(example_text)`` -- KEY=VALUE lines of a ``.env.example`` turned
  into a ``.env`` with SAFE LOCAL defaults.
* ``deploy_check(url, probe=...)`` -- poll a running preview over HTTP.

SECURITY MODEL (2026-10-10 review). The preview runs the project's own code BY
DESIGN -- its start command, its root install scripts, its build -- and only on
an explicit start. What this module adds is limited so a hostile or careless
project cannot make the hub act outside it:

* every path it reads is checked first: a symlink, a junction / mount point or
  an app-exec alias is refused, and so is anything whose realpath is not inside
  the project's realpath (component-wise, normcase); reads are capped and only
  regular files are read (``safe_read_text``);
* sub-projects are ONLY the ones the root DECLARES (package.json
  ``workspaces``, ``pnpm-workspace.yaml``) or the exact conventional pairs
  ``frontend/``+``backend/``, ``client/``+``server/``, ``web/``+``api/`` --
  never anything found by scanning; hidden, linked, node_modules, vendor,
  examples and fixtures folders are never entered;
* a sub-project install runs with lifecycle scripts OFF (``--ignore-scripts``,
  yarn berry ``--mode=skip-build``); python sub-projects are never installed
  here (pip runs build code); the root install keeps today's behaviour;
* ``.env`` is written by the caller with an exclusive create, never over an
  existing file or link (see workspace._deploy_seed_env).

Stdlib only; nothing here starts a process or binds a port, and every public
function is wrapped so it NEVER raises.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time

# Caps on what is ever read.
ENV_EXAMPLE_MAX_BYTES = 64 * 1024
PKG_JSON_MAX_BYTES = 512 * 1024
TEXT_MAX_BYTES = 256 * 1024
ENV_LINE_MAX = 1000
ENV_KEYS_MAX = 300
MEMBERS_MAX = 24

# Folder names that are never a sub-project and never entered.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "vendor", "examples", "example", "fixtures", "fixture", "__fixtures__",
    "samples", "sample", "test", "tests", "__tests__", "e2e", "docs",
    "dist", "build", "out", ".next", ".nuxt", "coverage", "logs",
    "tmp", "temp", "public", "static", "assets",
}

# The only sub-project pairs recognised without a declaration.
_PAIRS = (("frontend", "backend"), ("client", "server"), ("web", "api"))

_PROJECT_MARKERS = ("package.json", "requirements.txt", "pyproject.toml",
                    "app.py", "main.py", "manage.py", "server.py")

# package.json deps that mean "a frontend that must be BUILT".
_FRONTEND_DEP_RE = re.compile(
    r"\b(vite|next|nuxt|react-scripts|@vitejs|@angular|svelte|@sveltejs|"
    r"vue-cli-service|parcel|gatsby|astro|remix|@craco)\b", re.I)

_BUILD_OUTPUT_DIRS = ("dist", "build", "out", ".next", ".nuxt")

_DB_DRIVERS = (
    (re.compile(r"\b(better-sqlite3|sqlite3|aiosqlite)\b", re.I), "sqlite", True),
    (re.compile(r"\b(pg|postgres|postgresql|psycopg2?|asyncpg)\b", re.I), "postgres", False),
    (re.compile(r"\b(mysql2?|mariadb)\b", re.I), "mysql", False),
    (re.compile(r"\b(mongodb|mongoose|pymongo|motor)\b", re.I), "mongodb", False),
)

# Reparse tags that REDIRECT (a link of some kind). Other reparse kinds (cloud
# placeholders, dedup) are real files; the realpath check still applies.
_LINK_TAGS = {
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    getattr(stat, "IO_REPARSE_TAG_APPEXECLINK", 0x8000001B),
}


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


def is_link(path):
    """True for a symlink, a junction / mount point, an app-exec alias, or a
    reparse point whose kind cannot be read. Never raises."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    try:
        if os.path.isjunction(path):
            return True
    except (AttributeError, OSError, ValueError):
        pass
    attrs = getattr(st, "st_file_attributes", 0) or 0
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        tag = getattr(st, "st_reparse_tag", None)
        if tag is None or tag in _LINK_TAGS:
            return True
    return False


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def inside(child_real, root_real):
    """Component-wise: is `child_real` the root itself or under it."""
    try:
        c, r = _norm(child_real), _norm(root_real)
        return os.path.commonpath([c, r]) == r
    except (ValueError, TypeError):
        return False


def safe_path(path, root_real, *, realpath=None, is_link_fn=None):
    """`path`'s realpath when it is not a link and resolves inside
    `root_real`, else None. Never raises."""
    realpath = realpath or os.path.realpath
    is_link_fn = is_link_fn or is_link
    try:
        if is_link_fn(path):
            return None
        real = realpath(path)
    except Exception:                                            # noqa: BLE001
        return None
    return real if inside(real, root_real) else None


def safe_read_text(path, root_real, max_bytes, *, realpath=None, is_link_fn=None):
    """Read a REGULAR file inside the project: ``(text, None)`` or
    ``(None, why)``. Refuses links, paths resolving outside `root_real`,
    non-regular files, files that change identity between check and open, and
    files bigger than `max_bytes`. Never raises."""
    if safe_path(path, root_real, realpath=realpath, is_link_fn=is_link_fn) is None:
        return None, "a link, or outside the project"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, "unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        try:
            lst = os.lstat(path)
        except OSError:
            return None, "unreadable"
        if st.st_ino and lst.st_ino and (st.st_ino, st.st_dev) != (lst.st_ino, lst.st_dev):
            return None, "changed while being read"
        if st.st_size > max_bytes:
            return None, "larger than %d KB" % max(1, max_bytes // 1024)
        chunks, total = [], 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                return None, "larger than %d KB" % max(1, max_bytes // 1024)
            chunks.append(block)
        return b"".join(chunks).decode("utf-8", errors="replace"), None
    except OSError:
        return None, "unreadable"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _safe_dir(path, root_real, realpath, is_link_fn):
    try:
        return os.path.isdir(path) and safe_path(
            path, root_real, realpath=realpath, is_link_fn=is_link_fn) is not None
    except Exception:                                            # noqa: BLE001
        return False


def _plain_file(path, is_link_fn):
    try:
        return os.path.isfile(path) and not is_link_fn(path)
    except Exception:                                            # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# .env seeding
# --------------------------------------------------------------------------- #

_THIRD_PARTY_TOKENS = {
    "stripe", "sendgrid", "twilio", "aws", "ses", "s3", "openai", "anthropic",
    "supabase", "mailgun", "paypal", "smtp", "google", "github", "gitlab",
    "slack", "cloudinary", "firebase", "sentry", "datadog", "mailchimp",
    "recaptcha", "mapbox", "algolia",
}
_INTERNAL_PREFIX_TOKENS = {
    "jwt", "session", "cookie", "signing", "csrf", "encryption", "refresh",
    "access", "auth", "app",
}
_SECRET_NOUNS = {"secret", "token", "key", "salt", "pepper"}
_DB_PASSWORD_RE = re.compile(r"(db|database|postgres|mysql|mongo)[-_ ]?pass", re.I)
_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _key_tokens(key):
    # Whole tokens: `\b` is no boundary across "_" and a substring test would
    # read "ses" inside "SESSION".
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", key or "") if t}


def env_seed_plan(example_text, *, secret_factory=None):
    """KEY=VALUE lines of a ``.env.example`` body -> a ``.env`` body.

    ONLY well-formed KEY=VALUE lines are copied (comments, blank lines, junk,
    control characters and over-long lines are dropped; at most ENV_KEYS_MAX
    keys). Random values ONLY for the project's own signing secrets; every
    external service key keeps the example's placeholder (never fabricated); a
    bare DB password gets a local default; the rest is kept verbatim. Returns
    ``{"content", "secrets", "placeholders", "notes"}``; never raises."""
    if secret_factory is None:
        secret_factory = lambda: secrets.token_urlsafe(36)      # noqa: E731
    text = example_text if isinstance(example_text, str) else ""
    try:
        return _env_seed_plan(text, secret_factory)
    except Exception:                                            # noqa: BLE001
        return {"content": "", "secrets": [], "placeholders": [], "notes": []}


def _env_seed_plan(text, secret_factory):
    out, gen, ph, seen = [], [], [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > ENV_LINE_MAX or _CTRL_RE.search(line):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key in seen:
            continue
        if len(seen) >= ENV_KEYS_MAX:
            break
        seen.add(key)
        out.append("%s=%s" % (key, _env_value_for(key, val, secret_factory, gen, ph)))
    notes = []
    if gen:
        notes.append("generated a random local value for %d internal secret(s): %s"
                     % (len(gen), ", ".join(gen)))
    if ph:
        notes.append("left %d external key(s) as placeholders -- set real values "
                     "to enable them: %s" % (len(ph), ", ".join(ph)))
    return {"content": ("\n".join(out) + "\n") if out else "", "secrets": gen,
            "placeholders": ph, "notes": notes}


def _env_value_for(key, example_value, secret_factory, gen, ph):
    toks = _key_tokens(key)
    if toks & _THIRD_PARTY_TOKENS:
        ph.append(key)
        return example_value
    if toks & _SECRET_NOUNS and (
            "secret" in toks or "salt" in toks or (toks & _INTERNAL_PREFIX_TOKENS)):
        gen.append(key)
        return secret_factory()
    if _DB_PASSWORD_RE.search(key):
        return example_value or "postgres"
    if key.upper() == "NODE_ENV" and not example_value:
        return "development"
    return example_value


# --------------------------------------------------------------------------- #
# Package managers
# --------------------------------------------------------------------------- #


def pm_for(sub, project_dir):
    """The package manager a (sub-)project uses, from its lockfile (own, then
    the root's). Existence checks only."""
    for d in (sub, project_dir):
        try:
            if os.path.isfile(os.path.join(d, "pnpm-lock.yaml")):
                return "pnpm"
            if (os.path.isfile(os.path.join(d, "bun.lockb"))
                    or os.path.isfile(os.path.join(d, "bun.lock"))):
                return "bun"
            if os.path.isfile(os.path.join(d, "yarn.lock")):
                return ("yarn-berry" if os.path.isfile(os.path.join(d, ".yarnrc.yml"))
                        else "yarn")
            if os.path.isfile(os.path.join(d, "package-lock.json")):
                return "npm"
        except Exception:                                        # noqa: BLE001
            continue
    return "npm"


def pm_exe_name(pm):
    return "yarn" if pm == "yarn-berry" else (pm or "npm")


def pm_args(pm, kind):
    """argv after the executable. Installs ALWAYS run with lifecycle scripts
    off; None for an unknown pair."""
    if kind == "install":
        return {"npm": ["install", "--ignore-scripts"],
                "pnpm": ["install", "--ignore-scripts"],
                "yarn": ["install", "--ignore-scripts"],
                "yarn-berry": ["install", "--mode=skip-build"],
                "bun": ["install", "--ignore-scripts"]}.get(pm)
    if kind == "build":
        return ["run", "build"] if pm in ("npm", "pnpm", "yarn", "yarn-berry",
                                         "bun") else None
    return None


# --------------------------------------------------------------------------- #
# Layout analysis
# --------------------------------------------------------------------------- #


def analyze(project_dir, *, realpath=None, is_link_fn=None):
    """Describe how to make ``project_dir`` deploy. Never raises.

    ``steps`` is the EXTRA work after the preview's own root install, in
    order: ``{"kind": "install"|"build", "pm", "dir", "label", "timeout"}``."""
    realpath = realpath or os.path.realpath
    is_link_fn = is_link_fn or is_link
    try:
        return _analyze(project_dir, realpath, is_link_fn)
    except Exception:                                            # noqa: BLE001
        return _empty_plan(project_dir)


def _empty_plan(project_dir):
    return {
        "layout": "single",
        "project_dir": project_dir,
        "server_dir": project_dir,
        "server_kind": None,
        "serves_frontend": True,
        "members": [],
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


def _json(text):
    if not text:
        return None
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except Exception:                                            # noqa: BLE001
        return None


def _pnpm_globs(text):
    """The `packages:` list of a pnpm-workspace.yaml (a tiny, line-based read:
    no YAML library, nothing evaluated)."""
    out, in_pkgs = [], False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            in_pkgs = line.strip().rstrip(":") == "packages"
            continue
        item = line.strip()
        if in_pkgs and item.startswith("-"):
            out.append(item[1:].strip().strip("'\""))
    return out


def _expand_globs(project_dir, root_real, globs, realpath, is_link_fn):
    """Declared workspace patterns -> member dirs. Only "dir" and "dir/*" are
    understood; negations, "**" and other glob syntax are ignored; nothing
    hidden, linked, outside the project or in _SKIP_DIRS is ever entered."""
    members = []

    def ok_name(name):
        return name and not name.startswith(".") and name.lower() not in _SKIP_DIRS

    for g in globs:
        if not isinstance(g, str):
            continue
        g = g.strip().replace("\\", "/").strip("/")
        if not g or g.startswith("!") or "**" in g or os.path.isabs(g):
            continue
        parts = g.split("/")
        star = parts[-1] == "*"
        base = parts[:-1] if star else parts
        if not base or any(p in ("", ".", "..") or "*" in p or "?" in p or "[" in p
                           or not ok_name(p) for p in base):
            continue
        base_dir = os.path.join(project_dir, *base)
        if not _safe_dir(base_dir, root_real, realpath, is_link_fn):
            continue
        if not star:
            candidates = [base_dir]
        else:
            try:
                names = sorted(os.listdir(base_dir))
            except OSError:
                names = []
            candidates = [os.path.join(base_dir, n) for n in names if ok_name(n)]
        for c in candidates:
            if c in members or not _safe_dir(c, root_real, realpath, is_link_fn):
                continue
            if _plain_file(os.path.join(c, "package.json"), is_link_fn):
                members.append(c)
            if len(members) >= MEMBERS_MAX:
                return members
    return members


def _declared_members(project_dir, root_real, root_pkg, realpath, is_link_fn):
    """(layout, member dirs): the sub-projects the ROOT declares, or one exact
    conventional pair. Nothing found by scanning."""
    globs = []
    if isinstance(root_pkg, dict):
        ws = root_pkg.get("workspaces")
        if isinstance(ws, list):
            globs += ws
        elif isinstance(ws, dict) and isinstance(ws.get("packages"), list):
            globs += ws["packages"]
    text, _ = safe_read_text(os.path.join(project_dir, "pnpm-workspace.yaml"),
                             root_real, TEXT_MAX_BYTES, realpath=realpath,
                             is_link_fn=is_link_fn)
    if text:
        globs += _pnpm_globs(text)
    if globs:
        return "workspaces", _expand_globs(project_dir, root_real, globs,
                                           realpath, is_link_fn)
    for fe, be in _PAIRS:
        f, b = os.path.join(project_dir, fe), os.path.join(project_dir, be)
        if not (_safe_dir(f, root_real, realpath, is_link_fn)
                and _safe_dir(b, root_real, realpath, is_link_fn)):
            continue
        if all(any(_plain_file(os.path.join(d, m), is_link_fn) for m in _PROJECT_MARKERS)
               for d in (f, b)):
            return "monorepo", [f, b]
    return None, []


def _classify(sub, root_real, realpath, is_link_fn, project_dir):
    text, _ = safe_read_text(os.path.join(sub, "package.json"), root_real,
                             PKG_JSON_MAX_BYTES, realpath=realpath, is_link_fn=is_link_fn)
    pkg = _json(text)
    if isinstance(pkg, dict):
        deps = {}
        for field in ("dependencies", "devDependencies"):
            if isinstance(pkg.get(field), dict):
                deps.update(pkg[field])
        depblob = " ".join(str(k) for k in deps.keys())
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        is_fe = bool(_FRONTEND_DEP_RE.search(depblob)) and bool(scripts.get("build"))
        built = True
        if is_fe:
            built = any(_safe_dir(os.path.join(sub, d), root_real, realpath, is_link_fn)
                        and _nonempty(os.path.join(sub, d)) for d in _BUILD_OUTPUT_DIRS)
        return {"dir": sub, "stack": "node", "deps": depblob,
                "has_node_modules": os.path.isdir(os.path.join(sub, "node_modules")),
                "scripts": {k: scripts.get(k) for k in ("dev", "start", "build", "serve")},
                "is_frontend": is_fe, "built": built,
                "pm": pm_for(sub, project_dir)}
    if any(_plain_file(os.path.join(sub, m), is_link_fn) for m in _PROJECT_MARKERS[1:]):
        return {"dir": sub, "stack": "python", "deps": "", "has_node_modules": False,
                "scripts": {}, "is_frontend": False, "built": True, "pm": None}
    return None


def _nonempty(d):
    try:
        return bool(os.listdir(d))
    except OSError:
        return False


def _analyze(project_dir, realpath, is_link_fn):
    project_dir = os.path.abspath(project_dir)
    plan = _empty_plan(project_dir)
    if not os.path.isdir(project_dir):
        return plan
    root_real = realpath(project_dir)

    def read(path, cap):
        return safe_read_text(path, root_real, cap, realpath=realpath,
                              is_link_fn=is_link_fn)[0]

    root_pkg = _json(read(os.path.join(project_dir, "package.json"), PKG_JSON_MAX_BYTES))
    compose = any(_plain_file(os.path.join(project_dir, n), is_link_fn) for n in
                  ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                   "compose.yaml"))

    kind, member_dirs = _declared_members(project_dir, root_real, root_pkg,
                                          realpath, is_link_fn)
    members = [m for m in (_classify(d, root_real, realpath, is_link_fn, project_dir)
                           for d in member_dirs) if m]
    frontends = [m for m in members if m["is_frontend"]]
    backends = [m for m in members if not m["is_frontend"]]
    plan["members"] = [_rel(project_dir, m["dir"]) for m in members]
    plan["layout"] = "compose" if compose else (kind or "single")

    plan["server_dir"], plan["server_kind"] = _pick_server(
        project_dir, root_pkg, backends, is_link_fn)

    root_has_deps = isinstance(root_pkg, dict) and bool(root_pkg.get("dependencies"))
    steps = []
    if kind == "monorepo":
        # A pair's members are installed here (scripts OFF) -- except the
        # server's own folder (today's install() handles the run dir) and a
        # backend the ROOT already covers (a sub-install would shadow the
        # root's properly built native packages).
        for m in frontends + backends:
            if m["stack"] != "node" or m["has_node_modules"]:
                continue
            if os.path.normcase(m["dir"]) == os.path.normcase(plan["server_dir"]):
                continue
            if not m["is_frontend"] and root_has_deps and \
                    os.path.normcase(plan["server_dir"]) == os.path.normcase(project_dir):
                continue
            steps.append({"kind": "install", "pm": m["pm"], "dir": m["dir"],
                          "label": "%s install, scripts off (%s)"
                          % (pm_exe_name(m["pm"]), _rel(project_dir, m["dir"])),
                          "timeout": "install"})
    # npm/pnpm workspaces: the root install covers every member; only builds.
    for m in frontends:
        if not m["built"] and m["scripts"].get("build"):
            steps.append({"kind": "build", "pm": m["pm"], "dir": m["dir"],
                          "label": "%s run build (%s)"
                          % (pm_exe_name(m["pm"]), _rel(project_dir, m["dir"])),
                          "timeout": "build"})
    plan["steps"] = steps
    plan["frontends"] = [_rel(project_dir, m["dir"]) for m in frontends]
    plan["serves_frontend"] = _server_serves_frontend(
        plan["server_dir"], frontends, root_real, realpath, is_link_fn)

    ex = None
    for d in (project_dir, plan["server_dir"]):
        cand = os.path.join(d, ".env.example")
        if _plain_file(cand, is_link_fn) and safe_path(
                cand, root_real, realpath=realpath, is_link_fn=is_link_fn):
            ex = cand
            break
    needs = bool(ex) and not os.path.lexists(os.path.join(os.path.dirname(ex), ".env"))
    plan["env"] = {"needs_seed": needs, "example_path": ex}

    plan["db"] = _db_plan(project_dir, root_pkg, backends, read)
    plan["notes"] = _plan_notes(plan)
    plan["summary"] = summary(plan)
    return plan


def _pick_server(project_dir, root_pkg, backends, is_link_fn):
    if isinstance(root_pkg, dict):
        scripts = root_pkg.get("scripts") if isinstance(root_pkg.get("scripts"), dict) else {}
        if any(scripts.get(s) for s in ("start", "dev", "serve")):
            return project_dir, "node"
    for m in backends:
        if m["stack"] == "node" and any(m["scripts"].get(x) for x in ("start", "dev", "serve")):
            return m["dir"], "node"
    for m in backends:
        if m["stack"] == "python":
            return m["dir"], "python"
    for entry in ("app.py", "main.py", "server.py", "manage.py"):
        if _plain_file(os.path.join(project_dir, entry), is_link_fn):
            return project_dir, "python"
    return project_dir, ("node" if isinstance(root_pkg, dict) else None)


def _server_serves_frontend(server_dir, frontends, root_real, realpath, is_link_fn):
    if not frontends:
        return True
    for name in ("public", "static", "dist", "build", "www"):
        d = os.path.join(server_dir, name)
        if _safe_dir(d, root_real, realpath, is_link_fn) and _nonempty(d):
            return True
    return False


def _db_plan(project_dir, root_pkg, backends, read):
    blob = ""
    if isinstance(root_pkg, dict):
        for field in ("dependencies", "devDependencies"):
            if isinstance(root_pkg.get(field), dict):
                blob += " " + " ".join(str(k) for k in root_pkg[field].keys())
    for m in backends:
        blob += " " + m.get("deps", "")
    blob += " " + (read(os.path.join(project_dir, "requirements.txt"), TEXT_MAX_BYTES) or "")
    envex = read(os.path.join(project_dir, ".env.example"), ENV_EXAMPLE_MAX_BYTES) or ""
    engine, local_ok = "none", True
    for rx, name, ok in _DB_DRIVERS:
        if rx.search(blob) or rx.search(envex):
            engine, local_ok = name, ok
            break
    if engine == "none" and re.search(
            r"\b(DATABASE_URL|DB_HOST|POSTGRES|MONGO_URL|MYSQL)\b", envex, re.I):
        engine, local_ok = "unknown", False
    note = None
    if engine in ("postgres", "mysql", "mongodb", "unknown"):
        dbname = _env_value_of(envex, "DB_NAME") or _env_value_of(envex, "POSTGRES_DB")
        createdb = (" -- e.g. install it locally and `createdb %s`" % dbname) if (
            engine == "postgres" and dbname and re.match(r"^[\w.-]{1,63}$", dbname)) else ""
        note = ("needs a %s database; none is provisioned locally, so the app may "
                "run in degraded mode%s, or point DATABASE_URL at a hosted database"
                % (engine if engine != "unknown" else "external", createdb))
    elif engine == "sqlite":
        note = "uses SQLite -- no external database needed"
    return {"required": engine not in ("none", "sqlite"), "engine": engine,
            "local_ok": local_ok, "note": note}


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
        bits.append("workspaces monorepo (%d member%s)" % (
            len(plan.get("members") or []), "" if len(plan.get("members") or []) == 1 else "s"))
    elif layout == "compose":
        bits.append("Docker Compose app")
    nsteps = len(plan.get("steps") or [])
    if nsteps:
        bits.append("%d extra setup step(s) on start" % nsteps)
    if plan.get("env", {}).get("needs_seed"):
        bits.append(".env will be seeded from .env.example on start")
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
# Deploy check: confirm a STARTED preview answers over HTTP.
# --------------------------------------------------------------------------- #


def deploy_check(url, *, probe, now=None, sleep=None, deadline=25.0, interval=1.0):
    """Poll ``url`` until the server answers or ``deadline`` seconds pass.

    ``probe(url)`` returns an HTTP status (int) when it answered, None / raises
    when it is not up yet. ANY status line counts as alive. Returns
    ``{"ok", "status", "url", "waited", "error"}``; never raises."""
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
