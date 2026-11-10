# Working on claude-swap

For coding agents and the people driving them.

claude-swap switches Claude Code between several accounts. It reads and writes people's login credentials, so a careless change can cost someone their stored logins.

Read [docs/architecture.md](docs/architecture.md) first. It says which module owns what, how a switch, a usage fetch and a token refresh run, and the rules that hold across the codebase: lock order, no network under a lock, what may be written into a slot.

## Commands

- `uv sync` installs the locked dependencies.
- `uv run pytest` runs the whole suite, in parallel by default.
- `uv run pytest tests/test_usage_store.py` runs one file. Prefer this while you work.

No linter or formatter is configured. Match the style of the file you are in.

## Code

- Put a test beside the existing ones in `tests/test_<module>.py`. Tests run against a temporary home and an in-memory Keychain; do not work around those guards.
- CI runs on Linux, macOS and Windows. Credentials live in the Keychain on macOS and in files elsewhere, so go through `credentials.py` and `paths.py` instead of assuming one platform.
- Never print or log a token or the contents of a credential file. Summarise its state, as `oauth.build_token_status` does.
- A claim about how Claude Code or Anthropic's endpoints behave needs evidence. Say which Claude Code version you read or what you measured, as the existing docstrings do. Do not guess.
- `--json` output is a contract. Add fields; never rename or remove one.
- Runtime dependencies are deliberately few. Do not add one without asking in an issue first.
- Comments and docstrings describe what the code does now and why. Leave out the history of how you got there: no investigation logs, round numbers or "previously".

## Documentation

Each topic has one home. Link to it; do not copy it.

- `README.md` shows what the tool does and how to use each feature at a glance. Keep an addition to a line or two.
- `docs/` holds the exact rules and reference: `auto-switch.md`, `json-output.md`, `menubar.md`. When you change that behaviour, update that page.
- How a mechanism works belongs in the docstring of the module that owns it.
- `docs/architecture.md` changes only when module ownership, a cross-module flow or a codebase-wide rule changes.

Do not add planning, investigation or handoff documents to the repository.

## Pull requests

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you open a pull request; it says what one should look like. Three things apply to you as an agent in particular:

- Stay on the one problem you were asked to solve. Mention unrelated things you notice instead of fixing them in the same change.
- Reading the whole diff is the job of the person you are working for. Do not say they have done it, and do not stop to ask them to confirm it.
- Report only the checks you actually ran, and say what you left unverified.

Opening an issue or a pull request from the command line skips the forms, so follow them yourself. `.github/ISSUE_TEMPLATE/bug_report.yml` lists what a bug report needs, and `.github/pull_request_template.md` gives the shape of a description. Keep the detail in proportion to the change.
