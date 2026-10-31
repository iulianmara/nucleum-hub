# Nucleum Hub Dashboard for Home Assistant

Nucleum Hub is a wall dashboard for Home Assistant. This integration is the only thing you install: it puts the
dashboard in Home Assistant's sidebar as **Nucleum Hub** (rename it in Settings › Devices & services if you like), for every user and on every screen (tablet, phone, the
Home Assistant app, a computer). There are no files to copy and nothing to configure.

## Install

Nucleum Hub is a third-party product with its own licence. It is not in the Marketplace's catalogue or in HACS's
default list: you add this repository from a link, once. Needs Home Assistant 2026.3 or newer.

[![Open your Home Assistant instance and add this repository from a link, through the Marketplace (Home Assistant 2026.11 and newer) or HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=iulianmara&repository=nucleum-hub&category=integration)

### Home Assistant 2026.11 or newer: the Marketplace

1. Open **Settings › Marketplace**. The first time, it shows **Before you install from the Marketplace**: read it, tick
   **I understand the risks and agree** and select **Continue**. Each administrator accepts it once.
2. Connect GitHub when the Marketplace asks: it needs a GitHub connection to add a repository from a link. Home
   Assistant shows a code that you enter on github.com. A free account is enough, and the connection reads only public
   information.
3. Add this repository: select the button above (it opens the Marketplace in your Home Assistant) and confirm, or
   select **Add from link**, enter `https://github.com/iulianmara/nucleum-hub` and select **Add**. If it asked you to
   connect GitHub first, select the button again (or add the repository again).
4. Open **Nucleum Hub Dashboard** and select **Install**. Home Assistant opens Nucleum Hub's setup right away, without a
   restart: select **Submit**. If you closed it: **Settings › Devices & services › Add integration**, search for
   **Nucleum Hub**, select **Submit**.
5. **Nucleum Hub** appears in the sidebar. Open it.

### Older Home Assistant: HACS

1. Select the button above. Or, in HACS: **⋮ › Custom repositories**, add `https://github.com/iulianmara/nucleum-hub` with the
   type **Integration**, then open **Nucleum Hub Dashboard**.
2. Select **Download**.
3. Restart Home Assistant (**Settings › System › Restart Home Assistant**).
4. **Settings › Devices & services › Add integration**, search for **Nucleum Hub**, select **Submit**.
5. **Nucleum Hub** appears in the sidebar. Open it.

### From the zip

1. Download `nucleum-hub-<version>-sale.zip`.
2. Unzip it into your Home Assistant configuration folder (`/config`), so that the integration ends up in
   `/config/custom_components/wall_dashboard/`.
   - With the **File editor** or **Samba** add-on: open the zip on your computer and copy only the `wall_dashboard`
     folder (it is inside `custom_components`) into `/config/custom_components/`. Create `custom_components` if it is
     not there.
   - Do not drag the whole `custom_components` folder over the one in Home Assistant: on a Mac, Finder replaces a folder
     of the same name completely, and your other custom integrations (HACS, for one) would be gone.
   - With a terminal: `unzip -o nucleum-hub-<version>-sale.zip -d /config`.
3. Restart Home Assistant.
4. **Settings › Devices & services › Add integration › Nucleum Hub › Submit**.
5. **Nucleum Hub** appears in the sidebar.

The first time, Home Assistant installs the `pillow-heif` library from PyPI by itself (once, it takes a moment).

## What it does

- **The dashboard in the sidebar.** It is served only to users signed in to your Home Assistant, from your Home
  Assistant: the tablet never asks the internet for it.
- **Home Assistant's menu on a phone.** In the panel, Home Assistant's header is not shown; on a phone (and in the
  Home Assistant app) the dashboard's **More** menu ends with a **Home Assistant menu** row that opens the sidebar.
- **In the kiosk apps' dashboard lists.** Apps such as Kiosk Satellite list only Lovelace dashboards, so Nucleum Hub
  is a Lovelace dashboard too (hidden from the sidebar, which keeps one Nucleum Hub): pick **Nucleum Hub** as the
  kiosk's dashboard. It fills the whole screen; Home Assistant's menu opens from the dashboard's own menu button.
- **The weather card's pictures**, from the same package.
- **Music artwork converted at home.** A cover that a tablet's browser cannot show (an Apple TV's HEIC covers on
  Android, for example) is converted to JPEG inside Home Assistant, so the dashboard does not need to fetch it from the
  internet.

## Your licence

- **The free trial.** 3 days with everything open, once per home. A Home Assistant administrator starts it from the
  Nucleum Hub dashboard (**Start the free trial** on its first screen); until then the integration does not contact our
  licence server. It needs the internet when it starts.
