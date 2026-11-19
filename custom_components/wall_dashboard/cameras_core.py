"""The cameras' timeline (D-106, docs/CERCETARE-camere-2026-11.md §5), pure: every source brought to one shape, so the
dashboard draws the same band whatever the brand.

A marker is {"start", "end", "kind", "label", "source", "ref", "severity"}: seconds (the epoch; end None while it lasts),
one of KINDS (what the band colours), the source's own label as it said it, where it came from ("frigate", "event",
"sensor", "media"), the reference the integration resolves later into a signed address (a clip, a picture), and
"alert" / "detection" / None. A segment of recording is {"start", "end", "ref"}.

- Frigate's review items (``frigate/reviews/get``): alerts and detections, with their objects, zones and sub-labels;
- Home Assistant's event entities and binary sensors of the same device (their history): doorbell rings, motion,
  people, vehicles, animals, parcels; the labels are not standard, so LABELS maps each integration's to a kind;
- the media_source tree of an NVR that has one (Reolink, motionEye, Dahua, tapo_control, Nest, Netatmo): clips by day.

Nothing here imports Home Assistant; tests/test_cameras_core.py runs it as it is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

KINDS = ("person", "vehicle", "animal", "package", "doorbell", "face", "motion", "sound", "other")
# a label of any integration, as it writes it (lower case, any separator), to the band's kind
LABELS: dict[str, str] = {}
for kind, words in {
    "person": ("person", "people", "human", "human_shape", "pedestrian", "detections_person", "person_detected", "smart_detect_person"),
    "vehicle": ("vehicle", "car", "truck", "bus", "motorcycle", "bicycle", "detections_vehicle", "smart_detect_vehicle", "license_plate"),
    "animal": ("animal", "pet", "dog", "cat", "bird", "horse", "detections_animal", "smart_detect_animal"),
    "package": ("package", "parcel", "detections_package", "smart_detect_package"),
    "doorbell": ("doorbell", "ring", "ding", "visitor", "press", "doorbell_press"),
    "face": ("face", "face_detected", "detections_face"),
    "motion": ("motion", "movement", "moving", "motion_detected", "occupancy", "presence", "field_detection", "line_crossing"),
    "sound": ("sound", "audio", "speech", "bark", "glass_break", "smoke_alarm", "co_alarm", "baby_cry"),
}.items():
    for w in words:
        LABELS[w] = kind

MERGE_GAP = 20.0   # two markers of one kind this close become one (a sensor that flickers)
MAX_MARKERS = 2000


def kind_of(label: Any) -> str:
    """The band's kind for a label of any integration ("Person", "detections_person", "smart_detect-vehicle" …)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(label or "").lower()).strip("_")
    if s in LABELS:
        return LABELS[s]
    for part in reversed(s.split("_")):
        if part in LABELS:
            return LABELS[part]
    return "other"


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def frigate_markers(reviews: Any) -> list[dict[str, Any]]:
    """Frigate's review items: one marker each, its kind from its first object, its own words kept."""
    out: list[dict[str, Any]] = []
    if not isinstance(reviews, Iterable) or isinstance(reviews, (str, bytes, Mapping)):
        return out
    for r in reviews:
        if not isinstance(r, Mapping):
            continue
        start = _num(r.get("start_time"))
        if start is None:
            continue
        data = r.get("data") if isinstance(r.get("data"), Mapping) else {}
        objects = [o for o in (data.get("objects") or []) if isinstance(o, str)]
        audio = [a for a in (data.get("audio") or []) if isinstance(a, str)]
        label = objects[0] if objects else (audio[0] if audio else "motion")
        subs = [x for x in (data.get("sub_labels") or []) if isinstance(x, str)]
        out.append({
            "start": start, "end": _num(r.get("end_time")), "kind": kind_of(label), "label": label,
            "source": "frigate", "ref": f"frigate:review:{r.get('id')}" if r.get("id") else None,
            "severity": r.get("severity") if r.get("severity") in ("alert", "detection") else None,
            "zones": [z for z in (data.get("zones") or []) if isinstance(z, str)], "sub_labels": subs,
            "title": (data.get("metadata") or {}).get("title") if isinstance(data.get("metadata"), Mapping) else None,
        })
    return out


