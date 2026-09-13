# Remediation record — 2026-09-12/13

All eight audit findings fixed on `dev`, each with focused regressions,
commits `05fbdd0..7cbdb44`. Full suite at the final commit: 2427
passed, 10 skipped (platform-gated); browser 22+1 skip; node 8/8; wheel
builds with the new Security-framework dependency.

| ID | Fix | Commit |
| --- | --- | --- |
| F01 | Oversized Keychain credentials never enter argv: Security.framework write path (SecItemAdd/Update), refusal when bindings are absent, CLI-read fallback for framework-owned items, real-keychain 5 KB round-trip verified. `pyobjc-framework-Security` added to the menubar extra. | `05fbdd0`, `ecc762f` |
| F02 | Imported config stripped to identity keys at import (warning names dropped fields); both fresh-profile activation fallbacks bootstrap through the same allowlist. | `dea84a6` |
| F03 | Migration is a phased journal (copying → committed); a committed destination is authoritative and only source cleanup is retried; same-FS stays the atomic rename. The audit reproduction now fails at its destructive assertion. | `dfa7b3c` |
| F04 | remove/add/token-add/import mutation tails run inside the canonical account lock; removal re-reads under the lock and aborts if the slot identity changed during confirmation. Regressions pin both audit reproductions. | `0156679` |
| F05 | Purge covers retained `.prev` generations (Keychain + `.enc.prev`), reports INCOMPLETE with the failure list instead of claiming completion, and persists retry names in a durable inventory that ordinary-removal failures append to and successful purges clear. | `25bc399` |
| F06 | `_write_json` tempfiles are exclusive (`O_EXCL`) mode 0600 at creation with random suffix, written via descriptor, published by `os.replace`; mode observed 0600 at publication under umask 022. | `00c48b1` |
| F07 | All OAuth network helpers (profile, usage, token refresh) go through a no-redirect opener that raises on any redirect; test seams move from `urlopen` to `_bearer_urlopen`, regressions offline. | `6ead63e` |
| F08 | The explicit-account fast path (`run N` on the active account) scrubs override credentials with the same warning as session mode; plain `claude` keeps normal env semantics. | `7cbdb44` |

## Verification notes

- Both archived reproduction scripts re-run against the fixed tree: the
  credential probe aborts at its own capture assertion (the argv write
  no longer exists), the state probe fails its destructive migration
  assertion (the sole copy survives). F05's purge assertions are
  superseded by the updated expectations in `tests/test_switcher.py`.
- **Deferred:** the pixel-fidelity gate (boards 17/18) could not be
  re-run at the end of this session — the host's window server stopped
  compositing WKWebView surfaces entirely (a minimal plain-HTML
  window outside any product code also captures flat black; the DOM
  records render correctly). The gate last passed at `30e0380` and no
  panel web asset changed since (verified: `git log -- src/claude_swap/
  menubar/web/` is empty for the entire remediation range). Re-run
  `scripts/board_capture.py --states 17-right-dark,18-right-light` +
  the Pillow gate after a host restart/login.

## Not in scope of this pass (audit's lower tiers)

- GUI token-entry sentinel/timeout, TUI markup escaping, weekly-status
  policy (three UI edge cases).
- README storage-disclosure updates (Keychain fallback/stash/session
  exceptions), launchd log rotation, update-check preference.
- CI hardening (advisory audit, SHA-pinned actions, release gate).
