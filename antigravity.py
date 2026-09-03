"""Antigravity IDE: detect it, and say honestly how far the hub can connect it.

ASKED 2026-09-03: "why he dont detect antigravity to connect inside of it?" and
then "i want my hub to be able with simple connect button to do it -- or if
really need kilocode so should write it in the card of what should be done".

So the first job was to find out which of those two it is. MEASURED, not assumed:

  * Antigravity IDE 1.107.0 is installed, and `antigravity-ide.cmd --help` lists
    ONLY the VS Code launcher flags (--diff, --merge, --goto, --new-window).
    There is no headless `exec` mode, so it can never be an /agent backend the
    way codex/claude/opencode are.
  * Its own agent extension declares exactly five settings
    (marketplaceExtensionGalleryServiceURL, marketplaceGalleryItemURL,
    searchMaxWorkspaceFileCount, enableCursorImportCursor,
    persistentLanguageServer). No provider, no baseUrl, no apiKey. Its bundle
    has no SERVER_URL/BASE_URL/API_URL/ENDPOINT override either: it is
    Codeium-derived and Google-authenticated.

    => the BUILT-IN agent cannot be pointed at this hub. Not "not implemented":
       there is no seam to point.

  * `--install-extension` and `--list-extensions` ARE supported, and the IDE's
    gallery is Open VSX (product.json: https://open-vsx.org/vscode/gallery),
    where kilocode.kilo-code, saoudrizwan.claude-dev and
    RooVeterinaryInc.roo-cline all resolve.

    => an OpenAI-compatible agent extension inside Antigravity CAN use the hub,
       and installing it really is one click.

What the extension's own provider settings are NOT is writable from out here:
they live in state.vscdb (the IDE's SQLite state), not settings.json -- the
Antigravity User/settings.json on this machine holds only
cline.modelSettings.o3Mini.reasoningEffort, roo-cline.allowedCommands and
kilo-code.allowedCommands, i.e. no baseUrl/apiKey keys at all. Writing to a
running IDE's state database to fake a "Connect" button would risk corrupting
its state for a step that takes fifteen seconds by hand.

So this module does the two things that are real -- detect, and install -- and
the card says plainly which last step is the user's. A Connect button that
silently did nothing would be worse than no button.
"""
import json
import os
import shutil
import subprocess
import sys

# Marketplace ids VERIFIED against the Open VSX API on 2026-09-03 (the gallery
# Antigravity itself points at), not guessed from the settings keys:
#   kilocode.kilo-code          7.5.9   Kilo Code: AI Coding Agent
#   saoudrizwan.claude-dev      4.1.17  Cline
#   RooVeterinaryInc.roo-cline  3.54.0  Roo Code
AGENT_EXTENSIONS = [
    {
        "id": "kilocode.kilo-code",
        "name": "Kilo Code",
        "recommended": True,
        "settings_path": "Kilo Code panel -> gear -> Providers -> OpenAI Compatible",
    },
    {
        "id": "saoudrizwan.claude-dev",
        "name": "Cline",
        "recommended": False,
        "settings_path": "Cline panel -> gear -> API Provider -> OpenAI Compatible",
    },
    {
        "id": "RooVeterinaryInc.roo-cline",
        "name": "Roo Code",
        "recommended": False,
        "settings_path": "Roo Code panel -> gear -> Providers -> OpenAI Compatible",
    },
]

_DEFAULT_EXTENSION = "kilocode.kilo-code"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _bin_candidates():
    """Where the launcher lives. It is NOT on PATH on this machine, which is why
    the generic `shutil.which` CLI detector never saw Antigravity at all."""
    out = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or _home("AppData", "Local")
        progs = os.environ.get("ProgramFiles") or r"C:\Program Files"
        for root in (os.path.join(local, "Programs"), progs):
            for folder in ("Antigravity IDE", "Antigravity"):
                out.append(os.path.join(root, folder, "bin", "antigravity-ide.cmd"))
                out.append(os.path.join(root, folder, "bin", "antigravity.cmd"))
    elif sys.platform == "darwin":
        for app in ("Antigravity IDE.app", "Antigravity.app"):
            out.append(os.path.join("/Applications", app, "Contents", "Resources",
                                    "app", "bin", "antigravity-ide"))
    else:
        for root in ("/usr/share", "/opt"):
            for folder in ("antigravity-ide", "antigravity"):
                out.append(os.path.join(root, folder, "bin", "antigravity-ide"))
    return out


def find_binary():
    """Absolute path to the launcher, or None."""
    for cand in _bin_candidates():
        if os.path.isfile(cand):
            return cand
    for name in ("antigravity-ide", "antigravity"):
        found = shutil.which(name)
        if found:
            return found
    return None