- **Activate.** Open **Settings › Devices & services › Nucleum Hub › Configure**, paste the key from the e-mail you got
  after your purchase, choose whether this Home Assistant is **My home** or **A test instance**, and select **Submit**.
  Upper or lower case and spaces in the key do not matter. If the licence is active in another home, Nucleum Hub asks
  whether to move it here.
- **Checks.** Nucleum Hub then checks the licence with our server by itself: daily for a monthly subscription and for a
  test instance, weekly for a one-time licence, and whenever you open Configure. It sends the licence's id, a random id
  for your home, a fingerprint of your installation and the dashboard's version. Nothing from your home, and never your
  key again.
- **Without a licence** (after the trial, or when a subscription has ended), the dashboard shows only the screen to
  activate one. Home Assistant itself and its app keep working as always.
- **If the licence data is lost** (a file damaged by a power cut, for example), Configure says so: paste the key from
  your e-mail again. It brings the same licence back and does not count as a move.
- **Moving to new hardware.** Restoring a backup keeps the licence. On a new installation, open **Configure › Remove the
  licence from this Home Assistant** on the old one (or just activate on the new one and confirm the move). You can move
  it yourself 3 times in 12 months; moving the test instance does not count.

## Update

- **The Marketplace or HACS:** new versions appear in **Settings › Updates**, with their release notes. Select
  **Update**, then restart Home Assistant. They look for new versions by themselves, every two days or so (the
  Marketplace also when Home Assistant starts, and only while a GitHub account is connected). To look now, open
  **Nucleum Hub Dashboard** in the Marketplace and select **⋮ › Refresh information** (in HACS, **⋮ › Update
  information**).
- **After moving to Home Assistant 2026.11:** the Marketplace takes over what HACS installed, Nucleum Hub included, with
  your GitHub connection. It updates only once its warning is accepted on the administrator account you update from:
  open **Settings › Marketplace** on that account and accept it (each administrator accepts it once; an automation works
  once any administrator has). Until then, **Update** fails with "Open the Marketplace and read the warning first".
- **From version 1.0.3,** Nucleum Hub also tells its administrators when a new version is out, beside the clock
  (**Update · 1.0.4**, for example). Tap it to see what is new and to update. This needs Nucleum Hub installed through
  the Marketplace or HACS: installed from the zip, it shows nothing. To find out, Nucleum Hub asks them to look for a
  new version while an administrator has Nucleum Hub open: about once a day for your whole Home Assistant, not for
  each screen, and again after Home Assistant restarts (if GitHub could not be reached, again an hour later, a few times
  at most). It sends nothing else.
- **With a one-time licence:** Settings › Updates offers every new version, also one your licence does not cover, and
  installed from there, a version it does not cover stops the dashboard at the screen "This version came out after
  your update year". Update from the dashboard's own card
  instead: it offers **Update** only when your licence covers the version. To go back to a version it covers: in the
  Marketplace, **Nucleum Hub Dashboard › ⋮ › Install another version** and choose the version (in HACS, **⋮ › Redownload**), then
  restart Home Assistant.
- **Zip:** delete the old `/config/custom_components/wall_dashboard` folder, copy the new one, restart Home Assistant.

Screens that are already open show a **New version** button once Home Assistant is back: tap it to load the new
version. They never reload by themselves.

## Remove

**Settings › Devices & services › Nucleum Hub › ⋮ › Delete** (the sidebar entry goes at once). Then remove it in the
Marketplace or in HACS, or delete the `wall_dashboard` folder, and restart Home Assistant.

## If something is wrong

- **The panel says "Nucleum Hub could not load"**: select **Retry**. If it stays, look in **Settings › System › Logs**
  for `wall_dashboard`.
- **"The dashboard file is missing from the Nucleum Hub package"**: the folder was copied from the source code, not from
  the Marketplace, HACS or the zip. Install the package again.
- **No Nucleum Hub in the sidebar**: another dashboard already uses the address `/nucleum-hub` (the log says so). Give
  that dashboard another address, then reload Nucleum Hub (**Settings › Devices & services › Nucleum Hub › ⋮ › Reload**).
- **"Nucleum Hub is already set up"**: one is enough for each Home Assistant.
- **Update fails with "Open the Marketplace and read the warning first"**: the Marketplace asks it of each
  administrator. Open **Settings › Marketplace** on the administrator account you update from, tick **I understand the risks and agree**
  and select **Continue**, then select **Update** again from that account.
- **A new version is out, but Settings › Updates does not show it**: open **Nucleum Hub Dashboard** in the Marketplace
  and select **⋮ › Refresh information** (in HACS, **⋮ › Update information**).

Home Assistant is a trademark of the Open Home Foundation. Nucleum Hub is independent and not affiliated with or
endorsed by the Open Home Foundation or Nabu Casa.
