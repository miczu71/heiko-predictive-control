from datetime import datetime, timedelta

from heiko_predictive_control.attic import AtticInputs, AtticState, decide

MON = datetime(2026, 1, 12)   # poniedziałek
TUE = datetime(2026, 1, 13)

SETTINGS = {
    "attic_active_profile": "komfort",
    "attic_comfort_target_c": 22.0,
    "attic_economy_target_c": 20.5,
    "attic_work_start_hour": 8,
    "attic_work_end_hour": 16,
    "attic_preheat_lead_min": 45,
    "attic_preheat_max_min": 120,
}


def at(h, m=0, s=0, day=MON):
    return day.replace(hour=h, minute=m, second=s)


def inp(now, temp=17.0, ac="off", sp=24.0, workday=True, vac=False, window_since=None):
    return AtticInputs(now=now, is_workday=workday, on_vacation=vac, temp_c=temp,
                       ac_state=ac, ac_setpoint_c=sp, window_open_since=window_since)


def owned_state(mode="utrzymanie", hvac="heat", temp=23.5, ts=None, **kw):
    return AtticState(
        owned=True, owned_day=MON.date().isoformat(), mode=mode,
        last_cmd={"hvac": hvac, "temp": temp, "ts": (ts or at(9, 0)).isoformat()},
        **kw)


def cmds(d):
    return [(c["service"], c["data"]) for c in d.commands]


# ── start dogrzewania ────────────────────────────────────────────────────────

def test_planned_start_from_deficit_and_rate():
    # deficyt 6°C / 4°C/h = 90 min + 20 min marginesu (komfort) -> 06:10
    d = decide(inp(at(5, 0), temp=16.0), AtticState(), SETTINGS, enabled=True)
    assert d.planned_start == at(6, 10)
    assert d.commands == []
    assert d.phase == "czeka"


def test_planned_start_economy_smaller_margin():
    s = dict(SETTINGS, attic_active_profile="ekonomia")
    # deficyt 4,5 / 4 = 67,5 min + 10 = 77,5 min przed 8:00
    d = decide(inp(at(5, 0), temp=16.0), AtticState(), s, enabled=True)
    assert d.planned_start == at(6, 42, 30)


def test_planned_start_capped_by_max():
    d = decide(inp(at(4, 0), temp=5.0), AtticState(), SETTINGS, enabled=True)
    assert d.planned_start == at(6, 0)


def test_planned_start_fallback_without_temp():
    d = decide(inp(at(5, 0), temp=None), AtticState(), SETTINGS, enabled=True)
    assert d.planned_start == at(7, 15)


def test_takeover_starts_preheat():
    d = decide(inp(at(6, 10), temp=16.0), AtticState(), SETTINGS, enabled=True)
    assert cmds(d) == [("set_temperature", {"temperature": 25.5, "hvac_mode": "heat"})]
    assert d.phase == "dogrzewanie"
    assert d.state.owned and d.state.mode == "dogrzewanie"
    assert d.state.owned_day == "2026-01-12"
    assert d.state.preheat["temp"] == 16.0
    assert "takeover" in d.events
    assert d.dry_run is False


def test_takeover_barely_below_target_goes_straight_to_maintenance():
    d = decide(inp(at(9, 0), temp=21.5), AtticState(), SETTINGS, enabled=True)
    assert cmds(d) == [("set_temperature", {"temperature": 23.5, "hvac_mode": "heat"})]
    assert d.state.mode == "utrzymanie"


def test_warm_attic_is_left_alone():
    d = decide(inp(at(9, 0), temp=22.5), AtticState(), SETTINGS, enabled=True)
    assert d.commands == []
    assert d.state.owned is False
    assert d.phase == "utrzymanie"


def test_setpoint_clamped_to_ac_range():
    s = dict(SETTINGS, attic_comfort_target_c=27.0)
    d = decide(inp(at(9, 0), temp=20.0), AtticState(offset_c=4.0), s, enabled=True)
    assert cmds(d) == [("set_temperature", {"temperature": 30.0, "hvac_mode": "heat"})]


# ── dogrzewanie -> utrzymanie, uczenie ───────────────────────────────────────

