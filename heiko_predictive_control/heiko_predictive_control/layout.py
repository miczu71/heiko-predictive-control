"""Wczytywanie układu domu z pliku JSON + walidacja.

Prawdziwy dom użytkownika NIE jest częścią tego repo (repo add-onu musi być
publiczne — Supervisor klonuje repozytoria add-onów anonimowo). Add-on czyta go z
prywatnej konfiguracji Home Assistant (`map: homeassistant_config:ro`), domyślnie
`/homeassistant/heiko_predictive/house.json`. Brak pliku albo błąd = neutralny
przykład `example_house.json` + ostrzeżenie w logu i na pulpicie.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .rooms import (Box, Equipment, FrontFeature, House, Level, Room, Stairs,
                    WallFeature)

logger = logging.getLogger(__name__)

EXAMPLE_PATH = Path(__file__).with_name("example_house.json")


def _tup(v):
    if isinstance(v, list):
        return tuple(_tup(x) for x in v)
    return v


def _room(d: dict) -> Room:
    return Room(key=d["key"], floor=d["floor"], label=d["label"], rect=_tup(d["rect"]),
                entity=d.get("entity"), card=d.get("card"), kind=d.get("kind", "room"),
                extra=_tup(d.get("extra", [])), outline=_tup(d["outline"]) if d.get("outline") else None,
                floor_style=d.get("floor_style"), note=d.get("note"))


def _equipment(d: dict) -> Equipment:
    return Equipment(key=d["key"], kind=d["kind"], label=d["label"], entity=d["entity"],
                     card=d["card"], pos=_tup(d["pos"]) if d.get("pos") else None,
                     controlled=bool(d.get("controlled", False)), vents=_tup(d.get("vents", [])),
                     leader_to=_tup(d["leader_to"]) if d.get("leader_to") else None,
                     note=d.get("note"))


def parse(data: dict, source: str) -> House:
    return House(
        foot_w=float(data["footprint"][0]), foot_d=float(data["footprint"][1]),
        levels=tuple(Level(lv["key"], lv["label"], float(lv["z"]), bool(lv.get("attic", False)))
                     for lv in data["levels"]),
        rooms=tuple(_room(r) for r in data["rooms"]),
        equipment=tuple(_equipment(e) for e in data.get("equipment", [])),
        pump_box=_tup(data["pump_box"]) if data.get("pump_box") else None,
        stair_opening=_tup(data["stair_opening"]) if data.get("stair_opening") else None,
        pv=_tup(data["pv"]) if data.get("pv") else None,
        boxes=tuple(Box(b["floor"], _tup(b["rect"]), float(b["h"]), float(b.get("z0", 0)),
                        b.get("style", "neutral"), b.get("top")) for b in data.get("boxes", [])),
        walls=tuple(WallFeature(w["floor"], w["wall"], float(w["start"]), float(w["end"]),
                                w["style"], float(w.get("z0", 0)), float(w.get("z1", 250)))
                    for w in data.get("walls", [])),
        fronts=tuple(FrontFeature(f["floor"], f["wall"], float(f["start"]), float(f["end"]),
                                  float(f.get("h", 220)), f.get("style", "glass"))
                     for f in data.get("fronts", [])),
        stairs=tuple(Stairs(s["floor"], float(s["x"]), float(s["w"]), float(s["y_start"]),
                            float(s["y_end"]), int(s["steps"]), float(s["rise"]),
                            s.get("style", "stair"), s.get("top"))
                     for s in data.get("stairs", [])),
        source=source,
    )


def _area(points) -> float:
    pts = list(points)
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2


def validate(house: House) -> list[str]:
    """Błędy spójności układu (pusta lista = OK). Sprawdza to samo, co przedtem
    pilnowały testy zaszyte pod konkretny dom — teraz dla dowolnego pliku."""
    errors: list[str] = []
    floors = house.floors
    keys = [r.key for r in house.rooms]
    if len(keys) != len(set(keys)):
        errors.append("powtórzone klucze pokoi")
    for r in house.rooms:
        if r.floor not in floors:
            errors.append(f"{r.key}: nieznana kondygnacja {r.floor}")
        if abs(_area(r.plan_points()) - sum(w * d for _, _, w, d in r.rects)) > 0.5:
            errors.append(f"{r.key}: obrys ma inne pole niż prostokąty")
    for lv in house.levels:
        rects = [rc for r in house.rooms if r.floor == lv.key for rc in r.rects]
        area = 0.0
        for x, y, w, d in rects:
            if x < 0 or y < 0 or x + w > house.foot_w or y + d > house.foot_d:
                errors.append(f"{lv.key}: prostokąt {(x, y, w, d)} poza obrysem domu")
            area += w * d
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                ow = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                od = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                if ow > 0 and od > 0:
                    errors.append(f"{lv.key}: pokoje nachodzą na siebie {a} / {b}")
        if rects and abs(area - house.foot_w * house.foot_d) > 0.5:
            errors.append(f"{lv.key}: pokoje nie pokrywają całego rzutu")
    for e in house.equipment:
        for v in e.vents:
            if v not in keys:
                errors.append(f"{e.key}: nawiew w nieznanym pokoju {v}")
    for item in (*house.boxes, *house.walls, *house.fronts, *house.stairs):
        if item.floor not in floors:
            errors.append(f"element na nieznanej kondygnacji {item.floor}")
    return errors


def load_house(path: str | None) -> tuple[House, list[str]]:
    """(dom, ostrzeżenia). Nigdy nie rzuca — w najgorszym razie przykład."""
    warnings: list[str] = []
    if path:
        p = Path(path)
        if p.is_file():
            try:
                house = parse(json.loads(p.read_text(encoding="utf-8")), str(p))
                errors = validate(house)
                if not errors:
                    return house, []
                warnings = [f"Układ domu {p}: {e}" for e in errors]
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                warnings = [f"Nie da się wczytać układu domu {p}: {exc}"]
        else:
            warnings = [f"Brak pliku układu domu {p} — pokazuję przykładowy dom."]
    else:
        warnings = ["Nie ustawiono pliku układu domu — pokazuję przykładowy dom."]
    for w in warnings:
        logger.warning(w)
    return parse(json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")), "example"), warnings
