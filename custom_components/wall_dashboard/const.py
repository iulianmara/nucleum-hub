"""Constants of the Nucleum Hub integration (domain ``wall_dashboard``).

Kept free of Home Assistant imports, so the tests in ``integration/tests`` can load it with the
standard library only. The contracts with the dashboard: ``docs/ARTWORK-INTEGRARE.md`` (artwork)
and ``docs/PACHET.md`` (the package: the panel, its loader and the dashboard's file).
"""

from __future__ import annotations

from typing import Final

# The domain stays ``wall_dashboard`` (plan §5.8): the product name lives only in texts (manifest ``name``, config
# flow), so a change of name never touches an installation. NAME is also the entry's first title, and the sidebar
# shows the entry's title: renamed in Settings › Devices & services, the sidebar follows (__init__.py). In HACS the
# integration is listed as "Nucleum Hub Dashboard" (hacs.json); everywhere else it is "Nucleum Hub" (the owner, 23.10).
DOMAIN: Final = "wall_dashboard"
NAME: Final = "Nucleum Hub"

# --- Artwork (D-050): the first feature -------------------------------------------------------
# The WebSocket command the dashboard asks once per connection (4 s timeout on its side).
WS_ARTWORK_INFO: Final = f"{DOMAIN}/artwork/info"

# The two authenticated views (the dashboard loads them as <img> through auth/sign_path).
URL_ARTWORK_PLAYER: Final = f"/api/{DOMAIN}/artwork/player/{{entity_id}}"
URL_ARTWORK_URL: Final = f"/api/{DOMAIN}/artwork/url"

# Response headers of a converted picture, and of every refusal.
CACHE_OK: Final = "private, max-age=86400"
CACHE_ERROR: Final = "no-store"

# The longest query values the views accept (anything longer is a 400).
MAX_URL_PARAM: Final = 4096
MAX_V_PARAM: Final = 256

# How many conversions may run in the executor at once (the rest wait their turn).
CONVERT_CONCURRENCY: Final = 2

# A refusal (404 / 415 / 502) is remembered this long for the same picture, so several tablets
# asking for a dead address at once do not each wait for the 10 s timeout.
FAILURE_TTL: Final = 60.0

# Registered once per Home Assistant run (views cannot be removed from aiohttp's router; after an
# unload they answer as if the integration were not there).
DATA_REGISTERED: Final = f"{DOMAIN}_registered"

# --- The dashboard as a sidebar panel (D-051): one package to install --------------------------
# The panel: Home Assistant's own sidebar entry, on every screen, for every user.
PANEL_URL_PATH: Final = "nucleum-hub"
PANEL_COMPONENT: Final = "nucleum-hub-panel"  # the custom element of frontend/panel.js
PANEL_TITLE: Final = NAME  # the sidebar's title until the entry has its own
PANEL_ICON: Final = "mdi:tablet-dashboard"  # neutral: no Home Assistant or third-party mark
# The same dashboard as a Lovelace dashboard too (D-087, dashboard.py): the kiosk apps list only those. Hidden from the
# sidebar; its one card is frontend/panel.js's.
DASHBOARD_URL_PATH: Final = "nucleum-hub-dashboard"
CARD_TYPE: Final = "custom:nucleum-hub-card"

# What the package carries next to the code (integration/custom_components/wall_dashboard/frontend).
# panel.js is committed; the other three are put there by tools/package.mjs (frontend/README.txt).
FRONTEND_DIR_NAME: Final = "frontend"
FRONTEND_PANEL_JS: Final = "panel.js"
FRONTEND_HTML: Final = "wall-dashboard.html"
FRONTEND_BUILD_INFO: Final = "build-info.json"
FRONTEND_WEATHER: Final = "wall-dashboard-weather"

# Public static paths: they hold nothing secret (the loader, landscape pictures). The dashboard's
# relative "wall-dashboard-weather/" resolves here through the <base href> the loader sets.
STATIC_PREFIX: Final = "/nucleum_hub"
URL_PANEL_JS: Final = f"{STATIC_PREFIX}/{FRONTEND_PANEL_JS}"
URL_WEATHER: Final = f"{STATIC_PREFIX}/{FRONTEND_WEATHER}"

