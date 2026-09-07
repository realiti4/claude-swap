"""Codex path resolution: ~/.codex on the read side, cswap's own store on the write side."""

from __future__ import annotations

from pathlib import Path

from claude_swap.codex import paths as cpaths


def test_codex_home_defaults_to_dot_codex(temp_home: Path):
    assert cpaths.get_codex_home() == temp_home / ".codex"


def test_codex_home_honours_the_env_var(temp_home: Path, monkeypatch):
    """The codex CLI reads CODEX_HOME; cswap must resolve the same file it does."""
    monkeypatch.setenv("CODEX_HOME", str(temp_home / "elsewhere"))
    assert cpaths.get_codex_home() == temp_home / "elsewhere"


def test_live_auth_path_sits_directly_in_codex_home(temp_home: Path):
    assert cpaths.get_live_auth_path() == temp_home / ".codex" / "auth.json"


def test_legacy_registry_path_points_at_codex_auth_data(temp_home: Path):
    assert (
        cpaths.get_codex_auth_registry_path()
        == temp_home / ".codex" / "accounts" / "registry.json"
    )


def test_store_root_is_a_subtree_of_the_cswap_backup_root(temp_home: Path):
    from claude_swap.paths import get_backup_root

    root = cpaths.get_codex_store_root()
    assert root == get_backup_root() / "codex"
    assert root.parent == get_backup_root()


def test_store_paths_hang_off_the_store_root(temp_home: Path):
    root = cpaths.get_codex_store_root()
    assert cpaths.get_codex_sequence_path() == root / "sequence.json"
    assert cpaths.get_codex_credentials_dir() == root / "credentials"
    assert cpaths.get_codex_cache_dir() == root / "cache"
    assert cpaths.get_codex_lock_path() == root / ".lock"
