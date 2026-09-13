# Claude Swap — security, privacy, and code-quality audit

**Reviewed:** 12 September 2026 · **Version:** 0.28.0 · **Commit:** `504f67d`  
**Verdict:** Request changes before treating this as a hardened credential-management tool.

## Executive assessment

This is substantial software with meaningful defensive engineering, but its security and privacy quality is uneven. The strongest parts—credential provenance checks, recovery generations, locking during switches, and isolated tests—coexist with reproducible failures in adjacent paths. Four high-priority findings affect credential confidentiality, imported configuration, migration recovery, and concurrent account mutations. Four medium-priority findings concern deletion guarantees, temporary-file permissions, redirect policy, and account selection under environment overrides.

I found **no evidence of intentional exfiltration or a covert upload destination in the reviewed application source**. The source-defined network destinations are Anthropic/Claude OAuth APIs and PyPI. This is a bounded code-review result, not proof that the software, every dependency, or its author is trustworthy. No reliable ZCode attribution was present in the searched commit messages; the findings cannot responsibly be assigned to that agent without a known baseline or patch range. The coding agent’s nationality is not evidence for or against code quality.

The repository has 43 Python application files containing 25,580 physical lines and 42 test modules containing 44,140 lines, plus a bundled JavaScript/CSS menu-bar panel. The audit examined the architecture and high-risk flows across the current tree, reviewed related tests and automation, scanned reachable history for common credential patterns, ran the suite, and independently reproduced the principal failures with synthetic data.

**Practical decision:** I would not call the current release “really good” on security/privacy, despite its extensive tests. Address F01–F04 before recommending broad or security-sensitive use. Fix deletion guarantees and storage disclosures before representing removal as complete or macOS storage as exclusively Keychain-backed. These findings justify targeted repairs; they do not establish malware or require abandoning the project.

## Findings at a glance

Severity reflects impact **and the stated preconditions**. “High” does not mean remotely exploitable without user interaction. No unconditional critical remote exploit was demonstrated.

| ID | Priority | Finding | Evidence confidence |
|---|---|---|---|
| F01 | High | Large Keychain writes expose full secrets in process arguments | Synthetic subprocess argument capture |
| F02 | High, conditional | Ordinary import can install executable MCP configuration on a fresh/unusable profile | Import + activation reproduced; downstream execution not attempted |
| F03 | High | Interrupted cross-filesystem migration can destroy the only complete credential copy | Fault-injected filesystem reproduction |
| F04 | High, integrity | Account mutations bypass the lock and overwrite stale roster state | Held-lock and deterministic interleaving reproduction |
| F05 | Medium | Removal/purge can leave credentials while claiming completion | Mocked deletion failures and omitted `.prev` calls |
| F06 | Medium, conditional | Full-config temporary files start readable under ordinary umasks | Creation-mode inspection before chmod |
| F07 | Medium, conditional | Bearer requests permit cross-origin/HTTP redirect forwarding | Runtime redirect-handler reproduction, no requests sent |
| F08 | Medium | Explicit account launch can preserve overriding environment credentials | Source and existing behavioral tests |

## Detailed findings and remedies

### F01 — Large credentials are recoverable from process arguments

**Evidence:** [macos_keychain.py:158](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/macos_keychain.py:158>), especially lines 181–193; [test_macos_keychain.py:102](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/tests/test_macos_keychain.py:102>).

Small values go through `security -i` on stdin. When the resulting command exceeds 4,032 bytes, the implementation switches to `/usr/bin/security ... -X <hex-secret>` in argv. Hex encoding is fully reversible. Approximately 2 KB of original data is enough to trigger this path, depending on account/service-name length. Full credential objects can contain multiple integration tokens.

**Reproduced:** A 2,325-byte synthetic credential object was recovered byte-for-byte from the captured `-X` argument. The existing test explicitly expects this fallback, so its passing result does not establish confidentiality.

**Impact and preconditions:** A process observer or endpoint-monitoring product that records arguments during a write can obtain access tokens, refresh tokens, and any sibling secrets in the object. Visibility depends on local OS controls; capture can persist outside the Keychain in telemetry. No actual capture or compromise on this machine was observed.

