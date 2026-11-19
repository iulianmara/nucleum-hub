"""Our pictures of the cameras (D-106): for a camera that keeps no recording Home Assistant can reach (no NVR), the
integration keeps a picture at each of its events (a doorbell ring, a person, motion…), so the dashboard's band has an
image at every marker. Never of a camera that only captures a screen (a wall tablet's screenshot, core.is_screen_capture). Off until a person turns it on (the dashboard asks once, at the start, when it finds no NVR); a
limit of 2 GB by default, changed in the dashboard's Settings; 14 days at most; one picture per camera every 10 s at most.

- The pictures: JPEG files in ``<config>/nucleum_hub/camera_photos/``, named ``<camera>-<ms>.jpg``; their list and the
  settings in the store ``wall_dashboard.camera_photos``.
- The dashboard reads them with ``wall_dashboard/camera/photos`` and shows one through ``/api/wall_dashboard/camera_photo/
  <id>`` (authenticated, a signed path for an ``<img>``, only for a user who may read the camera); the settings with
  ``wall_dashboard/camera/photos/settings`` (changing them is for an administrator).
The rules themselves, pure, are in cameras_core.py.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from . import cameras_core as core
from .const import URL_CAMERA_PHOTO
from .models import async_loaded_data

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "wall_dashboard.camera_photos"
STORE_VERSION = 1
SAVE_DELAY = 10.0
FOLDER = ("nucleum_hub", "camera_photos")
GB = 1024 ** 3
PHOTO_ID = re.compile(r"^[a-z0-9_]+-[0-9]{10,16}$")


def _empty() -> dict[str, Any]:
    return {"settings": {"enabled": False, "limit_gb": core.LIMIT_DEFAULT_GB, "asked": False, "pick": {}}, "photos": []}


def _valid(data: Any) -> dict[str, Any]:
    out = _empty()
    if not isinstance(data, dict):
        return out
    st = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    pick = st.get("pick") if isinstance(st.get("pick"), dict) else {}
    out["settings"] = {"enabled": st.get("enabled") is True, "limit_gb": core.limit_gb(st.get("limit_gb", core.LIMIT_DEFAULT_GB)),
                       "asked": st.get("asked") is True,
                       # the owner's ticks in the assistant's "Your cameras" (D-172, D-182): camera -> takes pictures or not
                       "pick": {k: v for k, v in pick.items() if isinstance(k, str) and k.startswith("camera.") and isinstance(v, bool)}}
    out["photos"] = [p for p in (data.get("photos") or []) if isinstance(p, dict) and isinstance(p.get("id"), str)
                     and isinstance(p.get("cam"), str) and isinstance(p.get("at"), (int, float))]
    return out


class CameraPhotos:
    """The pictures at each event of the cameras without recordings, within the limit."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.data: dict[str, Any] = _empty()
        self.folder = hass.config.path(*FOLDER) if hasattr(hass.config, "path") else os.path.join("config", *FOLDER)
        self._unsub: list[Any] = []
        self._last: dict[str, float] = {}
        self._map: dict[str, tuple[str, str, bool]] | None = None   # related entity -> (camera, kind, is_event)

    async def async_start(self) -> None:
        try:
            self.data = _valid(await self.store.async_load())
        except Exception:  # noqa: BLE001 (a damaged store starts empty and off)
            _LOGGER.exception("The cameras' pictures could not be read; they start off")
            self.data = _empty()
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed))
        # the pictures an older version took of a screen capture (a tablet's screenshot) go, with their files
        screens = [p for p in self.data["photos"] if self._is_screen(p["cam"])]
        if screens:
            self.data["photos"] = [p for p in self.data["photos"] if not self._is_screen(p["cam"])]
            await self.hass.async_add_executor_job(self._remove, [p["id"] for p in screens])
            await self.store.async_save(self.data)

    async def async_stop(self) -> None:
        for un in self._unsub:
            try:
                un()
            except Exception:  # noqa: BLE001
                pass
        self._unsub.clear()

    # --- which entities tell which camera's events ---------------------------------------------------------------------
    def _is_screen(self, cam: str) -> bool:
        """A camera that does not look at the house (core.not_house, D-172): a tablet's, a robot's map, a radar, a printer."""
        reg = er.async_get(self.hass)
        entry = reg.async_get(cam)
        dev_id = getattr(entry, "device_id", None)
        device = dr.async_get(self.hass).async_get(dev_id) if dev_id else None
        text = " ".join(str(v) for v in (getattr(device, "manufacturer", None), getattr(device, "model", None), getattr(device, "name", None),
                                         getattr(device, "name_by_user", None)) if v)
        sibling = bool(dev_id) and any(
            e.entity_id != cam and e.entity_id.startswith("camera.") and core.is_screen_capture(e.entity_id, e.original_name, e.unique_id)
            for e in er.async_entries_for_device(reg, dev_id))
        st = self.hass.states.get(cam)
        name = (st.attributes.get("friendly_name") if st is not None else None) or getattr(entry, "original_name", None)
        return bool(core.not_house(cam, name, getattr(entry, "platform", None), text, sibling, getattr(entry, "unique_id", None))) or \
            core.is_screen_capture(cam, getattr(entry, "original_name", None), getattr(entry, "unique_id", None))

    def _cameras(self) -> list[str]:
        """The cameras that look at a place: the ones the owner ticked in "Your cameras" (D-172, D-182); one not ticked or
        unticked there, by the rule: not a tablet's, a robot's map, a radar or a printer (core.not_house)."""
        pick = self.data["settings"].get("pick") or {}
        return [e for e in self.hass.states.async_entity_ids("camera") if (pick[e] if e in pick else not self._is_screen(e))]

    def platforms(self) -> list[str]:
        reg = er.async_get(self.hass)
        out = []
        for cam in self._cameras():
            entry = reg.async_get(cam)
            if entry is not None and getattr(entry, "platform", None):
                out.append(entry.platform)
        return out

    def _related_map(self) -> dict[str, tuple[str, str, bool]]:
        if self._map is not None:
            return self._map
        reg = er.async_get(self.hass)
        rows = []
        for eid in list(self.hass.states.async_entity_ids("camera")) + list(self.hass.states.async_entity_ids("binary_sensor")) + \
                list(self.hass.states.async_entity_ids("event")):
            entry = reg.async_get(eid)
            st = self.hass.states.get(eid)
            attrs = st.attributes if st is not None else {}
            rows.append({"entity_id": eid, "device_id": getattr(entry, "device_id", None), "device_class": attrs.get("device_class"),
                         "event_types": attrs.get("event_types")})
        out: dict[str, tuple[str, str, bool]] = {}
        for cam in self._cameras():
            entry = reg.async_get(cam)
            if entry is not None and getattr(entry, "platform", None) in core.NVR_PLATFORMS:
                continue   # it keeps its own recordings
            for eid, (kind, is_event) in core.related(cam, rows).items():
                out[eid] = (cam, kind, is_event)
        self._map = out
        return out

    @callback
    def _changed(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        if not isinstance(eid, str) or not self.data["settings"]["enabled"]:
            return
        domain = eid.split(".", 1)[0]
        if domain == "camera" and (event.data.get("old_state") is None or event.data.get("new_state") is None):
            self._map = None   # a camera came or went: read again
            return
        if domain not in ("event", "binary_sensor"):
            return
        hit = self._related_map().get(eid)
        if hit is None:
            return
        new, old = event.data.get("new_state"), event.data.get("old_state")
        ns, os_ = getattr(new, "state", None), getattr(old, "state", None)
        cam, kind, is_event = hit
        if is_event:
            if ns in (None, "unknown", "unavailable") or ns == os_ or old is None:
                return
        elif ns != "on" or os_ == "on":
            return
        now = time.time()
        if now - self._last.get(cam, 0) < core.PHOTO_GAP:
            return
        self._last[cam] = now
        self.hass.async_create_task(self.async_take(cam, kind, now), f"wall_dashboard camera photo {cam}")

    # --- taking, keeping and pruning ---------------------------------------------------------------------------------
    async def async_take(self, cam: str, kind: str, at: float) -> dict[str, Any] | None:
        """One picture of the camera now, kept within the limit; None when the camera gives none."""
        try:
            from homeassistant.components import camera as ha_camera  # noqa: PLC0415 (only when a picture is taken)
            image = await ha_camera.async_get_image(self.hass, cam)
            body = getattr(image, "content", None)
        except Exception:  # noqa: BLE001 (a camera offline now: no picture, the marker stays)
            _LOGGER.debug("No picture from %s", cam, exc_info=True)
            return None
        if not isinstance(body, (bytes, bytearray)) or not body:
            return None
        pid = core.photo_id(cam, at)
        await self.hass.async_add_executor_job(self._write, pid, bytes(body))
        photo = {"id": pid, "cam": cam, "at": at, "kind": kind, "size": len(body)}
        self.data["photos"].append(photo)
        await self._async_prune(at)
        self.store.async_delay_save(lambda: self.data, SAVE_DELAY)
        return photo

    def path_of(self, pid: str) -> str:
        return os.path.join(self.folder, pid + ".jpg")

    def _write(self, pid: str, body: bytes) -> None:
        os.makedirs(self.folder, exist_ok=True)
        Path(self.path_of(pid)).write_bytes(body)   # in the executor

    def _remove(self, pids: list[str]) -> None:
        for pid in pids:
            try:
                os.remove(self.path_of(pid))
            except OSError:
                pass

    async def _async_prune(self, now: float) -> None:
        kept, removed = core.prune_photos(self.data["photos"], now, self.data["settings"]["limit_gb"] * GB)
        if removed:
            self.data["photos"] = kept
            await self.hass.async_add_executor_job(self._remove, [p["id"] for p in removed])

    # --- what the dashboard reads and sets ---------------------------------------------------------------------------
    def used(self) -> int:
        return sum(int(p.get("size") or 0) for p in self.data["photos"])

    def settings(self) -> dict[str, Any]:
        st = self.data["settings"]
        cams = self._cameras()
        return {"enabled": st["enabled"], "limit_gb": st["limit_gb"], "asked": st["asked"], "pick": dict(st.get("pick") or {}), "used_bytes": self.used(),
                "cameras": len(cams), "nvr": core.has_nvr(self.platforms()), "keep_days": core.PHOTO_KEEP_DAYS}

    async def async_set(self, enabled: Any = None, limit: Any = None, asked: Any = None, pick: Any = None) -> dict[str, Any]:
        st = self.data["settings"]
        if isinstance(pick, dict):
            st["pick"] = {k: v for k, v in pick.items() if isinstance(k, str) and k.startswith("camera.") and isinstance(v, bool)}
            self._map = None
        if isinstance(enabled, bool):
            st["enabled"] = enabled
            self._map = None
        if limit is not None:
            st["limit_gb"] = core.limit_gb(limit)
            await self._async_prune(time.time())
        if isinstance(asked, bool):
            st["asked"] = asked
        await self.store.async_save(self.data)
        return self.settings()

    def photos(self, cam: str, after: float, before: float) -> list[dict[str, Any]]:
        return [{"id": p["id"], "at": p["at"], "kind": p.get("kind") or "other"} for p in self.data["photos"]
                if p["cam"] == cam and after <= p["at"] <= before]

    def find(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.data["photos"] if p["id"] == pid), None)


class CameraPhotoView(HomeAssistantView):
    """``/api/wall_dashboard/camera_photo/<id>``: one of our pictures, for a user who may read its camera."""

    url = URL_CAMERA_PHOTO
    name = "api:wall_dashboard:camera_photo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, photo_id: str) -> web.Response:
        data = async_loaded_data(self.hass)
        keeper = getattr(data, "camera_photos", None) if data is not None else None
        if keeper is None or not PHOTO_ID.match(photo_id):
            return web.Response(status=404)
        photo = keeper.find(photo_id)
        if photo is None:
            return web.Response(status=404)
        user = request[KEY_HASS_USER]
        if not user.permissions.check_entity(photo["cam"], POLICY_READ):
            return web.Response(status=403)   # 403, never a 401 that HA's ban middleware would count
        try:
            body = await self.hass.async_add_executor_job(_read, keeper.path_of(photo_id))
        except OSError:
            return web.Response(status=404)
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _read(path: str) -> bytes:
    return Path(path).read_bytes()   # in the executor


@callback
def async_register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(CameraPhotoView(hass))
