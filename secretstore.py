"""AES-256-GCM for the provider keys sitting in config.json.

WHAT THIS DOES AND DOES NOT PROTECT, stated plainly because the honest version
is less impressive than the acronym and worth knowing before relying on it:

The master key lives in secret.key, next to config.json, readable by the same
user. So this is NOT protection against someone who can already read your home
directory as you -- nothing stored locally can be, short of a passphrase you
type on every start, which for a background hub means never starting unattended.

It IS protection against the ways these keys actually leak, all of which move
config.json somewhere its permissions do not follow:

  * a backup, a sync folder, a support bundle, a zip sent to someone helping;
  * a screenshot or a screen share of a config file;
  * `grep -r "sk-" ~` finding two dozen live provider keys in one file;
  * the settings export, which is a deliberate act, but on a file whose contents
    people do not always reread before sharing;
  * on WINDOWS specifically, the 0600 that config.py sets is a POSIX-only call
    and does nothing at all -- so on this machine plaintext keys in config.json
    had no file-permission protection to begin with.

An attacker who copies BOTH files gets the keys. One who copies the config,
which is the common case, does not.

The dependency is optional. If `cryptography` is missing the hub keeps working
with plaintext keys and says so through `available()` -- a gateway that will not
start because an encryption library is absent has traded a small risk for a
total outage.
"""
import base64
import os
import stat

try:                                            # optional, by design
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:                               # noqa: BLE001
    AESGCM = None
    _HAVE_CRYPTO = False

# Version-tagged so the format can change without guessing at what a stored
# value is. Anything without this prefix is plaintext, from before encryption
# or from a hand-edited file, and is read as-is.
PREFIX = "enc.v1:"

_KEY_BYTES = 32                                 # AES-256
_NONCE_BYTES = 12                               # GCM standard

_cached_key = [None]


def available():
    """Whether encryption can be performed at all."""
    return _HAVE_CRYPTO


def key_path(config_path):
    """secret.key, beside the config it protects."""
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "secret.key")


def _restrict(path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)      # 0600 where it means something
    except OSError:
        pass


def load_or_create_key(config_path):
    """The master key, generated on first use. None when unavailable."""
    if not _HAVE_CRYPTO:
        return None
    if _cached_key[0] is not None:
        return _cached_key[0]
    path = key_path(config_path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) == _KEY_BYTES:
            _cached_key[0] = raw
            return raw
        # A truncated or padded key file cannot decrypt anything that was
        # written with the real one. Refuse rather than generate a new key over
        # the top of it, which would silently destroy every stored secret.
        raise ValueError("secret.key is %d bytes, expected %d" % (len(raw), _KEY_BYTES))
    except FileNotFoundError:
        pass
    except OSError:
        return None
    raw = os.urandom(_KEY_BYTES)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # O_EXCL: if two hub processes start at once, exactly one key file is
    # created and the loser reads the winner's rather than overwriting it with
    # a key that cannot decrypt what the winner has already written.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) != _KEY_BYTES:
            return None
        _cached_key[0] = raw
        return raw
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return None
    _restrict(path)
    _cached_key[0] = raw
    return raw


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value, config_path):
    """Encrypt one secret. Returns it UNCHANGED when that is not possible --
    a hub that cannot encrypt must still be able to save your keys."""
    if not isinstance(value, str) or not value or is_encrypted(value):
        return value
    key = load_or_create_key(config_path)
    if not key:
        return value
    try:
        nonce = os.urandom(_NONCE_BYTES)
        blob = nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
        return PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception:                                        # noqa: BLE001
        return value


def decrypt(value, config_path):
    """Decrypt one secret; pass plaintext through untouched.

    Returns None for a value that IS encrypted but cannot be read -- a lost or
    replaced secret.key. None rather than the ciphertext, so the caller drops a
    dead key and asks for it again, instead of sending "enc.v1:..." to a
    provider as if it were an API key and reporting an authentication failure
    that no amount of re-entering the right key would fix."""
    if not is_encrypted(value):
        return value
    key = load_or_create_key(config_path)
    if not key:
        return None
    try:
        blob = base64.b64decode(value[len(PREFIX):].encode("ascii"))
        return AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:],
                                   None).decode("utf-8")
    except Exception:                                        # noqa: BLE001
        return None


def reset_cache():
    """Test hook: forget the loaded master key."""
    _cached_key[0] = None
