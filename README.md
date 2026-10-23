# Nucleum Hub Dashboard for Home Assistant

Nucleum Hub is a wall dashboard for Home Assistant. This integration is the only thing you install: it puts the
dashboard in Home Assistant's sidebar as **Nucleum Hub** (rename it in Settings › Devices & services if you like), for every user and on every screen (tablet, phone, the
Home Assistant app, a computer). There are no files to copy and nothing to configure.

## Install

### With HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=iulianmara&repository=nucleum-hub&category=integration)

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
- **The weather card's pictures**, from the same package.
- **Music artwork converted at home.** A cover that a tablet's browser cannot show (an Apple TV's HEIC covers on
  Android, for example) is converted to JPEG inside Home Assistant, so the dashboard does not need to fetch it from the
  internet.

## Your licence

- **The trial.** The first time Nucleum Hub starts, it asks our licence server for a 60-minute trial, with everything
  open. It needs the internet once for that, and you get it once per Home Assistant installation.
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

- **HACS:** when HACS shows an update, select **Update**, then restart Home Assistant.
- **Zip:** delete the old `/config/custom_components/wall_dashboard` folder, copy the new one, restart Home Assistant.

Screens that are already open show a **New version** button once Home Assistant is back: tap it to load the new
version. They never reload by themselves.

## Remove

**Settings › Devices & services › Nucleum Hub › ⋮ › Delete** (the sidebar entry goes at once). Then remove it in HACS,
or delete the `wall_dashboard` folder, and restart Home Assistant.

## If something is wrong

- **The panel says "Nucleum Hub could not load"**: select **Retry**. If it stays, look in **Settings › System › Logs**
  for `wall_dashboard`.
- **"The dashboard file is missing from the Nucleum Hub package"**: the folder was copied from the source code, not from
  the zip or HACS. Install the package again.
- **No Nucleum Hub in the sidebar**: another dashboard already uses the address `/nucleum-hub` (the log says so). Give
  that dashboard another address, then reload Nucleum Hub (**Settings › Devices & services › Nucleum Hub › ⋮ › Reload**).
- **"Nucleum Hub is already set up"**: one is enough for each Home Assistant.

Home Assistant is a trademark of the Open Home Foundation. Nucleum Hub is independent and not affiliated with or
endorsed by the Open Home Foundation or Nabu Casa.
