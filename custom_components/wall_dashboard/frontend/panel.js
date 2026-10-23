/* Nucleum Hub: the panel loader (D-051). The integration registers it as the custom panel "Nucleum Hub" in Home
   Assistant's sidebar (frontend_url_path "nucleum-hub", module_url "/nucleum_hub/panel.js?v=<integration version>",
   embed_iframe False), so it is on every screen, for every user, with no file to copy. The same module defines the card
   of the Lovelace dashboard /nucleum-hub-dashboard, which the kiosk apps list (D-087, "the card" below).
   Plain ES module, no build step, no framework, no request outside Home Assistant.

   What it does, once per page of Home Assistant's frontend:
   - When Home Assistant first sets .hass, it asks the integration for the dashboard: GET /api/wall_dashboard/app with
     "Authorization: Bearer <hass.auth's access token>" (a token that has expired is refreshed first; a 401 refreshes
     it once and asks once more). The view answers only an authenticated user; the integration adds nothing to the file.
   - It puts <base href="<Home Assistant's origin>/nucleum_hub/"> first in the file's <head>, so what the dashboard
     ships next to itself (wall-dashboard-weather/) comes from the integration's public folder, and shows the file in a
     borderless iframe that fills the panel. The base is written whole, origin and path: Chrome's preload scanner
     resolves a bare "/nucleum_hub/" in a srcdoc document against the page around it, not against the base (seen in
     the harness: the source file's modules asked for twice, once at the wrong place). The dashboard finds Home Assistant around it as it always has
     (window.parent.document.querySelector('home-assistant').hass): the frontend's own connection, never a socket of
     its own; the same origin, so the same storage and the same synced dashboards as a copy in /config/www.
   - Later .hass updates ask nothing again: the dashboard has its own live layer. narrow tells the dashboard whether
     Home Assistant's menu needs a way in (D-054); route is not used.
   - A failure is a short message in the panel, never a blank page: "Nucleum Hub could not load: <reason>", in
     English and in the Home Assistant user's language when it is one of the dashboard's 12, with a Retry button.

   srcdoc, not a Blob URL (measured in Chrome, 10.10.2026): a document from a revoked Blob URL comes back as an error
   page when it loads again, and it loads again whenever the panel is re-attached (Home Assistant removes and adds
   panels as you navigate) or anything reloads the frame; a <base> with a path does not resolve inside a blob:
   document either. A srcdoc document is drawn again from its attribute, has the parent's origin, its storage and
   its base URL, and leaves nothing to revoke. Its own location is about:srcdoc (location.origin "null"): the
   dashboard takes Home Assistant's origin from its parent (WD_ORIGIN), which is the same in both.

   The file is asked for once per page load (CACHE): a panel shown again, re-attached or reloaded by the dashboard
   (WD_HOST.reload(): the demo, a restore) uses it without asking again. The iframe element carries
   wdHost, which the dashboard reads as WD_HOST (src/modules/boot.js; its side of D-054: src/modules/hostui.js):
     {kind: 'panel', version, files: '/nucleum_hub/', reload(),
      menu(), toggleMenu(),              Home Assistant's menu on a narrow screen (D-054, menuShown)
      update(), applyUpdate(),           a new version installed, and the reload it needs (D-054, "a new version" below)
      on('menu' | 'update', fn) -> off}  told when either changes, until the frame is replaced

   Tested: tests/panel.test.mjs (the pure parts: the language, the reason, the <base>, the build and what changed,
   the menu's rule), WDProbe.scenarioPanel (dev/probe-panel.js, the harness in Home Assistant's place: ?panel=1). */

export const TAG = 'nucleum-hub-panel';
export const APP = '/api/wall_dashboard/app';
export const FILES = '/nucleum_hub/';
export const INFO = '/api/wall_dashboard/info';
/* the header of /api/wall_dashboard/app that carries the served file's sha256 (computed by the integration) */
export const SHA_HEADER = 'X-Nucleum-Hub-Sha256';
/* the integration's version, from this module's own address (?v=) */
export const VERSION = (() => { try { return new URL(import.meta.url).searchParams.get('v') || ''; } catch (e) { return ''; } })();
const WAIT_MS = 30000;   /* no answer from Home Assistant within 30 s: it could not be reached */

