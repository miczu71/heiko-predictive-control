"""Silnik izometrycznego modelu domu (pulpit 0.3.0).

Sam silnik — bez danych konkretnego domu. Układ (kondygnacje, pokoje, urządzenia,
meble) przychodzi z pliku JSON wczytywanego przez `layout.py` — prawdziwy dom
użytkownika trzymany jest poza tym (publicznym) repo, w prywatnej konfiguracji
Home Assistant; repo zawiera tylko neutralny przykład `example_house.json`.

Układ współrzędnych domu (cm):
    x — od lewej tylnej ściany (0) do prawej ściany przedniej (foot_w)
    y — od tylnej elewacji (0) do przedniej (foot_d)
    z — wysokość; kondygnacje są „rozsunięte" (exploded view), żeby się nie
        zasłaniały — to nie są realne wysokości

Rzut izometryczny 2:1, patrzymy od narożnika (foot_w, foot_d): ściany x=0 i y=0
są tylne (rysowane w całości, jak w domku dla lalek), przednie tylko jako
krawędź stropu. Kondygnacja `attic` ma dach dwuspadowy: tylna połać pełna
(opcjonalnie z panelami PV), przednia zdjęta — widać krokwie i wnętrze.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key

K = 0.36    # jednostki viewBox na cm w poziomie
KY = 0.5    # proporcja 2:1 klasycznej izometrii „pikselowej"
KZ = 0.36   # jednostki viewBox na cm w pionie

SLAB_T = 30     # grubość stropu (cm) — widoczna krawędź płyty
WALL_H = 250    # wysokość tylnych ścian przekroju

# Poddasze: ścianka kolankowa + dach dwuspadowy, kalenica nad środkiem głębokości.
KNEE_H = 90
# Wysokość kalenicy — celowo przesadzona: przy realnym ~40° tylna połać w tym
# rzucie jest prawie „na krawędź" i panele PV znikają w cienkim pasku.
RIDGE_H = 720

PLANK_W = 20              # szerokość deski na podłodze „wood" (cm)
BRICK_H = 14              # wysokość rzędu cegieł na ścianie „brick" (cm)
_VENT_AT = (0.78, 0.25)   # położenie nawiewu w prostokącie pokoju (ułamek szer./głęb.)


def iso(x: float, y: float, z: float = 0.0) -> tuple[float, float]:
    """Rzut punktu (cm) na współrzędne viewBox, przed przesunięciem sceny."""
    return (round((x - y) * K, 1), round((x + y) * K * KY - z * KZ, 1))


def rect_corners(x: float, y: float, w: float, d: float, z: float) -> list[tuple[float, float]]:
    """Cztery narożniki prostokąta podłogi w rzucie — kolejność obiegu stała
    (tył, prawo, przód, lewo), żeby wielokąt w SVG się nie „przekręcał"."""
    return [iso(x, y, z), iso(x + w, y, z), iso(x + w, y + d, z), iso(x, y + d, z)]


def points_attr(points: list[tuple[float, float]], dx: float = 0, dy: float = 0) -> str:
    return " ".join(f"{round(px + dx, 1)},{round(py + dy, 1)}" for px, py in points)


def temp_class(temp: float | None) -> str:
    """Klasa koloru temperatury — subtelna skala zimno→ciepło, żeby
    przegrzania były widać od razu na rzucie."""
    if temp is None:
        return "t-none"
    if temp < 19.5:
        return "t-cold"
    if temp < 21.0:
        return "t-cool"
    if temp < 23.5:
        return "t-ok"
    return "t-warm"


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class Level:
    key: str
    label: str
    z: float
    attic: bool = False


@dataclass(frozen=True)
class Room:
    key: str
    floor: str
    label: str
    rect: Rect                                 # x, y, szerokość, głębokość (cm)
    entity: str | None = None                  # None = pomieszczenie bez czujnika (tło)
    card: str | None = None                    # nadpisanie karty (domyślnie room-<key>)
    kind: str = "room"                         # 'room' | 'stairs' (styl tła)
    # Pokój niebędący prostokątem (np. w kształcie L): dodatkowe prostokąty
    # (do walidacji pokrycia rzutu) + jawny obrys w rzucie (x, y) do rysowania.
    # `rect` zostaje głównym prostokątem — od niego liczony jest środek etykiety.
    extra: tuple[Rect, ...] = ()
    outline: tuple[tuple[float, float], ...] | None = None
    floor_style: str | None = None             # np. 'wood' — realistyczna podłoga
    note: str | None = None                    # dopisek na karcie pokoju

    @property
    def card_key(self) -> str:
        return self.card or f"room-{self.key}"

    @property
    def rects(self) -> tuple[Rect, ...]:
        return (self.rect, *self.extra)

    def plan_points(self) -> list[tuple[float, float]]:
        if self.outline:
            return list(self.outline)
        x, y, w, d = self.rect
        return [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]