def test_preheat_to_maintenance_learns_rate_and_lowers_setpoint():
    st = AtticState(owned=True, owned_day="2026-01-12", mode="dogrzewanie",
                    last_cmd={"hvac": "heat", "temp": 25.5, "ts": at(6, 10).isoformat()},
                    preheat={"ts": at(6, 10).isoformat(), "temp": 16.0})
    d = decide(inp(at(7, 40), temp=21.8, ac="heat", sp=25.5), st, SETTINGS, enabled=True)
    assert d.state.mode == "utrzymanie"
    assert d.state.preheat is None
    # 5,8°C w 1,5 h = 3,87 °C/h -> EWMA 0,7*4,0 + 0,3*3,87
    assert abs(d.state.heat_rate_c_h - 3.96) < 0.01
    assert cmds(d) == [("set_temperature", {"temperature": 23.5, "hvac_mode": "heat"})]


def test_rate_not_learned_from_tiny_rise():
    st = AtticState(owned=True, owned_day="2026-01-12", mode="dogrzewanie",
                    last_cmd={"hvac": "heat", "temp": 25.5, "ts": at(7, 30).isoformat()},
                    preheat={"ts": at(7, 30).isoformat(), "temp": 21.6})
    d = decide(inp(at(7, 45), temp=21.8, ac="heat", sp=25.5), st, SETTINGS, enabled=True)
    assert d.state.heat_rate_c_h == 4.0


def test_maintenance_falls_back_to_preheat_when_far_below():
    st = owned_state()
    d = decide(inp(at(10, 0), temp=20.8, ac="heat", sp=23.5), st, SETTINGS, enabled=True)
    assert d.state.mode == "dogrzewanie"
    assert cmds(d) == [("set_temperature", {"temperature": 25.5, "hvac_mode": "heat"})]


# ── utrzymanie: offset i histereza ───────────────────────────────────────────

