import AppKit
import SwiftUI

@main
struct ClaudeSwapMenuBarPrototypeApp: App {
    var body: some Scene {
        MenuBarExtra("Claude Swap Prototype", systemImage: "arrow.trianglehead.2.clockwise.rotate.90") {
            FixturePopover()
        }
        .menuBarExtraStyle(.window)
    }
}

private enum Freshness: String, Sendable {
    case fresh
    case stale
    case unavailable

    var title: String {
        switch self {
        case .fresh:
            "Fresh"
        case .stale:
            "Stale"
        case .unavailable:
            "Unavailable"
        }
    }
}

private enum CapacityState: Sendable {
    case available
    case nearLimit
    case limitReached
    case unavailable

    init(usedPercent: Int?) {
        guard let usedPercent else {
            self = .unavailable
            return
        }
        if usedPercent >= 90 {
            self = .limitReached
        } else if usedPercent >= 70 {
            self = .nearLimit
        } else {
            self = .available
        }
    }

    var title: String {
        switch self {
        case .available:
            "Available"
        case .nearLimit:
            "Near limit"
        case .limitReached:
            "Limit reached"
        case .unavailable:
            "Unavailable"
        }
    }

    var tint: Color {
        switch self {
        case .available:
            Color(nsColor: .systemGreen)
        case .nearLimit:
            Color(nsColor: .systemOrange)
        case .limitReached:
            Color(nsColor: .systemRed)
        case .unavailable:
            Color(nsColor: .tertiaryLabelColor)
        }
    }
}

private struct UsageFixture: Identifiable, Sendable {
    let label: String
    let usedPercent: Int?
    let resetText: String
    let scope: String

    var id: String { label }
    var capacityState: CapacityState { CapacityState(usedPercent: usedPercent) }

    var accessibilityDescription: String {
        let usageText = usedPercent.map { "\($0) percent used" } ?? "usage unknown"
        return "\(label), \(usageText), \(capacityState.title), \(resetText)"
    }
}

private struct AccountFixture: Identifiable, Sendable {
    let slot: Int
    let alias: String
    let email: String
    let isActive: Bool
    let isHeld: Bool
    let freshness: Freshness
    let freshnessDetail: String
    let usage: [UsageFixture]

    var id: Int { slot }

    var capacitySummary: String {
        let availableCapacity = usage.compactMap { item in
            item.usedPercent.map { 100 - $0 }
        }
        guard let minimumCapacity = availableCapacity.min() else {
            return "Capacity unavailable"
        }
        return "\(minimumCapacity)% minimum capacity"
    }
}

private enum FixtureData {
    static let rotationPreviewThreshold = 85

