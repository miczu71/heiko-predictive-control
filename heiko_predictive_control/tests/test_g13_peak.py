"""Tryb „Szczyt G13”: czysta logika docelowego przesunięcia krzywej (bez I/O)."""
from datetime import date, datetime, timedelta

from heiko_predictive_control import g13_peak as g

DAY = date(2026, 11, 6)                       # piątek, dzień roboczy


def at(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m)


WINTER = g.Window(16, 21)
SUMMER = g.Window(19, 22)


def step(now, state=None, *, base=0.0, window=WINTER, room=21.5, current=0.0, cfg=None):
    return g.decide(now, window=window, current_shift=current, base_shift=base, min_room_c=room,
                    state=state or g.State(), cfg=cfg or g.Config())


# ── okna czasowe ────────────────────────────────────────────────────────────

def test_poza_oknem_nic_nie_robi():
    for h in (0, 6, 12):
        d = step(at(h), base=0.0)
        assert d.target is None and d.reason == "poza oknem"


def test_dogrzanie_13_16():
    for h, m in ((13, 0), (14, 30), (15, 59)):
        assert step(at(h, m), base=0.0).target == 1.0


def test_szczyt_16_21():
    for h, m in ((16, 0), (18, 0), (20, 59)):
        assert step(at(h, m), base=0.0).target == -3.0


def test_21_00_powrot_do_bazowego():
    d = step(at(21, 0), base=0.0, current=-3.0)
    assert d.target == 0.0 and d.reason == "koniec okna"


def test_przesuniecie_wzgledem_bazowego_i_obciecie_do_allowlisty():
    assert step(at(14), base=-1.0).target == 0.0
    assert step(at(17), base=-2.0).target == -3.0           # −5 obcięte do −3
    assert step(at(14), base=3.0).target == 3.0             # +4 obcięte do +3


def test_po_21_nic_do_cofania_gdy_juz_bazowe():
    d = step(at(22), base=0.0, current=0.0)
    assert d.target is None


def test_po_21_cofa_gdy_przesuniecie_zostalo_po_restarcie():
    d = step(at(22), base=0.0, current=-3.0)
    assert d.target == 0.0


# ── okno z taryfy: lato, brak szczytu, brak harmonogramu ────────────────────

def test_okno_zimowe_dogrzanie_od_13():
    assert WINTER.preheat_from == 13
    assert step(at(12, 59)).target is None
    assert step(at(13)).target == 1.0 and step(at(16)).target == -3.0


def test_okno_letnie_19_22():
    assert SUMMER.preheat_from == 16
    assert step(at(15), window=SUMMER).target is None
    for h in (16, 18):
        d = step(at(h), window=SUMMER)
        assert d.target == 1.0 and d.reason == "dogrzanie" and d.window == SUMMER
    for h in (19, 21):
        d = step(at(h), window=SUMMER)
        assert d.target == -3.0 and d.reason == "szczyt"
    d = step(at(22), window=SUMMER, current=-3.0)
    assert d.target == 0.0 and d.reason == "koniec okna"


def test_dogrzanie_nie_schodzi_ponizej_zera():
    assert g.Window(2, 5).preheat_from == 0
    assert step(at(0), window=g.Window(2, 5)).target == 1.0


def test_brak_szczytu_nic_nie_robi():
    d = step(at(17, day=date(2026, 11, 7)), window=g.NO_PEAK)
    assert d.target is None and d.reason == "brak szczytu evening_peak w taryfie"
    assert g.NO_PEAK.empty and not WINTER.empty


def test_okno_do_polnocy():
    w = g.Window(20, 24)
    assert step(at(17), window=w).target == 1.0
    assert step(at(23, 59), window=w).target == -3.0


def test_bezpiecznik_w_oknie_letnim():
    s = step(at(19, 0), window=SUMMER, room=20.0).state
    d = step(at(19, 15), s, window=SUMMER, room=20.0)
    assert d.target == 0.0 and d.state.tripped and d.event == "bezpiecznik"
    assert step(at(21, 0), d.state, window=SUMMER, room=22.0).target == 0.0


def test_brak_feedu_nic_nie_robi():
    d = step(at(17), window=None)
    assert d.target is None and d.reason == "brak harmonogramu taryfy"


def test_brak_feedu_nie_cofa_przesuniecia_w_trakcie_okna():
    # feed zniknął o 16:30 po wejściu w szczyt — nie zgadujemy, watchdog HA pilnuje reszty
    assert step(at(16, 30), window=None, current=-3.0).target is None


# ── bezpiecznik pokoi ───────────────────────────────────────────────────────

def test_bezpiecznik_nie_dziala_przed_15_minutami():
    d = step(at(17, 0), room=20.0)
    assert d.target == -3.0 and d.state.cold_since == at(17, 0) and not d.state.tripped
    d = step(at(17, 14), d.state, room=20.0)
    assert d.target == -3.0 and not d.state.tripped


def test_bezpiecznik_zadziala_po_15_minutach_i_trzyma_do_21():
    s = step(at(17, 0), room=20.0).state
    d = step(at(17, 15), s, room=20.0)
    assert d.target == 0.0 and d.state.tripped and d.reason == "bezpiecznik"
    assert d.event == "bezpiecznik"
    # ciepło po wyzwoleniu nie odblokowuje do 21:00 i zdarzenie nie powtarza się
    d2 = step(at(18, 0), d.state, room=22.0)
    assert d2.target == 0.0 and d2.event is None
    # następnego dnia stan się zeruje
    d3 = step(at(13, 0, day=DAY + timedelta(days=1)), d2.state, room=22.0)
    assert d3.target == 1.0 and not d3.state.tripped