/* the dashboard's 12 languages (the order of its catalogue) */
export const LANGS = ['en', 'ro', 'de', 'fr', 'nl', 'es', 'it', 'pl', 'sv', 'cs', 'da', 'pt-BR'];
export const TEXT = {
  en: { title: 'Nucleum Hub could not load', retry: 'Retry',
    update: 'New version', reload: 'Reload',
    auth: 'Home Assistant did not accept your sign-in (sign in again)', missing: 'the dashboard file is missing from the integration (reinstall Nucleum Hub)',
    broken: 'the dashboard file is damaged (reinstall Nucleum Hub)', server: 'Home Assistant answered with an error ({code})', network: 'Home Assistant could not be reached' },
  ro: { title: 'Nucleum Hub nu s-a putut încărca', retry: 'Reîncearcă',
    update: 'Versiune nouă', reload: 'Reîncarcă',
    auth: 'Home Assistant nu a acceptat autentificarea (autentifică-te din nou)', missing: 'fișierul dashboard-ului lipsește din integrare (reinstalează Nucleum Hub)',
    broken: 'fișierul dashboard-ului este deteriorat (reinstalează Nucleum Hub)', server: 'Home Assistant a răspuns cu o eroare ({code})', network: 'Home Assistant nu răspunde' },
  de: { title: 'Nucleum Hub konnte nicht geladen werden', retry: 'Erneut versuchen',
    update: 'Neue Version', reload: 'Neu laden',
    auth: 'Home Assistant hat die Anmeldung nicht angenommen (bitte erneut anmelden)', missing: 'die Dashboard-Datei fehlt in der Integration (Nucleum Hub neu installieren)',
    broken: 'die Dashboard-Datei ist beschädigt (Nucleum Hub neu installieren)', server: 'Home Assistant hat mit einem Fehler geantwortet ({code})', network: 'Home Assistant ist nicht erreichbar' },
  fr: { title: 'Nucleum Hub n’a pas pu se charger', retry: 'Réessayer',
    update: 'Nouvelle version', reload: 'Recharger',
    auth: 'Home Assistant n’a pas accepté votre connexion (reconnectez-vous)', missing: 'le fichier du tableau de bord manque dans l’intégration (réinstallez Nucleum Hub)',
    broken: 'le fichier du tableau de bord est endommagé (réinstallez Nucleum Hub)', server: 'Home Assistant a répondu par une erreur ({code})', network: 'Home Assistant est injoignable' },
  nl: { title: 'Nucleum Hub kon niet worden geladen', retry: 'Opnieuw proberen',
    update: 'Nieuwe versie', reload: 'Herladen',
    auth: 'Home Assistant heeft je aanmelding niet geaccepteerd (meld je opnieuw aan)', missing: 'het dashboardbestand ontbreekt in de integratie (installeer Nucleum Hub opnieuw)',
    broken: 'het dashboardbestand is beschadigd (installeer Nucleum Hub opnieuw)', server: 'Home Assistant antwoordde met een fout ({code})', network: 'Home Assistant is niet bereikbaar' },
  es: { title: 'No se pudo cargar Nucleum Hub', retry: 'Reintentar',
    update: 'Nueva versión', reload: 'Recargar',
    auth: 'Home Assistant no aceptó tu inicio de sesión (vuelve a iniciar sesión)', missing: 'falta el archivo del panel en la integración (reinstala Nucleum Hub)',
    broken: 'el archivo del panel está dañado (reinstala Nucleum Hub)', server: 'Home Assistant respondió con un error ({code})', network: 'no se puede conectar con Home Assistant' },
  it: { title: 'Impossibile caricare Nucleum Hub', retry: 'Riprova',
    update: 'Nuova versione', reload: 'Ricarica',
    auth: 'Home Assistant non ha accettato l’accesso (accedi di nuovo)', missing: 'nell’integrazione manca il file della dashboard (reinstalla Nucleum Hub)',
    broken: 'il file della dashboard è danneggiato (reinstalla Nucleum Hub)', server: 'Home Assistant ha risposto con un errore ({code})', network: 'Home Assistant non è raggiungibile' },
  pl: { title: 'Nie udało się wczytać Nucleum Hub', retry: 'Spróbuj ponownie',
    update: 'Nowa wersja', reload: 'Odśwież',
    auth: 'Home Assistant nie przyjął logowania (zaloguj się ponownie)', missing: 'w integracji brakuje pliku pulpitu (zainstaluj ponownie Nucleum Hub)',
    broken: 'plik pulpitu jest uszkodzony (zainstaluj ponownie Nucleum Hub)', server: 'Home Assistant odpowiedział błędem ({code})', network: 'brak połączenia z Home Assistant' },
  sv: { title: 'Nucleum Hub kunde inte läsas in', retry: 'Försök igen',
    update: 'Ny version', reload: 'Ladda om',
    auth: 'Home Assistant godkände inte din inloggning (logga in igen)', missing: 'instrumentpanelens fil saknas i integrationen (installera om Nucleum Hub)',
    broken: 'instrumentpanelens fil är skadad (installera om Nucleum Hub)', server: 'Home Assistant svarade med ett fel ({code})', network: 'Home Assistant går inte att nå' },
  cs: { title: 'Nucleum Hub se nepodařilo načíst', retry: 'Zkusit znovu',
    update: 'Nová verze', reload: 'Načíst znovu',
    auth: 'Home Assistant nepřijal přihlášení (přihlaste se znovu)', missing: 'v integraci chybí soubor panelu (přeinstalujte Nucleum Hub)',
    broken: 'soubor panelu je poškozený (přeinstalujte Nucleum Hub)', server: 'Home Assistant odpověděl chybou ({code})', network: 'Home Assistant není dostupný' },
  da: { title: 'Nucleum Hub kunne ikke indlæses', retry: 'Prøv igen',
    update: 'Ny version', reload: 'Genindlæs',
    auth: 'Home Assistant godkendte ikke dit login (log ind igen)', missing: 'dashboardets fil mangler i integrationen (geninstallér Nucleum Hub)',
    broken: 'dashboardets fil er beskadiget (geninstallér Nucleum Hub)', server: 'Home Assistant svarede med en fejl ({code})', network: 'Home Assistant kan ikke nås' },
  'pt-BR': { title: 'Não foi possível carregar o Nucleum Hub', retry: 'Tentar novamente',
    update: 'Nova versão', reload: 'Recarregar',
    auth: 'o Home Assistant não aceitou seu login (entre novamente)', missing: 'o arquivo do painel está faltando na integração (reinstale o Nucleum Hub)',
    broken: 'o arquivo do painel está danificado (reinstale o Nucleum Hub)', server: 'o Home Assistant respondeu com um erro ({code})', network: 'não foi possível conectar ao Home Assistant' },
};