    static let accounts: [AccountFixture] = [
        AccountFixture(
            slot: 1,
            alias: "studio",
            email: "studio@example.test",
            isActive: true,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated just now",
            usage: [
                UsageFixture(label: "5h", usedPercent: 42, resetText: "Resets in 2h 14m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 57, resetText: "Resets Thu, 9:00 AM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 18, resetText: "Resets Thu, 9:00 AM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 2,
            alias: "research",
            email: "research@example.test",
            isActive: false,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated 2 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 68, resetText: "Resets in 1h 08m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 42, resetText: "Resets Wed, 6:00 PM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 98, resetText: "Resets Wed, 6:00 PM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 3,
            alias: "personal",
            email: "personal@example.test",
            isActive: false,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated 4 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 6, resetText: "Resets in 4h 31m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 24, resetText: "Resets Fri, 7:00 AM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 12, resetText: "Resets Fri, 7:00 AM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 4,
            alias: "agency",
            email: "agency@example.test",
            isActive: false,
            isHeld: true,
            freshness: .fresh,
            freshnessDetail: "Updated 6 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 84, resetText: "Resets in 46m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 91, resetText: "Resets Tue, 2:00 PM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 100, resetText: "Resets Tue, 2:00 PM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 5,
            alias: "night shift",
            email: "night@example.test",
            isActive: false,
            isHeld: false,
            freshness: .stale,
            freshnessDetail: "Last confirmed 19 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 11, resetText: "Reset estimate in 3h 41m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 16, resetText: "Reset estimate Fri, 9:00 AM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 9, resetText: "Reset estimate Fri, 9:00 AM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 6,
            alias: "archive",
            email: "archive@example.test",
            isActive: false,
            isHeld: false,
            freshness: .unavailable,
            freshnessDetail: "No fixture reading available",
            usage: [
                UsageFixture(label: "5h", usedPercent: nil, resetText: "Usage unavailable", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: nil, resetText: "Usage unavailable", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: nil, resetText: "Usage unavailable", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 7,
            alias: "automation",
            email: "automation@example.test",
            isActive: false,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated 8 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 36, resetText: "Resets in 2h 53m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 71, resetText: "Resets Thu, 1:00 PM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 53, resetText: "Resets Thu, 1:00 PM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 8,
            alias: "sandbox",
            email: "sandbox@example.test",
            isActive: false,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated 11 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 73, resetText: "Resets in 1h 27m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 38, resetText: "Resets Sat, 10:00 AM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 64, resetText: "Resets Sat, 10:00 AM", scope: "model")
            ]
        ),
        AccountFixture(
            slot: 9,
            alias: "travel",
            email: "travel@example.test",
            isActive: false,
            isHeld: false,
            freshness: .fresh,
            freshnessDetail: "Updated 14 min ago",
            usage: [
                UsageFixture(label: "5h", usedPercent: 21, resetText: "Resets in 3h 19m", scope: "rolling"),
                UsageFixture(label: "Weekly", usedPercent: 8, resetText: "Resets Mon, 8:00 AM", scope: "weekly"),
                UsageFixture(label: "Fable", usedPercent: 33, resetText: "Resets Mon, 8:00 AM", scope: "model")
            ]
        )
    ]
}

private struct FixturePopover: View {
    @State private var feedback = "Choose an action to preview local, non-mutating feedback."

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(Array(FixtureData.accounts.enumerated()), id: \.element.id) { index, account in
                        AccountGroup(
                            account: account,
                            onMakeActive: { showMakeActiveFeedback(for: account) },
                            onLaunchSession: { showLaunchFeedback(for: account) }
                        )

                        if index < FixtureData.accounts.count - 1 {
                            Divider()
                        }
                    }
                }
                .padding(.horizontal, 12)
            }
            .frame(maxHeight: .infinity)
            Divider()
            footer
        }
        .frame(width: 520, height: 680)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Claude Swap fixture-only account capacity prototype")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text("CLAUDE SWAP")
                    .font(.system(.caption, design: .monospaced, weight: .bold))
                Spacer()
                Text("\(FixtureData.accounts.count) fixtures · local only")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                Button(action: refreshFixture) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help("Refresh the local fixture display. No request is made.")
                .accessibilityLabel("Refresh local fixture display")
            }

            Text("Fixture-only: no credentials, switching, sessions, or requests.")
                .font(.caption2)
                .foregroundStyle(.secondary)

            Text("rotation preview threshold  \(FixtureData.rotationPreviewThreshold)%")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Text(feedback)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .accessibilityLabel("Prototype feedback: \(feedback)")

            Spacer(minLength: 8)

            Button("Quit Prototype", action: quitPrototype)
                .controlSize(.small)
                .accessibilityLabel("Quit Claude Swap menu bar prototype")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private func refreshFixture() {
        feedback = "Fixture display refreshed locally. No request was made."
    }

    private func showMakeActiveFeedback(for account: AccountFixture) {
        feedback = "Prototype only: would make \(account.alias) active. Nothing changed."
    }

    private func showLaunchFeedback(for account: AccountFixture) {
        feedback = "Prototype only: would launch an isolated session for \(account.alias)."
    }

    private func quitPrototype() {
        NSApplication.shared.terminate(nil)
    }
}

private struct AccountGroup: View {
    let account: AccountFixture
    let onMakeActive: () -> Void
    let onLaunchSession: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(headerText)
                .font(.system(.caption, design: .monospaced, weight: .medium))
                .lineLimit(1)
                .truncationMode(.tail)

            VStack(spacing: 3) {
                ForEach(account.usage) { usage in
                    UsageRow(usage: usage, isStale: account.freshness == .stale)
                }
            }

