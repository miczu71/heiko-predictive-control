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


def test_callouts_outside_house_and_not_overlapping():
    house = example()
    scene = build_scene(house)
    assert {c.room.key for c in scene.callouts} == {r.key for r in house.rooms if r.entity}
    # sylwetka domu w poziomie = podłogi pokoi
    xs = [float(p.split(",")[0]) for rs in scene.rooms for p in rs.points.split()]
    for c in scene.callouts:
        left, right = c.pill[0] - c.w / 2, c.pill[0] + c.w / 2
        assert right < min(xs) or left > max(xs), c.room.key
        assert 0 <= left and right <= scene.width, c.room.key
        assert 0 <= c.pill[1] - c.h / 2 and c.pill[1] + c.h / 2 <= scene.height, c.room.key
    for side in ("left", "right"):
        ys = sorted(c.pill[1] for c in scene.callouts if c.side == side)
        assert all(b - a >= scene.callouts[0].h for a, b in zip(ys, ys[1:])), side


def test_callout_leader_starts_at_room_center():
    house = example()
    scene = build_scene(house)
    by_key = {rs.room.key: rs for rs in scene.rooms}
    for c in scene.callouts:
        assert c.leader.split()[0] == f"{c.anchor[0]},{c.anchor[1]}"
        assert c.anchor == by_key[c.room.key].label_xy
        end_x = float(c.leader.split()[-1].split(",")[0])
        assert end_x == c.pill[0] + (c.w / 2 if c.side == "left" else -c.w / 2)


def test_layout_callouts_pushes_overlapping_pills_down():
    from heiko_predictive_control.rooms import COL_GAP, PILL_GAP, PILL_H, PILL_W, layout_callouts
    rooms = example().rooms
    anchors = [(rooms[0], (-10.0, 100.0)), (rooms[1], (-20.0, 105.0)), (rooms[2], (50.0, 100.0))]
    out = {r.key: (side, pill) for r, side, pill, _ in layout_callouts(anchors, -100, 100, 0)}
    assert out[rooms[0].key] == ("left", (-100 - COL_GAP - PILL_W / 2, 100.0))
    assert out[rooms[1].key][1][1] == 100.0 + PILL_H + PILL_GAP
    assert out[rooms[2].key] == ("right", (100 + COL_GAP + PILL_W / 2, 100.0))


def _scene_with(house, **changes):
    return build_scene(house.__class__(**{**house.__dict__, **changes}))


def test_tall_boxes_cut_at_cut_h_and_high_boxes_skipped():
    house = example()
    tall = Box("parter", (10, 10, 50, 50), 200, style="graphite", top="oak")
    high = Box("parter", (100, 10, 50, 50), 40, z0=150, style="oak")
    stair = Box("parter", (200, 10, 50, 50), 30, z0=190, style="black", cut=False)
    scene = _scene_with(house, boxes=(tall, high, stair), stairs=())
    classes = [f.cls for f in scene.furniture]
    # wysoka: 3 ściany, góra jako przekrój (materiał korpusu, nie blatu)
    assert "bx graphite t cut" in classes and "bx oak t" not in classes
    # w całości ponad przekrojem: pominięta; cut=False: w pełnej wysokości
    assert classes.count("bx black t") == 1 and len(classes) == 6
    assert len(_scene_with(house, boxes=(tall,), stairs=()).furniture) == 3


def test_front_feature_sill():
    from heiko_predictive_control.rooms import FrontFeature
    house = example()
    door = FrontFeature("parter", "y_max", 100, 180, 210, "door")
    window = FrontFeature("parter", "y_max", 100, 180, 170, "glass", z0=100)
    scene = _scene_with(house, fronts=(door, window))
    ys = [[float(p.split(",")[1]) for p in f.points.split()] for f in scene.fronts]
    # okno z parapetem jest niższe (krótsze w pionie) niż drzwi od podłogi
    assert max(ys[1]) - min(ys[1]) < max(ys[0]) - min(ys[0])
    assert max(ys[1]) < max(ys[0])            # dolna krawędź okna ponad podłogą
