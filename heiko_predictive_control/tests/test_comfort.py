import inspect
from datetime import datetime, timedelta

from heiko_predictive_control import comfort
from heiko_predictive_control import db as dbm

NOW = datetime(2026, 1, 14, 10, 0)
SETTINGS = {"heiko_room_min_c": 18.5, "notify_service": "notify.family"}


def _conn(temps, reduced=0, names=("Sypialnia",)):
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    n = len(temps)
    for i, t in enumerate(temps):
        dbm.insert_cycle(conn, {"ts": (NOW - timedelta(minutes=15 * (n - 1 - i))).isoformat(), "loop": "heiko",
                                "active_profile": "ekonomia", "write_enabled": 0, "min_room_c": t,
                                "min_room_name": names[0], "reduced_active": reduced})
    return conn


class Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, service, title, message):
        self.sent.append((service, title, message))
        return True


def test_alarm_after_30_minutes_below_minimum_once_per_episode():
    conn, sink = _conn([18.0, 17.9, 17.8]), Sink()
    assert comfort.comfort_alarm(conn, SETTINGS, NOW, notify=sink) == "alarm"
    assert len(sink.sent) == 1 and "Sypialnia" in sink.sent[0][2] and "17.8" in sink.sent[0][2]
    assert comfort.comfort_alarm(conn, SETTINGS, NOW, notify=sink) is None      # ten sam epizod: cisza
    assert len(sink.sent) == 1


def test_no_alarm_when_recovering_or_too_short_or_unknown():
    sink = Sink()
    assert comfort.comfort_alarm(_conn([18.0, 19.0, 17.8]), SETTINGS, NOW, notify=sink) is None
    assert comfort.comfort_alarm(_conn([17.8, 17.8]), SETTINGS, NOW, notify=sink) is None     # 15 min
    assert comfort.comfort_alarm(_conn([None, None, None]), SETTINGS, NOW, notify=sink) is None
    assert sink.sent == []


def test_episode_ends_only_with_margin_and_can_alarm_again():
    conn, sink = _conn([18.0, 17.9, 17.8]), Sink()
    comfort.comfort_alarm(conn, SETTINGS, NOW, notify=sink)
    dbm.insert_cycle(conn, {"ts": (NOW + timedelta(minutes=15)).isoformat(), "loop": "heiko",
                            "active_profile": "ekonomia", "write_enabled": 0, "min_room_c": 18.6})
    assert comfort.comfort_alarm(conn, SETTINGS, NOW + timedelta(minutes=15), notify=sink) is None   # 18,6 < 18,8
    dbm.insert_cycle(conn, {"ts": (NOW + timedelta(minutes=30)).isoformat(), "loop": "heiko",
                            "active_profile": "ekonomia", "write_enabled": 0, "min_room_c": 19.0})
    assert comfort.comfort_alarm(conn, SETTINGS, NOW + timedelta(minutes=30), notify=sink) == "koniec"


def test_message_suggests_disabling_reduced_setpoint_when_active():
    conn, sink = _conn([18.0, 17.9, 17.8], reduced=1), Sink()
    comfort.comfort_alarm(conn, SETTINGS, NOW, notify=sink)
    assert "ograniczona nastawa" in sink.sent[0][2].lower() and "5.1" in sink.sent[0][2]


def test_alarm_module_never_calls_pump_services():
    source = inspect.getsource(comfort)
    assert "call_service" not in source                 # tylko notify (powiadomienie dla usera)