/* why a load failed: the texts that are reasons (the others are labels) */
export const REASONS = ['auth', 'missing', 'broken', 'server', 'network'];

/* ------------------------------------------------------------------ pure */
/* the Home Assistant user's language among the 12: exactly (pt-BR), then by its language (de-AT -> de, pt -> pt-BR) */
export function langOf(hass) {
  const raw = String((hass && (hass.language || (hass.locale && hass.locale.language))) || '').trim().replace('_', '-');
  if (!raw) return 'en';
  const exact = LANGS.find(l => l.toLowerCase() === raw.toLowerCase());
  if (exact) return exact;
  const base = raw.split('-')[0].toLowerCase();
  return LANGS.find(l => l.split('-')[0].toLowerCase() === base) || 'en';
}
/* why it failed, from an HTTP status or a thrown error: {key: auth | missing | broken | server | network, code} */
export function reasonOf(e) {
  const code = e && typeof e === 'object' && +e.status > 0 ? +e.status : 0;
  if (e && typeof e === 'object' && REASONS.includes(e.key)) return { key: e.key, code: code || e.code || 0 };
  if (code === 401 || code === 403) return { key: 'auth', code };
  if (code === 404) return { key: 'missing', code };
  if (code) return { key: 'server', code };
  return { key: 'network', code: 0 };
}
/* the message's lines: English, then the user's language when it is another one of the 12 */
export function lines(reason, lang) {
  const r = reason && REASONS.includes(reason.key) ? reason : { key: 'network', code: 0 };
  const one = l => TEXT[l].title + ': ' + TEXT[l][r.key].replace('{code}', String(r.code || ''));
  const out = [{ lang: 'en', text: one('en') }];
  if (lang && lang !== 'en' && TEXT[lang]) out.push({ lang, text: one(lang) });
  return out;
}
export function retryLabel(lang) { return lang && lang !== 'en' && TEXT[lang] ? TEXT[lang].retry + ' · ' + TEXT.en.retry : TEXT.en.retry; }
/* the file with <base href="..."> as the first element of its <head> (a <head> is made when it has none) */
export function withBase(html, href) {
  const s = String(html == null ? '' : html), tag = '<base href="' + String(href).replace(/"/g, '&quot;') + '">';
  const head = /<head(?=[\s>])[^>]*>/i.exec(s);
  if (head) return s.slice(0, head.index + head[0].length) + tag + s.slice(head.index + head[0].length);
  const root = /<html(?=[\s>])[^>]*>/i.exec(s);
  if (root) return s.slice(0, root.index + root[0].length) + '<head>' + tag + '</head>' + s.slice(root.index + root[0].length);
  const dt = /^﻿?\s*<!doctype[^>]*>/i.exec(s);
  if (dt) return s.slice(0, dt[0].length) + '<head>' + tag + '</head>' + s.slice(dt[0].length);
  return '<head>' + tag + '</head>' + s;
}
/* a page of HTML, not an error page or an empty answer */
export function looksLikePage(html) { return /<html[\s>]|<!doctype html/i.test(String(html || '').slice(0, 4096)); }
/* a sha256 as the integration writes it (64 lowercase hex digits), or '' */
export function shaOf(v) { const s = String(v == null ? '' : v).trim().toLowerCase(); return /^[0-9a-f]{64}$/.test(s) ? s : ''; }
/* what an /api/wall_dashboard/info answer says is installed now: {version, sha256} (sha256: of the file the
   dashboard's view serves now, its "file"), or null when it says nothing usable */
export function buildOf(info) {
  if (!info || typeof info !== 'object') return null;
  const version = typeof info.version === 'string' ? info.version.trim() : '', sha256 = shaOf(info.file);
  return version || sha256 ? { version, sha256 } : null;
}
/* what changed between the build this page runs (base) and the one installed now: 'page' (the integration: a new
   loader too, which only a page load can bring, the element being defined once per page), 'file' (only the
   dashboard's file: the same loader can show the new one), or '' (nothing, or nothing known on one side) */
export function changeOf(base, now) {
  if (!base || !now) return '';
  if (base.version && now.version && base.version !== now.version) return 'page';
  if (base.sha256 && now.sha256 && base.sha256 !== now.sha256) return 'file';
  return '';
}
/* whether Home Assistant's own menu button would show on this screen (its ha-menu-button, read 10.10.2026): only when
   the screen is narrow (870 px or less, the sidebar hidden in a drawer), not in kiosk mode and not in a companion
   app that has a sidebar of its own. Home Assistant also shows it on a wide screen whose sidebar is set to
   "Always hide"; D-054 keeps the dashboard's row to narrow screens (a wall tablet set that way stays clean). */
export function menuShown(hass, narrow) {
  if (!narrow) return false;
  try {
    if (hass && hass.kioskMode === true) return false;
    const ext = hass && hass.auth && hass.auth.external;
    if (ext && ext.config && ext.config.hasSidebar === true) return false;
  } catch (e) {}
  return true;
}

/* ------------------------------------------------------------------ Home Assistant */
/* the dashboard as Home Assistant gave it, once per page load, with the sha256 the integration sent with it;
   p: the request on its way, shared */
const CACHE = { html: null, sha: '', p: null };
export function forget() { CACHE.html = null; CACHE.sha = ''; CACHE.p = null; }
/* the integration's public folder, as a whole address on Home Assistant's origin */
export function filesHref(loc) { try { return new URL(FILES, (loc || location).href).href; } catch (e) { return FILES; } }
function tokenOf(auth) { try { return String(auth.accessToken || (auth.data && auth.data.access_token) || ''); } catch (e) { return ''; } }
async function refresh(auth) {
  if (!auth || typeof auth.refreshAccessToken !== 'function') return false;
  try { await auth.refreshAccessToken(); return true; } catch (e) { throw { key: 'auth' }; }
}
function ask(hass, token, path) {
  const url = hass && typeof hass.hassUrl === 'function' ? hass.hassUrl(path) : path;
  const ac = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = setTimeout(() => { try { if (ac) ac.abort(); } catch (e) {} }, WAIT_MS);
  return fetch(url, { method: 'GET', headers: { Authorization: 'Bearer ' + token }, credentials: 'same-origin', cache: 'no-store', signal: ac ? ac.signal : undefined })
    .then(r => { clearTimeout(timer); return r; }, () => { clearTimeout(timer); throw { key: 'network' }; });
}
/* one of the integration's two views, with the user's token: refreshed first when it has expired, and once more after
   a 401 (once: no loop) */
async function authed(hass, path) {
  const auth = hass && hass.auth;
  if (!auth) throw { key: 'auth' };
  let expired = false; try { expired = !!auth.expired; } catch (e) {}
  if (expired) await refresh(auth);
  let r = await ask(hass, tokenOf(auth), path);
  if (r.status === 401 && await refresh(auth)) r = await ask(hass, tokenOf(auth), path);
  if (!r.ok) throw { status: r.status };
  return r;
}
async function fetchApp(hass) {
  const r = await authed(hass, APP);
  let html = '', sha = '';
  try { sha = shaOf(r.headers.get(SHA_HEADER)); } catch (e) {}
  try { html = await r.text(); } catch (e) { throw { key: 'network' }; }
  if (!looksLikePage(html)) throw { key: 'broken' };
  return { html, sha };
}
function appHtml(hass) {
  if (CACHE.html) return Promise.resolve(CACHE.html);
  if (!CACHE.p) {
    const p = CACHE.p = fetchApp(hass).then(a => {
      if (CACHE.p === p) { CACHE.html = a.html; CACHE.sha = a.sha; CACHE.p = null; loaded(a.sha); }
      return a.html;
    }, e => { if (CACHE.p === p) CACHE.p = null; throw e; });
  }
  return CACHE.p;
}

/* ------------------------------------------------------------------ a new version (D-054) */
/* After the integration is updated (a new package, Home Assistant restarted), a screen left open still runs the old
   loader and the old dashboard. The page compares what it runs with what /api/wall_dashboard/info says is installed:
   - what it runs (base): the integration's version this module was loaded with (its ?v=) and the sha256 of the
     dashboard's file it was given (the header SHA_HEADER of /api/wall_dashboard/app, computed by the integration
     from the very bytes it served: no window between two requests, no hashing here, which plain http would not
     allow: crypto.subtle needs a secure context);
   - what is installed (now): /info's version and its "file", the sha256 of the file the dashboard's view serves now.
   It asks again when the frontend's connection comes back (Home Assistant restarted: 'ready'; while it is still
   starting, the view may not be there yet, so it tries again after 30 s, 1, 2 and 5 min) and every 30 minutes
   while a panel is on screen (and when one comes back on screen after that long). One small authenticated GET, on
   Home Assistant's origin, nothing else. Never a reload by itself: the dashboard shows "New version: reload"
   (WD_HOST.update / on('update')); a tap reloads (applyUpdate):
   - the integration changed ('page'): Home Assistant's whole page (location.reload(), as Home Assistant's own
     "reload" notices do), because the new loader is a new module and the custom element is defined once per page;
   - only the dashboard's file changed ('file'): just the panel's frame, with the new file (the same loader), which
     keeps Home Assistant's page as it is and takes about a second.
   One checker per page (the panel element is made again each time you come back to it). */
export const CHECK_MS = 30 * 60 * 1000;
export const RETRY_MS = [30000, 60000, 120000, 300000];
/* the page reload, a hook so the harness (which stands where Home Assistant's page is) can count it */
export const HOOKS = { reloadPage() { location.reload(); } };
const UP = { base: null, now: null, change: '', hass: null, conn: null, timer: 0, every: CHECK_MS, retry: 0, retryTimer: 0,
  busy: null, last: 0, asked: 0, els: new Set() };
/* a file was given (the first one of this page, or a new one after applyUpdate): it is what this page runs now */
function loaded(sha) {
  UP.base = { version: VERSION, sha256: sha || '' };
  if (UP.now) apply(UP.now); else if (UP.change) { UP.change = ''; emit(); }
}
function apply(now) {
  const b = UP.base; if (!b || !now) return;
  if (!b.version && now.version) b.version = now.version;                      /* a loader without ?v= (development) */
  if (!b.sha256 && now.sha256 && !changeOf(b, now)) b.sha256 = now.sha256;     /* a file given without its sha256 */
  UP.now = now;
  const c = changeOf(b, now);
  if (c !== UP.change) { UP.change = c; emit(); }
}
function emit() { UP.els.forEach(el => { try { el._updated(); } catch (e) {} }); }
/* the update the dashboard is told about: {change: 'page' | 'file', version, sha256, from} or null */
export function updateInfo() {
  return UP.change && UP.now ? { change: UP.change, version: UP.now.version, sha256: UP.now.sha256, from: (UP.base && UP.base.version) || '' } : null;
}
/* ask /api/wall_dashboard/info once (one request at a time); why: 'ready' (the connection came back), 'retry',
   'timer', 'shown' or 'probe' -> the change ('page' | 'file' | '') or null when it could not be asked */
export function checkNow(why) {
  if (!UP.hass || !UP.base) return Promise.resolve(null);
  if (UP.busy) return UP.busy;
  UP.asked++; UP.last = Date.now();
  const p = UP.busy = authed(UP.hass, INFO).then(r => r.json()).then(j => {
    clearTimeout(UP.retryTimer); UP.retry = 0;
    apply(buildOf(j));
    return UP.change;
  }, () => {
    /* Home Assistant still starting after a restart (the view not there yet, or the integration not set up): later */
    if ((why === 'ready' || why === 'retry') && UP.retry < RETRY_MS.length) {
      clearTimeout(UP.retryTimer); UP.retryTimer = setTimeout(() => checkNow('retry'), RETRY_MS[UP.retry++]);
    }
    return null;
  }).finally(() => { if (UP.busy === p) UP.busy = null; });
  return p;
}
/* every `ms` while a panel is on screen (the probe makes it short; 0 or nothing: back to CHECK_MS) -> the period */
export function checkEvery(ms) {
  UP.every = +ms > 0 ? +ms : CHECK_MS;
  if (UP.timer) { clearInterval(UP.timer); UP.timer = 0; start(); }
  return UP.every;
}
/* what the checker knows (the probe) */
export function updateState() {
  return { base: UP.base && Object.assign({}, UP.base), now: UP.now && Object.assign({}, UP.now), change: UP.change,
    asked: UP.asked, last: UP.last, every: UP.every, retry: UP.retry, shown: UP.els.size };
}
function start() {
  if (UP.timer) return;
  UP.timer = setInterval(() => { if (UP.els.size) checkNow('timer'); }, UP.every);
}
function onReady() { UP.retry = 0; clearTimeout(UP.retryTimer); checkNow('ready'); }
/* the latest .hass (its token, its connection); the connection's 'ready' follows the frontend's connection */
function watch(hass) {
  UP.hass = hass;
  start();
  let conn = null; try { conn = hass.connection || null; } catch (e) {}
  if (conn === UP.conn) return;
  try { if (UP.conn && typeof UP.conn.removeEventListener === 'function') UP.conn.removeEventListener('ready', onReady); } catch (e) {}
  UP.conn = conn;
  try { if (conn && typeof conn.addEventListener === 'function') conn.addEventListener('ready', onReady); } catch (e) {}
}
/* back to the start (the probe): what this page runs and what is installed forgotten, timers stopped */
export function forgetUpdate() {
  clearInterval(UP.timer); clearTimeout(UP.retryTimer);
  try { if (UP.conn) UP.conn.removeEventListener('ready', onReady); } catch (e) {}
  Object.assign(UP, { base: null, now: null, change: '', conn: null, timer: 0, every: CHECK_MS, retry: 0, retryTimer: 0, busy: null, last: 0, asked: 0 });
  emit();
}

/* ------------------------------------------------------------------ the panel */
const CSS = `
:host{display:block;position:relative;width:100%;height:100%;overflow:hidden;
  background:var(--primary-background-color,#111);color:var(--primary-text-color,#e1e1e1);
  font-family:var(--ha-font-family-body,var(--paper-font-body1_-_font-family,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif))}
.box{position:absolute;inset:0}
iframe{position:absolute;inset:0;display:block;width:100%;height:100%;border:0;margin:0;padding:0;background:transparent}
.wait{position:absolute;left:50%;top:50%;width:28px;height:28px;margin:-14px 0 0 -14px;border-radius:50%;
  border:3px solid var(--divider-color,rgba(127,127,127,.25));border-top-color:var(--primary-color,#03a9f4);
  animation:nh-spin 1s linear infinite;opacity:0;transition:opacity .2s}
.wait.on{opacity:1}
@keyframes nh-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.wait{animation-duration:3s}}
.msg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;
  padding:24px 16px;box-sizing:border-box;text-align:center;overflow:auto}
.msg svg{width:40px;height:40px;flex:0 0 auto;color:var(--error-color,#db4437)}
.msg p{margin:0;max-width:34em;font-size:16px;line-height:1.45;overflow-wrap:anywhere}
.msg p.loc{color:var(--secondary-text-color,#9e9e9e);font-size:15px}
.msg button{margin-top:8px;min-height:44px;min-width:120px;padding:0 22px;border:2px solid var(--primary-color,#03a9f4);border-radius:10px;
  cursor:pointer;font:inherit;font-size:15px;font-weight:600;background:transparent;color:var(--primary-text-color,#e1e1e1)}
.msg button:hover{background:rgba(127,127,127,.12)}
.msg button:focus-visible{outline:2px solid var(--primary-text-color,#e1e1e1);outline-offset:3px}
.upd{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:1;display:flex;align-items:center;gap:8px;
  max-width:calc(100% - 32px);min-height:44px;padding:0 18px;box-sizing:border-box;border:0;border-radius:22px;cursor:pointer;
  font:inherit;font-size:14px;font-weight:600;line-height:1.3;text-align:center;
  background:var(--primary-color,#03a9f4);color:var(--text-primary-color,#fff);box-shadow:0 4px 16px rgba(0,0,0,.35)}
.upd[hidden]{display:none}
.upd svg{width:18px;height:18px;flex:0 0 auto}
.upd:focus-visible{outline:2px solid var(--primary-text-color,#e1e1e1);outline-offset:3px}
`;
const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M11 15h2v2h-2zm0-8h2v6h-2zm1-5C6.47 2 2 6.5 2 12a10 10 0 0 0 10 10a10 10 0 0 0 10-10A10 10 0 0 0 12 2m0 18a8 8 0 0 1-8-8a8 8 0 0 1 8-8a8 8 0 0 1 8 8a8 8 0 0 1-8 8"/></svg>';
const RELOAD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z"/></svg>';
const Base = typeof HTMLElement !== 'undefined' ? HTMLElement : class {};

export class NucleumHubPanel extends Base {
  constructor() {
    super();
    this._hass = null;
    this._state = 'idle';            /* idle | loading | shown | error */
    this._frame = null;
    this._timer = null;
    this._ls = null;                 /* the listeners the dashboard in the current frame gave WD_HOST.on: {menu, update} */
    this._narrow = false;
    this._menu = false;              /* Home Assistant's menu button would show (menuShown) */
    this._menuAlways = false;        /* the card's: the menu on every screen (D-087) */
    this.loads = 0;                  /* documents the iframe has loaded (the probe reads it) */
    this.route = null; this.panel = null;   /* set by Home Assistant, not used */
    const root = this.attachShadow({ mode: 'open', delegatesFocus: true });
    root.innerHTML = '<style>' + CSS + '</style><div class="box"></div><button class="upd" type="button" hidden></button>';
    this._box = root.querySelector('.box');
    this._upd = root.querySelector('.upd');
    this._upd.addEventListener('click', () => this.applyUpdate());
  }
  /* the dashboard is asked for when .hass is first set; later updates do nothing (its own live layer) but keep the
     token, the connection and the menu's rule current */
  get hass() { return this._hass; }
  set hass(h) {
    const first = !this._hass && !!h;
    if (h) { this._hass = h; watch(h); this._menuSync(); }
    if (first) this._go();
  }
  /* set by Home Assistant: true at 870 px or less, its sidebar hidden in a drawer */
  get narrow() { return this._narrow; }
  set narrow(v) { this._narrow = !!v; this._menuSync(); }
  /* set by the card (D-087): Home Assistant's sidebar is a drawer on every screen there, opened only from the dashboard */
  get menuAlways() { return this._menuAlways; }
  set menuAlways(v) { this._menuAlways = !!v; this._menuSync(); }
  connectedCallback() {
    UP.els.add(this);
    this._fit();
    if (!this._onResize && typeof window !== 'undefined') { this._onResize = () => this._fit(); window.addEventListener('resize', this._onResize); }
    this._go();
    this._updated();
    /* back on screen after the period: ask once now rather than wait for the timer */
    if (UP.base && UP.last && Date.now() - UP.last > UP.every) checkNow('shown');
  }
  disconnectedCallback() {
    clearTimeout(this._timer); UP.els.delete(this);
    if (this._onResize) { window.removeEventListener('resize', this._onResize); this._onResize = null; }
    if (this._ro) { this._ro.disconnect(); this._ro = null; this._roBox = null; }
  }
  /* Home Assistant 2026.10 puts the panel in an inline partial-panel-resolver and a ha-panel-custom of auto height, so
     height:100% was 0 and the dashboard sat in a frame 0 px high: a black page. Without a height from its container the
     panel reaches from its top to the bottom of the first box above it that has one (Home Assistant's drawer: the
     screen beside the sidebar), measured again when the window or that box changes size (its own height cleared first,
     or the boxes it fills would measure themselves). A container with a height of its own keeps height:100%. */
  _fit() {
    if (!this.isConnected || typeof ShadowRoot === 'undefined') return;
    if (this._fitted) { this.style.height = ''; this._fitted = false; }
    if (this.offsetHeight > 0) return;
    let box = null;
    for (let n = this.parentNode; n && !box;) {
      const el = n instanceof ShadowRoot ? n.host : n;
      if (el.nodeType === 1 && el.clientHeight > 0) box = el;
      else n = el.parentNode;
    }
    if (!box) return;
    const h = Math.round(box.getBoundingClientRect().bottom - this.getBoundingClientRect().top);
    if (h > 0) { this.style.height = h + 'px'; this._fitted = true; }
    if (box !== this._roBox && typeof ResizeObserver !== 'undefined') {
      if (this._ro) this._ro.disconnect();
      this._roBox = box;
      this._ro = new ResizeObserver(() => this._fit());
      this._ro.observe(box);
    }
  }
  /* re-attached with its iframe: the iframe draws its srcdoc again by itself, nothing is asked */
  _go() {
    if (!this.isConnected || !this._hass || this._state !== 'idle') return;
    this._load();
  }
  _load() {
    this._state = 'loading';
    const w = document.createElement('div'); w.className = 'wait'; w.setAttribute('role', 'progressbar'); w.setAttribute('aria-label', 'Nucleum Hub');
    this._box.replaceChildren(w); this._frame = null; this._ls = null;
    clearTimeout(this._timer); this._timer = setTimeout(() => w.classList.add('on'), 400);
    this._updated();
    appHtml(this._hass).then(html => this._show(html), e => this._fail(reasonOf(e)));
  }
  _show(html) {
    if (this._state !== 'loading') return;
    this._render(html);
  }
  /* a new iframe each time (a new document, and no entry in the browser's history) */
  _render(html) {
    clearTimeout(this._timer);
    const f = document.createElement('iframe');
    f.setAttribute('title', 'Nucleum Hub');
    f.setAttribute('allow', 'fullscreen');
    f.allowFullscreen = true;
    /* what the dashboard in this frame may hear about, until the frame is replaced */
    const ls = this._ls = { menu: new Set(), update: new Set() };
    f.wdHost = Object.freeze({
      kind: 'panel', version: VERSION, files: FILES,
      reload: () => { setTimeout(() => this._reload(), 0); },
      /* Home Assistant's menu (D-054): whether its own menu button would show here, and a tap on it */
      menu: () => this._menu,
      toggleMenu: () => this.toggleMenu(),
      /* a new version installed: {change, version, sha256, from} or null; applyUpdate(): the reload it needs */
      update: () => updateInfo(),
      applyUpdate: () => this.applyUpdate(),
      /* on('menu' | 'update', fn) -> off(): fn(menu()) / fn(update()) when it changes */
      on: (type, fn) => {
        const set = ls[type]; if (!set || typeof fn !== 'function') return () => {};
        set.add(fn); if (type === 'update') this._updated();
        return () => { set.delete(fn); if (type === 'update') this._updated(); };
      },
    });
    f.addEventListener('load', () => {
      if (f !== this._frame) return;
      this.loads++;
      const w = this._box.querySelector('.wait'); if (w) w.remove();
      this._state = 'shown';
      this._focus();
      this._updated();
    });
    f.srcdoc = withBase(html, filesHref());
    const w = this._box.querySelector('.wait');
    this._box.replaceChildren(...(w ? [w, f] : [f]));
    this._frame = f;
    this._updated();
  }
  /* keyboard focus goes into the dashboard */
  _focus() { try { if (this.isConnected && this._frame) { this._frame.focus({ preventScroll: true }); if (this._frame.contentWindow) this._frame.contentWindow.focus(); } } catch (e) {} }
  /* WD_HOST.reload(): the same file again, from memory */
  _reload() {
    if (!this.isConnected) { this._state = 'idle'; this._frame = null; this._ls = null; this._box.replaceChildren(); return; }
    if (CACHE.html) { this._state = 'loading'; this._render(CACHE.html); return; }
    this._state = 'idle'; this._go();
  }
  _fail(reason) {
    if (this._state !== 'loading') return;
    clearTimeout(this._timer);
    this._state = 'error'; this._frame = null; this._ls = null;
    const lang = langOf(this._hass), m = document.createElement('div');
    m.className = 'msg'; m.setAttribute('role', 'alert'); m.setAttribute('data-reason', reason.key);
    m.innerHTML = ICON;
    lines(reason, lang).forEach((l, i) => { const p = document.createElement('p'); p.textContent = l.text; p.lang = l.lang; if (i) p.className = 'loc'; m.appendChild(p); });
    const b = document.createElement('button'); b.type = 'button'; b.textContent = retryLabel(lang);
    b.addEventListener('click', () => this.retry());
    m.appendChild(b);
    this._box.replaceChildren(m);
    this._updated();
    try { if (this.isConnected) b.focus({ preventScroll: true }); } catch (e) {}
  }
  /* Retry: ask Home Assistant again (a failed request is never kept) */
  retry() {
    if (this._state === 'loading') return;
    this._state = 'idle'; this._go();
  }
  /* what the dashboard in the frame listens to; a call into a document that is gone is skipped */
  _emit(type, v) { const ls = this._ls; if (ls) ls[type].forEach(fn => { try { fn(v); } catch (e) {} }); }
  _menuSync() { const m = this._menuAlways || menuShown(this._hass, this._narrow); if (m !== this._menu) { this._menu = m; this._emit('menu', m); } }
  /* Home Assistant's sidebar, opened or closed as its own menu button does it (ha-menu-button: fireEvent(this,
     'hass-toggle-menu'), an Event that bubbles and is composed, with detail {}): fired on this element, in Home
     Assistant's document, so it leaves the shadow roots and reaches home-assistant-main, which listens on itself */
  toggleMenu() {
    try {
      const ev = new Event('hass-toggle-menu', { bubbles: true, composed: true, cancelable: false });
      ev.detail = {};
      this.dispatchEvent(ev);
      return true;
    } catch (e) { return false; }
  }
  /* the checker's news: the dashboard is told; when no dashboard listens (one that could not start, the error
     message), the panel shows its own small button, in Home Assistant's colours and its user's language */
  _updated() {
    const info = updateInfo();
    this._emit('update', info);
    /* decided once the frame has loaded (its dashboard subscribes while it starts, before that), or on the message */
    const own = !!info && (this._state === 'error' || (this._state === 'shown' && !(this._ls && this._ls.update.size)));
    if (!own) { this._upd.hidden = true; return; }
    const tx = TEXT[langOf(this._hass)] || TEXT.en;
    this._upd.innerHTML = RELOAD;
    const s = document.createElement('span'); s.textContent = tx.update + ' · ' + tx.reload; this._upd.appendChild(s);
    this._upd.hidden = false;
  }
  /* the tap on "New version: reload": the page when the integration changed, else just the frame with the new file */
  applyUpdate() {
    const c = UP.change;
    if (!c) return '';
    if (c === 'page' || !this.isConnected) { HOOKS.reloadPage(); return 'page'; }
    forget(); this._state = 'idle'; this._go();
    return 'file';
  }
  /* what the panel shows: {state, reason, loads, update} (the probe) */
  get status() { const m = this._box.querySelector('.msg'); return { state: this._state, reason: m ? m.getAttribute('data-reason') : null, loads: this.loads, update: !this._upd.hidden }; }
  get frame() { return this._frame; }
  static forget() { forget(); }
}

/* ------------------------------------------------------------------ the card (D-087) */
/* The same dashboard as a Lovelace dashboard too: the kiosk apps list only those, so the integration also registers
   /nucleum-hub-dashboard (hidden from the sidebar, read-only: one panel view with this card; dashboard.py), and loads
   this module on every page of Home Assistant's frontend, at the panel's own address (one module, one CACHE). The card
   holds a panel element (the file, the frame, WD_HOST, a new version: all of the above) and makes it the whole screen,
   the owner's choice (24.10.2026): nothing of Home Assistant's around it, its sidebar only when the dashboard's menu
   button is tapped.
   - Lovelace's toolbar: while the card is on screen, a style in hui-root's shadow root hides it and the room it keeps
     (ROOT_CSS, hui-root's own selectors, read in the frontend of Home Assistant 2026.10, 20260930), taken out again
     when the card leaves, hui-root being the dashboard's and not only this view's. The panel element then takes its
     height from the view, as it does in the sidebar (_fit). A toolbar that another Home Assistant's selectors miss
     stays on screen, its menu button working: never a dashboard cut off or a blank page.
   - Home Assistant's sidebar: its kiosk mode while the card is on screen ("hass-kiosk-mode" on window, which
     Home Assistant's sidebar mixin turns into hass.kioskMode, never stored): home-assistant-main then keeps the sidebar
     in a drawer over the page, whatever the screen's width or the user's choice to dock it, and opens it only on
     "hass-toggle-menu". Turned off again when the last card leaves, and only when the card had turned it on (a kiosk
     app's own kiosk mode is left as it was).
   - The menu button: the dashboard's own (D-054: the app's tile, or the row at the end of the phone's More sheet) on
     every screen (menuAlways), since Home Assistant shows none in kiosk mode. */
export const CARD_TAG = 'nucleum-hub-card';
export const ROOT_CSS = '.header{display:none!important}hui-view-container{padding-top:0!important;padding-bottom:0!important}';
export const ROOT_MARK = 'data-nucleum-hub';
export const KIOSK_EVENT = 'hass-kiosk-mode';
/* the hui-root a node sits in, across shadow roots, or null */
export function rootOf(node) {
  for (let n = node; n;) {
    if (n.localName === 'hui-root') return n;
    n = n.parentNode || (n.host && n.nodeType === 11 ? n.host : null);
  }
  return null;
}
/* the cards on screen in each hui-root (its style goes when the last one leaves: Lovelace may add a new card before it
   takes the old one out), and whether the kiosk mode is the cards' */
const ROOTS = new WeakMap();
const KIOSK = { cards: new Set(), mine: false };
function kioskMode(on) {
  try { window.dispatchEvent(new CustomEvent(KIOSK_EVENT, { detail: { enable: on } })); } catch (e) {}
}

export class NucleumHubCard extends Base {
  constructor() {
    super();
    this._hass = null; this._config = {}; this._root = null;
    const root = this.attachShadow({ mode: 'open', delegatesFocus: true });
    root.innerHTML = '<style>:host{display:block;position:relative}</style>';
    this._panel = document.createElement(TAG);
    this._panel.menuAlways = true;
    root.appendChild(this._panel);
  }
  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }
  get hass() { return this._hass; }
  set hass(h) {
    this._hass = h;
    if (this.isConnected) this._full(true);
    this._panel.hass = h;
  }
  connectedCallback() { this._full(true); }
  disconnectedCallback() { this._full(false); }
  /* the whole screen while this card is on screen, Home Assistant's own again when no card of ours is left */
  _full(on) {
    if (on) {
      if (!KIOSK.cards.has(this)) {
        KIOSK.cards.add(this);
        if (!KIOSK.mine && !(this._hass && this._hass.kioskMode === true)) { KIOSK.mine = true; kioskMode(true); }
      }
      const root = rootOf(this);
      if (!root || !root.shadowRoot || root === this._root) return;
      this._root = root;
      const cards = ROOTS.get(root) || new Set(); cards.add(this); ROOTS.set(root, cards);
      if (!root.shadowRoot.querySelector('style[' + ROOT_MARK + ']')) {
        const s = document.createElement('style'); s.setAttribute(ROOT_MARK, ''); s.textContent = ROOT_CSS;
        root.shadowRoot.appendChild(s);
      }
      return;
    }
    KIOSK.cards.delete(this);
    if (!KIOSK.cards.size && KIOSK.mine) { KIOSK.mine = false; kioskMode(false); }
    const root = this._root; this._root = null;
    if (!root) return;
    const cards = ROOTS.get(root);
    if (cards) { cards.delete(this); if (cards.size) return; ROOTS.delete(root); }
    const s = root.shadowRoot && root.shadowRoot.querySelector('style[' + ROOT_MARK + ']');
    if (s) s.remove();
  }
  /* the panel element inside, and whether the kiosk mode is the cards' (the probe) */
  get panelEl() { return this._panel; }
  static get kiosk() { return { cards: KIOSK.cards.size, mine: KIOSK.mine }; }
}

if (typeof customElements !== 'undefined' && !customElements.get(TAG)) customElements.define(TAG, NucleumHubPanel);
if (typeof customElements !== 'undefined' && !customElements.get(CARD_TAG)) customElements.define(CARD_TAG, NucleumHubCard);
