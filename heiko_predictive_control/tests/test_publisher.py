from heiko_predictive_control.publisher import MQTTPublisher


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


def make():
    pub = MQTTPublisher("h", 1883, "", "", "0.0.0")
    pub._client = FakeClient()
    pub._connected = True
    return pub, pub._client


def by_slug(client):
    return {t.rsplit("/", 1)[-1]: p for t, p, _ in client.published}


def test_missing_values_are_skipped():
    pub, client = make()
    pub.publish_values({"attic_phase": "utrzymanie"})
    assert by_slug(client) == {"attic_phase": "utrzymanie"}


def test_none_clears_sticky_sensors_with_none_payload():
    # bez tego retained „planowany start" z poprzedniego dnia wisi po końcu okna
    pub, client = make()
    pub.publish_values({"attic_planned_start": None, "attic_ac_setpoint_cmd": None,
                        "attic_offset": None})
    assert by_slug(client) == {"attic_planned_start": "None", "attic_ac_setpoint_cmd": "None"}


def test_binary_values_map_to_on_off():
    pub, client = make()
    pub.publish_values({"attic_owned": True, "heiko_write_enabled": False})
    assert by_slug(client) == {"attic_owned": "on", "heiko_write_enabled": "off"}
