# SwiftUI menu-bar prototype

Throwaway, native macOS exploration of a scrollable account-capacity popover. It is isolated from `claude-swap`: every account, reset time, and capacity value is fixed in `ClaudeSwapMenuBarPrototype.swift`.

The prototype never reads credentials, opens a network connection, invokes Claude Code, controls Terminal, or changes account/session state. **Make Active**, **Launch Isolated Session**, and refresh only update in-app feedback. Use **Quit Prototype** to exit.

## Build

```bash
./prototypes/swiftui/build-and-run.sh build
```

The app bundle is produced and registered at:

```text
build/swiftui-menubar-prototype/ClaudeSwapMenuBarPrototype.app
```

## Build and run

```bash
./prototypes/swiftui/build-and-run.sh
```

Click the status-bar icon to open the custom SwiftUI popover. The app is an accessory app (`LSUIElement=true`), so it does not appear in the Dock.

Delete this prototype or fold its validated visual decisions into the production implementation once the direction is chosen.
