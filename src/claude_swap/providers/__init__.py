"""Provider registry.

cswap manages accounts for more than one AI CLI. Each provider owns its own
account store, credential format and usage API; what they share is the read
model (``AccountSnapshot``/``UsageEntry``) and a small set of verbs, which is
where the seam sits — see ``providers.base.ProviderSwitcher``.
"""

from __future__ import annotations
