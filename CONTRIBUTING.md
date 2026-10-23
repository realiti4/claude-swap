# Contributing to claude-swap

Thanks for helping. claude-swap holds people's Claude logins, so every change has to meet three rules: it is tested, it never lets a secret out, and it never overwrites something on a user's machine by surprise.

## Before you start

- **A bug:** open an issue with the *Bug report* template. Include `cswap --version`, your OS, how you run Claude Code, and the command output, with emails redacted and never a token.
- **A new feature or a change in behaviour:** open an issue first with the *Feature request* template and describe the problem. Agreeing on the shape first saves a rewrite.
- **A small fix** (a typo, an obvious bug with a test) can go straight to a pull request.

## Set up

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync --locked
uv run cswap help
```

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. If you change one, commit both files together.

## Run the tests

```bash
uv run pytest                            # the whole suite, in parallel (pytest-xdist, -n auto)
uv run pytest tests/test_proxy.py -n0    # one file, serially, for debugging
```

CI (`.github/workflows/ci.yml`) runs the full suite on Linux, Windows and macOS with `uv sync --locked`. A pull request must be green on all three.

## Rules for tests

- **A behaviour change comes with a test that fails without it.** Write the test first, watch it fail for the reason you expect, then make it pass.
- **Tests never touch your real accounts.** `tests/conftest.py` gives every test an isolated home, blocks the real macOS Keychain and the OAuth profile fetch, and installs an audit hook that refuses any write under the real claude-swap paths from any thread (see `tests/test_real_store_guard.py`). Use the `temp_home` fixture, and do not work around the guard.
- **No network.** Fake at the boundary: `urlopen`, `subprocess`, or a local server on `127.0.0.1`.
- **No sleeping.** Inject a clock (the `FakeClock` pattern in the existing tests) instead of waiting for real time to pass.
- **All three platforms.** Code that touches paths, file modes, the Keychain, signals or sockets has to pass on Linux, Windows and macOS.

## Secrets

- Never log, print, raise or return in `--json` output an access token, a refresh token, an API key, or the contents of a credential file. To compare tokens, compare fingerprints (`oauth.credential_fingerprint`, `oauth.access_token_fingerprint`).
- A change that touches credentials or logging gets a test that asserts the token does not appear in the output or the log.
- Never put a real token in an issue, a pull request or a test fixture.

## Code

- Python 3.12+, type hints, and `from __future__ import annotations`, like the code around you.
- No new runtime dependency without agreeing on it in an issue first. The runtime dependencies are deliberately few (`textual`, `truststore`, and `keyring` on Windows).
- Write comments and docstrings that say why, and record what you measured when a number decides the design.
- Anything a user can see (a command, a flag, a setting, a change in output) gets its README entry and its `--help` text in the same pull request.
- Keep `--json` output backward compatible: add fields, do not rename or remove them.

## Pull requests

- One concern per pull request. Branch from `main` and keep it up to date with `main`.
- The title says what changes, in plain words, for example *"Keep the live shared fields when a usage refresh rewrites the active store"*. A scope prefix such as `fix(auto):` is welcome.
- The description, in this order:
  1. **Summary:** what changes and why, in two or three sentences.
  2. **Why:** the problem, with evidence (log lines, a measurement, the issue).
  3. **How it works:** the design, and what you deliberately did not change.
  4. **Tests:** what you added, and the `uv run pytest` result.
  5. **Not in this PR:** anything left for later.
- Link the issue (`Closes #123`).
- Do not change `version` in `pyproject.toml`; the maintainer bumps it when releasing.
- If this is your first contribution, the CI run waits until a maintainer approves it. That is normal.

## License

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
