"""Merging several providers into the one view a shell renders."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.providers.aggregate import (
    group_by_provider,
    merged_snapshot,
    provider_label,
)
from claude_swap.providers.registry import available_providers, codex_is_present
from claude_swap.usage_store import UsageEntry


class FakeProvider:
    """A provider that returns a canned snapshot and records what was asked."""

    def __init__(self, provider_id: str, numbers: list[str], *, raises: bool = False):
        self.provider_id = provider_id
        self._numbers = numbers
        self._raises = raises
        self.fetch_calls: list[set[str] | None] = []

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_calls.append(fetch)
        if self._raises:
            raise RuntimeError("store is corrupt")
        return AccountsSnapshot(
            active_number=self._numbers[0] if self._numbers else None,
            accounts=tuple(
                AccountSnapshot(
                    number=n,
                    email=f"{self.provider_id}-{n}@x",
                    org_name="",
                    org_uuid="",
                    is_active=n == self._numbers[0],
                    kind="oauth",
                    switchable=True,
                    usage=UsageEntry(),
                    provider=self.provider_id,
                )
                for n in self._numbers
            ),
            taken_at=0.0,
            provider=self.provider_id,
        )


def test_row_keys_stay_unique_across_providers():
    """Slot numbers are per-provider: "1" names a Claude account AND a Codex
    one. Without a composite key a keystroke lands on the wrong provider."""
    snap, owners = merged_snapshot(
        [FakeProvider("claude", ["1", "2"]), FakeProvider("codex", ["1", "2"])]
    )
    assert [a.key for a in snap.accounts] == [
        "claude:1",
        "claude:2",
        "codex:1",
        "codex:2",
    ]
    assert len(owners) == 4


def test_owners_map_each_row_to_the_provider_that_owns_it():
    claude, codex = FakeProvider("claude", ["1"]), FakeProvider("codex", ["1"])
    _snap, owners = merged_snapshot([claude, codex])
    assert owners["claude:1"] is claude
    assert owners["codex:1"] is codex


def test_ordering_is_provider_major_and_stable():
    """Rows must not move between refreshes, or a keystroke aimed at the row
    under the cursor lands somewhere else."""
    providers = [FakeProvider("claude", ["1", "2"]), FakeProvider("codex", ["1"])]
    first = [a.key for a in merged_snapshot(providers)[0].accounts]
    second = [a.key for a in merged_snapshot(providers)[0].accounts]
    assert first == second == ["claude:1", "claude:2", "codex:1"]


def test_active_number_still_means_claude():
    """Every existing consumer of that field means Claude by it."""
    snap, _ = merged_snapshot(
        [FakeProvider("claude", ["3", "4"]), FakeProvider("codex", ["1"])]
    )
    assert snap.active_number == "3"


def test_each_row_carries_its_own_active_flag():
    snap, _ = merged_snapshot(
        [FakeProvider("claude", ["1", "2"]), FakeProvider("codex", ["5", "6"])]
    )
    active = {a.key for a in snap.accounts if a.is_active}
    assert active == {"claude:1", "codex:5"}


def test_one_providers_failure_does_not_blank_the_others():
    """A dashboard that goes empty because a secondary store is corrupt is far
    worse than one missing section."""
    snap, owners = merged_snapshot(
        [FakeProvider("claude", ["1"]), FakeProvider("codex", ["1"], raises=True)]
    )
    assert [a.key for a in snap.accounts] == ["claude:1"]
    assert list(owners) == ["claude:1"]


def test_fetch_none_is_passed_through_to_every_provider():
    claude, codex = FakeProvider("claude", ["1"]), FakeProvider("codex", ["1"])
    merged_snapshot([claude, codex], fetch=None)
    assert claude.fetch_calls == [None]
    assert codex.fetch_calls == [None]


def test_a_fetch_set_is_split_per_provider():
    claude, codex = FakeProvider("claude", ["1", "2"]), FakeProvider("codex", ["1"])
    merged_snapshot([claude, codex], fetch={"claude:2", "codex:1"})
    assert claude.fetch_calls == [{"2"}]
    assert codex.fetch_calls == [{"1"}]


def test_a_provider_named_by_no_fetch_key_gets_an_empty_set_not_none():
    """An empty set means "no network"; None would mean "fetch everything" —
    the opposite, and a silent one."""
    claude, codex = FakeProvider("claude", ["1"]), FakeProvider("codex", ["1"])
    merged_snapshot([claude, codex], fetch={"claude:1"})
    assert codex.fetch_calls == [set()]


def test_grouping_preserves_the_render_order():
    snap, _ = merged_snapshot(
        [FakeProvider("claude", ["1", "2"]), FakeProvider("codex", ["1"])]
    )
    groups = group_by_provider(snap)
    assert [(pid, [a.key for a in rows]) for pid, rows in groups] == [
        ("claude", ["claude:1", "claude:2"]),
        ("codex", ["codex:1"]),
    ]


def test_grouping_an_empty_snapshot_yields_nothing():
    snap = AccountsSnapshot(active_number=None, accounts=(), taken_at=0.0)
    assert group_by_provider(snap) == []


def test_provider_labels_are_short_enough_for_a_row_badge():
    assert provider_label("claude") == "claude"
    assert provider_label("codex") == "codex"
    assert provider_label("unknown") == "unknown"  # never blank


# ---- registry ----------------------------------------------------------


def test_codex_is_absent_on_a_clean_machine(temp_home: Path):
    assert codex_is_present() is False


def test_codex_is_present_once_it_has_a_slot(codex_home: Path):
    from claude_swap.codex.auth_file import account_key
    from claude_swap.codex.store import CodexStore

    CodexStore().upsert_slot(account_key("u", "a"), email="a@x")
    assert codex_is_present() is True


def test_codex_is_present_when_only_an_unimported_registry_exists(codex_home: Path):
    """So the provider shows up on the first run, not only after the user has
    found a `cswap codex` command."""
    d = codex_home / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text("{}")
    assert codex_is_present() is True


def test_a_claude_only_machine_gets_exactly_one_provider(temp_home: Path):
    """The whole point: nothing about a Claude-only install changes."""
    claude = FakeProvider("claude", ["1"])
    assert available_providers(claude) == [claude]


def test_codex_joins_the_list_once_present(codex_home: Path):
    from claude_swap.codex.auth_file import account_key
    from claude_swap.codex.store import CodexStore

    CodexStore().upsert_slot(account_key("u", "a"), email="a@x")
    claude = FakeProvider("claude", ["1"])
    providers = available_providers(claude)
    assert [p.provider_id for p in providers] == ["claude", "codex"]
