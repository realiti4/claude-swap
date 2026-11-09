# Remote sync: how it works

Reference for `cswap remote`. Setting it up and everyday use are in the [README](../README.md#share-setup-tokens-between-machines-remote-sync).

## What can be shared

Only accounts added from a `claude setup-token` token (`sk-ant-oat…`, see [add-token](../README.md#add-an-account-from-a-raw-token-or-api-key)). Such a token never refreshes, so any number of machines can use it at the same time. A normal `/login` refreshes about every 8 hours, and each refresh switches off the previous access token straight away: copied to a second machine, it would sign the first one out at the next refresh. Those accounts are listed as *not shareable* (`n/a` in the dashboard), and so are API-key accounts.

## Commands

```bash
cswap remote add NAME URL [--branch B] [--ssh-key PATH | --token [-]] [--username U] [--no-remember]
cswap remote list                       # the repositories this machine knows (no network)
cswap remote show NAME                  # fetch and compare
cswap remote push NAME [ACCOUNT...] [--yes]
cswap remote pull NAME [ENTRY...] [--yes] [--replace]
cswap remote add-token NAME [TOKEN|-] [--email E] [--alias A]
cswap remote rm-entry NAME ENTRY [--yes]
cswap remote skip NAME ENTRY [--undo]
cswap remote passwd NAME
cswap remote forget NAME                # stop saving the passphrase on this machine
cswap remote set NAME [--url U] [--branch B] [--ssh-key PATH | --ssh-default | --token [-]] [--username U]
cswap remote remove NAME [--yes]        # forget it here; the repository is not changed
```

`ACCOUNT` is a local account (number, email or alias). `ENTRY` is an account in the repository: its email label, its alias, the first characters of its entry id (from `show`), or the part of the label before `@` when that is unique.

The dashboard has the same operations under *Remote sync…*: pick a repository, then an account to see what applies to it.

## Matching accounts

The first time a repository and a machine meet, accounts are matched by token: a local setup-token account and a repository entry holding the same token are the same account, whatever their labels. From then on the local account records the entry's id (`remotes: {<repository id>: <entry id>}` on its record in `sequence.json`), so a renewed token on either side shows up as *differs* instead of as a new account.

| State | Meaning |
|-------|---------|
| in sync | same token on both sides |
| differs | linked, but the tokens are different; push or pull asks which to keep |
| here only | a local setup-token account the repository doesn't have |
| repo only | a repository entry this machine doesn't have |
| skipped | a repository entry marked *not on this machine* (`skip`) |
| unreadable | an entry file that doesn't decrypt or validate; left alone |
| not shareable | a refreshing login or an API key |

A local account with the same email label as an entry is **not** a match, because labels are not unique across machines: two machines can each have a `setup-token-3@token.local` that are different accounts.

## What push and pull do

`push NAME` with no accounts named adds every *here only* account, except:

- one that was in the repository and has been removed from it (`rm-entry` on any machine). Name it to add it again.
- one whose email label the repository already holds with a different token. Pull that entry to take its token, or name the account to add it as a second entry.

For every *differs* account it asks whether to replace the repository's copy. `--yes` accepts all of them; without a terminal and without `--yes` the repository keeps its copy.

`pull NAME` with no entries named adds every *repo only* entry, except skipped ones and any whose email label a local account already uses (cswap keeps one token account per label). Naming an entry pulls it even when skipped, and clears the skip. `--replace` lets a named entry overwrite the token of the local account with its label. For every *differs* account it asks whether to replace this machine's token.

`add-token` puts a token in the repository without adding an account on this machine. Without `--email` the entry gets a placeholder label.

## Git login

- **Default:** your SSH agent and default keys, as plain `git` would use them.
- **`--ssh-key PATH`:** that key only (`IdentitiesOnly`).
- **`--token`:** an HTTPS access token (for example a fine-grained GitHub token with read/write *Contents* on that one repository). `--token -` reads it from stdin, and `--token` on its own asks for it. A token typed on the command line is refused, because it would end up in your shell history. git receives it through a credential helper fed from the environment, so it is never in a process's arguments or in `.git/config`. It is saved like the passphrase (see below).

git never prompts: SSH runs in batch mode with no terminal. A host whose key SSH doesn't know yet fails with a hint, so connect once with `ssh -T git@github.com` (or your host) to accept the key.

## The passphrase

Each repository has one passphrase, at least 8 characters. Every machine needs it, and it decrypts every account in that repository.

It is saved on each machine unless you pass `--no-remember`. On macOS it goes in the Keychain. Elsewhere it goes in a 0600 file in the cswap data folder, which is the same protection cswap gives stored logins on those platforms. `cswap remote forget NAME` stops saving it. `CSWAP_REMOTE_PASSPHRASE` supplies it for scripts and takes precedence over the saved one. If the saved passphrase stops working because it was changed on another machine, cswap asks for the new one and saves that.

`passwd` re-encrypts every entry under a new passphrase, with a new salt. **It does not rewrite git history:** older commits can still be decrypted with the old passphrase. If the old passphrase may have leaked, treat the tokens as leaked too, and replace them with new setup-tokens.

## Repository format

```
claude-swap-remote.json   plain JSON: format version, scrypt parameters and salt,
                          and an encrypted check value
accounts/<uuid4>.enc      one per account
```

Each entry is AES-256-GCM, keyed by scrypt from the passphrase (`n=2^15, r=8, p=1`), with a fresh nonce per write and the entry id as associated data. A file renamed to another entry's id therefore fails to decrypt. Encrypted inside each entry: the email label, alias, token, and when and from which host it was last updated.

Someone who can read the repository but doesn't have the passphrase learns how many accounts it holds and when each one changed (from the git history). They don't learn labels or tokens. Commits are authored as `claude-swap <claude-swap@localhost>`.

Before it uses them, cswap checks the scrypt parameters in a manifest against an upper bound. It also checks that every decrypted entry holds a setup-token and a valid label. cswap refuses to initialize a repository that already contains other files (a README, LICENSE or .gitignore is fine).

## Concurrency

cswap keeps its own clone of each repository and resets it to the remote branch before every command, so it never merges. Don't edit that clone; work in a separate clone if you need to look at the files. If another machine pushes between cswap's fetch and its push, cswap fetches again and redoes its change on the newer state, up to four times. A lock per repository keeps two cswap processes on one machine from working on it at once.

## Files

In the cswap data folder (see [Data locations](../README.md#data-locations)):

- `remotes.json`: the repositories (name, URL, branch, login type, skipped entries). It holds no secrets.
- `remotes/<id>/repo/`: cswap's clone.
- `remotes/<id>/secrets.json`: the saved passphrase and HTTPS token, on platforms other than macOS (0600).

`cswap remote remove` deletes these for one repository, and `cswap purge` deletes them all (including the Keychain items). Neither one changes the repository.