def test_offset_rises_when_room_below_target():
    d = decide(inp(at(9, 30), temp=21.4, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert abs(d.state.offset_c - 1.68) < 0.001
    assert d.commands == []          # 23,68 -> 23,5, brak realnej różnicy


def test_offset_falls_when_room_above_target():
    d = decide(inp(at(9, 30), temp=22.6, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert abs(d.state.offset_c - 1.32) < 0.001


def test_offset_deadband():
    d = decide(inp(at(9, 30), temp=22.1, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert d.state.offset_c == 1.5


def test_offset_waits_for_ac_to_settle():
    st = owned_state(ts=at(9, 0))
    d = decide(inp(at(9, 5), temp=21.4, ac="heat", sp=23.5), st, SETTINGS, enabled=True)
    assert d.state.offset_c == 1.5


def test_offset_clamped():
    d = decide(inp(at(9, 30), temp=21.5, ac="heat", sp=23.5), owned_state(offset_c=3.95),
               SETTINGS, enabled=True)
    assert d.state.offset_c <= 4.0
    d = decide(inp(at(9, 30), temp=24.0, ac="heat", sp=23.5), owned_state(offset_c=-0.95),
               SETTINGS, enabled=True)
    assert d.state.offset_c >= -1.0


def test_protective_off_when_too_warm():
    d = decide(inp(at(11, 0), temp=22.8, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert cmds(d) == [("turn_off", {})]
    assert d.state.owned is True
    assert d.state.last_cmd["hvac"] == "off"


def test_resumes_heating_after_protective_off():
    st = owned_state(hvac="off", temp=None)
    d = decide(inp(at(12, 0), temp=21.6, ac="off", sp=23.5), st, SETTINGS, enabled=True)
    assert cmds(d) == [("set_temperature", {"temperature": 23.5, "hvac_mode": "heat"})]
    d = decide(inp(at(12, 0), temp=21.8, ac="off", sp=23.5), st, SETTINGS, enabled=True)
    assert d.commands == []


def test_regulation_writes_are_throttled():
    st = owned_state(ts=at(9, 29))
    d = decide(inp(at(9, 30), temp=21.4, ac="heat", sp=25.5), st, SETTINGS, enabled=True)
    assert d.commands == []


def test_missing_temp_keeps_last_setpoint():
    d = decide(inp(at(9, 30), temp=None, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert d.commands == []
    assert d.state.offset_c == 1.5


# ── ręczna zmiana ────────────────────────────────────────────────────────────

def test_manual_setpoint_change_releases_for_the_day():
    st = owned_state()
    d = decide(inp(at(9, 30), temp=21.0, ac="heat", sp=26.0), st, SETTINGS, enabled=True)
    assert d.commands == []
    assert d.state.owned is False
    assert d.state.override_date == "2026-01-12"
    assert d.phase == "przejecie_reczne"
    assert "manual_override" in d.events
    # dalsze cykle tego dnia: nadal zero zapisów, mimo zimna
    d2 = decide(inp(at(9, 45), temp=18.0, ac="off", sp=26.0), d.state, SETTINGS, enabled=True)
    assert d2.commands == []
    assert d2.phase == "przejecie_reczne"


def test_manual_switch_off_releases():
    d = decide(inp(at(9, 30), temp=21.0, ac="off"), owned_state(), SETTINGS, enabled=True)
    assert d.state.override_date == "2026-01-12"
    assert d.commands == []


def test_override_expires_next_day():
    st = AtticState(override_date="2026-01-12")
    d = decide(inp(at(9, 0, day=TUE), temp=16.0), st, SETTINGS, enabled=True)
    assert d.state.owned is True
    assert cmds(d)[0][0] == "set_temperature"


def test_grace_period_after_own_write():
    st = owned_state(ts=at(9, 29))
    d = decide(inp(at(9, 30), temp=21.0, ac="heat", sp=26.0), st, SETTINGS, enabled=True)
    assert d.state.override_date is None
    assert d.state.owned is True


def test_ac_switched_on_by_human_is_not_taken_over():
    d = decide(inp(at(7, 0), temp=16.0, ac="heat", sp=24.0), AtticState(), SETTINGS,
               enabled=True)
    assert d.commands == []
    assert d.state.owned is False
    assert d.phase == "reczne_uzycie"


def test_unavailable_ac_means_no_commands():
    d = decide(inp(at(7, 0), temp=16.0, ac=None), AtticState(), SETTINGS, enabled=True)
    assert d.commands == []
    assert d.phase == "brak_ac"


# ── koniec okna, dni nieaktywne ──────────────────────────────────────────────

def test_end_of_window_turns_off_owned_ac():
    d = decide(inp(at(16, 0), temp=22.0, ac="heat", sp=23.5), owned_state(),
               SETTINGS, enabled=True)
    assert cmds(d) == [("turn_off", {})]
    assert d.state.owned is False
    assert d.phase == "koniec_okna"


def test_end_of_window_leaves_unowned_ac_alone():
    d = decide(inp(at(16, 0), temp=22.0, ac="heat", sp=23.5), AtticState(),
               SETTINGS, enabled=True)
    assert d.commands == []


def test_stale_ownership_from_previous_day_is_released():
    st = owned_state()
    d = decide(inp(at(6, 0, day=TUE), temp=20.0, ac="heat", sp=23.5), st, SETTINGS,
               enabled=True)
    assert cmds(d) == [("turn_off", {})]
    assert d.state.owned is False


def test_weekend_never_writes():
    d = decide(inp(at(8, 30), temp=12.0, workday=False), AtticState(), SETTINGS, enabled=True)
    assert d.commands == []
    assert d.phase == "poza_oknem"
    assert d.planned_start is None


def test_vacation_never_writes():
    d = decide(inp(at(8, 30), temp=12.0, vac=True), AtticState(), SETTINGS, enabled=True)
    assert d.commands == []
    assert d.phase == "poza_oknem"


def test_unknown_workday_is_treated_as_inactive():
    d = decide(inp(at(8, 30), temp=12.0, workday=None), AtticState(), SETTINGS, enabled=True)
    assert d.commands == []


# ── okno otwarte ─────────────────────────────────────────────────────────────

def test_open_window_pauses_owned_ac():
    st = owned_state()
    d = decide(inp(at(10, 0), temp=21.0, ac="heat", sp=23.5,
                   window_since=at(9, 56)), st, SETTINGS, enabled=True)
    assert cmds(d) == [("turn_off", {})]
    assert d.state.paused_by_window is True
    assert d.state.owned is True
    assert d.phase == "pauza_okno"


def test_briefly_open_window_does_not_pause():
    d = decide(inp(at(10, 0), temp=21.4, ac="heat", sp=23.5,
                   window_since=at(9, 59)), owned_state(), SETTINGS, enabled=True)
    assert d.phase != "pauza_okno"
    assert d.commands == []


def test_paused_ac_not_seen_as_manual_override_and_resumes():
    st = owned_state()
    d = decide(inp(at(10, 0), temp=21.0, ac="heat", sp=23.5, window_since=at(9, 56)),
               st, SETTINGS, enabled=True)
    d2 = decide(inp(at(10, 10), temp=20.0, ac="off", sp=23.5, window_since=at(9, 56)),
                d.state, SETTINGS, enabled=True)
    assert d2.commands == [] and d2.state.owned is True and d2.phase == "pauza_okno"
    d3 = decide(inp(at(10, 20), temp=20.0, ac="off", sp=23.5), d2.state, SETTINGS,
                enabled=True)
    assert d3.state.paused_by_window is False
    assert d3.state.override_date is None
    assert cmds(d3)[0][0] == "set_temperature"


def test_open_window_blocks_takeover():
    d = decide(inp(at(7, 0), temp=16.0, window_since=at(6, 0)), AtticState(),
               SETTINGS, enabled=True)
    assert d.commands == []
    assert d.state.owned is False


# ── dry-run i przekazanie ────────────────────────────────────────────────────

def test_disabled_control_reports_would_be_write_as_dry_run():
    d = decide(inp(at(7, 0), temp=16.0), AtticState(), SETTINGS, enabled=False)
    assert cmds(d)[0][0] == "set_temperature"
    assert d.dry_run is True


def test_disabling_while_owned_hands_ac_back_off():
    d = decide(inp(at(9, 0), temp=22.0, ac="heat", sp=23.5), owned_state(), SETTINGS,
               enabled=False)
    assert cmds(d) == [("turn_off", {})]
    assert d.dry_run is False
    assert d.state.owned is False
    assert d.phase == "przekazanie"


def test_disabling_while_owned_and_ac_already_off_writes_nothing():
    d = decide(inp(at(9, 0), temp=22.0, ac="off"), owned_state(hvac="off", temp=None),
               SETTINGS, enabled=False)
    assert d.commands == []
    assert d.state.owned is False


# ── serializacja stanu ───────────────────────────────────────────────────────

def test_state_roundtrip():
    st = owned_state(offset_c=2.1, heat_rate_c_h=3.5, paused_by_window=True,
                     override_date="2026-01-11")
    assert AtticState.from_dict(st.as_dict()) == st


def test_state_from_garbage_gives_defaults():
    assert AtticState.from_dict(None) == AtticState()
    assert AtticState.from_dict({"offset_c": "x", "unknown": 1}).offset_c == 1.5


# ── scenariusz: poranek z 13.01.2026 (15,9°C o 07:00, grzanie ~4°C/h) ──────────

def test_morning_simulation_reaches_target_and_holds_it_until_16():
    temp, ac_state, ac_sp = 15.9, "off", 24.0
    st = AtticState()
    now = at(5, 0)
    reached_at_8 = None
    temps = []
    while now <= at(16, 10):
        d = decide(inp(now, temp=round(temp, 2), ac=ac_state, sp=ac_sp), st, SETTINGS,
                   enabled=True)
        st = d.state
        for c in d.commands:
            if c["service"] == "turn_off":
                ac_state = "off"
            else:
                ac_state, ac_sp = "heat", c["data"]["temperature"]
        # obiekt: AC widzi pomieszczenie o 1,3°C cieplejsze i grzeje 4°C/h do nastawy
        heating = ac_state == "heat" and temp + 1.3 < ac_sp
        temp += (4.0 if heating else 0.0) / 12 - 0.3 / 12
        if now == at(8, 0):
            reached_at_8 = temp
        if at(8, 0) <= now < at(16, 0):
            temps.append(temp)
        now += timedelta(minutes=5)
    assert reached_at_8 >= 21.5
    assert min(temps) >= 21.4 and max(temps) <= 23.0
    assert ac_state == "off" and st.owned is False