def test_odbicie_temperatury_zeruje_licznik():
    s = step(at(17, 0), room=20.0).state
    s = step(at(17, 10), s, room=21.0).state
    assert s.cold_since is None
    d = step(at(17, 20), s, room=20.0)
    assert d.target == -3.0 and d.state.cold_since == at(17, 20)


def test_brak_odczytu_pokoi_nie_wyzwala_i_nie_zeruje_licznika():
    s = step(at(17, 0), room=20.0).state
    d = step(at(17, 10), s, room=None)
    assert d.state.cold_since == at(17, 0) and not d.state.tripped
    d = step(at(17, 16), d.state, room=None)
    assert not d.state.tripped                               # bez odczytu nie ma dowodu
    assert d.target == -3.0


def test_bezpiecznik_nie_dziala_w_dogrzewaniu_13_16():
    s = step(at(14, 0), room=20.0).state
    d = step(at(14, 15), s, room=20.0)
    assert d.target == 1.0 and d.reason == "dogrzanie" and not d.state.tripped and d.event is None
    assert d.state.cold_since == at(14, 0)                      # licznik idzie dalej, ale nie wyzwala


def test_zimny_pokoj_z_dogrzewania_wyzwala_bezpiecznik_od_razu_o_16():
    s = step(at(15, 40), room=20.0).state
    d = step(at(15, 55), s, room=20.0)
    assert d.target == 1.0 and not d.state.tripped
    d = step(at(16, 0), d.state, room=20.0)                     # zimno od ≥15 min: bez zapisu −3 i cofania
    assert d.target == 0.0 and d.state.tripped and d.event == "bezpiecznik"


def test_ocieplenie_w_dogrzewaniu_zeruje_licznik_przed_szczytem():
    s = step(at(15, 40), room=20.0).state
    s = step(at(15, 50), s, room=21.0).state
    d = step(at(16, 0), s, room=21.0)
    assert d.target == -3.0 and not d.state.tripped


def test_prog_bezpiecznika_jest_ostry_ponizej():
    assert step(at(17), room=20.5).state.cold_since is None   # równo z progiem = OK
    assert step(at(17), room=20.49).state.cold_since == at(17)


def test_wlasny_prog_i_przesuniecia_z_cfg():
    cfg = g.Config(min_room_c=19.0, preheat_shift=2.0, peak_shift=-3.0)
    assert step(at(14), cfg=cfg).target == 2.0
    d = step(at(17), cfg=cfg, room=19.5)
    assert d.target == -3.0 and d.state.cold_since is None


# ── restart w oknie ─────────────────────────────────────────────────────────

def test_restart_w_oknie_z_zapamietana_baza_daje_ten_sam_cel():
    assert step(at(18, 30), base=-1.0, current=-5.0 + 1.0).target == -3.0   # baza zapamiętana, stan pusty


def test_restart_w_oknie_bez_bazy_bierze_biezace_przesuniecie():
    d = g.decide(at(14), window=WINTER, current_shift=1.0, base_shift=None, min_room_c=21.5,
                 state=g.State(), cfg=g.Config())
    assert d.base == 1.0 and d.target == 2.0


def test_baza_zapamietana_gdy_podana():
    d = step(at(14), base=0.0)
    assert d.base == 0.0


# ── okno z feedu ────────────────────────────────────────────────────────────

class FakeFeed:
    def __init__(self, rows, source="feed"):
        self.rows, self.source = rows, source

    def status(self, now=None):
        return {"source": self.source}

    def at(self, dt):
        return self.rows.get(dt.strftime("%Y-%m-%dT%H"))


class Row:
    def __init__(self, zone):
        self.zone = zone


def day_rows(peak_hours, day=DAY, skip=()):
    return {f"{day.isoformat()}T{h:02d}": Row("evening_peak" if h in peak_hours else "other")
            for h in range(24) if h not in skip}


def test_window_from_feed_pelna_doba():
    assert g.window_from_feed(FakeFeed(day_rows(range(16, 21))), DAY) == g.Window(16, 21)
    assert g.window_from_feed(FakeFeed(day_rows(range(19, 22))), DAY) == g.Window(19, 22)
    assert g.window_from_feed(FakeFeed(day_rows(range(20, 24))), DAY) == g.Window(20, 24)


def test_window_from_feed_dziura_w_dobie_to_none():
    assert g.window_from_feed(FakeFeed(day_rows(range(16, 21), skip=(3,))), DAY) is None
    assert g.window_from_feed(FakeFeed({}), DAY) is None


def test_window_from_feed_brak_strefy_to_no_peak():
    assert g.window_from_feed(FakeFeed(day_rows(())), DAY) == g.NO_PEAK


def test_window_from_feed_pierwszy_ciagly_odcinek():
    assert g.window_from_feed(FakeFeed(day_rows((7, 8, 18, 19))), DAY) == g.Window(7, 9)


def test_window_from_feed_swiezosc_bez_wplywu():
    for source in ("stale", "none"):
        assert g.window_from_feed(FakeFeed(day_rows(range(16, 21)), source=source), DAY) == WINTER


def test_stale_godziny_i_dzien_roboczy_usuniete():
    assert [n for n in dir(g) if n.endswith("_H")] == ["PREHEAT_LEAD_H"]
    assert not [n for n in dir(g) if "workday" in n.lower()]


def test_domyslny_peak_shift_to_minus_3():
    assert g.Config().peak_shift == -3.0
