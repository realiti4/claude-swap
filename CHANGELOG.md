# Changelog

All notable changes to this project are documented here. Generated with
[git-cliff](https://git-cliff.org) from conventional commit messages; a
commit that doesn't match a known prefix lands in "Other". History before
this file was introduced can be backfilled with `git cliff --output CHANGELOG.md`
(config: `cliff.toml`) — this seed only covers commits since the last tagged
release, `v0.18.1`.

## [Unreleased]

### Features

- macOS menu bar app with usage stats, account switching, and auto-switch (#65)
- Desktop notifications, max-drain config, and setup docs

### Bug Fixes

- switch: never back up an empty current credential (Keychain timeout)

### Other

- Hide legacy `--flags` from `cswap` help so the bare subcommands are the one documented interface (#98)
- README updates
