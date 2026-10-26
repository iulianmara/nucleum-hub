"""The licence of Nucleum Hub (D-064 to D-068, D-073): only in the sale edition's package.

The owner's package has no licence code at all (D-037): ``tools/package.mjs --edition owner`` leaves this
folder out. So the rest of the integration never imports it at module level: setup registers the panel
first, on its own path, then imports this package lazily and inside ``try``, only when the dashboard it
serves is the sale edition (docs/LANSARE-PLAN.md §4.2). A licence that fails, or a package without this
folder, must never take the panel and its alarm keypad with it.

What each module does (the server's side of every rule: server/README.md §7.1 and §10.9):

- ``contract``: the server's address, routes and limits, and the shapes of what goes to it and comes back.
  Pure (the standard library only), so the tests load it without Home Assistant.
- ``identity``: the home id, kept in a Store of its own that removing the config entry never touches
  (plan §4.3), and the instance's hash, ``iidh``, computed at each request (server/README.md §10.9).
- ``api``: the client for /v1/trial, /v1/activate, /v1/refresh and /v1/release on Home Assistant's
  shared HTTP session, with every error code of the contract as a Python exception.
- ``verifier``: the token and the revocation statements verified here (D-067), the port of
  server/lib/token.mjs: Ed25519 with the cryptography package, the build's roots (const.LICENCE_ROOTS),
  the self-test at start, and ``Verifier``, which the licence makes once and asks for the state.
- ``state``: the state rule, the port of server/lib/licence-state.mjs. Pure, like ``contract``. Both ports
  give exactly the results of server/lib/token-vectors.json, every case of it.
- ``schedule``: when the licence asks the server again (the rhythm of each plan, the backoff, the budget under the
  server's rate limits). Pure: the standard library and ``state`` only.
- ``storage``: what the licence keeps between restarts, in ``.storage/wall_dashboard.licence``.
- ``manager``: the licence at work, one per setup of the entry: the free trial an admin starts from the dashboard
  (D-090), the activation and the release, the refreshes, the revocation statements, and the state the dashboard is
  told (websocket.py).
- ``flow``: Configure in Home Assistant's settings (the options flow): the key, the check, the release.

Nothing in this package imports its modules here: importing ``licence`` costs nothing and cannot fail.
"""
