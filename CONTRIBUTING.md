# Contributing to claude-swap

Thanks for wanting to help. I maintain claude-swap in my spare time, and a lot of what is good in it came from other people. Bug reports, ideas, documentation fixes and code are all welcome, whether this is your first contribution anywhere or your hundredth.

## Ways to help

- **Report a bug.** Use the [bug report form](https://github.com/realiti4/claude-swap/issues/new/choose). It asks for the few things needed to reproduce the problem and tells you what to redact.
- **Suggest something.** Use the feature request form for a concrete proposal, or [Discussions](https://github.com/realiti4/claude-swap/discussions) for a question or a half-formed idea.
- **Improve the documentation.** Typos, unclear sentences and missing examples are all worth a pull request.
- **Send code.** The rest of this page is about that.

## Before you write code

For a bug fix, a test or a documentation fix, go ahead and open a pull request.

For a new feature, a new flag or a change to how something is meant to behave, please open an issue first. A short conversation up front saves you from building something that turns out not to fit, and I can usually point you at the right place in the code.

## Setting up

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It installs the right Python for you.

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap help
```

`uv run cswap ...` runs the code in your checkout, not an installed copy. To work on the macOS menu bar, use `uv sync --extra menubar`.

## Trying your change

The copy in your checkout works on your real accounts, so save a copy of them before you experiment. Keep the file outside the checkout: it holds your logins in plain text.

```bash
cswap export ~/cswap-backup.cswap --full
```

Treat it as a safety net, not an undo button. Login tokens rotate, so an older export can hold a token that no longer works. If an account goes missing, `cswap import ~/cswap-backup.cswap` puts it back and leaves working accounts alone. If an account ends up needing a fresh login, [Refresh expired tokens](README.md#refresh-expired-tokens) in the README says how.

A few things that help when something misbehaves:

- Add `--debug` to a command to see debug logging, for example `uv run cswap list --debug`.
- The log file is `claude-swap.log` in the data directory (its location per platform is in the [README](README.md#data-locations)).
- For VS Code, `.vscode/launch.json` has debugger configurations ready, including one that asks for the arguments.

## Tests

```bash
uv run pytest                                        # everything, in parallel
uv run pytest tests/test_usage_store.py              # one file
uv run pytest tests/test_usage_store.py -n 0 --pdb   # no parallelism, stop in the debugger on a failure
```

The tests never touch your real accounts: they run in a temporary home with a fake Keychain.

A bug fix should come with a test that fails without it. CI runs on Linux, macOS and Windows; if you could only try one of them, say so in the pull request and that is fine.

There is no formatter or linter to run. Match the code around your change.

## Finding your way around

[docs/architecture.md](docs/architecture.md) says which module owns what and how the main flows run. If you work with a coding agent, [AGENTS.md](AGENTS.md) holds the short rules for it.

## Opening a pull request

Using AI tools is welcome. What matters is that a person stands behind the change:

- Keep a pull request to one problem, together with its tests and documentation. Send unrelated fixes separately; focused pull requests are easier to review.
- Read your whole diff before you ask for review, and be ready to explain it.
- Say what you tested and how, and what you could not test.
- Keep README additions to a line or two. Detail belongs in `docs/`.

## What happens next

Reviews can take me a while. I may ask for changes, or merge and tidy something up in a follow-up commit. Neither is a judgement of your work. If a pull request has been quiet for a while, a ping is welcome.

## Stuck?

Open a discussion, or a draft pull request with a question in it. Asking early is always fine.