def sensor_markers(entity_id: str, label: Any, history: Any, is_event: bool) -> list[dict[str, Any]]:
    """A device's event entity (each new state is a moment: its time) or binary sensor ("on" from … to "off") as markers.
    history: [{"state", "when"}] in time order, as lists.py / cameras.py read it from the recorder."""
    out: list[dict[str, Any]] = []
    kind = kind_of(label)
    if not isinstance(history, Iterable):
        return out
    open_at: float | None = None
    for h in history:
        if not isinstance(h, Mapping):
            continue
        when, st = _num(h.get("when")), h.get("state")
        if when is None:
            continue
        if is_event:
            if st not in (None, "unknown", "unavailable", ""):
                out.append({"start": when, "end": when, "kind": kind, "label": str(label), "source": "event", "ref": None, "severity": None})
            continue
        if st == "on" and open_at is None:
            open_at = when
        elif st != "on" and open_at is not None:
            out.append({"start": open_at, "end": when, "kind": kind, "label": str(label), "source": "sensor", "ref": None, "severity": None})
            open_at = None
    if open_at is not None:
        out.append({"start": open_at, "end": None, "kind": kind, "label": str(label), "source": "sensor", "ref": None, "severity": None})
    return out


def merge(markers: Iterable[Mapping[str, Any]], gap: float = MERGE_GAP) -> list[dict[str, Any]]:
    """In time order; two of one kind and one source this close become one (the later end); at most MAX_MARKERS, the
    newest kept."""
    ms = sorted((dict(m) for m in markers if isinstance(m, Mapping) and _num(m.get("start")) is not None), key=lambda m: m["start"])
    out: list[dict[str, Any]] = []
    last_of: dict[tuple[str, str], dict[str, Any]] = {}
    for m in ms:
        last = last_of.get((m["kind"], m["source"]))
        if last and m["source"] != "frigate":
            end = last["end"] if last["end"] is not None else last["start"]
            if m["start"] - end <= gap:
                last["end"] = None if (last["end"] is None and m["end"] is None) else max(x for x in (last["end"], m["end"], m["start"]) if x is not None)
                continue
        out.append(m)
        last_of[(m["kind"], m["source"])] = m
    return out[-MAX_MARKERS:]


def within(markers: Iterable[Mapping[str, Any]], after: float, before: float) -> list[dict[str, Any]]:
    """The markers that touch [after, before]."""
    return [dict(m) for m in markers if m["start"] <= before and (m["end"] if m["end"] is not None else m["start"]) >= after]


def segments(spans: Any, after: float, before: float) -> list[dict[str, Any]]:
    """Recording segments from an NVR's own list ({"start_time", "end_time"} as Frigate's recordings, or {"start", "end"}),
    clipped to the window, joined where they touch (a recording is written in pieces of a few seconds)."""
    raw = []
    if isinstance(spans, Iterable) and not isinstance(spans, (str, bytes, Mapping)):
        for s in spans:
            if not isinstance(s, Mapping):
                continue
            a = _num(s.get("start_time", s.get("start")))
            b = _num(s.get("end_time", s.get("end")))
            if a is None or b is None or b <= a or b < after or a > before:
                continue
            raw.append((max(a, after), min(b, before)))
    raw.sort()
    out: list[dict[str, Any]] = []
    for a, b in raw:
        if out and a - out[-1]["end"] <= 2.0:
            out[-1]["end"] = max(out[-1]["end"], b)
        else:
            out.append({"start": a, "end": b})
    return out


