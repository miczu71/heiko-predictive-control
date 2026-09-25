from heiko_predictive_control.model import ThermalModel, DEFAULT_K_LOSS, DEFAULT_K_GAIN


def test_seed_defaults():
    m = ThermalModel()
    assert m.k_loss == DEFAULT_K_LOSS
    assert m.k_gain == DEFAULT_K_GAIN
    assert m.samples_seen == 0


def test_predict_delta_heating_active():
    m = ThermalModel(k_loss=0.08, k_gain=0.2)
    delta = m.predict_delta_c(indoor_c=20, outdoor_c=5, setpoint_c=30,
                               dt_hours=1.0, heating_active=True)
    assert delta == 0.2 * (30 - 20)  # k_gain * drive * dt


def test_predict_delta_idle_loses_heat():
    m = ThermalModel(k_loss=0.1, k_gain=0.2)
    delta = m.predict_delta_c(indoor_c=20, outdoor_c=5, setpoint_c=30,
                               dt_hours=2.0, heating_active=False)
    assert delta == -0.1 * (20 - 5) * 2.0
    assert delta < 0


def test_predict_delta_zero_dt():
    m = ThermalModel()
    assert m.predict_delta_c(20, 5, 30, 0.0, True) == 0.0


def test_update_moves_toward_observed_sample_heating():
    m = ThermalModel(k_gain=0.1, alpha=0.5)
    # Obserwacja: w 1h przy drive=10°C (setpoint-indoor) temp wzrosła o 2°C
    # => observed_rate=2, sample = 2/10 = 0.2
    m.update(indoor_before_c=20, indoor_after_c=22, outdoor_c=5,
              setpoint_c=30, dt_hours=1.0, heating_active=True)
    assert m.k_gain == 0.5 * 0.1 + 0.5 * 0.2
    assert m.samples_seen == 1
    assert m.k_loss == 0.08  # niezmienione — inna gałąź


def test_update_moves_toward_observed_sample_idle():
    m = ThermalModel(k_loss=0.1, alpha=0.5)
    # Obserwacja: w 1h przy drive=15°C (indoor-outdoor) temp spadła o 1.5°C
    # => sample = 1.5/15 = 0.1
    m.update(indoor_before_c=20, indoor_after_c=18.5, outdoor_c=5,
              setpoint_c=30, dt_hours=1.0, heating_active=False)
    assert round(m.k_loss, 6) == round(0.5 * 0.1 + 0.5 * 0.1, 6)
    assert m.samples_seen == 1


def test_update_rejects_tiny_drive():
    m = ThermalModel()
    before = m.as_dict()
    m.update(indoor_before_c=20, indoor_after_c=20.1, outdoor_c=19.8,
              setpoint_c=30, dt_hours=1.0, heating_active=False)
    # drive = 20-19.8 = 0.2 < 0.5 próg -> próbka odrzucona
    assert m.as_dict() == before


def test_update_rejects_out_of_bounds_sample():
    m = ThermalModel()
    before_k_gain = m.k_gain
    # drive=1 (bardzo mały ale >0.5), rate ekstremalny -> sample poza granicami
    m.update(indoor_before_c=20, indoor_after_c=40, outdoor_c=5,
              setpoint_c=21, dt_hours=1.0, heating_active=True)
    assert m.k_gain == before_k_gain  # odrzucone, brak zmiany
    assert m.samples_seen == 0


def test_update_zero_dt_noop():
    m = ThermalModel()
    before = m.as_dict()
    m.update(20, 22, 5, 30, 0.0, True)
    assert m.as_dict() == before


def test_roundtrip_dict():
    m = ThermalModel(k_loss=0.12, k_gain=0.33, samples_seen=7)
    restored = ThermalModel.from_dict(m.as_dict())
    assert restored.k_loss == m.k_loss
    assert restored.k_gain == m.k_gain
    assert restored.samples_seen == m.samples_seen


def test_from_dict_none_uses_defaults():
    m = ThermalModel.from_dict(None)
    assert m.k_loss == DEFAULT_K_LOSS
    assert m.k_gain == DEFAULT_K_GAIN
