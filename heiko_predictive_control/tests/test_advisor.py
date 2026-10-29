"""Silnik doradcy: propozycje w bazie, deduplikacja, TTL, eksperymenty, decyzje próbne, powiadomienia, odtworzenie zimy."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from heiko_predictive_control import advisor, comfort
from heiko_predictive_control import db as dbm
from heiko_predictive_control.analyzers import Draft
from heiko_predictive_control.kpi import outdoor_class

NOW = datetime(2026, 1, 14, 7, 0)
ZONES = "sensor.z1,sensor.z2,sensor.z3"
SETTINGS = {"day_zone_temp_entities": ZONES, "heiko_room_target_c": 20.6, "heiko_room_min_c": 18.5,
            "advisor_managed_keys": "dhw_setpoint,backup_heater"}


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def draft(to=-1.0, key="curve:curve_shift", analyzer="curve", kind="zmiana", lens="ekonomia", param="curve_shift",
          frm=0.0, ttl=6, reason="powód"):
    return Draft(analyzer=analyzer, dedupe_key=key, lens=lens, kind=kind, reason=reason, confidence="niska", ttl_h=ttl,
                 param_key=param, from_value=frm, to_value=to, evidence={"a": 1}, effects={"cost_day_delta_pln": -0.4})


def rows(conn, status=None):
    q = "SELECT * FROM proposals" + (" WHERE status = ?" if status else "") + " ORDER BY id"
    return conn.execute(q, (status,) if status else ()).fetchall()


# ── minimum komfortu z wybranych pokoi ───────────────────────────────────────

def test_min_room_entities_default_all_zones_and_subset_from_options():
    assert comfort.min_room_entities(SETTINGS) == ["sensor.z1", "sensor.z2", "sensor.z3"]
    assert comfort.min_room_entities({**SETTINGS, "comfort_min_entities": "sensor.z1, sensor.z3"}) == ["sensor.z1", "sensor.z3"]


def test_min_room_entities_ignores_unknown_entities_so_the_fuse_never_loses_all_rooms():
    assert comfort.min_room_entities({**SETTINGS, "comfort_min_entities": "sensor.typo"}) == ["sensor.z1", "sensor.z2", "sensor.z3"]


# ── synchronizacja propozycji ────────────────────────────────────────────────

def test_sync_inserts_then_refreshes_instead_of_duplicating(conn):
    assert len(advisor.sync(conn, [draft()], NOW)) == 1
    later = NOW + timedelta(hours=1)
    assert advisor.sync(conn, [draft(reason="nowy powód")], later) == []
    (row,) = rows(conn)
    assert row["reason"] == "nowy powód" and row["created"] == NOW.isoformat(timespec="seconds")
    assert row["expires"] == (later + timedelta(hours=6)).isoformat(timespec="seconds")     # rolling TTL


def test_sync_supersedes_when_value_changes_and_expires_when_conditions_vanish(conn):
    advisor.sync(conn, [draft(to=-1.0)], NOW)
    advisor.sync(conn, [draft(to=1.0, kind="zmiana", lens="komfort")], NOW + timedelta(hours=1))
    assert [r["status"] for r in rows(conn)] == ["zastąpiona", "oczekuje"]
    advisor.sync(conn, [], NOW + timedelta(hours=2))
    assert [r["status"] for r in rows(conn)] == ["zastąpiona", "wygasła"]


def test_expire_old_marks_pending_past_ttl(conn):
    advisor.sync(conn, [draft(ttl=6)], NOW)
    assert advisor.expire_old(conn, NOW + timedelta(hours=5)) == 0
    assert advisor.expire_old(conn, NOW + timedelta(hours=7)) == 1
    assert rows(conn)[0]["status"] == "wygasła"


def test_managed_parameter_becomes_text_only_alert():
    d = advisor.apply_managed(draft(param="dhw_setpoint", to=50.0, kind="zmiana", frm=48.0), frozenset({"dhw_setpoint"}))
    assert d.kind == "alert" and d.to_value is None and "automatyzacja" in d.reason
    keep = advisor.apply_managed(draft(), frozenset({"dhw_setpoint"}))
    assert keep.to_value == -1.0 and keep.kind == "zmiana"


# ── decyzje (tryb próbny) ────────────────────────────────────────────────────

def test_decide_only_changes_status_and_marks_trial(conn):
    (pid,) = advisor.sync(conn, [draft()], NOW)
    out = advisor.decide(conn, pid, "zatwierdzona", NOW + timedelta(minutes=5))
    assert out == {"ok": True, "status": "zatwierdzona", "trial": True}
    row = rows(conn)[0]
    assert row["status"] == "zatwierdzona" and row["trial"] == 1 and row["decided_at"]
    assert advisor.decide(conn, pid, "odrzucona", NOW)["ok"] is False                        # decyzja jest jednorazowa


def test_decide_rejects_expired_unknown_and_bad_decision(conn):
    (pid,) = advisor.sync(conn, [draft(ttl=1)], NOW)
    assert "wygasła" in advisor.decide(conn, pid, "zatwierdzona", NOW + timedelta(hours=2))["error"]
    assert advisor.decide(conn, 999, "zatwierdzona", NOW)["ok"] is False
    assert advisor.decide(conn, pid, "wykonaj", NOW)["ok"] is False


def test_revalidation_failure_supersedes_and_does_not_approve(conn):
    (pid,) = advisor.sync(conn, [draft()], NOW)
    out = advisor.decide(conn, pid, "zatwierdzona", NOW, revalidate=lambda row: (False, "warunki się zmieniły"))
    assert out == {"ok": False, "error": "warunki się zmieniły"}
    assert rows(conn)[0]["status"] == "zastąpiona"


def test_rejecting_skips_revalidation(conn):
    (pid,) = advisor.sync(conn, [draft()], NOW)
    assert advisor.decide(conn, pid, "odrzucona", NOW, revalidate=lambda row: (_ for _ in ()).throw(AssertionError))["ok"]


def test_revalidator_checks_fresh_analysis_and_current_parameter_value(conn):
    telemetry_rows(conn, NOW, room_temps=(19.4, 19.4, 19.4))                                # za zimno → zmiana +1 (bez modelu)
    states = _states(shift="0")
    fresh = advisor.build_snapshot(conn, SETTINGS, NOW, states)
    (d,) = [x for x in advisor.run_analyzers(fresh) if x.analyzer == "curve"]
    (pid,) = advisor.sync(conn, [d], NOW)
    row = dict(rows(conn)[0])
    assert advisor.make_revalidator(conn, SETTINGS, NOW, states)(row) == (True, "")
    ok, why = advisor.make_revalidator(conn, SETTINGS, NOW, _states(shift="-1"))(row)      # user zmienił parametr w międzyczasie
    assert not ok
    ok, why = advisor.make_revalidator(conn, SETTINGS, NOW, _states(curve="off"))(row)
    assert not ok and "nieaktualna" in why


# ── migawka z telemetrii ─────────────────────────────────────────────────────

def telemetry_rows(conn, now, room_temps=(21.0, 21.0, 21.0), hours=24, mode=None):
    """Telemetria co 15 min z ostatnich `hours` godzin: pokoje (i opcjonalnie tryb pracy)."""
    ids = {}
    names = ZONES.split(",") + (["working_mode"] if mode is not None else [])
    for name in names:
        conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
        ids[name] = conn.execute("SELECT id FROM telemetry_keys WHERE name = ?", (name,)).fetchone()["id"]
    for step in range(hours * 4):
        ts = int((now - timedelta(minutes=15 * step)).timestamp()) // 60 * 60
        for name, temp in zip(ZONES.split(","), room_temps):
            conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?)", (ts, ids[name], temp))
        if mode is not None:
            conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?)", (ts, ids["working_mode"], mode))
    conn.commit()


def _states(shift="0", curve="on", hyst="5", alert="off"):
    return [
        {"entity_id": "switch.x_heiko_heat_pump_heating_curve", "state": curve},
        {"entity_id": "number.x_heiko_heat_pump_heating_curve_parallel_shift", "state": shift},
        {"entity_id": "number.x_heiko_heat_pump_dhw_restart_dt", "state": hyst},
        {"entity_id": "binary_sensor.eev", "state": alert, "attributes": {"friendly_name": "EEV zablokowany"}},
        {"entity_id": "sensor.z1", "state": "21", "attributes": {"friendly_name": "Salon"}},
    ]


def test_room_window_stats_uses_only_selected_rooms_for_the_cold_side(conn):
    telemetry_rows(conn, NOW, room_temps=(22.0, 22.0, 17.0))
    all_rooms = advisor.room_window_stats(conn, SETTINGS, NOW)
    picked = advisor.room_window_stats(conn, {**SETTINGS, "comfort_min_entities": "sensor.z1,sensor.z2"}, NOW,
                                       {"sensor.z1": {"attributes": {"friendly_name": "Salon"}}})
    assert all_rooms["avg24_c"] == picked["avg24_c"] == pytest.approx(20.333, abs=1e-3)   # średnia zawsze ze wszystkich stref
    assert all_rooms["cold24_c"] == 17.0 and picked["cold24_c"] == 22.0                   # minimum tylko z wybranych
    assert picked["coldest_name"] in ("sensor.z2", "Salon")


def test_room_window_stats_needs_enough_complete_samples(conn):
    telemetry_rows(conn, NOW, hours=2)
    assert advisor.room_window_stats(conn, SETTINGS, NOW) == {}


def test_dhw_peak_share_counts_dhw_samples_in_g12w_peak(conn):
    day = datetime(2026, 1, 14, 6, 0)                          # środa: szczyt 6–13 i 15–22
    for hour, mode in ((7, 1), (8, 1), (13, 1), (14, 1), (9, 2)):    # 1 = CWU; 2 = grzanie (pomijane)
        telemetry_rows_at(conn, day.replace(hour=hour), mode)
    out = advisor.dhw_peak_share(conn, datetime(2026, 1, 15, 12, 0))
    assert out == {"share": 0.5, "samples": 4, "pv_covered": 0}


GRID = "sensor.power_meter_active_power"


def test_dhw_peak_share_skips_peak_samples_covered_by_pv(conn):
    day = datetime(2026, 1, 14, 6, 0)                          # środa
    telemetry_rows_at(conn, day.replace(hour=11), 1, GRID, 2500)    # szczyt, oddawanie do sieci → PV, nie liczy
    telemetry_rows_at(conn, day.replace(hour=12), 1, GRID, -200)    # szczyt, mały pobór (< próg) → nie liczy
    telemetry_rows_at(conn, day.replace(hour=17), 1, GRID, -1800)   # szczyt, pobór z sieci → liczy
    telemetry_rows_at(conn, day.replace(hour=14), 1, GRID, -1800)   # poza szczytem
    telemetry_rows_at(conn, day.replace(hour=8), 1)                  # brak odczytu sieci → pominięta
    out = advisor.dhw_peak_share(conn, datetime(2026, 1, 15, 12, 0), grid_key=GRID)
    assert out == {"share": 0.25, "samples": 4, "pv_covered": 2}


def test_dhw_peak_share_without_grid_readings_is_none(conn):
    telemetry_rows_at(conn, datetime(2026, 1, 14, 8, 0), 1)
    assert advisor.dhw_peak_share(conn, datetime(2026, 1, 15, 12, 0), grid_key=GRID) is None


def telemetry_rows_at(conn, when, mode, grid_key=None, grid_w=None):
    ts = int(when.timestamp()) // 60 * 60
    for name, value in (("working_mode", mode), (grid_key, grid_w)):
        if name is None:
            continue
        conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
        key = conn.execute("SELECT id FROM telemetry_keys WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?)", (ts, key, value))
    conn.commit()


def test_run_creates_inertia_test_proposal_and_alert_from_live_snapshot(conn):
    telemetry_rows(conn, NOW, room_temps=(21.6, 21.7, 21.5))
    settings = {**SETTINGS, "advisor_anomaly_entities": "binary_sensor.eev"}
    info = advisor.run(conn, settings, NOW, _states(alert="on"), notify=lambda *a, **k: True)
    assert info["new"] == 2 and info["notified"] == 0                                        # soczewka „żaden” = cisza
    assert {(r["analyzer"], r["kind"]) for r in rows(conn)} == {("curve", "test"), ("anomaly", "alert")}


def test_run_makes_no_curve_proposal_while_a_test_is_running(conn):
    telemetry_rows(conn, NOW, room_temps=(21.6, 21.7, 21.5))
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('bezwladnosc', 'w_toku', ?, '[]', 0)",
                 (NOW.isoformat(),))
    conn.commit()
    advisor.run(conn, SETTINGS, NOW, _states(), notify=lambda *a, **k: True)
    assert [r for r in rows(conn) if r["analyzer"] == "curve"] == []


def test_test_proposal_cannot_be_approved_only_rejected(conn):
    (pid,) = advisor.sync(conn, [draft(kind="test", param=None, to=None, frm=None)], NOW)
    res = advisor.decide(conn, pid, "zatwierdzona", NOW)
    assert not res["ok"] and "zakładce Testy" in res["error"]
    assert rows(conn)[0]["status"] == "oczekuje"                                              # nic się nie stało
    assert advisor.decide(conn, pid, "odrzucona", NOW)["ok"]


def test_test_proposal_notification_names_it_a_proposed_test(conn):
    advisor.sync(conn, [draft(kind="test", param=None, to=None, frm=None, reason="Zrób test.")], NOW)
    sent = []
    advisor.notify_new(conn, {"advisor_lens": "ekonomia", "advisor_notify_service": "notify.kacper"}, NOW,
                       notify=lambda service, title, text, data=None: sent.append((title, text)) or True)
    assert sent == [("Heiko Doradca: proponowany test", "Zrób test.")]


def test_migrate_expires_pending_legacy_experiments_but_keeps_history(conn):
    for status in ("oczekuje", "zatwierdzona"):
        conn.execute("INSERT INTO proposals (created, updated, analyzer, dedupe_key, lens, kind, reason, evidence, effects, "
                     "confidence, expires, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (NOW.isoformat(), NOW.isoformat(), "curve", f"k{status}", "ekonomia", "eksperyment", "x", "{}", "{}",
                      "niska", (NOW + timedelta(hours=6)).isoformat(), status))
    conn.commit()
    dbm.migrate(conn)
    assert [(r["kind"], r["status"]) for r in rows(conn)] == [("eksperyment", "wygasła"), ("eksperyment", "zatwierdzona")]


# ── powiadomienia ────────────────────────────────────────────────────────────

def test_notify_new_only_for_highlighted_lens_and_once(conn):
    advisor.sync(conn, [draft(), draft(key="dhw:hyst", analyzer="dhw", lens="komfort", kind="zmiana", param="dhw_restart_dt", to=6.0, frm=5.0),
                        draft(key="anomaly:x", analyzer="anomaly", lens="obie", kind="alert", param=None, to=None, frm=None)], NOW)
    sent = []
    fake = lambda service, title, text, data=None: sent.append((service, title, text, data)) or True
    off = {"advisor_lens": "żaden", "advisor_notify_service": "notify.phone"}
    assert advisor.notify_new(conn, off, NOW, fake) == 0 and sent == []
    on = {"advisor_lens": "ekonomia", "advisor_notify_service": "notify.phone", "advisor_link_path": "/hassio/ingress/x"}
    assert advisor.notify_new(conn, on, NOW, fake) == 2                                      # ekonomia + „obie”; komfort nie
    assert advisor.notify_new(conn, on, NOW, fake) == 0                                      # raz na propozycję
    assert sent[0][3] == {"url": "/hassio/ingress/x", "clickAction": "/hassio/ingress/x"}
    assert "Przesunięcie krzywej" in sent[0][2] and "z 0 na -1" in sent[0][2]


def test_failed_notification_is_retried(conn):
    advisor.sync(conn, [draft()], NOW)
    cfg = {"advisor_lens": "ekonomia", "advisor_notify_service": "notify.phone"}
    assert advisor.notify_new(conn, cfg, NOW, lambda *a, **k: False) == 0
    assert advisor.notify_new(conn, cfg, NOW, lambda *a, **k: True) == 1


def test_list_proposals_splits_pending_and_history_with_remaining_time(conn):
    advisor.sync(conn, [draft(), draft(key="dhw:h", analyzer="dhw", param="dhw_restart_dt", to=6.0, frm=5.0, kind="zmiana")], NOW)
    advisor.decide(conn, rows(conn)[1]["id"], "odrzucona", NOW)
    data = advisor.list_proposals(conn, NOW + timedelta(hours=2))
    assert [p["analyzer"] for p in data["pending"]] == ["curve"] and data["pending"][0]["remaining_h"] == 4.0
    assert data["pending"][0]["label"] == "Przesunięcie krzywej grzewczej" and data["history"][0]["status"] == "odrzucona"
    assert data["pending"][0]["evidence"] == {"a": 1}


# ── odtworzenie zimy ─────────────────────────────────────────────────────────

def _lts(days=12, room=(21.5, 21.6, 21.4), outdoor=2.0):
    """Godzinowe LTS (start w ms UTC) dla 3 stref i temp. zewnętrznej — od 15.10.2025."""
    start = datetime(2025, 10, 15, tzinfo=timezone.utc)
    out = {}
    for name, value in (("sensor.z1", room[0]), ("sensor.z2", room[1]), ("sensor.z3", room[2]), ("sensor.out", outdoor)):
        out[name] = [{"start": int((start + timedelta(hours=h)).timestamp() * 1000), "mean": value} for h in range(days * 24)]
    return out


def test_replay_winter_counts_days_with_slack_and_estimates_effects():
    numeric = {**{f"number.heiko_heat_pump_curve_ambient_temp_{i}": v for i, v in enumerate([-13, -7, 0, 7, 13], 1)},
               **{f"number.heiko_heat_pump_curve_water_temp_{i}": v for i, v in enumerate([27, 26, 25, 24, 23], 1)}}
    from heiko_predictive_control.floor_model import FloorModel
    out = advisor.replay_winter({**SETTINGS, "outdoor_temp_entity": "sensor.out"}, datetime(2026, 9, 26), FloorModel(),
                                get_statistics=lambda ids, a, b, **k: _lts(), get_numeric=numeric.get)
    assert out["ok"] and out["days_evaluated"] >= 8
    assert out["down"]["days"] == out["days_evaluated"] and out["up"]["days"] == 0        # pokoje stale +0,9°C nad celem
    assert out["down"]["energy_delta_kwh_day"] < 0 and out["down"]["cost_delta_pln_day"] < 0
    assert out["down"]["days_with_effects"] == out["down"]["days"]
    cls = str(outdoor_class(2.0))
    assert out["by_class"][cls]["down"] == out["days_evaluated"] and "caveat" in out and out["samples"]


def test_replay_winter_reports_too_cold_days_as_plus_one():
    from heiko_predictive_control.floor_model import FloorModel
    out = advisor.replay_winter({**SETTINGS, "outdoor_temp_entity": "sensor.out"}, datetime(2026, 9, 26), FloorModel(),
                                get_statistics=lambda ids, a, b, **k: _lts(room=(19.0, 19.1, 18.9)), get_numeric=lambda e: None)
    assert out["up"]["days"] == out["days_evaluated"] and out["down"]["days"] == 0
    assert out["up"]["days_with_effects"] == 0                                            # bez punktów krzywej: brak oszacowania…
    assert out["up"]["cost_delta_pln_day"] is None and out["up"]["energy_delta_kwh_season"] is None   # …a nie „0”


def test_replay_winter_without_statistics_reports_error():
    from heiko_predictive_control.floor_model import FloorModel
    out = advisor.replay_winter({**SETTINGS, "outdoor_temp_entity": "sensor.out"}, datetime(2026, 9, 26), FloorModel(),
                                get_statistics=lambda *a, **k: None, get_numeric=lambda e: None)
    assert out["ok"] is False and "LTS" in out["error"]


def test_replay_roundtrip_in_cache(conn):
    advisor.save_replay(conn, {"created": "2026-09-26T10:00", "ok": True})
    assert advisor.load_replay(conn) == {"created": "2026-09-26T10:00", "ok": True}


# ── P0: migawka i zatwierdzenie ──────────────────────────────────────────────

P0_STATES = [{"entity_id": "number.x_heiko_heat_pump_circulation_pump_p0_run_time", "state": "5"},
             {"entity_id": "number.x_heiko_heat_pump_circulation_pump_p0_stop_time", "state": "5"}]


def _add_p0_test(conn, verdict="1/6", outdoor=2.0, kind="p0_ab", status="zakończony"):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline, result) VALUES (?,?,?,'[]',1,?)",
                 (kind, status, NOW.isoformat(), json.dumps({"verdict": verdict, "confidence": "wysoka", "outdoor_c": outdoor})))
    conn.commit()


def test_p0_inputs_reads_last_verdict_outdoor_average_and_readiness(conn):
    assert advisor.p0_inputs(conn, NOW) == (None, None, False, False)            # D4b niezakończony
    _add_p0_test(conn, kind="bezwladnosc", verdict=None)
    conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, outdoor_temp_c) VALUES (?, 'heiko', 'komfort', 0, 4.0)", ((NOW - timedelta(days=1)).isoformat(),))
    conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, outdoor_temp_c) VALUES (?, 'heiko', 'komfort', 0, 9.0)", ((NOW - timedelta(days=5)).isoformat(),))
    _add_p0_test(conn)
    verdict, outdoor, ready, _ = advisor.p0_inputs(conn, NOW)
    assert verdict == {"verdict": "1/6", "confidence": "wysoka", "outdoor_c": 2.0} and outdoor == 4.0 and ready


def test_p0_test_proposal_rejected_snoozes_it_for_two_weeks(conn):
    _add_p0_test(conn, kind="bezwladnosc", verdict=None)
    (pid,) = advisor.sync(conn, [draft(kind="test", analyzer="p0", key="p0:test", param=None, to=None, frm=None)], NOW)
    advisor.decide(conn, pid, "odrzucona", NOW)
    assert advisor.p0_inputs(conn, NOW + timedelta(days=13))[2:] == (False, False)
    assert advisor.p0_inputs(conn, NOW + timedelta(days=15))[2] is True


def test_p0_revalidation_fails_when_stop_time_changed_meanwhile(conn):
    _add_p0_test(conn, kind="bezwladnosc", verdict=None)
    _add_p0_test(conn, verdict="1/6")
    states = [*_states(), *P0_STATES]
    d = p0_analyzer_draft(conn, states)
    assert (d.from_value, d.to_value) == (5.0, 1.0)
    (pid,) = advisor.sync(conn, [d], NOW)
    row = rows(conn)[0]
    ok, _ = advisor.make_revalidator(conn, SETTINGS, NOW, states)(row)
    assert ok
    changed = [*_states(), P0_STATES[0], {**P0_STATES[1], "state": "6"}]
    ok, why = advisor.make_revalidator(conn, SETTINGS, NOW, changed)(row)
    assert not ok and "postoju" in why


def p0_analyzer_draft(conn, states):
    snap_ = advisor.build_snapshot(conn, SETTINGS, NOW, states)
    return next(d for d in advisor.run_analyzers(snap_) if d.analyzer == "p0")


def test_p0_rejected_change_proposal_snoozes_changes_but_not_tests(conn):
    (pid,) = advisor.sync(conn, [draft(analyzer="p0", key="p0:pair:1/6", param="p0_run_time", to=1.0, frm=5.0)], NOW)
    advisor.decide(conn, pid, "odrzucona", NOW)
    _, _, test_ready, change_snoozed = advisor.p0_inputs(conn, NOW)
    assert change_snoozed and not test_ready                              # test: brak D4b w tym teście
    assert advisor.p0_inputs(conn, NOW + timedelta(days=15))[3] is False


# ── H4: cooldown po „Odrzuć” i zapis po akceptacji (slot 52) ─────────────────

def _reject(conn, d, when):
    (pid,) = advisor.sync(conn, [d], when)
    assert advisor.decide(conn, pid, "odrzucona", when)["ok"]


def slot_draft(to=35.0, frm=30.0):
    return draft(to=to, frm=frm, key="dhw_slot:backup_start_delay", analyzer="dhw_slot", param="backup_start_delay", ttl=72)


def test_sync_cooldown_skips_recently_rejected_identical_proposal(conn):
    _reject(conn, slot_draft(), NOW)
    assert advisor.sync(conn, [slot_draft()], NOW + timedelta(days=6)) == []
    assert [r["status"] for r in rows(conn)] == ["odrzucona"]


def test_sync_cooldown_does_not_apply_for_other_values_or_after_seven_days(conn):
    _reject(conn, slot_draft(), NOW)
    assert len(advisor.sync(conn, [slot_draft(to=25.0)], NOW + timedelta(days=1))) == 1        # inne to_value
    conn.execute("DELETE FROM proposals WHERE status = 'oczekuje'")
    assert len(advisor.sync(conn, [slot_draft(frm=35.0, to=40.0)], NOW + timedelta(days=1))) == 1   # inne from_value
    conn.execute("DELETE FROM proposals WHERE status = 'oczekuje'")
    assert len(advisor.sync(conn, [slot_draft()], NOW + timedelta(days=8))) == 1               # po 7 dniach wraca


def test_sync_cooldown_exempts_anomaly_and_p0(conn):
    for analyzer, key in (("anomaly", "anomaly:x"), ("p0", "p0:x")):
        d = draft(analyzer=analyzer, key=key, kind="alert", param=None, to=None, frm=None)
        _reject(conn, d, NOW)
        assert len(advisor.sync(conn, [d], NOW + timedelta(days=1))) == 1


def test_decide_with_apply_writes_and_marks_not_trial(conn):
    (pid,) = advisor.sync(conn, [slot_draft()], NOW)
    seen = []
    out = advisor.decide(conn, pid, "zatwierdzona", NOW, apply=lambda row: seen.append(row) or (True, ""))
    assert out == {"ok": True, "status": "zatwierdzona", "trial": False, "written": True}
    assert len(seen) == 1 and (seen[0]["from_value"], seen[0]["to_value"]) == (30.0, 35.0)
    assert rows(conn)[0]["trial"] == 0 and rows(conn)[0]["status"] == "zatwierdzona"


def test_decide_with_failing_apply_keeps_proposal_pending(conn):
    (pid,) = advisor.sync(conn, [slot_draft()], NOW)
    out = advisor.decide(conn, pid, "zatwierdzona", NOW, apply=lambda row: (False, "HA nie przyjęło zapisu"))
    assert out == {"ok": False, "error": "HA nie przyjęło zapisu"}
    assert rows(conn)[0]["status"] == "oczekuje"


def test_decide_without_apply_or_for_other_params_stays_trial(conn):
    (a, b) = advisor.sync(conn, [slot_draft(), draft()], NOW)
    assert advisor.decide(conn, a, "zatwierdzona", NOW)["trial"] is True                       # brak apply — jak dotąd
    boom = lambda row: (_ for _ in ()).throw(AssertionError)
    assert advisor.decide(conn, b, "zatwierdzona", NOW, apply=boom) == {"ok": True, "status": "zatwierdzona", "trial": True}
    (c,) = advisor.sync(conn, [slot_draft(to=25.0)], NOW + timedelta(days=1))
    assert advisor.decide(conn, c, "odrzucona", NOW + timedelta(days=1), apply=boom)["trial"] is True    # odrzucenie nie zapisuje


def test_as_dict_marks_writable_changes(conn):
    advisor.sync(conn, [slot_draft(), draft()], NOW)
    by = {p["analyzer"]: p for p in advisor.list_proposals(conn, NOW)["pending"]}
    assert by["dhw_slot"]["writes"] is True and by["curve"]["writes"] is False