# The dashboard itself and what is installed: only to a signed-in user (the loader sends its token).
URL_APP: Final = f"/api/{DOMAIN}/app"
URL_INFO: Final = f"/api/{DOMAIN}/info"
CACHE_APP: Final = "no-store"
# The sha256 of the file the dashboard's view served, sent with it; /info gives the one it serves now ("file"), so a
# screen left open after an update knows it runs an older file (D-054: "New version: reload").
HEADER_SHA256: Final = "X-Nucleum-Hub-Sha256"

# --- The licence's root keys (plan §4.4, §6.9; T9 A: a main root and a backup, kept apart) ------------------
# The public keys the licence trusts (licence/verifier.py): each certifies the server's online keys, which sign
# the tokens. Only the sale edition reads them (D-037); in the owner's package nothing does. The owner's two roots,
# the main one and the backup, made with `node tools/keys.mjs root` on 23.10.2026 (their private halves stay with him,
# encrypted). Never a TEST- key of server/lib/token-vectors.json (their seeds are public): the tests check it, and the
# verifier drops one.
LICENCE_ROOTS: Final[tuple[dict[str, str], ...]] = (
    {"kid": "root-CjTzEWcQFaTnYOh8", "pub": "SKWP7MiGjCGoGzhsviu1gZ3Bw9yCAz8j__NlPM_drIY"},   # main
    {"kid": "root-C8ItVqKwcXrmBN-c", "pub": "9P2x5IO9z1Pf5DExe47ymiZ9XMZHdTJLfnAmCfr7u3g"},   # backup
)
# Online keys known to be stolen, by kid: a token or a statement one of them signed is refused (key_revoked) even
# inside its 90 days. A new build carries the list (plan §6.9, the runbook for a leaked key).
LICENCE_REVOKED_KIDS: Final[tuple[str, ...]] = ()

# --- The licence's place outside its folder (D-067: the integration judges, the dashboard is told) ----------------
# The dashboard's edition that carries the licence (frontend/build-info.json "edition", tools/editions.mjs): only then
# does setup start licence/ (D-037), after the panel.
EDITION_SALE: Final = "sale"
# The state, asked once: {"type": "wall_dashboard/licence", "language"?: "ro", "refresh"?: true (an admin's settings
# opened)}. And the same state pushed at every change: {"type": "wall_dashboard/licence/subscribe", "language"?}.
# Any signed-in user may ask (every screen shows the activation screen, D-065). Registered in the sale edition only.
WS_LICENCE: Final = f"{DOMAIN}/licence"
WS_LICENCE_SUBSCRIBE: Final = f"{DOMAIN}/licence/subscribe"
# "Start the free trial" on the activation screen (D-090): {"type": "wall_dashboard/licence/trial"}, from a Home
# Assistant admin only (any other user gets the WebSocket error "unauthorized"). The answer: {"ok": true}, or
# {"ok": false, "error": <code>, "minutes": <a number for rate_limited, else null>} (websocket.py TRIAL_ERRORS).
WS_LICENCE_TRIAL: Final = f"{DOMAIN}/licence/trial"
# Sent on Home Assistant's dispatcher when the state changed; the subscriptions survive a reload of the entry.
SIGNAL_LICENCE: Final = f"{DOMAIN}_licence"
DATA_LICENCE_REGISTERED: Final = f"{DOMAIN}_licence_registered"
# The sale edition's entry data, from the moment its licence starts until it unloads: the commands read it there, not
# through the entry's state, which Home Assistant sets to loaded only after setup, when the start was already told.
DATA_LICENCE_ENTRY: Final = f"{DOMAIN}_licence_entry"

# --- Later features, not built yet (plan §4.2, §6.3, Anexa G) --------------------------------
# WS_FEEDBACK_INFO = f"{DOMAIN}/feedback/info"      # docs/FEEDBACK-INTEGRARE.md
# WS_FEEDBACK_SEND = f"{DOMAIN}/feedback/send"
# the update entity: beta 2 (LANSARE-PLAN §4.8, §6.4)
