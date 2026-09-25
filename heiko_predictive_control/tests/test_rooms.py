"""Silnik modelu domu + walidacja układu — na neutralnym przykładzie.

Prawdziwy dom użytkownika nie jest częścią tego repo (patrz layout.py); jego
potwierdzone rozmieszczenie sprawdzają testy trzymane obok prywatnego pliku.
"""
import json
import re

import yaml
from pathlib import Path

from heiko_predictive_control.layout import EXAMPLE_PATH, load_house, parse, validate
from heiko_predictive_control.rooms import (
    K, KY, KZ, Box, Stairs, build_scene, iso, rect_corners, stair_boxes, temp_class,
)

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def example():
    return parse(json.loads(EXAMPLE_PATH.read_text()), "example")


def test_iso_origin_and_axes():
    assert iso(0, 0, 0) == (0, 0)
    # +x w prawo i w dół, +y w lewo i w dół (klasyczne 2:1)
    assert iso(100, 0, 0) == (100 * K, 100 * K * KY)
    assert iso(0, 100, 0) == (-100 * K, 100 * K * KY)
    # z podnosi punkt w górę ekranu, bez przesunięcia w poziomie
    assert iso(0, 0, 100) == (0, -100 * KZ)


def test_rect_corners_order_and_projection():
    pts = rect_corners(0, 0, 100, 50, 0)
    assert pts == [iso(0, 0), iso(100, 0), iso(100, 50), iso(0, 50)]


def test_higher_floor_is_higher_on_screen():
    floors = example().floors
    ys = [iso(10, 10, floors[k])[1] for k in ("parter", "pietro", "poddasze")]
    assert ys[2] < ys[1] < ys[0]


def test_temp_class_thresholds():
    assert temp_class(None) == "t-none"
    assert temp_class(18.0) == "t-cold"
    assert temp_class(19.5) == "t-cool"
    assert temp_class(21.0) == "t-ok"
    assert temp_class(23.49) == "t-ok"
    assert temp_class(23.5) == "t-warm"


def test_example_house_is_valid():
    assert validate(example()) == []


def test_validate_catches_overlap_hole_and_bad_outline():
    data = json.loads(EXAMPLE_PATH.read_text())
    data["rooms"][1]["rect"] = [0, 400, 400, 500]     # nachodzi na salon, dziura na dole
    data["rooms"][0]["outline"] = [[0, 0], [10, 0], [10, 10], [0, 10]]
    errors = validate(parse(data, "test"))
    assert any("nachodzą" in e for e in errors)
    assert any("obrys" in e for e in errors)


def test_load_house_falls_back_to_example(tmp_path):
    house, warnings = load_house(str(tmp_path / "brak.json"))
    assert house.source == "example" and warnings
    bad = tmp_path / "zly.json"
    bad.write_text("{nie json")
    house, warnings = load_house(str(bad))
    assert house.source == "example" and "Nie da się wczytać" in warnings[0]


def test_load_house_reads_file(tmp_path):
    p = tmp_path / "house.json"
    p.write_text(EXAMPLE_PATH.read_text())
    house, warnings = load_house(str(p))
    assert house.source == str(p) and warnings == []


def test_every_default_day_zone_entity_has_a_room():
    """Domyślne opcje add-onu pasują do przykładowego domu (świeża instalacja
    bez pliku układu pokazuje spójny pulpit)."""
    options = yaml.safe_load(CONFIG.read_text())["options"]
    zone = [e.strip() for e in options["day_zone_temp_entities"].split(",")]
    mapped = {r.entity for r in example().rooms if r.entity}
    assert set(zone) <= mapped
    assert options["attic_temp_entity"] in mapped


def test_stair_boxes_rise_and_order():
    s = Stairs("parter", x=600, w=90, y_start=700, y_end=440, steps=13, rise=17)
    steps = stair_boxes(s)
    assert len(steps) == 13
    assert steps[0].rect[1] + steps[0].rect[3] == 700      # pierwszy stopień przy y_start
    assert abs(steps[-1].rect[1] - 440) < 1e-6              # ostatni dochodzi do y_end
    assert [b.h for b in steps] == [17 * (i + 1) for i in range(13)]


def test_furniture_painter_order_far_first():
    house = example()
    far = Box("parter", (0, 0, 50, 50), 100, style="black")
    near = Box("parter", (100, 100, 50, 50), 40, style="oak")
    scene = build_scene(house.__class__(**{**house.__dict__, "boxes": (near, far)}))
    classes = [f.cls for f in scene.furniture]
    assert classes.index("bx black s") < classes.index("bx oak s")


def test_scene_viewbox_contains_everything():
    scene = build_scene(example())
    w, h = scene.width, scene.height
    assert scene.viewbox == f"0 0 {w} {h}"
    coords = []
    for group in (scene.faces_back, scene.roof, scene.pv, scene.faces_front, scene.pump,
                  scene.overlays, scene.furniture, scene.wall_features, scene.fronts):
        for face in group:
            coords += face.points.split()
    for rs in scene.rooms:
        coords += rs.points.split()
    coords += [p for line in scene.rafters for p in line.split()]
    for c in coords:
        x, y = map(float, c.split(","))
        assert 0 <= x <= w and 0 <= y <= h, c
    for es in scene.equipment:
        for x, y in ([es.xy] if es.xy else []) + es.vents:
            assert 0 <= x <= w and 0 <= y <= h, es.eq.key


def test_scene_contents():
    house = example()
    scene = build_scene(house)
    assert len(scene.rooms) == len(house.rooms)
    assert [e.eq.key for e in scene.equipment] == [e.key for e in house.equipment]
    assert len(scene.pv) == 18
    assert {label for label, _ in scene.floor_labels} == {"Parter", "Piętro", "Poddasze"}
    assert scene.floor_lines                       # salon przykładu ma podłogę 'wood'
    assert len(scene.wall_features) == 1           # okno
    assert len(scene.furniture) == 3               # 1 bryła × 3 ściany
    for rs in scene.rooms:
        corners = len(rs.room.plan_points())
        assert re.fullmatch(r"(-?[\d.]+,-?[\d.]+ ){%d}-?[\d.]+,-?[\d.]+" % (corners - 1),
                            rs.points), rs.room.key