def tier(has_live: bool, has_markers: bool, has_recordings: bool, our_pictures: bool) -> str:
    """What a camera can show (the mockup's states): 'recordings' (an NVR's clips), 'pictures' (our pictures at each
    event), 'markers' (events without a picture), 'live' (only the picture now), or 'none'."""
    if has_recordings:
        return "recordings"
    if has_markers and our_pictures:
        return "pictures"
    if has_markers:
        return "markers"
    return "live" if has_live else "none"


# --- our pictures (D-106): a picture at each event of a camera that keeps no recording, within a limit ------------------
# the integrations whose cameras keep recordings Home Assistant can reach (an NVR, or a camera's own card with a timeline)
NVR_PLATFORMS = frozenset({"frigate", "unifiprotect", "reolink", "motioneye", "dahua", "tapo_control", "nest", "netatmo"})
PHOTO_KEEP_DAYS = 14
PHOTO_GAP = 10.0          # one picture per camera at most this often (a burst of events is one picture)
LIMIT_MIN_GB, LIMIT_MAX_GB, LIMIT_DEFAULT_GB = 0.5, 20.0, 2.0


def has_nvr(platforms: Iterable[Any]) -> bool:
    """Whether any of these integrations (the cameras' platforms) keeps recordings: then the question at the start is not
    asked ("I found no NVR...")."""
    return any(isinstance(p, str) and p in NVR_PLATFORMS for p in platforms)


def limit_gb(value: Any) -> float:
    """The limit a person set, within LIMIT_MIN_GB..LIMIT_MAX_GB, by halves (0.5 GB steps)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return LIMIT_DEFAULT_GB
    if v != v:   # NaN
        return LIMIT_DEFAULT_GB
    return min(LIMIT_MAX_GB, max(LIMIT_MIN_GB, round(v * 2) / 2))


def prune_photos(photos: Iterable[Mapping[str, Any]], now: float, limit_bytes: float,
                 keep_days: float = PHOTO_KEEP_DAYS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, removed): the pictures older than keep_days go, then the oldest until the rest fits in limit_bytes."""
    ps = sorted((dict(p) for p in photos if isinstance(p, Mapping) and _num(p.get("at")) is not None), key=lambda p: p["at"])
    removed = [p for p in ps if p["at"] < now - keep_days * 86400]
    kept = [p for p in ps if p["at"] >= now - keep_days * 86400]
    total = sum(int(p.get("size") or 0) for p in kept)
    while kept and total > limit_bytes:
        p = kept.pop(0)
        total -= int(p.get("size") or 0)
        removed.append(p)
    return kept, removed


def photo_id(cam: str, at: float) -> str:
    """A picture's name and address: the camera's object id and the time in milliseconds ("front_door-1793600000123")."""
    obj = re.sub(r"[^a-z0-9_]+", "_", str(cam).split(".", 1)[-1].lower()).strip("_") or "camera"
    return f"{obj}-{int(round(at * 1000))}"


# A "camera" that shows a screen, not a place: a wall tablet's screenshot (Kiosk Satellite over ESPHome names it
# "Screenshot", camera.<tablet>_screenshot). Its device's motion sensor is the tablet's own front camera, so every person
# walking past took a screenshot of the dashboard (17.11.2026, 8,833 of them on the owner's Home Assistant in a week).
SCREEN_CAPTURE = re.compile(r"screen[ _-]?(shot|cap(ture)?)", re.I)


def is_screen_capture(entity_id: Any, original_name: Any = None, unique_id: Any = None) -> bool:
    """Whether a camera only captures a screen (a tablet's screenshot): its entity id, its integration's own name for
    it or its unique id says so. Such a camera gets no picture of ours at its device's events."""
    return any(isinstance(v, str) and SCREEN_CAPTURE.search(v) for v in (entity_id, original_name, unique_id))