@dataclass(frozen=True)
class Equipment:
    key: str
    kind: str          # 'ac' | 'pump'
    label: str
    entity: str
    card: str          # która karta szczegółów się otwiera
    pos: tuple[float, float, float] | None   # główna ikona; None = tylko nawiewy
    controlled: bool   # czy add-on nim steruje (reszta: tylko podgląd)
    vents: tuple[str, ...] = ()   # klucze pokoi z nawiewem (klimatyzacja kanałowa)
    # Przerywana linia od ikony do punktu (x, y, z) zakończona kropką — pokazuje,
    # nad którym miejscem wisi urządzenie albo gdzie jest jego druga jednostka.
    leader_to: tuple[float, float, float] | None = None
    note: str | None = None


@dataclass(frozen=True)
class Box:
    """Mebel lub inna bryła stojąca na podłodze kondygnacji."""
    floor: str
    rect: Rect
    h: float
    z0: float = 0
    style: str = "neutral"
    top: str | None = None     # inny materiał blatu (np. dąb na grafitowej szafce)


@dataclass(frozen=True)
class WallFeature:
    """Element na tylnej ścianie: okno, cegła, TV, pas farby."""
    floor: str
    wall: str                  # 'x0' (ściana x=0) | 'y0' (ściana y=0)
    start: float
    end: float
    style: str
    z0: float = 0
    z1: float = WALL_H


@dataclass(frozen=True)
class FrontFeature:
    """Otwór w niewidocznej ścianie przedniej (np. drzwi tarasowe) — obrys-duch."""
    floor: str
    wall: str                  # 'x_max' | 'y_max'
    start: float
    end: float
    h: float = 220
    style: str = "glass"


@dataclass(frozen=True)
class Stairs:
    """Prosty bieg schodów wzdłuż osi y (od y_start w dół do y_end)."""
    floor: str
    x: float
    w: float
    y_start: float
    y_end: float
    steps: int
    rise: float
    style: str = "stair"
    top: str | None = None


@dataclass(frozen=True)
class House:
    foot_w: float
    foot_d: float
    levels: tuple[Level, ...]
    rooms: tuple[Room, ...]
    equipment: tuple[Equipment, ...] = ()
    pump_box: tuple[float, float, float, float, float] | None = None
    stair_opening: Rect | None = None
    pv: tuple[int, int] | None = None          # (kolumny, rzędy) paneli na tylnej połaci
    boxes: tuple[Box, ...] = ()
    walls: tuple[WallFeature, ...] = ()
    fronts: tuple[FrontFeature, ...] = ()
    stairs: tuple[Stairs, ...] = ()
    source: str = "example"                    # 'example' albo ścieżka pliku

    @property
    def floors(self) -> dict[str, float]:
        return {lv.key: lv.z for lv in self.levels}

    @property
    def floor_labels(self) -> dict[str, str]:
        return {lv.key: lv.label for lv in self.levels}

    def room(self, key: str) -> Room:
        return next(r for r in self.rooms if r.key == key)

    def by_card(self, card: str) -> tuple[Room | None, Equipment | None]:
        """Pokój i urządzenie przypisane do karty (np. 'loop_attic')."""
        room = next((r for r in self.rooms if r.card == card), None)
        eq = next((e for e in self.equipment if e.card == card and e.kind == "ac"), None)
        return room, eq


@dataclass
class Face:
    points: str
    cls: str


@dataclass
class RoomShape:
    room: Room
    points: str
    label_xy: tuple[float, float]


@dataclass
class EquipmentShape:
    eq: Equipment
    xy: tuple[float, float] | None
    vents: list[tuple[float, float]] = field(default_factory=list)
    leader: str | None = None                     # atrybut `points` linii prowadzącej
    leader_end: tuple[float, float] | None = None  # kropka na końcu linii


