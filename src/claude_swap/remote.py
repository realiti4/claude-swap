"""Remote sync: share setup-token accounts through an encrypted git repository.

A remote is a git repository holding one encrypted file per shared account::

    claude-swap-remote.json      plain: format version, the scrypt parameters
                                 and salt for the passphrase, and an encrypted
                                 check value that tells a wrong passphrase
                                 apart from a damaged file
    accounts/<uuid4>.enc         one per account: AES-256-GCM over a JSON body
                                 (email label, alias, token, when and where it
                                 was last updated), bound to its file name

Only ``claude setup-token`` accounts are shared. They never refresh, so any
number of machines can hold the same token at once; a refreshing login would
be revoked on every machine except the one that refreshed last (measured: a
refresh kills the previous access token immediately).

Nothing polls. Every operation first replaces cswap's own clone with the
remote's current state (cswap owns the clone, so there is never a merge),
then applies its change, commits and pushes, and starts again from a fresh
fetch when another machine pushed first.

Accounts are matched by token (a SHA-256 of it, compared in memory), then
tracked by the entry's uuid, which the local account records as
``remotes: {<remote id>: <entry id>}`` in sequence.json. A renewed token keeps
its entry id, so it shows as "differs" rather than as a new account.

Secrets (passphrase, HTTPS token) live in the macOS Keychain, or elsewhere in
a 0600 file beside the clone -- the same protection cswap gives stored logins
on that platform. They never reach argv, commit messages or logs.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from claude_swap import oauth
from claude_swap.exceptions import (
    ClaudeSwitchError,
    PassphraseError,
    RemoteError,
    ValidationError,
)
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.models import get_timestamp, normalize_alias

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

FORMAT_VERSION = 1
MANIFEST_NAME = "claude-swap-remote.json"
ACCOUNTS_DIR = "accounts"
ENTRY_SUFFIX = ".enc"
CONFIG_NAME = "remotes.json"
CONFIG_SCHEMA = 1
AUTH_KINDS = ("default", "ssh-key", "https-token")
MIN_PASSPHRASE = 8
GIT_TIMEOUT_S = 120.0
PUSH_ATTEMPTS = 4

#: scrypt for new repositories (32 MiB, ~0.1 s), and the most a manifest may
#: ask for: the parameters come from the repository, so they are capped before
#: use or a crafted manifest could make every open allocate gigabytes.
KDF_DEFAULTS = {"name": "scrypt", "n": 2**15, "r": 8, "p": 1}
_KDF_LIMITS = {"n": 2**16, "r": 8, "p": 2}

_CHECK_PLAINTEXT = b"claude-swap remote v1"
_CHECK_AAD = b"claude-swap-remote:v1:check"
_SECRET_SERVICE = "claude-swap-remote"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Same pattern as ClaudeAccountSwitcher._validate_email: an imported label
# becomes part of a filename there, so it must pass the same gate.
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_SETUP_TOKEN_RE = re.compile(r"^sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{16,}$")
_BRANCH_RE = re.compile(r"^(?![-/.])(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![/.])$")
# Files a hosting service may create in a new repository ("initialize with a
# README"); anything else means the repository is not ours to write into.
_ALLOWED_FOREIGN = ("readme", "license", "licence", ".gitignore", ".gitattributes")

# A credential helper fed from the environment: the HTTPS token never reaches
# argv or .git/config, and resetting the helper list first keeps the user's
# own helpers from storing it.
_CREDENTIAL_HELPER = (
    "!f() { test \"$1\" = get && printf 'username=%s\\npassword=%s\\n' "
    "\"$CSWAP_GIT_USERNAME\" \"$CSWAP_GIT_PASSWORD\"; }; f"
)

# Row states, in display order.
IN_SYNC = "in-sync"
DIFFERS = "differs"
HERE_ONLY = "here-only"
REPO_ONLY = "repo-only"
SKIPPED = "skipped"
UNREADABLE = "unreadable"
NOT_SHAREABLE = "not-shareable"
_STATE_ORDER = {s: i for i, s in enumerate(
    (DIFFERS, HERE_ONLY, REPO_ONLY, IN_SYNC, SKIPPED, UNREADABLE, NOT_SHAREABLE)
)}
_REMOVED_NOTE = "removed from the repository"


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------


def _aesgcm():
    # Imported on first use: only remote sync needs it, and it is slow to load.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RemoteError(
            "Remote sync needs the 'cryptography' package, which comes with "
            "claude-swap; this install is missing it. Reinstall: "
            "uv tool install --force claude-swap (or pip install --force-reinstall claude-swap)."
        ) from None
    return AESGCM


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, what: str) -> bytes:
    if not isinstance(value, str):
        raise RemoteError(f"{what} is malformed")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise RemoteError(f"{what} is malformed") from None


def _kdf_params(kdf: object) -> tuple[bytes, int, int, int]:
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise RemoteError("the manifest names an unsupported key derivation")
    values = []
    for key in ("n", "r", "p"):
        v = kdf.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= _KDF_LIMITS[key]:
            raise RemoteError(f"the manifest's scrypt '{key}' is out of range")
        values.append(v)
    n, r, p = values
    if n < 2 or n & (n - 1):
        raise RemoteError("the manifest's scrypt 'n' is not a power of two")
    salt = _unb64(kdf.get("salt"), "the manifest salt")
    if len(salt) < 16:
        raise RemoteError("the manifest salt is too short")
    return salt, n, r, p


def derive_key(passphrase: str, kdf: object) -> bytes:
    salt, n, r, p = _kdf_params(kdf)
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        maxmem=128 * r * (n + p + 2) + (1 << 20), dklen=32,
    )


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> dict:
    nonce = os.urandom(12)
    return {
        "nonce": _b64(nonce),
        "ciphertext": _b64(_aesgcm()(key).encrypt(nonce, plaintext, aad)),
    }


def _unseal(key: bytes, blob: object, aad: bytes, what: str) -> bytes | None:
    """Plaintext, or None when the tag does not verify (wrong key or tampered)."""
    if not isinstance(blob, dict):
        raise RemoteError(f"{what} is malformed")
    nonce = _unb64(blob.get("nonce"), what)
    ciphertext = _unb64(blob.get("ciphertext"), what)
    if len(nonce) != 12:
        raise RemoteError(f"{what} is malformed")
    aesgcm = _aesgcm()
    from cryptography.exceptions import InvalidTag

    try:
        return aesgcm(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        return None


def check_new_passphrase(passphrase: str) -> None:
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValidationError(
            f"The passphrase must be at least {MIN_PASSPHRASE} characters."
        )


def new_manifest(passphrase: str) -> tuple[dict, bytes]:
    """A fresh manifest (new salt) and the key it describes."""
    check_new_passphrase(passphrase)
    kdf = {**KDF_DEFAULTS, "salt": _b64(os.urandom(16))}
    key = derive_key(passphrase, kdf)
    manifest = {
        "format": FORMAT_VERSION,
        "kdf": kdf,
        "check": _seal(key, _CHECK_PLAINTEXT, _CHECK_AAD),
    }
    return manifest, key


def unlock_manifest(manifest: object, passphrase: str) -> bytes:
    """The repository key, or PassphraseError when the passphrase is wrong."""
    if not isinstance(manifest, dict):
        raise RemoteError("the repository manifest is malformed")
    fmt = manifest.get("format")
    if fmt != FORMAT_VERSION:
        raise RemoteError(
            f"the repository uses remote format {fmt!r}; this cswap reads "
            f"format {FORMAT_VERSION}. Upgrade cswap on this machine."
        )
    key = derive_key(passphrase or "", manifest.get("kdf"))
    if _unseal(key, manifest.get("check"), _CHECK_AAD, "the manifest check") != _CHECK_PLAINTEXT:
        raise PassphraseError("Wrong passphrase for this repository.")
    return key


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------


def token_fingerprint(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_setup_token(token: str) -> str:
    token = (token or "").strip()
    if not _SETUP_TOKEN_RE.match(token):
        raise ValidationError(
            "That is not a setup-token. Remote sync shares only long-lived "
            "tokens from `claude setup-token` (sk-ant-oat…)."
        )
    return token


def _entry_aad(entry_id: str) -> bytes:
    return f"claude-swap-remote:v1:entry:{entry_id}".encode()


@dataclass(frozen=True)
class Entry:
    """One shared account, decrypted."""

    id: str
    email: str
    token: str
    alias: str = ""
    added_at: str = ""
    updated_at: str = ""
    updated_by: str = ""
    kind: str = "setup-token"

    @property
    def fingerprint(self) -> str:
        return token_fingerprint(self.token)

    @property
    def label(self) -> str:
        return f"{self.alias} ({self.email})" if self.alias else self.email


def seal_entry(key: bytes, entry: Entry) -> str:
    body = json.dumps({
        "id": entry.id,
        "kind": entry.kind,
        "email": entry.email,
        "alias": entry.alias,
        "token": entry.token,
        "addedAt": entry.added_at,
        "updatedAt": entry.updated_at,
        "updatedBy": entry.updated_by,
    }).encode("utf-8")
    blob = {"format": FORMAT_VERSION, **_seal(key, body, _entry_aad(entry.id))}
    return json.dumps(blob, indent=2) + "\n"


def open_entry(key: bytes, entry_id: str, text: str) -> Entry:
    """Decrypt and validate one entry file. Raises RemoteError with the reason."""
    try:
        blob = json.loads(text)
    except ValueError:
        raise RemoteError("not valid JSON") from None
    if not isinstance(blob, dict) or blob.get("format") != FORMAT_VERSION:
        raise RemoteError("unsupported entry format")
    plain = _unseal(key, blob, _entry_aad(entry_id), "the entry")
    if plain is None:
        raise RemoteError(
            "does not decrypt with this repository's key (damaged, renamed, "
            "or written with another passphrase)"
        )
    try:
        data = json.loads(plain)
    except ValueError:
        raise RemoteError("decrypts to invalid JSON") from None
    if not isinstance(data, dict) or data.get("id") != entry_id:
        raise RemoteError("its contents name a different entry")
    if data.get("kind") != "setup-token":
        raise RemoteError(f"unsupported account kind {data.get('kind')!r}")
    email = data.get("email")
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise RemoteError("its email label is invalid")
    token = data.get("token")
    if not isinstance(token, str) or not _SETUP_TOKEN_RE.match(token):
        raise RemoteError("it does not hold a setup-token")
    alias = data.get("alias") or ""
    if alias:
        try:
            alias = normalize_alias(alias)
        except (ValueError, TypeError):
            alias = ""
    strings = {}
    for key_name in ("addedAt", "updatedAt", "updatedBy"):
        v = data.get(key_name)
        strings[key_name] = v[:100] if isinstance(v, str) else ""
    return Entry(
        id=entry_id, email=email, token=token, alias=alias,
        added_at=strings["addedAt"], updated_at=strings["updatedAt"],
        updated_by=strings["updatedBy"],
    )


# ---------------------------------------------------------------------------
# configuration and secrets
# ---------------------------------------------------------------------------


@dataclass
class RemoteConfig:
    """One remote as this machine knows it (no secrets)."""

    id: str
    name: str
    url: str
    branch: str = "main"
    auth: str = "default"
    ssh_key: str = ""
    username: str = ""
    remember_passphrase: bool = True
    #: Entry ids deliberately not imported on this machine.
    skipped: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "url": self.url,
            "branch": self.branch, "auth": self.auth, "sshKey": self.ssh_key,
            "username": self.username,
            "rememberPassphrase": self.remember_passphrase,
            "skipped": list(self.skipped),
        }

    @classmethod
    def from_json(cls, data: object) -> "RemoteConfig | None":
        if not isinstance(data, dict):
            return None
        rid, name, url = data.get("id"), data.get("name"), data.get("url")
        if not (isinstance(rid, str) and _UUID4_RE.match(rid)):
            return None
        if not (isinstance(name, str) and isinstance(url, str) and name and url):
            return None
        auth = data.get("auth") if data.get("auth") in AUTH_KINDS else "default"
        skipped = [s for s in data.get("skipped") or [] if isinstance(s, str) and _UUID4_RE.match(s)]
        return cls(
            id=rid, name=name, url=url,
            branch=data.get("branch") if isinstance(data.get("branch"), str) else "main",
            auth=auth,
            ssh_key=data.get("sshKey") if isinstance(data.get("sshKey"), str) else "",
            username=data.get("username") if isinstance(data.get("username"), str) else "",
            remember_passphrase=data.get("rememberPassphrase") is not False,
            skipped=skipped,
        )


class _Secrets:
    """Passphrase and HTTPS token per remote: Keychain on macOS, else a 0600 file."""

    KINDS = ("passphrase", "token")

    def __init__(self, root: Path, use_keychain: bool):
        self.root = root
        self.use_keychain = use_keychain

    def _file(self, rid: str) -> Path:
        return self.root / rid / "secrets.json"

    def _read_file(self, rid: str) -> dict:
        try:
            raw = self._file(rid).read_text(encoding="utf-8")
            data = json.loads(base64.b64decode(raw).decode("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_file(self, rid: str, data: dict) -> None:
        path = self._file(rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not data:
            path.unlink(missing_ok=True)
            return
        encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        atomic_write_text(path, encoded)
        os.chmod(path, 0o600)

    def get(self, rid: str, kind: str) -> str | None:
        if self.use_keychain:
            from claude_swap import macos_keychain

            try:
                return macos_keychain.get_password(_SECRET_SERVICE, f"{rid}:{kind}")
            except Exception as e:  # noqa: BLE001
                raise RemoteError(f"Could not read the Keychain: {e}") from None
        value = self._read_file(rid).get(kind)
        return value if isinstance(value, str) else None

    def set(self, rid: str, kind: str, value: str) -> None:
        if self.use_keychain:
            from claude_swap import macos_keychain

            try:
                macos_keychain.set_password(_SECRET_SERVICE, f"{rid}:{kind}", value)
            except Exception as e:  # noqa: BLE001
                raise RemoteError(f"Could not write the Keychain: {e}") from None
            return
        data = self._read_file(rid)
        data[kind] = value
        self._write_file(rid, data)

    def delete(self, rid: str, kind: str) -> None:
        if self.use_keychain:
            from claude_swap import macos_keychain

            with contextlib.suppress(Exception):
                macos_keychain.delete_password(_SECRET_SERVICE, f"{rid}:{kind}")
            return
        data = self._read_file(rid)
        if data.pop(kind, None) is not None:
            self._write_file(rid, data)

    def delete_all(self, rid: str) -> None:
        for kind in self.KINDS:
            self.delete(rid, kind)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


#: GIT_* variables passed through to git: transport and proxy settings. Every
#: other one is dropped -- GIT_DIR or GIT_WORK_TREE inherited from a hook or
#: alias would point cswap's reset and clean at the user's own repository.
_GIT_ENV_ALLOWED = frozenset({
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_PROXY_COMMAND",
    "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_NO_VERIFY",
    "GIT_HTTP_PROXY_AUTHMETHOD", "GIT_TRACE", "GIT_CURL_VERBOSE",
})


class _PushRejected(Exception):
    """Another machine pushed first; start again from a fresh fetch."""


_GIT_HINTS = (
    ("host key verification failed",
     "the server's SSH host key is not known yet: connect once with `ssh -T` "
     "to that host and accept it, then retry"),
    ("permission denied (publickey",
     "the SSH key was refused: check the key has access to this repository"),
    ("could not read username", "the server asked for a login: use an HTTPS token "
     "(--token) or an SSH URL"),
    ("authentication failed", "the HTTPS login was refused: check the token and username"),
    ("repository not found", "the repository does not exist, or this login cannot see it"),
    ("could not resolve host", "the host name did not resolve: check the URL and network"),
)


class _Git:
    """git, run against cswap's own clone of one remote."""

    def __init__(self, workdir: Path, cfg: RemoteConfig, token: str | None, hooks_dir: Path):
        self.workdir = workdir
        self.cfg = cfg
        self.token = token or ""
        self.hooks_dir = hooks_dir

    def _env(self) -> dict:
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") or k in _GIT_ENV_ALLOWED
        }
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "LC_ALL": "C",
        })
        if self.cfg.auth == "ssh-key" and self.cfg.ssh_key:
            env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(part) for part in (
                "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-i", self.cfg.ssh_key,
            ))
        if self.cfg.auth == "https-token":
            env["CSWAP_GIT_USERNAME"] = self.cfg.username or "git"
            env["CSWAP_GIT_PASSWORD"] = self.token
        return env

    def _cmd(self, args: tuple[str, ...]) -> list[str]:
        cmd = [
            "git",
            "-c", f"core.hooksPath={self.hooks_dir}",
            "-c", "commit.gpgsign=false",
            "-c", "user.name=claude-swap",
            "-c", "user.email=claude-swap@localhost",
            "-c", f"init.defaultBranch={self.cfg.branch}",
        ]
        if self.cfg.auth == "https-token":
            cmd += ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
        return cmd + ["-C", str(self.workdir), *args]

    def _explain(self, what: str, stderr: str) -> str:
        text = stderr or ""
        if self.token:
            text = text.replace(self.token, "***")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        detail = " / ".join(lines[-3:]) if lines else "no details"
        lower = text.lower()
        for needle, hint in _GIT_HINTS:
            if needle in lower:
                return f"git {what} failed: {hint} ({detail})"
        return f"git {what} failed: {detail}"

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                self._cmd(args), env=self._env(), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                # No controlling terminal: ssh cannot stop and ask for a
                # passphrase or host-key confirmation behind the TUI.
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError:
            raise RemoteError("git is not installed or not on PATH.") from None
        except subprocess.TimeoutExpired:
            raise RemoteError(
                f"git {args[0]} timed out after {GIT_TIMEOUT_S:.0f} s "
                "(network or login problem?)"
            ) from None
        if check and proc.returncode != 0:
            raise RemoteError(self._explain(args[0], proc.stderr))
        return proc

    def sync(self) -> None:
        """Make the clone exactly the remote branch, or empty if it has none."""
        branch = self.cfg.branch
        if not (self.workdir / ".git").is_dir():
            self.workdir.mkdir(parents=True, exist_ok=True)
            self.run("init", "-q")
            self.run("remote", "add", "origin", self.cfg.url)
        else:
            self.run("remote", "set-url", "origin", self.cfg.url)
        self.run("fetch", "--prune", "--quiet", "origin")
        has_branch = self.run(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
            check=False,
        ).returncode == 0
        if has_branch:
            self.run("checkout", "-q", "-f", "-B", branch, f"origin/{branch}")
            self.run("reset", "-q", "--hard", f"origin/{branch}")
        else:
            # Empty repository (or new branch): start from nothing.
            self.run("symbolic-ref", "HEAD", f"refs/heads/{branch}")
            self.run("update-ref", "-d", f"refs/heads/{branch}", check=False)
            self.run("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".")
        self.run("clean", "-q", "-f", "-d", "-x")

    def commit_and_push(self, message: str) -> bool:
        self.run("add", "-A")
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.run("commit", "-q", "-m", message)
        proc = self.run("push", "-q", "origin", f"HEAD:refs/heads/{self.cfg.branch}", check=False)
        if proc.returncode != 0:
            err = (proc.stderr or "").lower()
            if any(s in err for s in ("rejected", "non-fast-forward", "fetch first", "stale info")):
                raise _PushRejected()
            raise RemoteError(self._explain("push", proc.stderr))
        return True


# ---------------------------------------------------------------------------
# status rows
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One account in a remote's status, from both sides."""

    state: str
    email: str
    alias: str = ""
    local_num: str | None = None
    entry_id: str | None = None
    entry: Entry | None = None
    note: str = ""
    #: A local account with the same email label (and no organization) that
    #: is not this entry: adding the entry here would replace its token.
    conflict_num: str | None = None
    #: Here-only: an unlinked repository entry already uses this email label
    #: with a different token. Pushing would put two accounts with one label
    #: in the repository, and only one of them fits on any machine.
    label_taken_by: str | None = None

    @property
    def pushed_by_default(self) -> bool:
        """Whether a push of everything includes this here-only row."""
        return (self.state == HERE_ONLY and self.note != _REMOVED_NOTE
                and self.label_taken_by is None)

    @property
    def label(self) -> str:
        return f"{self.alias} ({self.email})" if self.alias else self.email


@dataclass
class TokenReplaced:
    """What ``set_token`` did: the entry as it now is, the local slot holding
    the account (None when it isn't on this machine), whether the
    repository's token changed (False: it already had it) and whether this
    machine's did."""

    entry: Entry
    local_num: str | None
    changed: bool
    local_changed: bool = False


@dataclass
class RemoteStatus:
    remote: RemoteConfig
    rows: list[Row]


@dataclass
class SyncReport:
    """What push/pull did, as lines for the user."""

    lines: list[str] = field(default_factory=list)
    changed: int = 0


@dataclass
class _Local:
    num: str
    email: str
    alias: str
    org: str
    kind: str
    token: str | None  # the setup-token, or None for anything else
    link: str | None


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class RemoteSync:
    """Remote sync operations over one switcher's store."""

    def __init__(
        self,
        switcher: "ClaudeAccountSwitcher",
        *,
        use_keychain: bool | None = None,
        hostname: str | None = None,
    ):
        self.switcher = switcher
        backup = Path(switcher.backup_dir)
        self.root = backup / "remotes"
        self.config_path = backup / CONFIG_NAME
        self.secrets = _Secrets(
            self.root, sys.platform == "darwin" if use_keychain is None else use_keychain
        )
        self.hostname = hostname or socket.gethostname() or "unknown"

    # -- configuration ------------------------------------------------------

    def remotes(self) -> list[RemoteConfig]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("remotes") if isinstance(data, dict) else None
        out = []
        for item in items if isinstance(items, list) else []:
            cfg = RemoteConfig.from_json(item)
            if cfg is not None:
                out.append(cfg)
        return out

    def _write_remotes(self, remotes: list[RemoteConfig]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": CONFIG_SCHEMA, "remotes": [r.to_json() for r in remotes]}
        atomic_write_text(self.config_path, json.dumps(payload, indent=2) + "\n")
        os.chmod(self.config_path, 0o600)

    def _save(self, cfg: RemoteConfig) -> None:
        remotes = [r for r in self.remotes() if r.id != cfg.id]
        remotes.append(cfg)
        self._write_remotes(remotes)

    def get(self, name: str) -> RemoteConfig:
        wanted = (name or "").strip().lower()
        for cfg in self.remotes():
            if cfg.name.lower() == wanted or cfg.id == wanted:
                return cfg
        raise RemoteError(f"No remote named '{name}'. See `cswap remote list`.")

    def stored_passphrase(self, cfg: RemoteConfig) -> str | None:
        if not cfg.remember_passphrase:
            return None
        return self.secrets.get(cfg.id, "passphrase")

    def forget_passphrase(self, name: str) -> RemoteConfig:
        cfg = self.get(name)
        self.secrets.delete(cfg.id, "passphrase")
        cfg.remember_passphrase = False
        self._save(cfg)
        return cfg

    def _remember(self, cfg: RemoteConfig, passphrase: str) -> None:
        if cfg.remember_passphrase and self.secrets.get(cfg.id, "passphrase") != passphrase:
            self.secrets.set(cfg.id, "passphrase", passphrase)

    # -- plumbing -----------------------------------------------------------

    def _dir(self, cfg: RemoteConfig) -> Path:
        return self.root / cfg.id

    def _git(self, cfg: RemoteConfig) -> _Git:
        hooks = self.root / ".no-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        token = self.secrets.get(cfg.id, "token") if cfg.auth == "https-token" else None
        if cfg.auth == "https-token" and not token:
            raise RemoteError(
                f"No access token is stored for '{cfg.name}'. Set one with "
                f"`cswap remote set {cfg.name} --token -`."
            )
        return _Git(self._dir(cfg) / "repo", cfg, token, hooks)

    @contextlib.contextmanager
    def _locked(self, cfg: RemoteConfig) -> Iterator[None]:
        d = self._dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
            os.chmod(d, 0o700)
        lock = FileLock(d / ".lock")
        if not lock.acquire(timeout=30):
            raise RemoteError(f"Another cswap is using '{cfg.name}' right now; try again.")
        try:
            yield
        finally:
            lock.release()

    def _unlock(self, git: _Git, passphrase: str) -> bytes:
        path = git.workdir / MANIFEST_NAME
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RemoteError(
                "This repository is not a claude-swap remote (it has no "
                f"{MANIFEST_NAME}). Add it again to initialize it."
            ) from None
        except (OSError, ValueError):
            raise RemoteError(f"The repository's {MANIFEST_NAME} is unreadable.") from None
        return unlock_manifest(manifest, passphrase)

    @staticmethod
    def _read_entries(workdir: Path, key: bytes) -> tuple[dict[str, Entry], dict[str, str]]:
        """Decrypted entries by id, and per-file errors by id or file name."""
        entries: dict[str, Entry] = {}
        errors: dict[str, str] = {}
        folder = workdir / ACCOUNTS_DIR
        if not folder.is_dir():
            return entries, errors
        for path in sorted(folder.iterdir()):
            name = path.name
            if name.startswith("."):
                continue  # .gitkeep and the like
            stem = name[: -len(ENTRY_SUFFIX)] if name.endswith(ENTRY_SUFFIX) else None
            if stem is None or not _UUID4_RE.match(stem) or not path.is_file() or path.is_symlink():
                errors[name] = "ignored: not a claude-swap entry file name"
                continue
            try:
                entries[stem] = open_entry(key, stem, path.read_text(encoding="utf-8"))
            except RemoteError as e:
                errors[stem] = str(e)
            except (OSError, UnicodeDecodeError):
                errors[stem] = "unreadable file"
        return entries, errors

    @staticmethod
    def _entry_path(workdir: Path, entry_id: str) -> Path:
        if not _UUID4_RE.match(entry_id):
            raise RemoteError("invalid entry id")
        return workdir / ACCOUNTS_DIR / f"{entry_id}{ENTRY_SUFFIX}"

    def _write_entry(self, workdir: Path, key: bytes, entry: Entry) -> None:
        path = self._entry_path(workdir, entry.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seal_entry(key, entry), encoding="utf-8")

    def _transaction(
        self, cfg: RemoteConfig, passphrase: str,
        change: Callable[[Path, bytes, dict[str, Entry], dict[str, str]], str | None],
    ) -> bool:
        """Fetch, apply ``change``, commit and push; again from scratch if beaten.

        ``change`` must derive everything from the state it is handed: on a
        retry it runs again against the newer repository.
        """
        git = self._git(cfg)
        for _ in range(PUSH_ATTEMPTS):
            git.sync()
            key = self._unlock(git, passphrase)
            entries, errors = self._read_entries(git.workdir, key)
            message = change(git.workdir, key, entries, errors)
            if message is None:
                return False
            try:
                return git.commit_and_push(message)
            except _PushRejected:
                continue
        raise RemoteError(
            "The repository kept changing while cswap was pushing; try again."
        )

    # -- local side -----------------------------------------------------------

    def _locals(self, rid: str) -> list[_Local]:
        data = self.switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        out = []
        for num in data.get("sequence", []):
            n = str(num)
            rec = accounts.get(n) or {}
            email = rec.get("email", "")
            kind = "api_key" if rec.get("kind") == "api_key" else "oauth"
            token = None
            if kind == "oauth":
                try:
                    creds = self.switcher._read_account_credentials(n, email, cached=True)
                except Exception:  # noqa: BLE001 -- unreadable: not shareable now
                    creds = ""
                oa = oauth.extract_oauth_data(creds or "")
                if oauth.is_setup_token(oa) and isinstance(oa.get("accessToken"), str):
                    token = oa["accessToken"]
            links = rec.get("remotes") if isinstance(rec.get("remotes"), dict) else {}
            link = links.get(rid)
            out.append(_Local(
                num=n, email=email, alias=rec.get("alias") or "",
                org=rec.get("organizationUuid") or "", kind=kind, token=token,
                link=link if isinstance(link, str) and _UUID4_RE.match(link) else None,
            ))
        return out

    def _set_links(self, rid: str, changes: dict[str, str | None]) -> None:
        """Record (or with None, drop) slot -> entry links for one remote."""
        if not changes:
            return
        with FileLock(self.switcher.lock_file):
            data = self.switcher._get_sequence_data() or {}
            accounts = data.get("accounts", {})
            dirty = False
            for num, entry_id in changes.items():
                rec = accounts.get(str(num))
                if not rec:
                    continue
                links = dict(rec.get("remotes") or {}) if isinstance(rec.get("remotes"), dict) else {}
                before = links.get(rid)
                if entry_id:
                    links[rid] = entry_id
                else:
                    links.pop(rid, None)
                if links.get(rid) == before:
                    continue
                dirty = True
                if links:
                    rec["remotes"] = links
                else:
                    rec.pop("remotes", None)
            if dirty:
                data["lastUpdated"] = get_timestamp()
                self.switcher._write_json(self.switcher.sequence_file, data)

    def _drop_links(self, rid: str, entry_id: str | None = None) -> None:
        changes = {
            loc.num: None for loc in self._locals(rid)
            if loc.link and (entry_id is None or loc.link == entry_id)
        }
        self._set_links(rid, changes)

    # -- comparison -----------------------------------------------------------

    @staticmethod
    def _compare(
        cfg: RemoteConfig, locals_: list[_Local],
        entries: dict[str, Entry], errors: dict[str, str],
    ) -> list[Row]:
        rows: list[Row] = []
        claimed: set[str] = set()
        by_fp: dict[str, str] = {}
        for eid, entry in entries.items():
            by_fp.setdefault(entry.fingerprint, eid)
        for loc in locals_:
            if loc.token is None:
                why = "API key" if loc.kind == "api_key" else "refreshing login: can't be shared"
                rows.append(Row(NOT_SHAREABLE, loc.email, loc.alias, loc.num, note=why))
                continue
            fp = token_fingerprint(loc.token)
            if loc.link and loc.link in entries:
                entry = entries[loc.link]
                claimed.add(loc.link)
                same = entry.fingerprint == fp
                note = "" if same else _updated_note(entry)
                rows.append(Row(IN_SYNC if same else DIFFERS, loc.email, loc.alias,
                                loc.num, loc.link, entry, note))
            elif loc.link and loc.link in errors:
                claimed.add(loc.link)
                rows.append(Row(UNREADABLE, loc.email, loc.alias, loc.num, loc.link,
                                note=errors[loc.link]))
            elif fp in by_fp and by_fp[fp] not in claimed:
                eid = by_fp[fp]
                claimed.add(eid)
                rows.append(Row(IN_SYNC, loc.email, loc.alias, loc.num, eid, entries[eid]))
            else:
                note = _REMOVED_NOTE if loc.link else ""
                rows.append(Row(HERE_ONLY, loc.email, loc.alias, loc.num, note=note))
        unclaimed_labels = {
            entry.email.lower(): eid for eid, entry in entries.items() if eid not in claimed
        }
        for row in rows:
            if row.state == HERE_ONLY and not row.note:
                taken = unclaimed_labels.get(row.email.lower())
                if taken:
                    row.label_taken_by = taken
                    row.note = f"the repository has a different token labelled {row.email} (entry {taken[:8]})"
        org_less = {loc.email.lower(): loc.num for loc in locals_ if not loc.org}
        for eid, entry in entries.items():
            if eid in claimed:
                continue
            conflict = org_less.get(entry.email.lower())
            note = f"same email label as local Account-{conflict}" if conflict else ""
            rows.append(Row(SKIPPED if eid in cfg.skipped else REPO_ONLY, entry.email,
                            entry.alias, None, eid, entry, note, conflict_num=conflict))
        for key, message in errors.items():
            if key not in claimed:
                rows.append(Row(UNREADABLE, "?", entry_id=key if _UUID4_RE.match(key) else None,
                                note=f"{key}: {message}"))
        rows.sort(key=lambda r: (_STATE_ORDER[r.state], r.email.lower(), r.local_num or ""))
        return rows

    # -- operations -----------------------------------------------------------

    def add(
        self, name: str, url: str, *, passphrase: str, branch: str = "main",
        auth: str = "default", ssh_key: str = "", username: str = "",
        token: str = "", remember: bool = True,
    ) -> tuple[RemoteConfig, bool, int]:
        """Add (and if empty, initialize) a remote. Returns (config, created, entries)."""
        name = (name or "").strip()
        url = (url or "").strip()
        if not _NAME_RE.match(name):
            raise ValidationError(
                "Remote names use letters, digits, '.', '_' and '-' (up to 64)."
            )
        if any(r.name.lower() == name.lower() for r in self.remotes()):
            raise ValidationError(f"A remote named '{name}' already exists.")
        if not url or any(c.isspace() for c in url) or url.startswith("-"):
            raise ValidationError("The repository URL is invalid.")
        branch = (branch or "main").strip()
        if not _BRANCH_RE.match(branch):
            raise ValidationError(f"'{branch}' is not a usable branch name.")
        if auth not in AUTH_KINDS:
            raise ValidationError(f"Unknown login type '{auth}'.")
        if auth == "ssh-key":
            key_path = Path(os.path.expanduser(ssh_key or "")).resolve()
            if not ssh_key or not key_path.is_file():
                raise ValidationError(f"SSH key file not found: {ssh_key or '(none)'}")
            ssh_key = str(key_path)
        else:
            ssh_key = ""
        if auth == "https-token" and not (token or "").strip():
            raise ValidationError("An HTTPS access token is required for this login type.")
        if not passphrase:
            raise ValidationError("A passphrase is required.")
        _aesgcm()  # a broken install: say so before touching the network

        cfg = RemoteConfig(
            id=str(uuid.uuid4()), name=name, url=url, branch=branch, auth=auth,
            ssh_key=ssh_key, username=(username or "").strip() if auth == "https-token" else "",
            remember_passphrase=remember,
        )
        try:
            if auth == "https-token":
                self.secrets.set(cfg.id, "token", token.strip())
            with self._locked(cfg):
                created, count = self._open_or_initialize(cfg, passphrase)
            self._save(cfg)
            self._remember(cfg, passphrase)
        except BaseException:
            self.secrets.delete_all(cfg.id)
            shutil.rmtree(self._dir(cfg), ignore_errors=True)
            raise
        return cfg, created, count

    def _open_or_initialize(self, cfg: RemoteConfig, passphrase: str) -> tuple[bool, int]:
        git = self._git(cfg)
        for _ in range(PUSH_ATTEMPTS):
            git.sync()
            if (git.workdir / MANIFEST_NAME).exists():
                key = self._unlock(git, passphrase)
                entries, _errors = self._read_entries(git.workdir, key)
                return False, len(entries)
            foreign = [
                p.name for p in git.workdir.iterdir()
                if p.name != ".git" and not p.name.lower().startswith(_ALLOWED_FOREIGN)
            ]
            if foreign:
                raise RemoteError(
                    "This repository already holds other files and no "
                    f"{MANIFEST_NAME}, so cswap will not write into it. Use an "
                    "empty repository (a README is fine)."
                )
            manifest, _key = new_manifest(passphrase)
            atomic_write_text(git.workdir / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
            (git.workdir / ACCOUNTS_DIR).mkdir(exist_ok=True)
            (git.workdir / ACCOUNTS_DIR / ".gitkeep").write_text("", encoding="utf-8")
            try:
                git.commit_and_push("claude-swap: initialize remote")
                return True, 0
            except _PushRejected:
                continue  # someone initialized it meanwhile: open theirs
        raise RemoteError("The repository kept changing while cswap was initializing it.")

    def update(
        self, name: str, *, url: str | None = None, branch: str | None = None,
        auth: str | None = None, ssh_key: str | None = None,
        username: str | None = None, token: str | None = None,
    ) -> RemoteConfig:
        """Change where a remote is or how cswap logs in to it."""
        cfg = self.get(name)
        if url is not None:
            url = url.strip()
            if not url or any(c.isspace() for c in url) or url.startswith("-"):
                raise ValidationError("The repository URL is invalid.")
            cfg.url = url
        if branch is not None:
            if not _BRANCH_RE.match(branch.strip()):
                raise ValidationError(f"'{branch}' is not a usable branch name.")
            cfg.branch = branch.strip()
        if auth is not None:
            if auth not in AUTH_KINDS:
                raise ValidationError(f"Unknown login type '{auth}'.")
            cfg.auth = auth
        if cfg.auth == "ssh-key":
            if ssh_key is not None:
                key_path = Path(os.path.expanduser(ssh_key)).resolve()
                if not key_path.is_file():
                    raise ValidationError(f"SSH key file not found: {ssh_key}")
                cfg.ssh_key = str(key_path)
            if not cfg.ssh_key:
                raise ValidationError("The ssh-key login type needs --ssh-key PATH.")
        else:
            cfg.ssh_key = ""
        if username is not None:
            cfg.username = username.strip()
        if token is not None:
            if not token.strip():
                raise ValidationError("The access token is empty.")
            self.secrets.set(cfg.id, "token", token.strip())
        if cfg.auth == "https-token" and not self.secrets.get(cfg.id, "token"):
            raise ValidationError("The https-token login type needs --token.")
        if cfg.auth != "https-token":
            self.secrets.delete(cfg.id, "token")
        self._save(cfg)
        return cfg

    def remove(self, name: str) -> RemoteConfig:
        """Forget a remote on this machine: clone, secrets, links. The repository is untouched."""
        cfg = self.get(name)
        with self._locked(cfg):
            self._drop_links(cfg.id)
            self.secrets.delete_all(cfg.id)
            self._write_remotes([r for r in self.remotes() if r.id != cfg.id])
        shutil.rmtree(self._dir(cfg), ignore_errors=True)
        return cfg

    def status(self, name: str, passphrase: str) -> RemoteStatus:
        """Fetch, decrypt and compare. Records links for matching tokens."""
        cfg = self.get(name)
        with self._locked(cfg):
            git = self._git(cfg)
            git.sync()
            key = self._unlock(git, passphrase)
            self._remember(cfg, passphrase)
            entries, errors = self._read_entries(git.workdir, key)
            locals_ = self._locals(cfg.id)
            rows = self._compare(cfg, locals_, entries, errors)
            self._link_matches(cfg, locals_, rows)
        return RemoteStatus(cfg, rows)

    def _link_matches(self, cfg: RemoteConfig, locals_: list[_Local], rows: list[Row]) -> None:
        """Record links for accounts found by token."""
        changes: dict[str, str | None] = {}
        locals_by_num = {loc.num: loc for loc in locals_}
        for row in rows:
            if row.local_num is None:
                continue
            loc = locals_by_num.get(row.local_num)
            if loc is None:
                continue
            if row.state == IN_SYNC and loc.link != row.entry_id:
                changes[row.local_num] = row.entry_id
        self._set_links(cfg.id, changes)

    def _resolve_entry(self, rows: list[Row], ref: str) -> Row:
        """A row with an entry, by email label, alias, local slot, entry id
        prefix (4+ characters) or, when unique, the part of the label before @."""
        ref_l = ref.strip().lower()
        with_entry = [r for r in rows if r.entry_id]

        def exact(r: Row) -> bool:
            return (r.email.lower() == ref_l or (bool(r.alias) and r.alias.lower() == ref_l)
                    or r.local_num == ref_l or r.entry_id == ref_l)

        def loose(r: Row) -> bool:
            return ((len(ref_l) >= 4 and r.entry_id.startswith(ref_l))
                    or r.email.lower().split("@", 1)[0] == ref_l)

        for test in (exact, loose):
            hits = [r for r in with_entry if test(r)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise RemoteError(
                    f"'{ref}' matches {len(hits)} entries; use more of the entry id "
                    "(shown by `cswap remote show`)."
                )
        raise RemoteError(
            f"Nothing in the repository matches '{ref}'. Use its email label, alias "
            "or entry id (shown by `cswap remote show`)."
        )

    def push(
        self, name: str, passphrase: str, accounts: list[str] | None = None,
        *, confirm: Callable[[Row], bool] = lambda row: False,
    ) -> SyncReport:
        """Copy local setup-token accounts to the repository.

        With no ``accounts``: every here-only account (except ones deliberately
        removed from the repository) plus, when ``confirm`` agrees, every
        account whose token differs. Named accounts are pushed regardless.
        """
        cfg = self.get(name)
        with self._locked(cfg):
            selected: set[str] | None = None
            if accounts:
                selected = set()
                for ref in accounts:
                    num, _email, _org = self.switcher.resolve_account(ref)
                    selected.add(str(num))
            decisions: dict[tuple[str, str], bool] = {}
            outcome: dict = {}

            def change(workdir, key, entries, errors):
                report = SyncReport()
                links: dict[str, str | None] = {}
                locals_ = self._locals(cfg.id)
                by_num = {loc.num: loc for loc in locals_}
                by_fp = {e.fingerprint: e for e in entries.values()}
                now = get_timestamp()
                added = updated = 0
                for row in self._compare(cfg, locals_, entries, errors):
                    if row.local_num is None:
                        continue
                    if selected is not None and row.local_num not in selected:
                        continue
                    loc = by_num[row.local_num]
                    tag = f"Account-{row.local_num} {row.label}"
                    if row.state == NOT_SHAREABLE:
                        if selected is not None:
                            report.lines.append(f"{tag}: not shared ({row.note})")
                        continue
                    if row.state == IN_SYNC:
                        links[row.local_num] = row.entry_id
                        if selected is not None:
                            report.lines.append(f"{tag}: already in sync")
                        continue
                    if row.state == UNREADABLE:
                        report.lines.append(f"{tag}: left alone, its repository entry {row.note}")
                        continue
                    if row.state == HERE_ONLY:
                        if selected is None and row.note == _REMOVED_NOTE:
                            report.lines.append(
                                f"{tag}: skipped, it was removed from the repository "
                                "(name it to push it again)"
                            )
                            continue
                        if selected is None and row.label_taken_by:
                            report.lines.append(
                                f"{tag}: skipped, {row.note}. Pull that entry to take its "
                                "token, or name this account to add it as well"
                            )
                            continue
                        twin = by_fp.get(token_fingerprint(loc.token))
                        if twin is not None:
                            report.lines.append(
                                f"{tag}: its token is already in the repository as {twin.label}"
                            )
                            continue
                        entry = Entry(
                            id=str(uuid.uuid4()), email=loc.email, token=loc.token,
                            alias=loc.alias, added_at=now, updated_at=now,
                            updated_by=self.hostname,
                        )
                        self._write_entry(workdir, key, entry)
                        by_fp[entry.fingerprint] = entry
                        links[row.local_num] = entry.id
                        added += 1
                        report.lines.append(f"{tag}: added to the repository")
                    elif row.state == DIFFERS:
                        decision_key = (row.local_num, row.entry.fingerprint)
                        if decision_key not in decisions:
                            decisions[decision_key] = bool(confirm(row))
                        if not decisions[decision_key]:
                            report.lines.append(f"{tag}: differs, kept the repository's copy")
                            continue
                        entry = replace(row.entry, token=loc.token, alias=loc.alias,
                                        updated_at=now, updated_by=self.hostname)
                        self._write_entry(workdir, key, entry)
                        links[row.local_num] = entry.id
                        updated += 1
                        report.lines.append(f"{tag}: updated in the repository")
                report.changed = added + updated
                outcome["report"], outcome["links"] = report, links
                if not report.changed:
                    return None
                return f"claude-swap: add {added}, update {updated}"

            self._transaction(cfg, passphrase, change)
            self._remember(cfg, passphrase)
            self._set_links(cfg.id, outcome.get("links", {}))
        report = outcome.get("report") or SyncReport()
        if not report.lines:
            report.lines.append("Nothing to push.")
        return report

    def pull(
        self, name: str, passphrase: str, entries: list[str] | None = None,
        *, confirm: Callable[[Row], bool] = lambda row: False, replace_conflicts: bool = False,
    ) -> SyncReport:
        """Copy repository accounts to this machine.

        With no ``entries``: every repo-only entry that is not skipped and has
        no email conflict, plus, when ``confirm`` agrees, every linked account
        whose token differs. Named entries are pulled even when skipped;
        ``replace_conflicts`` lets a pulled entry replace the token of a local
        account with the same email label.
        """
        cfg = self.get(name)
        report = SyncReport()
        with self._locked(cfg):
            git = self._git(cfg)
            git.sync()
            key = self._unlock(git, passphrase)
            self._remember(cfg, passphrase)
            found, errors = self._read_entries(git.workdir, key)
            rows = self._compare(cfg, self._locals(cfg.id), found, errors)
            chosen: list[Row] | None = None
            if entries:
                chosen = [self._resolve_entry(rows, ref) for ref in entries]
            links: dict[str, str | None] = {}
            unskip: set[str] = set()
            pulled_emails: set[str] = set()
            for row in rows if chosen is None else chosen:
                tag = row.entry.label if row.entry else row.label
                if row.state == IN_SYNC:
                    links[row.local_num] = row.entry_id
                    if chosen is not None:
                        report.lines.append(f"{tag}: already in sync")
                    continue
                if row.state in (REPO_ONLY, SKIPPED):
                    if row.state == SKIPPED and chosen is None:
                        continue
                    if row.entry.email.lower() in pulled_emails:
                        report.lines.append(
                            f"{tag}: not added, another entry with that email label "
                            "was just added (cswap keeps one account per label)"
                        )
                        continue
                    if row.conflict_num and not replace_conflicts:
                        report.lines.append(
                            f"{tag}: not added, local Account-{row.conflict_num} already "
                            "uses that email label (pull it by name with replace to "
                            "overwrite that account's token)"
                        )
                        continue
                    num = self._install(row.entry)
                    pulled_emails.add(row.entry.email.lower())
                    links[num] = row.entry_id
                    unskip.add(row.entry_id)
                    report.changed += 1
                    verb = "replaced the token of" if row.conflict_num else "added as"
                    report.lines.append(f"{tag}: {verb} Account-{num}")
                elif row.state == DIFFERS:
                    if not confirm(row):
                        report.lines.append(f"Account-{row.local_num} {row.label}: differs, kept this machine's copy")
                        continue
                    self._install(row.entry, email=row.email, expect=row.local_num)
                    links[row.local_num] = row.entry_id
                    report.changed += 1
                    report.lines.append(
                        f"Account-{row.local_num} {row.label}: token updated from the repository"
                    )
                elif row.state == UNREADABLE and chosen is not None:
                    report.lines.append(f"{tag}: cannot be read ({row.note})")
                elif row.state in (HERE_ONLY, NOT_SHAREABLE) and chosen is not None:
                    report.lines.append(f"{tag}: not in the repository")
            self._set_links(cfg.id, links)
            if unskip & set(cfg.skipped):
                cfg.skipped = [s for s in cfg.skipped if s not in unskip]
                self._save(cfg)
        if not report.lines:
            report.lines.append("Nothing to pull.")
        return report

    def _install(self, entry: Entry, email: str | None = None, expect: str | None = None) -> str:
        """Store a repository entry's token as a local account; returns its slot.

        ``expect``: the slot that must receive it (an in-place update). It is
        checked first, since the store keys token accounts by email label with
        no organization and anything else would land in a new slot.
        """
        label = email or entry.email
        if expect is not None:
            data = self.switcher._get_sequence_data() or {}
            if self.switcher._find_account_slot(data, label, "") != expect:
                raise RemoteError(
                    f"Account-{expect} is not stored as a plain token account, so "
                    "its token cannot be updated in place."
                )
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            self.switcher.add_account_from_token(entry.token, email=label)
        data = self.switcher._get_sequence_data() or {}
        num = self.switcher._find_account_slot(data, label, "")
        if num is None:
            raise RemoteError(f"{label} was not stored; nothing was linked.")
        if entry.alias and email is None and not (data["accounts"][num].get("alias")):
            if self.switcher._alias_in_use(entry.alias, exclude_num=num) is None:
                with contextlib.redirect_stdout(sink), contextlib.suppress(ClaudeSwitchError):
                    self.switcher.set_alias(num, entry.alias)
        return num

    def add_token(
        self, name: str, passphrase: str, token: str, *,
        email: str | None = None, alias: str | None = None,
    ) -> Entry:
        """Put a setup-token straight into the repository, not into this machine."""
        token = validate_setup_token(token)
        if email is not None:
            email = email.strip()
            if not _EMAIL_RE.match(email):
                raise ValidationError(f"Invalid email label: {email}")
        if alias:
            try:
                alias = normalize_alias(alias)
            except ValueError as e:
                raise ValidationError(str(e)) from e
        cfg = self.get(name)
        made: dict = {}
        with self._locked(cfg):
            def change(workdir, key, entries, errors):
                fp = token_fingerprint(token)
                for existing in entries.values():
                    if existing.fingerprint == fp:
                        raise RemoteError(f"That token is already in the repository as {existing.label}.")
                eid = str(uuid.uuid4())
                now = get_timestamp()
                entry = Entry(
                    id=eid, email=email or f"setup-token-{eid[:8]}@token.local",
                    token=token, alias=alias or "", added_at=now, updated_at=now,
                    updated_by=self.hostname,
                )
                self._write_entry(workdir, key, entry)
                made["entry"] = entry
                return "claude-swap: add 1, update 0"

            self._transaction(cfg, passphrase, change)
            self._remember(cfg, passphrase)
        return made["entry"]

    def set_token(self, name: str, passphrase: str, ref: str, token: str) -> TokenReplaced:
        """Replace the token of one repository entry, whether or not this
        machine has the account, and this machine's copy when it has one.

        The entry keeps its id, label and alias, so every other machine that
        has it sees it as *differs* and a pull there takes the new token. The
        repository is written first: when this machine's copy then fails, a
        pull retries it. ``ref`` as for ``pull``: label, alias, entry id
        prefix or local slot."""
        token = validate_setup_token(token)
        cfg = self.get(name)
        done: dict = {}
        with self._locked(cfg):
            def change(workdir, key, entries, errors):
                rows = self._compare(cfg, self._locals(cfg.id), entries, errors)
                row = self._resolve_entry(rows, ref)
                entry = entries.get(row.entry_id or "")
                if entry is None:
                    reason = errors.get(row.entry_id or "", "it can't be read")
                    raise RemoteError(
                        f"Entry {(row.entry_id or ref)[:8]} can't be updated: {reason}. "
                        "Delete it and add the token again."
                    )
                fp = token_fingerprint(token)
                if row.local_num:
                    self._check_local_token(row.local_num, token)
                done["label"] = row.email
                if entry.fingerprint == fp:
                    done["result"] = TokenReplaced(entry, row.local_num, changed=False)
                    return None
                for other in entries.values():
                    if other.id != entry.id and other.fingerprint == fp:
                        raise RemoteError(f"That token is already in the repository as {other.label}.")
                updated = replace(entry, token=token, updated_at=get_timestamp(),
                                  updated_by=self.hostname)
                self._write_entry(workdir, key, updated)
                done["result"] = TokenReplaced(updated, row.local_num, changed=True)
                return "claude-swap: add 0, update 1"

            self._transaction(cfg, passphrase, change)
            self._remember(cfg, passphrase)
            result: TokenReplaced = done["result"]
            if result.local_num:
                try:
                    result.local_changed = self._store_local_token(
                        result.local_num, done["label"], token)
                except ClaudeSwitchError as e:
                    raise RemoteError(
                        f"The repository has the new token, but Account-{result.local_num} "
                        f"here could not be updated: {e} `cswap remote pull {cfg.name} "
                        f"{result.entry.id[:8]}` tries again."
                    ) from e
        return result

    def set_local_token(self, num: str, token: str) -> bool:
        """Replace the token of a setup-token account on this machine only
        (no repository involved). Its settings, alias and links stay. Returns
        False when it already had that token."""
        token = validate_setup_token(token)
        loc = next((x for x in self._locals("") if x.num == str(num)), None)
        if loc is None or loc.token is None:
            raise RemoteError(f"Account-{num} is not a setup-token account on this machine.")
        return self._store_local_token(loc.num, loc.email, token)

    def _check_local_token(self, num: str, token: str) -> None:
        """Refuse a token another local account already holds."""
        fp = token_fingerprint(token)
        for loc in self._locals(""):
            if loc.num != num and loc.token and token_fingerprint(loc.token) == fp:
                raise RemoteError(f"That token is already Account-{loc.num}'s on this machine.")

    def _store_local_token(self, num: str, label: str, token: str) -> bool:
        """Replace local token account ``num``'s token in place, through
        ``add_account_from_token``'s refresh path (which also updates Claude
        Code's copy when it is the live login). False: it already had it."""
        data = self.switcher._get_sequence_data() or {}
        if self.switcher._find_account_slot(data, label, "") != num:
            raise RemoteError(
                f"Account-{num} is not stored as a plain token account, so its token "
                "cannot be replaced in place."
            )
        self._check_local_token(num, token)
        current = next((x for x in self._locals("") if x.num == num), None)
        if current is not None and current.token == token:
            return False
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            self.switcher.add_account_from_token(token, email=label)
        return True

    def remove_entry(self, name: str, passphrase: str, ref: str) -> str:
        """Delete one account from the repository (local accounts stay)."""
        cfg = self.get(name)
        done: dict = {}
        with self._locked(cfg):
            def change(workdir, key, entries, errors):
                rows = self._compare(cfg, self._locals(cfg.id), entries, errors)
                row = self._resolve_entry(rows, ref)
                self._entry_path(workdir, row.entry_id).unlink(missing_ok=True)
                done["id"] = row.entry_id
                done["label"] = row.entry.label if row.entry else row.entry_id
                return "claude-swap: remove 1"

            self._transaction(cfg, passphrase, change)
            self._remember(cfg, passphrase)
            if "id" in done:
                self._drop_links(cfg.id, done["id"])
                if done["id"] in cfg.skipped:
                    cfg.skipped.remove(done["id"])
                    self._save(cfg)
        return done.get("label", ref)

    def set_skipped(self, name: str, passphrase: str, ref: str, skipped: bool) -> str:
        """Mark a repository account as not imported on this machine (or undo)."""
        status = self.status(name, passphrase)
        row = self._resolve_entry(status.rows, ref)
        cfg = self.get(name)
        if skipped and row.entry_id not in cfg.skipped:
            cfg.skipped.append(row.entry_id)
        elif not skipped and row.entry_id in cfg.skipped:
            cfg.skipped.remove(row.entry_id)
        self._save(cfg)
        return row.entry.label if row.entry else row.entry_id

    def change_passphrase(self, name: str, old: str, new: str) -> int:
        """Re-encrypt every entry under a new passphrase; returns how many."""
        check_new_passphrase(new)
        cfg = self.get(name)
        counted: dict = {}
        with self._locked(cfg):
            def change(workdir, key, entries, errors):
                bad = [k for k in errors if _UUID4_RE.match(k)]
                if bad:
                    raise RemoteError(
                        f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} cannot be "
                        "decrypted, so they cannot be re-encrypted. Remove them first "
                        "(`cswap remote rm-entry`)."
                    )
                manifest, new_key = new_manifest(new)
                atomic_write_text(workdir / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
                for entry in entries.values():
                    self._write_entry(workdir, new_key, entry)
                counted["n"] = len(entries)
                return "claude-swap: change passphrase"

            self._transaction(cfg, old, change)
            if cfg.remember_passphrase:
                self.secrets.set(cfg.id, "passphrase", new)
        return counted.get("n", 0)


def _updated_note(entry: Entry) -> str:
    when = entry.updated_at or "unknown time"
    who = entry.updated_by or "unknown machine"
    return f"repository copy updated {when} by {who}"
