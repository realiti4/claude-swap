# Modern menu-bar prototype

Throwaway AppKit/PyObjC exploration of a scrollable account-capacity popover. It is deliberately isolated from the application: its nine accounts and every usage reading are in `modern_menubar.py` fixture data.

## Run

Use a macOS Python environment that already provides PyObjC's `AppKit` bridge:

```bash
python3 prototypes/modern_menubar.py
```

Click the SF Symbol in the menu bar. The popover is transient and closes when focus moves away; use **Quit Prototype** to exit the app. Refresh, **Make Active**, and **Launch Isolated Session** only update visible prototype feedback. They do not access project services, stored credentials, the network, a terminal, or account state.

## Validate fixture shaping

```bash
python3 -m pytest tests/test_modern_menubar_prototype.py -q
```

Delete this prototype or fold its validated visual decisions into the production implementation once the direction is chosen.
