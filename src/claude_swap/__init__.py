"""Multi-account switcher for Claude Code."""

# Both names load on first use (PEP 562), so importing a light submodule --
# ``claude_swap.statusline`` runs after every Claude Code response -- does not
# pay for the whole switcher.
__all__ = ["ClaudeAccountSwitcher", "__version__"]


def __getattr__(name: str):
    if name == "__version__":
        from importlib.metadata import version

        value = version("claude-swap")
    elif name == "ClaudeAccountSwitcher":
        from claude_swap.switcher import ClaudeAccountSwitcher as value
    else:
        raise AttributeError(f"module 'claude_swap' has no attribute {name!r}")
    globals()[name] = value
    return value