@dataclass
class Scene:
    viewbox: str
    width: float
    height: float
    faces_back: list[Face] = field(default_factory=list)
    wall_features: list[Face] = field(default_factory=list)
    wall_lines: list[Face] = field(default_factory=list)  # fugi cegieł itp. (polilinie)
    roof: list[Face] = field(default_factory=list)
    pv: list[Face] = field(default_factory=list)
    rooms: list[RoomShape] = field(default_factory=list)
    floor_lines: list[Face] = field(default_factory=list)  # deski podłogi (polilinie)
    furniture: list[Face] = field(default_factory=list)
    overlays: list[Face] = field(default_factory=list)
    faces_front: list[Face] = field(default_factory=list)
    fronts: list[Face] = field(default_factory=list)
    rafters: list[str] = field(default_factory=list)     # atrybut `points` polilinii
    pump: list[Face] = field(default_factory=list)
    equipment: list[EquipmentShape] = field(default_factory=list)
    floor_labels: list[tuple[str, tuple[float, float]]] = field(default_factory=list)


# ── Bryły ──────────────────────────────────────────────────────────────────

def box_faces(x: float, y: float, w: float, d: float, z0: float, z1: float
              ) -> dict[str, list[tuple[float, float]]]:
    """Trzy widoczne ściany prostopadłościanu: góra, bok od +x, przód od +y."""
    return {
        "t": [iso(x, y, z1), iso(x + w, y, z1), iso(x + w, y + d, z1), iso(x, y + d, z1)],
        "s": [iso(x + w, y, z0), iso(x + w, y + d, z0), iso(x + w, y + d, z1),
              iso(x + w, y, z1)],
        "f": [iso(x, y + d, z0), iso(x + w, y + d, z0), iso(x + w, y + d, z1),
              iso(x, y + d, z1)],
    }


def _painter_cmp(a: Box, b: Box) -> int:
    """Kolejność malowania brył na jednej kondygnacji: dalsza (mniejsze x/y)
    najpierw. Dla nienachodzących rzutów wystarcza porównanie rozdzielenia."""
    ax, ay, aw, ad = a.rect
    bx, by, bw, bd = b.rect
    if ax + aw <= bx:
        return -1
    if bx + bw <= ax:
        return 1
    if ay + ad <= by:
        return -1
    if by + bd <= ay:
        return 1
    return -1 if a.z0 < b.z0 else (1 if a.z0 > b.z0 else 0)


def stair_boxes(s: Stairs) -> list[Box]:
    """Stopnie jako pełne bryły od podłogi — najwyższy najdalej (mniejsze y)."""
    depth = (s.y_start - s.y_end) / s.steps
    out = []
    for i in range(s.steps):
        y1 = s.y_start - i * depth
        out.append(Box(s.floor, (s.x, y1 - depth, s.w, depth), s.rise * (i + 1),
                       style=s.style, top=s.top))
    return out


# ── Scena ──────────────────────────────────────────────────────────────────