**Remedy:** Use Security.framework through a stable helper/API that accepts arbitrary-size data over an anonymous pipe or another non-argv channel. If that is unavailable, refuse the unsafe write or expose a clearly disclosed private-file fallback. Never describe hex as redaction. Audit error reporting so exceptions cannot reintroduce argv contents.

**Acceptance tests:** Exercise empty, small, threshold-adjacent, and large values; assert that neither raw nor encoded synthetic credentials appear in argv, logs, or errors. Verify round trips without weakening Keychain access controls.

### F02 — Account import crosses an executable-configuration trust boundary

**Evidence:** Arbitrary config accepted at [transfer.py:387](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/transfer.py:387>), serialized at line 435, and stored at [transfer.py:539](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/transfer.py:539>); full-config activation fallback at [switcher.py:6894](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:6894>) through 6942, with another fallback around 7198–7206.

Default export deliberately minimizes configuration, but import trusts whatever config object the file supplies. If the local Claude config is missing or unusable, activation can write that entire imported object into the live profile. An attacker can add a top-level `mcpServers` definition. No full-configuration import opt-in is required.

**Reproduced:** An ordinary version-1 import followed by fresh-profile activation preserved an arbitrary MCP definition in active `.claude.json`. The command was the inert `/AUDIT_DO_NOT_EXECUTE`; network and subprocess execution were blocked. **This demonstrates configuration injection, not direct code execution by cswap.**

