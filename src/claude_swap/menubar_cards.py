"""Native graphical account rows for the macOS menu."""
import time
import weakref

import AppKit as A
from objc import super

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
    def viewDidMoveToSuperview(self):
        super(AccountUsageCard, self).viewDidMoveToSuperview()
        parent = self.superview()
        if parent is not None:
            frame = self.frame()
            available = parent.bounds().size.width - frame.origin.x
            if available > frame.size.width:
                self.setFrameSize_((available, frame.size.height))

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
        surface = BLUE.colorWithAlphaComponent_(.16) if active else A.NSColor.labelColor().colorWithAlphaComponent_(.035)
        box(((7, 2), (w - 14, h - 4)), surface, 10)
        if active:
            box(((10, 12), (3, h - 24)), BLUE, 1.5)
        title = ('✓ ' if active else '') + d['name']
        font = A.NSFont.boldSystemFontOfSize_(13)
        title_width = min(110, A.NSString.stringWithString_(title).sizeWithAttributes_(
            {A.NSFontAttributeName: font}).width + 2)
        label(title, 19, 9, title_width, 13, BLUE if active else None, True)
        email_x = 19 + title_width + 8
        right_edge = w - 19
        rejected = isinstance(d['usage'], dict) and d['usage'].get('rejected')
        if rejected or d['disabled']:
            state = tr('Rejected') if rejected else tr('Disabled')
            right_edge -= 60
            label(state, right_edge, 11, 60, 10, ORANGE, right=True)
            right_edge -= 6
        small_font = A.NSFont.systemFontOfSize_(10)
        email_width = max(0, right_edge - email_x)
        if d['expiry']:
            attrs = {A.NSFontAttributeName: small_font}
            expiry = A.NSString.stringWithString_(d['expiry'])
            email = A.NSString.stringWithString_(d['email'])
            expiry_width = expiry.sizeWithAttributes_(attrs).width + 2
            email_width = min(email.sizeWithAttributes_(attrs).width + 2,
                              max(0, email_width - expiry_width - 6))
            label(expiry, email_x + email_width + 6, 11, expiry_width, 10, ORANGE)
        label(d['email'], email_x, 11, email_width, 10, secondary)
        usage = d['usage']
        if not isinstance(usage, dict):
            label(str(usage or tr('Usage unavailable')), 19, 42, w - 48, 12, ORANGE)
            return
        now = time.time()
        windows = [(tr('5 hours'), usage.get('five_hour'), d['threshold']),
                   (tr('Weekly'), _rolled_weekly_window(usage.get('seven_day'), now), d['weekly'])]
        models = usage.get('scoped') or []
        gap = 14
        available = w - 38
        model_width = 40 if models else 0
        col = (available - gap - (model_width + gap if models else 0)) / 2
        for i, scoped in enumerate(models):
            model = _rolled_weekly_window(scoped, now) or {}
            value = model.get('pct')
            text = f'{value:.0f}%' if isinstance(value, (float, int)) else '—'
            x = w - 19 - model_width
            label(scoped.get('name') or tr('Model'), x, 32 + 35 * i, model_width, 10, secondary, right=True)
            label(text, x, 47 + 35 * i, model_width, 11, secondary, bold=True, right=True)
        for i, (name, window, threshold) in enumerate(windows):
            x = 19 + i * (col + gap)
            y = 32
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
    count = len(usage.get('scoped') or []) if isinstance(usage, dict) else 0
    height = max(81, 33 + count * 35)
    view = AccountUsageCard.alloc().initWithFrame_(((0, 0), (354, height)))
    view.setAutoresizingMask_(A.NSViewWidthSizable)
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
    # Keep the native menu from measuring the full text fallback as a row title.
    item._menuitem.setTitle_(str(num))
    item._menuitem.setView_(view)
    item._account_card = view
