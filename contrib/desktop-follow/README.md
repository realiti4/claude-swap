# desktop-follow (Windows)

Bills Claude Desktop's Code tab to the account `cswap` has active. The Code tab
runs its CLI with the app's own login, so `cswap switch` never reaches it.
cswap-pin re-bills it on macOS and Linux (`cswap pin`), but needs POSIX file
locks and FIFOs, so it refuses Windows. This is the Windows route.

```powershell
uv tool install mitmproxy
cswap desktop-follow --on
```

`--on` does what `cswap pin` does:

- starts a [mitmproxy](https://mitmproxy.org) detached on `127.0.0.1:8899` (`--port N` to change it) with its own CA under `~\.claude-swap-backup\desktop-follow`;
- writes `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` into the `env` block of Claude Code's global config (`~\.claude.json`), so every new session uses it and no Code tab environment is needed. A variable you already set to something else is left alone;
- registers a Scheduled Task, `cswap-desktop-follow`, that runs `--ensure` every minute: it starts the proxy after logon and restarts it if it dies.

Start a new Code session afterwards. Running sessions keep their old connection.

| Command | What it does |
| --- | --- |
| `cswap desktop-follow --on` | Turn it on (above). |
| `cswap desktop-follow --off` | Stop the proxy, remove exactly the variables `--on` wrote, delete the task. |
| `cswap desktop-follow --state` | Print `OK`, `NOT-OK` or `OFF`; detail on stderr. |
| `cswap desktop-follow --heal` | Restart a dead proxy. If it cannot be restarted, remove the variables so sessions go direct instead of failing, and say so. |
| `cswap desktop-follow --ensure` | The same, silently. This is what the task runs. |

## What the proxy changes

Only the bearer on `/v1/messages`, swapped for the active account's access token
from `~/.claude/.credentials.json` (or `CLAUDE_CONFIG_DIR`). Profile, usage, OAuth
and Remote Control keep the app's own login. It never refreshes or writes a
credential, and a token that has expired is not substituted, so the app's login
keeps working. While it runs it lists `claude-desktop` in
`~\.claude-swap-backup\pin-proxy\inference-follows`, which the swap-meter band
reads to show the right account as billed.

## Limits

- The token is only refreshed when a terminal `claude` runs on the active account.
- Terminal sessions use the proxy too (the variables are global). They get the same token they would have used anyway.
- Remote Control and Artifacts stay on the app's account.
- Every session goes through a local proxy that handles your tokens. It listens on loopback only.
