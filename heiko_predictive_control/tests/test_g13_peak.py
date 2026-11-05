"""Tryb „Szczyt G13”: czysta logika docelowego przesunięcia krzywej (bez I/O)."""
from datetime import date, datetime, timedelta

from heiko_predictive_control import g13_peak as g

DAY = date(2026, 11, 6)                       # piątek, dzień roboczy


def at(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m)


def step(now, state=None, *, base=0.0, workday=True, room=21.5, current=0.0, cfg=None):
    return g.decide(now, workday=workday, current_shift=current, base_shift=base, min_room_c=room,
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
        assert step(at(h, m), base=0.0).target == -4.0


def test_21_00_powrot_do_bazowego():
    d = step(at(21, 0), base=0.0, current=-4.0)
    assert d.target == 0.0 and d.reason == "koniec okna"


def test_przesuniecie_wzgledem_bazowego_i_obciecie_do_allowlisty():
    assert step(at(14), base=-1.0).target == 0.0
    assert step(at(17), base=-2.0).target == -4.0           # −6 obcięte do −4
    assert step(at(14), base=4.0).target == 4.0             # +5 obcięte do +4


def test_po_21_nic_do_cofania_gdy_juz_bazowe():
    d = step(at(22), base=0.0, current=0.0)
    assert d.target is None


def test_po_21_cofa_gdy_przesuniecie_zostalo_po_restarcie():
    d = step(at(22), base=0.0, current=-4.0)
    assert d.target == 0.0


# ── dzień nieroboczy i brak feedu ───────────────────────────────────────────

def test_weekend_i_swieto_nic_nie_robia():
    d = step(at(17, day=date(2026, 11, 7)), workday=False)
    assert d.target is None and d.reason == "dzień nieroboczy"


def test_brak_feedu_nic_nie_robi():
    d = step(at(17), workday=None)
    assert d.target is None and d.reason == "brak harmonogramu taryfy"


def test_brak_feedu_nie_cofa_przesuniecia_w_trakcie_okna():
    # feed zniknął o 16:30 po wejściu w szczyt — nie zgadujemy, watchdog HA pilnuje reszty
    assert step(at(16, 30), workday=None, current=-4.0).target is None


# ── bezpiecznik pokoi ───────────────────────────────────────────────────────

def test_bezpiecznik_nie_dziala_przed_15_minutami():
    d = step(at(17, 0), room=20.0)
    assert d.target == -4.0 and d.state.cold_since == at(17, 0) and not d.state.tripped
    d = step(at(17, 14), d.state, room=20.0)
    assert d.target == -4.0 and not d.state.tripped


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
    assert d.target == -4.0 and d.state.cold_since == at(17, 20)


def test_brak_odczytu_pokoi_nie_wyzwala_i_nie_zeruje_licznika():
    s = step(at(17, 0), room=20.0).state
    d = step(at(17, 10), s, room=None)
    assert d.state.cold_since == at(17, 0) and not d.state.tripped
    d = step(at(17, 16), d.state, room=None)
    assert not d.state.tripped                               # bez odczytu nie ma dowodu
    assert d.target == -4.0


def test_bezpiecznik_dziala_tez_w_dogrzewaniu_13_16():
    s = step(at(14, 0), room=20.0).state
    d = step(at(14, 15), s, room=20.0)
    assert d.target == 0.0 and d.state.tripped


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
    assert step(at(18, 30), base=-1.0, current=-5.0 + 1.0).target == -4.0   # baza zapamiętana, stan pusty


def test_restart_w_oknie_bez_bazy_bierze_biezace_przesuniecie():
    d = g.decide(at(14), workday=True, current_shift=1.0, base_shift=None, min_room_c=21.5,
                 state=g.State(), cfg=g.Config())
    assert d.base == 1.0 and d.target == 2.0


def test_baza_zapamietana_gdy_podana():
    d = step(at(14), base=0.0)
    assert d.base == 0.0


# ── dzień roboczy z feedu ───────────────────────────────────────────────────

class FakeFeed:
    def __init__(self, rows, source="feed"):
        self.rows, self.source = rows, source

    def status(self, now=None):
        return {"source": self.source}

    def at(self, dt):
        return self.rows.get(dt.strftime("%Y-%m-%dT%H"))


class Row:
    def __init__(self, is_peak):
        self.is_peak = is_peak


def test_workday_from_feed():
    assert g.workday_from_feed(FakeFeed({"2026-11-06T17": Row(True)}), DAY, at(10)) is True
    assert g.workday_from_feed(FakeFeed({"2026-11-07T17": Row(False)}), date(2026, 11, 7), at(10)) is False
    assert g.workday_from_feed(FakeFeed({}), DAY, at(10)) is None                       # brak godziny
    assert g.workday_from_feed(FakeFeed({"2026-11-06T17": Row(True)}, source="fallback"), DAY, at(10)) is None
