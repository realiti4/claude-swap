# Credential storage and session security

This document describes the credential-storage and session-isolation security
properties, migration steps, and compatibility limits. It does not imply an
endorsement by Anthropic.

## Credential backups

New file-backed credential backups, retained generations, recovery stashes,
configuration snapshots and swap staging files use authenticated encryption
(Fernet). The encryption key is stored in the OS credential store, separately
from the backup files. Each storage directory has a distinct key. Plaintext
keyring backends are rejected. A missing/locked/unavailable credential store
stops writes; it does not silently produce a Base64 backup.

On Linux install and unlock Secret Service (or libsecret); on Windows use
Credential Manager. macOS uses the system Keychain. Headless machines without
an OS credential store are intentionally unsupported for new encrypted backups.
Do not delete the `claude-swap-file-encryption` keychain entries: doing so makes
existing encrypted files unreadable. Copying or moving a backup directory alone
is not a portable backup, because its key is bound to the OS account and storage
path. Use password-encrypted export/import to move accounts or storage paths.

Legacy Base64 and JSON backups remain readable for compatibility. To encrypt
existing backups, stop other cswap processes and run:

```sh
cswap secure-storage
```

The migration is explicit, resumable and serialized with account writes. It does
not modify native Claude Code profiles or delete your independent backups.
Previously exported files, external backup snapshots, filesystem snapshots and
cloud copies are not erased or retroactively encrypted. Rotate credentials that
may already have been exposed. The key is shared by files in its directory;
compromising the OS account/credential store can expose that directory's data.

## Portable export

`cswap export backup.cswap` now prompts for a password twice and encrypts the
entire account payload with AES-256-GCM and scrypt (N=32768, r=8, p=1). Import
prompts for the same password. The password is never accepted as a command-line
argument or stored in configuration. Choose a strong unique password: exported
files allow offline password guessing. There is no password recovery.

For an existing external-encryption pipeline only, opt in to plaintext:

```sh
cswap export - --plaintext | gpg -c > backup.gpg
```

Plaintext exports contain live credentials. The CLI emits a warning on stderr.
Password prompts refuse to fall back to echoed input when no terminal exists.
Legacy plaintext imports remain supported; imported backup files are encrypted
on write.

## Account isolation and token ownership

`cswap run N` no longer shares settings, rules, skills or MCP configuration by
default. Use `--share` only when both accounts should have access to the same
configuration and inline MCP credentials. Fresh session seeds include only the
account login, not the source profile's MCP/plugin or device OAuth state.
Explicit-account launches scrub authentication override environment variables
on both their regular and already-active-account paths. History sharing remains a separate,
explicit option. This is configuration isolation, not an OS sandbox: processes
running as the same OS user can still read that user's files.

Nested `cswap run` with `CLAUDE_CONFIG_DIR` already set is refused before token
refresh or account resolution. Launch from a fresh terminal instead.

Session setup and automatic switching now serialize through the same state
lock. Preparing a session reserves its profile before refreshing credentials.
Automatic rotation skips reserved profiles, including idle profiles, and
rechecks the reservation immediately before activation. Manual activation also
checks the reservation under the account-store lock. A failed session setup
can leave a reserved directory; this intentionally favors holding over rotating
an uncertain credential. To return an account to automatic rotation, close its
sessions and remove/re-add that account through cswap (which uses its existing
live-session guards). Do not remove a profile while Claude is running in it.

macOS session credentials are written and verified in their profile-specific
Keychain entry instead of seeding a plaintext file. Active macOS writes stop
when the Keychain is unavailable. Oversized Keychain values are refused instead
of being exposed as hexadecimal secrets in process arguments. Linux/Windows
still require Claude Code's native plaintext active credential file; backup
encryption cannot remove this upstream requirement. POSIX files/directories use
0600/0700; Windows active files inherit the OS user's directory ACLs. An existing
macOS active credential shadow file is still synchronized for Claude hot reload.

External Claude Code instances are not made transactional across all processes
by this change. Avoid using the same account in separate
profiles and on multiple machines concurrently. The upstream refresh-token
protocol and native client behavior can still cause re-login requirements.

## Service rules

These changes do not authorize credential sharing, account pooling, bypassing
usage restrictions or third-party use of subscription OAuth credentials. Account
policy and billing restrictions are controlled by Anthropic, not by these protections.
Use accounts you are authorized to use and check current Anthropic terms. There
is no claim that automated rotation is approved or immune to account limits.

Legacy Base64 inputs are accepted only for backward compatibility and have no
authenticity protection. Run secure-storage before treating stored backups as
encrypted; keep the entire account-store directory writable only by its owner.
