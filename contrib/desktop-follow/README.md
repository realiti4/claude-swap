# desktop-follow (Windows)

Bills Claude Desktop's Code tab to the account `cswap` has active. The stock
route, cswap-pin's `pin.desktopFollows`, does not run on Windows (POSIX file
locks and FIFOs), so this uses a small [mitmproxy](https://mitmproxy.org) addon
instead. Only `/v1/messages` is rewritten; profile, usage, OAuth and Remote
Control keep the app's own login.

## Setup

```powershell
uv tool install mitmproxy
mitmdump -p 8899 -s <path-to>\follow_active.py
```

Run `mitmdump` once so it creates `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem`, then in
Claude Desktop's Code tab add a local environment with:

```
HTTPS_PROXY=http://127.0.0.1:8899
NODE_EXTRA_CA_CERTS=C:\Users\<you>\.mitmproxy\mitmproxy-ca-cert.pem
```

Start a new Code session; running sessions keep their old proxy. Then
`cswap switch N` moves new requests of that session to account N.

While it runs, the addon lists `claude-desktop` in
`~/.claude-swap-backup/pin-proxy/inference-follows` (the file cswap-pin writes on
macOS and Linux) and removes it on a clean exit. The swap-meter band reads that
file to show the active account as billed in desktop sessions. If the proxy is
killed rather than stopped, delete the file by hand, or the band keeps saying the
desktop follows the switch.

## Limits

- It reads `~/.claude/.credentials.json` (or `CLAUDE_CONFIG_DIR`) and never
  refreshes. An expired token is not substituted, so the app's login is used
  instead. Run `claude` in a terminal on that account to refresh it.
- While the proxy is down, Code sessions started with the variables set cannot connect.
- Not covered: Remote Control and Artifacts (they stay on the app's account).
