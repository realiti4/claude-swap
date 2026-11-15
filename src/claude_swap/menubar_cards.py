"""Native graphical account rows for the macOS menu."""
import time
import weakref

import AppKit as A

from claude_swap.menubar_locale import duration


def color(r, g, b, a=1):
    return A.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


BLUE = color(.22, .43, .91)
ORANGE = color(.86, .48, .12)
RED = color(.86, .22, .25)
GREEN = color(.16, .58, .45)


def box(rect, fill, radius=6):
    fill.setFill()
    A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()


def label(text, x, y, width, size=11, ink=None, bold=False, right=False):
    font = (A.NSFont.boldSystemFontOfSize_ if bold else A.NSFont.systemFontOfSize_)(size)
    style = A.NSMutableParagraphStyle.alloc().init()
    if right:
        style.setAlignment_(A.NSTextAlignmentRight)
    style.setLineBreakMode_(A.NSLineBreakByTruncatingTail)
    A.NSString.stringWithString_(str(text)).drawInRect_withAttributes_(
        ((x, y), (width, size + 5)),
        {A.NSFontAttributeName: font,
         A.NSForegroundColorAttributeName: ink or A.NSColor.labelColor(),
         A.NSParagraphStyleAttributeName: style},
    )


class AccountUsageCard(A.NSView):
    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        from claude_swap.menubar import _live_countdown, _rolled_weekly_window

        d = self.data
        tr = self.owner()._t
        w = self.bounds().size.width
        active = d['active']
        h = self.bounds().size.height
        secondary = A.NSColor.secondaryLabelColor()
        surface = BLUE.colorWithAlphaComponent_(.085) if active else A.NSColor.labelColor().colorWithAlphaComponent_(.035)
        box(((7, 2), (w - 14, h - 4)), surface, 10)
        label(d['name'], 21, 10, 98, 14, bold=True)
        label(d['email'], 122, 13, w - 210, 10, secondary)
        rejected = isinstance(d['usage'], dict) and d['usage'].get('rejected')
        state = tr('Rejected') if rejected else tr('Disabled') if d['disabled'] else tr('Active') if active else ''
        if state:
            tone = ORANGE if rejected or d['disabled'] else BLUE
            badge_width = 68 if rejected else 47
            box(((w - badge_width - 20, 10), (badge_width, 18)), tone.colorWithAlphaComponent_(.12), 9)
            label(state, w - badge_width - 12, 12, badge_width - 8, 10, tone, True)
        usage = d['usage']
        if not isinstance(usage, dict):
            label(str(usage or tr('Usage unavailable')), 24, 63, w - 48, 12, ORANGE)
            return
        now = time.time()
        windows = [(tr('5 hours'), usage.get('five_hour'), d['threshold']),
                   (tr('Weekly'), _rolled_weekly_window(usage.get('seven_day'), now), d['weekly'])]
        for scoped in usage.get('scoped') or []:
            windows.append((scoped.get('name', tr('Model')), _rolled_weekly_window(scoped, now), None))
        if len(windows) < 3:
            windows.append((tr('Model quota'), None, None))
        col = (w - 42 - 36) / 3
        # Additional scoped models expand the card vertically rather than disappearing.
        for i, (name, window, threshold) in enumerate(windows):
            x = 21 + (i % 3) * (col + 18)
            y = 39 + (i // 3) * 58
            value = window.get('pct') if isinstance(window, dict) else None
            valid = isinstance(value, (int, float))
            tint = RED if valid and value >= (threshold or 97) else ORANGE if valid and value >= 80 else BLUE if i == 0 else GREEN
            label(name, x, y, col - 44, 10, A.NSColor.secondaryLabelColor())
            label(f'{value:.0f}%' if valid else '—', x + col - 42, y - 3, 42, 14, tint if valid else A.NSColor.tertiaryLabelColor(), True, right=True)
            box(((x, y + 21), (col, 5)), A.NSColor.separatorColor().colorWithAlphaComponent_(.22), 2.5)
            if valid and value > 0:
                box(((x, y + 21), (max(3, col * min(100, max(0, value)) / 100), 5)), tint, 2.5)
            if threshold is not None:
                # A tick makes the configured switch line visible on each quota bar.
                box(((x + col * threshold / 100 - .5, y + 19), (1, 9)), A.NSColor.secondaryLabelColor().colorWithAlphaComponent_(.65), .5)
            reset = _live_countdown(window, now) if valid else None
            text = (tr("Resets in {time}", time=duration(d["language"], reset))
                    if reset else tr("No reset time") if valid else tr("No data"))
            label(text, x, y + 29, col, 10, A.NSColor.secondaryLabelColor())
        if d['expiry']:
            label(d['expiry'], 24, self.bounds().size.height - 22, w - 48, 10, ORANGE)

    def mouseUp_(self, event):
        if not A.NSPointInRect(self.convertPoint_fromView_(event.locationInWindow(), None), self.bounds()):
            return
        owner = self.owner()
        if owner is not None:
            menu = owner.menu._menu
            if menu is not None:
                menu.cancelTracking()
            owner._make_switch_to(self.data['num'])(None)


def attach_card(item, owner, num, email, active, usage, alias, disabled,
                login_expires_at, quarantined, settings):
    from claude_swap import oauth
    tr = owner._t
    expiry = oauth.format_login_expiry(login_expires_at, quarantined, time.time())
    if expiry:
        time_left = expiry.removeprefix("re-login in ")
        expiry = (tr("Sign in again") if time_left == expiry else
                  tr("Sign in again in {time}", time=duration(owner.settings.language, time_left)))
    count = 2 + (len(usage.get('scoped') or []) if isinstance(usage, dict) else 0)
    extra_rows = max(0, (count - 1) // 3)
    height = 94 + extra_rows * 58 + (22 if expiry else 0)
    view = AccountUsageCard.alloc().initWithFrame_(((0, 0), (470, height)))
    view.owner = weakref.ref(owner)
    view.data = dict(num=num, email=email, active=active, usage=usage,
                     language=owner.settings.language, name=alias or tr('Account {num}', num=num), disabled=disabled, expiry=expiry,
                     threshold=settings.threshold,
                     weekly=settings.weekly_threshold or settings.threshold)
    # Keep the original full row as tooltip/accessibility text and preserve native actions.
    view.setToolTip_(item.title)
    view.setAccessibilityElement_(True)
    view.setAccessibilityLabel_(item.title)
    view.setAccessibilityRole_(A.NSAccessibilityButtonRole)
    item._menuitem.setView_(view)
    item._account_card = view