**Impact and preconditions:** The user must import a malicious/tampered bundle and activate it when the local config is absent/unusable. Subsequent Claude behavior can turn an injected stdio MCP definition into a local process, subject to its version and administrative/trust controls. Anthropic documents both local-process stdio servers and user-scoped servers stored in `~/.claude.json`, making this a consequential boundary. [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

**Remedy:** Treat every imported object as untrusted even if the normal exporter emits safe data. Default to a strict identity/login allowlist. Apply the same allowlist when bootstrapping a fresh profile. Provide a separate, explicit full-configuration restore operation with a preview of MCP commands, endpoints, environment fields, and project/trust settings. Validate that bundle identity and config identity agree.

**Acceptance tests:** Add hostile extra config fields to otherwise valid imports; prove normal import + activation excludes them with missing, empty, malformed, and existing local configs. Test full restore separately and preserve unrelated existing local settings.

### F03 — Interrupted migration can discard the only complete copy

**Evidence:** [paths.py:159](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/paths.py:159>), especially lines 198–202 and 212–215; automatic constructor invocation at [switcher.py:315](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:315>).

The migration uses `shutil.move` and a single “migrating” marker. Across filesystems, move copies the directory and then deletes its source. If source deletion is interrupted, the destination can be complete while the source is only partial. On retry, the marker causes the complete destination to be recursively deleted before retrying from that partial source.

**Reproduced:** Forced an `EXDEV` rename failure, allowed the complete copy, then interrupted source cleanup after deleting one synthetic credential. Before retry, only the destination contained that credential. After retry, neither copy did; the remaining roster still existed.

**Impact and preconditions:** Linux/WSL legacy migration to another filesystem, followed by interruption or failure during source cleanup. This can lose credentials, config backups, and session data. It can run during ordinary initialization, before the user chooses a modifying command. Same-filesystem atomic rename does not have this specific failure window.

**Remedy:** Use an external migration lock and a durable phased journal: stage copy, verify completeness, commit destination, then clean the old source. Once committed, destination must remain authoritative regardless of a partially existing source. When the phase cannot be established, preserve both copies and report recovery instructions.

**Acceptance tests:** Fault-inject every stage, including **source deletion after successful copy**, journal writes, concurrent startup, and cleanup retries. Assert that at least one complete copy always survives and migration is idempotent.

### F04 — Public account mutations bypass the transaction lock

**Evidence:** [switcher.py:3944](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:3944>), particularly roster read at 3995, confirmation at 4011–4018, and rewrite at 4026. Add paths begin at [switcher.py:3482](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:3482>) and [switcher.py:3738](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:3738>); dispatch at [cli.py:1385](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/cli.py:1385>) supplies no outer lock. Import also performs its multi-step writes without an encompassing account transaction.

Switching and refresh logic use a canonical account lock, but add/remove operations do not consistently participate. Removal reads a full roster, may wait indefinitely for confirmation, then writes its old snapshot. Atomic rename only makes each write indivisible; it does not make a read-modify-write transaction safe.

**Reproduced:** Removal completed while the global account lock was already held. A deterministic update to another account’s alias during the confirmation wait disappeared when removal committed. The probe used synthetic state and stubbed the actual credential deletion; it demonstrates missing serialization and a lost update, not a live cross-account credential leak.

**Impact and preconditions:** Concurrent CLI, TUI, menu-bar, or auto-switch activity. Updates can be lost, roster/credential state can diverge, and refresh/switch checks that assume serialized slot mutation become unreliable. The practical risk is significant for a tool explicitly designed to run multiple frontends at once.

**Remedy:** Centralize mutations in one account transaction API. Collect confirmation outside the lock; then acquire it, reread state, and verify that the selected identity/generation still matches what the user approved. Commit credentials, config, and roster with explicit rollback/recovery rules. Apply to add, overwrite, remove, import, and relevant migrations. Do not simply hold the lock across terminal input or indiscriminately nest existing locks.

**Acceptance tests:** Deterministically interleave add/remove/import with switching and refresh. Verify no lost roster updates, no deletion of a newly reused slot, and no successful mutation while the canonical lock belongs to another writer. Include same-process threads and separate processes.

### F05 — Credential deletion is not reliably complete

**Evidence:** [switcher.py:7359](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:7359>) through 7384, directory deletion at 7405–7413, and completion output at 7431. Best-effort ordinary account deletion at [credentials.py:1347](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/credentials.py:1347>); previous-generation names at [credentials.py:1457](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/credentials.py:1457>).

Purge constructs current and legacy account names but omits retained `.prev` Keychain items. It suppresses Keychain deletion failures, deletes the roster, and prints “Purge complete.” Ordinary removal also logs and suppresses credential deletion failures, then removes the account from that roster. A later roster-based purge cannot discover those orphaned entries. The comment promising that purge sweeps them is therefore not a reliable privacy guarantee.

**Reproduced:** Mocked-Keychain probes showed `.prev` deletion was never requested. In another run, every native deletion failed, yet the backup directory and roster were destroyed and completion was reported.

**Impact and limits:** Stored secrets can remain after the user believes they were erased. Whether an older token is still usable depends on the provider; its possible expiration does not justify leaving it undisclosed. The active Claude login is explicitly excluded by the purge UI; that exclusion is intentional and is not this defect. Unclaimed stashes are files, not a separate Keychain namespace; deleting the backup tree normally removes them.

**Remedy:** Maintain a durable inventory/tombstone for deletion retries. Cover current, previous, legacy, and session-profile credential namespaces. Distinguish “not found” from “could not verify.” Preserve sufficient cleanup metadata and return a nonzero/incomplete result on failure. Never describe residual credentials as harmless solely because the roster no longer references them.

**Acceptance tests:** Locked/unavailable Keychain; current and previous generations; already removed accounts; absent/corrupt roster; stale sessions; permission failures. Verify each store is absent before reporting complete deletion. Test that retry metadata survives a failed cleanup.

### F06 — JSON temp files receive private permissions too late

**Evidence:** [switcher.py:559](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:559>) through 580.

`_write_json` writes a predictable PID-based temporary filename before chmodding it to `0600`. Under umask `022`, that temporary file is initially `0644`. This writer also handles full live Claude config, which may contain project metadata and MCP environment secrets. PID-only names additionally collide between threads writing the same target.

**Reproduced:** A hook immediately before chmod observed mode `0644` and the complete synthetic secret; the final file was `0600`. This was a permissions observation, not an attempted cross-user read.

**Impact and preconditions:** Disclosure requires other users to traverse the parent path. A `0700` home/private parent mitigates it; permissive homes and shared config directories make it relevant. A failed write before chmod can lengthen exposure. Same-process collisions are a separate integrity concern; they were not independently raced in this audit.

**Remedy:** Create unique temporary files with `tempfile.mkstemp` or equivalent exclusive `0600` creation, write via the open descriptor, and atomically replace the destination with cleanup on exceptions. Preserve intended symlink semantics and Windows replacement retries. Consolidate the several JSON writers around these explicit invariants.

**Acceptance tests:** Observe permissions from creation onward under different umasks; abort before replace; pre-create hostile names; overlap same-process writers. Verify destinations remain complete and no secret-bearing intermediate is broadly readable.

### F07 — Bearer authentication can follow an unsafe redirect

**Evidence:** [oauth.py:282](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/oauth.py:282>) through 290 and [oauth.py:397](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/oauth.py:397>) through 407.

Profile/usage requests attach Authorization as a normal request header and use the default urllib opener. The tested Python runtime’s redirect handler preserved that header when constructing a redirect to another origin over HTTP.

**Reproduced:** A synthetic `HTTPS api.anthropic.com → HTTP other.invalid` redirect retained `Bearer SYNTHETIC_ACCESS`. No request was sent and no real endpoint was observed redirecting this way. Python’s extensible redirect handling is documented in [urllib.request](https://docs.python.org/3/library/urllib.request.html).

**Impact and preconditions:** An accepted redirect must come from the authenticated endpoint, a trusted TLS intermediary, or an otherwise compromised trusted path. A random network attacker cannot force this through correctly validated HTTPS. It is a concrete policy weakness with conditional exploitability, not evidence of existing exfiltration.

**Remedy:** Deny redirects for credential-bearing API calls, or strictly require the original HTTPS origin. Explicitly strip authentication on an origin change. Apply a consistent policy to all OAuth network helpers; keep certificate validation enabled.

**Acceptance tests:** Reject HTTP downgrade and foreign-host redirects for every relevant redirect status. Verify same-origin policy deliberately, and test response-size limits and bounded errors alongside request timeouts.

### F08 — Explicit account choice can be overridden by inherited credentials

**Evidence:** [session.py:579](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/session.py:579>) through 601 versus scrubbing at 605–623; override list at 192–198. Existing test [test_session.py:1397](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/tests/test_session.py:1397>) explicitly preserves the behavior.

When the selected account matches the configured active account, `cswap run N` launches plain Claude with the entire environment. For a nonactive account, the session path removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and related token/descriptor overrides because they can defeat the selected identity.

**Impact and preconditions:** A shell retains another account/organization’s override credential and the active-account fast path is selected. Depending on Claude’s auth precedence, the invocation can use/bill the override identity despite the explicit selection and reassuring account message. No live Claude request or billing event was executed in this audit.

This is an intentional implementation contract, not an accidental regression: tests require it and documentation describes the plain-Claude fast path. Nevertheless, it is an unsafe inconsistency for users relying on account isolation.

**Remedy:** Check override credentials before branching. For explicit `run N`, either remove them consistently or refuse with a clear explanation and a deliberate passthrough option. Leave an explicitly unqualified “run default” operation free to preserve normal environment semantics.

**Acceptance tests:** Every override variable across active/nonactive, default/profile, and require-session branches. Assert that effective identity matches the operation’s advertised contract.

## Additional lower-priority findings

| Area | Observation | Remedy |
|---|---|---|
| GUI token entry | [app.py:551](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/menubar/app.py:551>) passes literal `-` into a backend that treats it as stdin input. A terminal-launched panel can wait indefinitely; request promises have no timeout. | Separate CLI input collection from literal-token registration; reject sentinels in GUI; bound requests. |
| TUI confirmation | [modals.py:40](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/tui/modals.py:40>) renders interpolated confirmation text as markup; caller at [app.py:314](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/tui/app.py:314>). A synthetic malformed email caused `MarkupError`. Normal import/token email validation blocks this input, so corrupted/unvalidated metadata is required. | Render external strings as plain `Text` or disable markup. |
| Weekly status accuracy | [viewmodel.py:107](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/menubar/viewmodel.py:107>) rolls an expired cached weekly window forward and substitutes 0% without a measurement. The redesigned panel preserves measured/stale data, so surfaces disagree. | Reuse one stale-data policy; display awaiting-data rather than invented usage. No engine mis-switch was proven. |

Bridge hardening opportunities: validate before spawning workers, bound request size/concurrency, authenticate the exact main document/frame, and clear token input when the native popover closes. The reviewed code already escapes HTML, JSON-serializes bridge messages, uses action allowlists, blocks external navigation, and includes a restrictive CSP. No remote bridge entry point or exploitable XSS chain was found. Native WebKit enforcement was not tested end to end.

## Privacy inventory and storage reality

| Data | Location/purpose | Protection and retention reality |
|---|---|---|
| OAuth access/refresh tokens; managed API keys | Per-account backups and active Claude store | macOS normally uses Keychain. Linux/WSL/Windows backups use base64 `.enc` files. macOS can fall back to files. |
| Previous token generation | `.prev` Keychain items or `.enc.prev` files | One retained predecessor per slot; potentially sensitive even if superseded. Purge omission is F05. |
| Unclaimed credential material | `credentials/.unclaimed-*.enc` plus manifest | Base64 files on **every platform**, including macOS; append-only preservation can retain unknown-account credentials until adopted/retired/manually purged. No universal age-based expiry. |
| Identity/config snapshots | Roster and per-account config backups | Emails, account/org IDs, organization names, aliases; full config may contain project paths, trust settings, MCP configuration, and embedded integration secrets. |
| Usage/activity | `cache/usage.json`, auto-switch state, application logs | Account identity, utilization, timing, error/status data. Removing an account does not prune its usage row; ordinary logs retain identity events. |
| Session profiles | Backup-root `sessions/` | Credential/config copies, session records, and conversation state. macOS bootstrap can create plaintext credential files inside private profiles. Within isolated session profiles, conversation-history sharing is opt-in. The active-account fast path uses the ordinary Claude profile and its existing history. |
| Explicit exports | User-selected destination or stdout | Plaintext JSON; deliberately documented. Default strips machine-shared credential siblings; `--full` keeps broader material. File exports use private atomic writes; stdout inherits the receiving tool’s storage policy. |
| Service state/logs | LaunchAgents plist and `~/Library/Logs/com.cswap.menubar.*` | Separate from backup tree; uninstall-service is a distinct operation. No complete “all traces” guarantee should be made without including these artifacts. |

**Base64 is not encryption.** See [credentials.py:1117](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/credentials.py:1117>) and the deliberate macOS stash policy at [credentials.py:1555](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/credentials.py:1555>). POSIX `0600` files inside `0700` directories are useful access controls, but do not protect against same-user processes or readable backups/sync copies. Windows relies on inherited ACLs rather than an explicitly enforced private DACL. This is a design exposure, not a demonstrated Windows cross-user exploit.

The README’s macOS “Keychain” table omits fallback, stash, and session-file exceptions. Document them clearly, show effective backend in diagnostics without printing values, consider an opt-in strict-vault mode, and prefer platform-protected storage for persistent secrets. If portable encryption is added, use authenticated encryption with a key stored separately; a key next to ciphertext does not solve this threat.

Application logs rotate at roughly 1 MiB with three backups, which bounds size but not age. They include email/account events and menu-bar usage data; there is no universal secret-redaction filter. No direct normal token logging was found, but raw exceptions deserve review. Launchd stdout/stderr logs have no rotation configured by this repository. Add documented retention, private creation, optional pseudonymous identity labels, and explicit account-associated metadata cleanup. Preserve recovery material only under a deliberate, visible policy; do not blindly TTL-delete the only surviving refresh token.

**Outbound data:** OAuth refresh posts credential grant material to `platform.claude.com`; profile/usage requests send bearer authorization to `api.anthropic.com`. The passive update check contacts PyPI and discloses normal connection metadata, not account tokens in its request. It has a 24-hour cache and no dedicated user setting to disable it. Offer an offline/update-check preference and document these flows. No source-defined analytics collector or repository-content upload was found. `cswap run` launches Claude, whose subsequent networking is outside cswap’s source and outside this audit.

**Current user precautions:** Use private, nonsynced credential directories and encrypted device/backups where appropriate. Prefer `cswap add-token`’s hidden prompt or stdin over a token in the command line; positional secrets can enter shell history and process telemetry. Import only trusted bundles until F02 is fixed. Keep independent backups during migration. Do not rely on purge as verified secret erasure yet. If actual exposure is established, revoke/rotate the affected credentials; this audit alone does not establish an incident requiring blanket rotation.

## Broader engineering quality

### Strengths worth retaining

- Broad tests for platform behavior, provenance, rollback, stale generations, token refresh, session safety, and UI contracts.
- Tests include process-global protection against writing real account stores, fake Keychain access by default, temporary homes, and explicit gates for real-Keychain CI tests.
- Fixed HTTPS API origins, certificate verification, and bounded network/subprocess timeouts; `/usr/bin/security` is pinned rather than resolved from PATH.
- Several sensitive writers create private temporary files and atomically replace destinations.
- Default export minimizes shared tokens/device context; import validates the complete input list before starting writes and rejects invalid slots/identities/duplicate aliases.
- Existing switch transactions acquire both tool and Claude credential/config locks, preserve account-independent OAuth fields, and distinguish missing credentials from inaccessible ones.
- Menu-bar presentation is separated into a pure view model and bridge; most dynamic UI content is escaped/plain text. Within isolated session profiles, history sharing is opt-in; the active-account fast path uses the ordinary Claude profile and its existing history.

### Maintainability and correctness concerns

The core orchestrator has **7,431 lines and 171 functions**, with a 573-line switch routine and 558-line active-usage routine. The auto-switch engine has a 491-line tick routine. These counts include extensive commentary, but still indicate a large review surface. Several comments claim invariants that adjacent paths do not uphold—most clearly the missing mutation lock and later purge sweep.

The highest-value refactor is a **single credential/account transaction boundary**, not arbitrary file splitting. Then separate configuration restoration, account lifecycle/deletion, and network refresh orchestration. Make outcomes typed and distinguish absent, unreadable, corrupt, partially committed, and cleanup-pending states consistently. Prefer one tested private-write primitive over the current mix of unique tempfiles, PID tempfiles, and direct truncating writes.

`_write_account_config` at [switcher.py:936](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/switcher.py:936>) writes directly to the destination before chmod. Private parent directories usually mitigate disclosure, but a write failure can truncate a valid backup. Multi-entry import is validated first but not an all-or-nothing credential/config/roster transaction on I/O failures. These deserve fault-injection coverage when F04 is repaired.

Some cache parsing remains less defensive than credential parsing: for example integer coercion in [usage_store.py:899](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/src/claude_swap/usage_store.py:899>) can raise on a syntactically valid but malformed persisted row. Cache corruption should degrade to unavailable data, not destabilize the entire collector. No external exploit was demonstrated.

Performance defenses include shared cache claims, adaptive polling, backoff, and off-main-thread UI work. However, there are no measured coverage, memory, or performance budgets in CI. Broad exception handling, large routines, and thread-per-bridge-request behavior merit attention; these are maintainability/hardening concerns rather than invented remote vulnerabilities.

### Dependencies and release controls

**Live audit result:** `uv audit --frozen --cache-dir /tmp/cswap-audit-uv-cache --no-python-downloads` returned **no known vulnerabilities and no adverse project statuses in 28 packages**. The initial sandbox run could not resolve the advisory service; the authorized network-enabled retry completed successfully. This included the locked optional/development packages. uv documents its audit scope and known-advisory behavior in [its command reference](https://docs.astral.sh/uv/reference/cli/#uv-audit).

The lockfile uses PyPI and wheel/sdist hashes; no alternate registry or VCS dependency was found. CI uses locked installs and tests on Linux, Windows, and macOS; the macOS job installs/imports the real menu-bar extra. These are positive controls.

Gaps in [ci.yml:1](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/.github/workflows/ci.yml:1>), [publish.yml:1](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/.github/workflows/publish.yml:1>), and [pyproject.toml:1](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/pyproject.toml:1>):

- No dependency-advisory audit, secret scan, static security check, lint/type gate, or measured coverage gate is configured in the checked-in workflows.
- Actions use movable tags, including the publishing action. Pin reviewed full commit SHAs and automate updates. [GitHub’s secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).
- uv and isolated build tooling are not pinned to exact versions; publish installs `build` and resolves the build backend independently. Add a reproducible, reviewed build-tool lock/constraints policy.
- The release-triggered publish workflow has no visible dependency on a successful test/build-verification job for that release commit. Add an explicit release gate and validate the artifact that will be uploaded. Remote branch protection, environment rules, and PyPI trusted-publisher policy were not inspected, so their absence is **not** asserted.
- OIDC publishing avoids a checked-in long-lived PyPI secret. Keep it, with minimum permissions and a protected release environment appropriate to the repository.
- Package-manager success and clean advisories do not establish package provenance or absence of newly malicious behavior. Dependency source and installed binary provenance were not exhaustively audited.

## Verification, evidence, and limits

| Check | Observed result |
|---|---|
| Main test suite | 2,330 passed, 4 skipped, 1 failed, 3 deprecation warnings in 20.06 seconds; Python 3.14.7/macOS, four workers, temporary home, explicit pytest plugins |
| Failure diagnosis | `test_own_process_started_in_the_past` failed because sandbox denied `/bin/ps`; the command independently returned “operation not permitted” |
| Same test outside sandbox | 1 passed in 2.58 seconds with a temporary home; no application change |
| Focused credential/transfer/session run | 291 passed in 4.23 seconds, reported by independent reviewer; overlaps the main suite |
| Focused menu-bar/TUI run | 147 passed in 3.27 seconds, reported by independent reviewer; overlaps the main suite |
| Synthetic principal proofs | Both archived reproduction scripts rerun by the primary reviewer; all assertions passed |
| Dependency audit | 28 locked packages, no known vulnerabilities/adverse statuses |
| Secret-pattern scan | No matches in tracked files or 3,233 path-associated reachable Git objects across 578 reachable commits for the selected Anthropic/GitHub/AWS/private-key signatures; values would have been redacted |
| Screenshot spot-check | Main dark panel and TUI example inspected; no visible bearer token/private key found |
| Worktree | Clean before audit; only report/evidence files added |

The signature scan is deliberately described narrowly: it is not entropy-based, does not identify arbitrary unknown secret formats, and does not inspect unreachable Git objects or OCR every image. It is not a certificate that no secret has ever been committed. No real account store, login Keychain, or production API was read for credential content. Reproductions used fake secrets, temporary directories, and mocked native/network calls. The vulnerability database query sent dependency names/versions, not source or account data.

This audit did not run the native menu-bar app, execute imported MCP commands, make live Claude requests, exercise actual token rotation, measure cross-user process access, validate Windows ACLs, rerun Linux/Windows CI, build/publish a release artifact, or inspect upstream release protections. Thus no claims of full platform certification, direct remote code execution, or complete dependency trust are made. Review coverage is broad and risk-driven, not a formal proof of every line or historical revision.

### Evidence files

- [test-results.txt](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/docs/audits/2026-09-12/test-results.txt>)
- [verification-summary.txt](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/docs/audits/2026-09-12/verification-summary.txt>)
- [credential-probes.py](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/docs/audits/2026-09-12/credential-probes.py>)
- [state-probes.py](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/docs/audits/2026-09-12/state-probes.py>)
- [probe-results.txt](</Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap/docs/audits/2026-09-12/probe-results.txt>)

The scripts are audit reproductions, separate from production code. Their “PASS”/“CONFIRMED” output means the defect is reproducible, **not that it has been fixed**. Run them from the repository with its existing virtual environment and `PYTHONPATH=src`; they use synthetic data and must retain their isolation/mocks.

## Recommended remediation sequence

1. **Before a hardened release:** close argv exposure; enforce safe import defaults; repair migration commit/recovery; centralize and test account mutation locking. These are F01–F04.
2. **Next security patch:** make deletion verifiable/retryable; create private JSON tempfiles; constrain redirects; make explicit-account environment behavior consistent. Update storage/privacy disclosures at the same time.
3. **Quality hardening:** fix the three UI edge cases, atomic backup-config writes, malformed-cache handling, request bounds, and log/metadata retention.
4. **Release gate:** convert the reproductions into isolated regressions, run the platform matrix and native integration checks on the repaired commit, audit locked dependencies, and test the built artifact. Add secret/static checks and immutable action/tooling pins.

Each fix should be small enough to review independently, with explicit recovery behavior and a focused regression. The report proposes remedies; **no application behavior has been changed and none of these findings is marked resolved**.