def user_dir():
    """The IDE's config root (holds User/settings.json), or None."""
    if sys.platform == "win32":
        roaming = os.environ.get("APPDATA") or _home("AppData", "Roaming")
        cands = [os.path.join(roaming, "Antigravity"),
                 os.path.join(roaming, "Antigravity IDE")]
    elif sys.platform == "darwin":
        cands = [_home("Library", "Application Support", "Antigravity"),
                 _home("Library", "Application Support", "Antigravity IDE")]
    else:
        cands = [_home(".config", "Antigravity"), _home(".config", "antigravity-ide")]
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


def extensions_dir():
    """product.json says dataFolderName is ".antigravity-ide", so extensions land
    in ~/.antigravity-ide/extensions -- NOT ~/.antigravity, which also exists on
    this machine and holds something else entirely."""
    for name in (".antigravity-ide", ".antigravity"):
        d = _home(name, "extensions")
        if os.path.isdir(d):
            return d
    return None


def _ids_from_manifest(ext_dir):
    """extensions.json is the IDE's own index and is authoritative."""
    path = os.path.join(ext_dir, "extensions.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    ids = []
    for row in data:
        ident = (row or {}).get("identifier") or {}
        ext_id = ident.get("id")
        if ext_id:
            ids.append(ext_id)
    return ids


def _ids_from_folders(ext_dir):
    """Fallback: folders are "<publisher>.<name>-<version>[-<target>]"."""
    ids = []
    try:
        entries = os.listdir(ext_dir)
    except OSError:
        return ids
    for name in entries:
        if not os.path.isdir(os.path.join(ext_dir, name)) or "." not in name:
            continue
        # strip the trailing -<semver>[-target]: split on the first '-' that is
        # followed by a digit, so a hyphenated extension name survives.
        head = name
        for i, ch in enumerate(name):
            if ch == "-" and i + 1 < len(name) and name[i + 1].isdigit():
                head = name[:i]
                break
        ids.append(head)
    return ids


def installed_extension_ids():
    """Read from disk rather than shelling out to `--list-extensions`: that boots
    a whole Electron process and takes seconds, and this runs on every render of
    the Connect page."""
    ext_dir = extensions_dir()
    if not ext_dir:
        return []
    ids = _ids_from_manifest(ext_dir)
    if ids is None:
        ids = _ids_from_folders(ext_dir)
    return ids


def _match(ext_id, installed):
    """Marketplace ids are case-insensitive (RooVeterinaryInc vs rooveterinaryinc
    -- the folder on disk is lowercased, the manifest is not)."""
    low = ext_id.lower()
    return any((i or "").lower() == low for i in installed)


def detect():
    """Everything the card needs, in one dict."""
    binary = find_binary()
    installed_ids = installed_extension_ids() if binary else []
    agents = [{
        "id": e["id"],
        "name": e["name"],
        "recommended": e["recommended"],
        "settings_path": e["settings_path"],
        "installed": _match(e["id"], installed_ids),
    } for e in AGENT_EXTENSIONS]
    return {
        "installed": bool(binary),
        "path": binary,
        "user_dir": user_dir(),
        "extensions_dir": extensions_dir(),
        "agents": agents,
        "agent_ready": any(a["installed"] for a in agents),
        "default_extension": _DEFAULT_EXTENSION,
        # The honest part. The built-in agent has no provider seam at all, so
        # the card must never imply the hub can capture it.
        "builtin_agent_connectable": False,
    }


def install_extension(ext_id=None, timeout=180):
    """One click, for the one step that genuinely is one click.

    Returns (ok, message). Never raises: the caller is an HTTP route."""
    ext_id = ext_id or _DEFAULT_EXTENSION
    if not any(e["id"].lower() == ext_id.lower() for e in AGENT_EXTENSIONS):
        return False, "Unknown extension: %s" % ext_id
    binary = find_binary()
    if not binary:
        return False, "Antigravity IDE not found on this machine."
    try:
        out = subprocess.run(
            [binary, "--install-extension", ext_id, "--force"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "Install timed out after %ds. Antigravity may be downloading it still." % timeout
    except Exception as exc:                                       # noqa: BLE001
        return False, "Could not run Antigravity: %s" % exc
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()
        return False, (tail[-1] if tail else "Install failed (exit %d)." % out.returncode)
    # `--install-extension` reports "successfully installed" / "already installed"
    # on stdout; show its own words rather than a claim of our own.
    said = (out.stdout or "").strip().splitlines()
    return True, (said[-1] if said else "Installed %s." % ext_id)
