"""Tests for the directory → account mapping store (claude_swap.mappings)."""

from __future__ import annotations

from pathlib import Path

from claude_swap.mappings import MappingStore, normalize_path


def test_set_then_get_exact(tmp_path: Path):
    backup = tmp_path / "backup"
    repo = tmp_path / "work" / "app"
    repo.mkdir(parents=True)
    store = MappingStore(backup)

    store.set(repo, "work@co.com", "org-1")

    entry = store.get(repo)
    assert entry is not None
    assert entry["email"] == "work@co.com"
    assert entry["organizationUuid"] == "org-1"
    assert entry["added"]  # timestamp present


def test_get_missing_returns_none(tmp_path: Path):
    store = MappingStore(tmp_path / "backup")
    assert store.get(tmp_path / "nope") is None


def test_resolve_exact_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = MappingStore(tmp_path / "backup")
    store.set(repo, "a@x.com", "")

    match = store.resolve(repo)
    assert match is not None
    assert match[0] == normalize_path(repo)
    assert match[1]["email"] == "a@x.com"


def test_resolve_nested_subdir_inherits(tmp_path: Path):
    repo = tmp_path / "repo"
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    store = MappingStore(tmp_path / "backup")
    store.set(repo, "a@x.com", "")

    match = store.resolve(sub)
    assert match is not None
    assert match[1]["email"] == "a@x.com"


def test_resolve_longest_ancestor_wins(tmp_path: Path):
    outer = tmp_path / "work"
    inner = outer / "client"
    cwd = inner / "src"
    cwd.mkdir(parents=True)
    store = MappingStore(tmp_path / "backup")
    store.set(outer, "outer@x.com", "")
    store.set(inner, "inner@x.com", "")

    match = store.resolve(cwd)
    assert match is not None
    assert match[1]["email"] == "inner@x.com"


def test_resolve_sibling_prefix_does_not_match(tmp_path: Path):
    mapped = tmp_path / "foo" / "bar"
    sibling = tmp_path / "foo" / "barbaz"
    mapped.mkdir(parents=True)
    sibling.mkdir(parents=True)
    store = MappingStore(tmp_path / "backup")
    store.set(mapped, "a@x.com", "")

    assert store.resolve(sibling) is None


def test_resolve_unmapped_returns_none(tmp_path: Path):
    store = MappingStore(tmp_path / "backup")
    store.set(tmp_path / "a", "a@x.com", "")
    other = tmp_path / "b"
    other.mkdir()
    assert store.resolve(other) is None


def test_remove(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = MappingStore(tmp_path / "backup")
    store.set(repo, "a@x.com", "")

    assert store.remove(repo) is True
    assert store.get(repo) is None
    assert store.remove(repo) is False  # already gone


def test_set_overwrites_same_key(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = MappingStore(tmp_path / "backup")
    store.set(repo, "a@x.com", "")
    store.set(repo, "b@x.com", "org-9")

    entry = store.get(repo)
    assert entry["email"] == "b@x.com"
    assert entry["organizationUuid"] == "org-9"
    assert len(store.all()) == 1


def test_prune_account(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
    store = MappingStore(tmp_path / "backup")
    store.set(a, "work@x.com", "org-1")
    store.set(b, "work@x.com", "org-1")
    store.set(c, "personal@x.com", "")

    removed = store.prune_account("work@x.com", "org-1")

    assert removed == 2
    assert store.get(a) is None
    assert store.get(b) is None
    assert store.get(c) is not None


def test_load_missing_file_is_empty(tmp_path: Path):
    store = MappingStore(tmp_path / "backup")
    assert store.load() == {}
    assert store.all() == {}
    assert store.resolve(tmp_path) is None


def test_load_corrupt_file_is_empty(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "mappings.json").write_text("{ not json", encoding="utf-8")
    store = MappingStore(backup)
    assert store.load() == {}


def test_normalize_path_expands_and_resolves(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # Relative + trailing slash + "." segment all collapse to one key.
    a = normalize_path(repo)
    b = normalize_path(str(repo) + "/")
    c = normalize_path(repo / "." )
    assert a == b == c


def test_persisted_schema(tmp_path: Path):
    import json
    backup = tmp_path / "backup"
    repo = tmp_path / "repo"
    repo.mkdir()
    store = MappingStore(backup)
    store.set(repo, "a@x.com", "org-1")

    data = json.loads((backup / "mappings.json").read_text())
    assert data["schemaVersion"] == 1
    assert normalize_path(repo) in data["mappings"]


def test_normalize_path_applies_normcase(monkeypatch, tmp_path: Path):
    """normalize_path runs paths through os.path.normcase (Windows case-fold)."""
    import claude_swap.mappings as m

    calls = []

    def fake_normcase(s):
        calls.append(s)
        return s.lower()

    monkeypatch.setattr(m.os.path, "normcase", fake_normcase)
    repo = tmp_path / "Repo"
    repo.mkdir()

    key = m.normalize_path(repo)

    assert calls, "normalize_path did not call os.path.normcase"
    assert key == key.lower(), "normcase result was not applied to the key"