            HStack(spacing: 6) {
                Button("Make Active", action: onMakeActive)
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Make \(account.alias) active, prototype only")
                    .help("Shows local prototype feedback only. No account is changed.")

                Button("Launch Isolated Session", action: onLaunchSession)
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Launch isolated session for \(account.alias), prototype only")
                    .help("Shows local prototype feedback only. No session is launched.")
            }
            .controlSize(.mini)
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accountAccessibilityLabel)
    }

    private var headerText: String {
        "\(String(format: "%02d", account.slot))  \(account.alias) (\(account.email))  [\(stateText)]  \(account.freshness.title.lowercased()) · \(account.freshnessDetail)"
    }

    private var stateText: String {
        if account.isActive {
            "active"
        } else if account.isHeld {
            "held"
        } else {
            "ready"
        }
    }

    private var accountAccessibilityLabel: String {
        "Slot \(account.slot), \(account.alias), \(account.email), \(stateText), usage \(account.freshness.title.lowercased()). \(account.freshnessDetail). \(account.capacitySummary)"
    }
}

private struct UsageRow: View {
    let usage: UsageFixture
    let isStale: Bool

    var body: some View {
        HStack(spacing: 6) {
            Text(usage.label)
                .frame(width: 42, alignment: .leading)

            GlyphMeter(usage: usage, isStale: isStale)
                .frame(width: 86, alignment: .leading)

            Text(percentText)
                .foregroundStyle(usage.capacityState.tint.opacity(isStale ? 0.55 : 1))
                .frame(width: 32, alignment: .trailing)

            if usage.usedPercent == nil {
                Text("usage unknown")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Text("resets")
                    .foregroundStyle(.secondary)
                    .frame(width: 40, alignment: .leading)

                Text(resetValue)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .font(.system(.caption, design: .monospaced))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(usage.accessibilityDescription)
    }

    private var percentText: String {
        usage.usedPercent.map { String(format: "%3d%%", $0) } ?? "  —"
    }

    private var resetValue: String {
        if let value = usage.resetText.stripPrefix("Reset estimate in ") {
            return "~\(value)"
        }
        if let value = usage.resetText.stripPrefix("Reset estimate ") {
            return "~\(value)"
        }
        if let value = usage.resetText.stripPrefix("Resets in ") {
            return value
        }
        return usage.resetText.stripPrefix("Resets ") ?? usage.resetText
    }
}

private struct GlyphMeter: View {
    let usage: UsageFixture
    let isStale: Bool

    private let width = 12
    private let threshold = FixtureData.rotationPreviewThreshold

    var body: some View {
        HStack(spacing: 0) {
            ForEach(0..<width, id: \.self) { index in
                Text(glyph(at: index))
                    .foregroundStyle(color(at: index))
            }
        }
        .font(.system(.caption, design: .monospaced))
        .accessibilityHidden(true)
    }

    private func glyph(at index: Int) -> String {
        guard let usedPercent = usage.usedPercent else {
            return "─"
        }
        let tickIndex = min(width - 1, max(0, Int((Double(threshold) / 100 * Double(width)).rounded())))
        if index == tickIndex {
            return "┃"
        }

        let filledCells = Double(usedPercent) / 100 * Double(width)
        let completeCells = Int(filledCells)
        if index < completeCells {
            return "━"
        }
        if index == completeCells, filledCells - Double(completeCells) >= 0.5 {
            return "╸"
        }
        return "─"
    }

    private func isFilled(at index: Int, usedPercent: Int) -> Bool {
        let filledCells = Double(usedPercent) / 100 * Double(width)
        let completeCells = Int(filledCells)
        return index < completeCells || (index == completeCells && filledCells - Double(completeCells) >= 0.5)
    }

    private func color(at index: Int) -> Color {
        guard let usedPercent = usage.usedPercent else {
            return Color(nsColor: .tertiaryLabelColor)
        }
        let tickIndex = min(width - 1, max(0, Int((Double(threshold) / 100 * Double(width)).rounded())))
        if index == tickIndex {
            return Color(nsColor: .systemOrange)
        }
        if isFilled(at: index, usedPercent: usedPercent) {
            return usage.capacityState.tint.opacity(isStale ? 0.55 : 1)
        }
        return Color(nsColor: .tertiaryLabelColor)
    }
}

private extension String {
    func stripPrefix(_ prefix: String) -> String? {
        guard hasPrefix(prefix) else {
            return nil
        }
        return String(dropFirst(prefix.count))
    }
}