def build_scene(house: House) -> Scene:
    """Cała scena w surowych współrzędnych, potem przesunięta tak, żeby viewBox
    zaczynał się od (0,0) z marginesem — w szablonie zero matematyki."""
    W, D = house.foot_w, house.foot_d
    ridge_y = D / 2

    back: list[tuple[list, str]] = []
    front: list[tuple[list, str]] = []
    roof: list[tuple[list, str]] = []
    pv: list[list] = []
    overlays: list[tuple[list, str]] = []
    rafters: list[list] = []
    labels: list[tuple[str, tuple[float, float]]] = []

    def front_edges(z: float) -> list[tuple[list, str]]:
        return [
            ([iso(W, 0, z), iso(W, D, z), iso(W, D, z - SLAB_T), iso(W, 0, z - SLAB_T)],
             "slab-light"),
            ([iso(0, D, z), iso(W, D, z), iso(W, D, z - SLAB_T), iso(0, D, z - SLAB_T)],
             "slab-dark"),
        ]

    def slope(u: float, t: float, z: float) -> tuple[float, float]:
        """Punkt na tylnej połaci: u wzdłuż kalenicy, t od okapu (0) do kalenicy (1)."""
        return iso(u, t * ridge_y, z + KNEE_H + (RIDGE_H - KNEE_H) * t)

    for lv in house.levels:
        z = lv.z
        if not lv.attic:
            back.append(([iso(0, 0, z), iso(0, D, z), iso(0, D, z + WALL_H),
                          iso(0, 0, z + WALL_H)], "wall-dark"))
            back.append(([iso(0, 0, z), iso(W, 0, z), iso(W, 0, z + WALL_H),
                          iso(0, 0, z + WALL_H)], "wall-light"))
            front += front_edges(z)
            labels.append((lv.label, iso(0, D, z + WALL_H + 40)))
            continue
        # Poddasze: szczyt tylny (pięciokąt — widać kształt dachu), ścianka
        # kolankowa, tylna połać (z PV), przednia połać zdjęta (krokwie).
        back.append(([iso(0, 0, z), iso(0, D, z), iso(0, D, z + KNEE_H),
                      iso(0, ridge_y, z + RIDGE_H), iso(0, 0, z + KNEE_H)], "wall-dark"))
        back.append(([iso(0, 0, z), iso(W, 0, z), iso(W, 0, z + KNEE_H),
                      iso(0, 0, z + KNEE_H)], "wall-light"))
        roof.append(([slope(0, 0, z), slope(W, 0, z), slope(W, 1, z), slope(0, 1, z)],
                     "roof-back"))
        if house.pv:
            cols, rows = house.pv
            u0, u1 = 40, W - 40
            cw = (u1 - u0) / cols
            rh = 0.84 / rows
            for r in range(rows):
                t0 = 0.08 + r * rh
                t1 = t0 + rh * 0.9
                for c in range(cols):
                    a, b = u0 + c * cw + 5, u0 + (c + 1) * cw - 5
                    pv.append([slope(a, t0, z), slope(b, t0, z), slope(b, t1, z),
                               slope(a, t1, z)])
        front += front_edges(z)
        step = W / 4
        for i in range(5):
            u = round(i * step, 1)
            rafters.append([iso(u, D, z + KNEE_H), iso(u, ridge_y, z + RIDGE_H)])
        rafters.append([iso(0, ridge_y, z + RIDGE_H), iso(W, ridge_y, z + RIDGE_H)])
        rafters.append([iso(W, 0, z), iso(W, 0, z + KNEE_H), iso(W, ridge_y, z + RIDGE_H),
                        iso(W, D, z + KNEE_H), iso(W, D, z)])
        labels.append((lv.label, iso(0, D, z + KNEE_H + 60)))
        if house.stair_opening:
            sx, sy, sw, sd = house.stair_opening
            overlays.append((rect_corners(sx, sy, sw, sd, z), "stairs"))

    floors = house.floors

    # Elementy na tylnych ścianach (okna, cegła, TV, farba).
    wall_features: list[tuple[list, str]] = []
    wall_lines: list[tuple[list, str]] = []
    for wf in house.walls:
        z = floors[wf.floor]
        za, zb = z + wf.z0, z + wf.z1

        def at(u: float, zz: float, wall=wf.wall) -> tuple[float, float]:
            return iso(u, 0, zz) if wall == "y0" else iso(0, u, zz)

        wall_features.append(([at(wf.start, za), at(wf.end, za), at(wf.end, zb),
                               at(wf.start, zb)], f"wf {wf.style}"))
        if wf.style == "brick":
            zz = za + BRICK_H
            while zz < zb - 1:
                wall_lines.append(([at(wf.start, zz), at(wf.end, zz)], "mortar"))
                zz += BRICK_H
        if wf.style == "window":
            mid = (wf.start + wf.end) / 2
            wall_lines.append(([at(mid, za), at(mid, zb)], "mullion"))

    rooms: list[tuple[Room, list, tuple[float, float]]] = []
    floor_lines: list[tuple[list, str]] = []
    for room in house.rooms:
        z = floors[room.floor]
        x, y, w, d = room.rect
        poly = [iso(px, py, z) for px, py in room.plan_points()]
        rooms.append((room, poly, iso(x + w / 2, y + d / 2, z)))
        if room.floor_style == "wood":
            for rx, ry, rw, rd in room.rects:
                u = rx + PLANK_W
                while u < rx + rw - 1:
                    floor_lines.append(([iso(u, ry, z), iso(u, ry + rd, z)], "plank"))
                    u += PLANK_W

    # Meble i schody — per kondygnacja, w kolejności malowania.
    furniture: list[tuple[list, str]] = []
    all_boxes = list(house.boxes)
    for s in house.stairs:
        all_boxes += stair_boxes(s)
    for lv in house.levels:
        mine = sorted((b for b in all_boxes if b.floor == lv.key), key=cmp_to_key(_painter_cmp))
        for b in mine:
            x, y, w, d = b.rect
            faces = box_faces(x, y, w, d, lv.z + b.z0, lv.z + b.z0 + b.h)
            for part in ("s", "f", "t"):
                material = (b.top or b.style) if part == "t" else b.style
                furniture.append((faces[part], f"bx {material} {part}"))

    # Otwory w ścianach przednich — obrys-duch, żeby nie zasłaniać wnętrza.
    fronts: list[tuple[list, str]] = []
    for ff in house.fronts:
        z = floors[ff.floor]
        if ff.wall == "x_max":
            pts = [iso(W, ff.start, z), iso(W, ff.end, z), iso(W, ff.end, z + ff.h),
                   iso(W, ff.start, z + ff.h)]
        else:
            pts = [iso(ff.start, D, z), iso(ff.end, D, z), iso(ff.end, D, z + ff.h),
                   iso(ff.start, D, z + ff.h)]
        fronts.append((pts, f"ghost {ff.style}"))

    by_key = {r.key: r for r in house.rooms}

    def vent_xy(key: str) -> tuple[float, float]:
        r = by_key[key]
        x, y, w, d = r.rect
        return iso(x + w * _VENT_AT[0], y + d * _VENT_AT[1], floors[r.floor])

    pump: list[tuple[list, str]] = []
    if house.pump_box:
        px, py, pw, pd, ph = house.pump_box
        pump = [
            ([iso(px, py, ph), iso(px + pw, py, ph), iso(px + pw, py + pd, ph),
              iso(px, py + pd, ph)], "pump-top"),
            ([iso(px + pw, py, 0), iso(px + pw, py + pd, 0), iso(px + pw, py + pd, ph),
              iso(px + pw, py, ph)], "pump-side"),
            ([iso(px, py + pd, 0), iso(px + pw, py + pd, 0), iso(px + pw, py + pd, ph),
              iso(px, py + pd, ph)], "pump-front"),
        ]

    equipment = []
    for eq in house.equipment:
        leader = [iso(*eq.pos), iso(*eq.leader_to)] if eq.pos and eq.leader_to else None
        equipment.append((eq, iso(*eq.pos) if eq.pos else None,
                          [vent_xy(k) for k in eq.vents], leader))

    # Granice sceny → przesunięcie + viewBox.
    every: list[tuple[float, float]] = []
    for poly, _ in back + front + roof + pump + furniture + fronts:
        every += poly
    for _, poly, _ in rooms:
        every += poly
    for line in rafters:
        every += line
    for _, xy, vents, leader in equipment:
        every += ([xy] if xy else []) + vents + (leader or [])
    margin = 40
    min_x = min(p[0] for p in every) - margin
    min_y = min(p[1] for p in every) - margin
    max_x = max(p[0] for p in every) + margin
    max_y = max(p[1] for p in every) + margin
    dx, dy = -min_x, -min_y
    width, height = round(max_x - min_x, 1), round(max_y - min_y, 1)

    def shift(xy: tuple[float, float]) -> tuple[float, float]:
        return (round(xy[0] + dx, 1), round(xy[1] + dy, 1))

    def faces_of(items: list[tuple[list, str]]) -> list[Face]:
        return [Face(points_attr(p, dx, dy), c) for p, c in items]

    return Scene(
        viewbox=f"0 0 {width} {height}", width=width, height=height,
        faces_back=faces_of(back), wall_features=faces_of(wall_features),
        wall_lines=faces_of(wall_lines), roof=faces_of(roof),
        pv=[Face(points_attr(p, dx, dy), "pv") for p in pv],
        rooms=[RoomShape(r, points_attr(p, dx, dy), shift(c)) for r, p, c in rooms],
        floor_lines=faces_of(floor_lines), furniture=faces_of(furniture),
        overlays=faces_of(overlays), faces_front=faces_of(front), fronts=faces_of(fronts),
        rafters=[points_attr(line, dx, dy) for line in rafters],
        pump=faces_of(pump),
        equipment=[EquipmentShape(eq, shift(xy) if xy else None, [shift(v) for v in vents],
                                  points_attr(leader, dx, dy) if leader else None,
                                  shift(leader[-1]) if leader else None)
                   for eq, xy, vents, leader in equipment],
        floor_labels=[(label, shift(xy)) for label, xy in labels],
    )