# A camera that does not look at the house (D-172, the analysis of 17.11.2026, P1-3): a tablet's screenshot or its own front
# camera, a robot's map, a weather radar, a 3D printer's camera, Frigate's Birdseye. The same rule as the dashboard's
# WD_CAMS.notHouse (src/modules/cameras.js).
AWAY_PLATFORM = {"xiaomi_cloud_map_extractor": "map", "dreame_vacuum": "map", "valetudo": "map", "valetudo_vacuum_camera": "map",
                 "ecovacs": "map", "roborock": "map", "buienradar": "radar", "rainviewer": "radar", "bambu_lab": "printer",
                 "octoprint": "printer", "prusalink": "printer", "moonraker": "printer", "fully_kiosk": "tablet"}
AWAY_WORDS = (("map", re.compile(r"(^|[^a-z])(map|floor_?plan)([^a-z]|$)", re.I)), ("radar", re.compile(r"radar", re.I)),
              ("printer", re.compile(r"(printer|chamber|octoprint|bambu|prusa)", re.I)), ("birdseye", re.compile(r"birds_?eye", re.I)),
              ("tablet", re.compile(r"(tablet|kiosk)", re.I)))
AWAY_DEVICE = (("tablet", re.compile(r"(tablet|kiosk|fully)", re.I)), ("printer", re.compile(r"(printer|bambu|prusa|octoprint|creality)", re.I)))


def not_house(entity_id: Any, name: Any = None, platform: Any = None, device_text: Any = None, screen_sibling: bool = False,
              unique_id: Any = None) -> str:
    """Why a camera does not look at the house ('screen', 'tablet', 'map', 'radar', 'printer', 'birdseye'), or '' for a camera
    of the house. Such a camera gets no picture of ours at its device's events."""
    if not isinstance(entity_id, str) or entity_id.split(".", 1)[0] != "camera":
        return ""
    if is_screen_capture(entity_id, name, unique_id):
        return "screen"
    if isinstance(platform, str) and platform in AWAY_PLATFORM:
        return AWAY_PLATFORM[platform]
    text = entity_id.split(".", 1)[-1] + " " + (name if isinstance(name, str) else "")
    for reason, rx in AWAY_WORDS:
        if rx.search(text):
            return reason
    if isinstance(device_text, str):
        for reason, rx in AWAY_DEVICE:
            if rx.search(device_text):
                return reason
    return "tablet" if screen_sibling else ""


NOT_SEEN = frozenset({"door", "window", "opening", "garage_door", "battery", "battery_charging", "connectivity", "problem", "update",
                      "plug", "power", "running", "safety", "tamper", "lock", "moisture", "smoke", "gas", "carbon_monoxide", "heat",
                      "cold", "light", "vibration", "sound_alarm"})


def related(cam: str, entities: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, bool]]:
    """The entities that tell a camera's events, as the dashboard reads them (src/modules/cameras.js relatedOf): its
    device's event entities and binary sensors that see something (not a door, a battery, a problem…).
    entities: [{"entity_id", "device_id", "device_class", "event_types"}]; -> {entity_id: (kind, is_event)}"""
    ents = [e for e in entities if isinstance(e, Mapping)]
    dev = next((e.get("device_id") for e in ents if e.get("entity_id") == cam), None)
    out: dict[str, tuple[str, bool]] = {}
    if not dev:
        return out
    for e in ents:
        eid, dc = str(e.get("entity_id") or ""), e.get("device_class") or ""
        if eid == cam or e.get("device_id") != dev:
            continue
        domain, obj = eid.split(".", 1) if "." in eid else ("", eid)
        if domain == "event":
            types = " ".join(t for t in (e.get("event_types") or []) if isinstance(t, str))
            kind = "doorbell" if dc == "doorbell" else kind_of(types) if kind_of(types) != "other" else kind_of(obj)
            out[eid] = (kind, True)
        elif domain == "binary_sensor" and dc not in NOT_SEEN:
            kind = kind_of(re.sub(r"_?occupancy$", "", obj))
            if kind == "other" and dc:
                kind = kind_of(dc)
            if kind == "other" and not dc:
                continue
            out[eid] = (kind, False)
    return out
