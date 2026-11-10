"""E1: nakładka ustawień bieżącej doby testu (czysta funkcja, bez I/O)."""
import copy

from heiko_predictive_control.testkit.overlay import effective_settings

SETTINGS = {
    "heiko_enabled": True,
    "g13_peak_preheat_shift": 0,
    "g13_peak_peak_shift": 0,
    "tail_margin_k": 1,
    "advisor_notify_service": "notify.x",
}


def test_applies_overrides():
    out = effective_settings(SETTINGS, {"overrides": {"g13_peak_preheat_shift": 1, "tail_margin_k": 2}})
    assert out["g13_peak_preheat_shift"] == 1
    assert out["tail_margin_k"] == 2
    assert out["g13_peak_peak_shift"] == 0
    assert out["heiko_enabled"] is True
    assert "_overlay_rejected" not in out


def test_does_not_mutate_input():
    before = copy.deepcopy(SETTINGS)
    out = effective_settings(SETTINGS, {"overrides": {"g13_peak_preheat_shift": 1}})
    assert SETTINGS == before
    assert out is not SETTINGS


def test_none_active_returns_equal_copy():
    out = effective_settings(SETTINGS, None)
    assert out == SETTINGS
    assert out is not SETTINGS


def test_empty_overrides_returns_equal_copy():
    assert effective_settings(SETTINGS, {"overrides": {}}) == SETTINGS


def test_key_outside_overridable_is_rejected_not_applied():
    out = effective_settings(SETTINGS, {"overrides": {"g13_peak_enabled": False, "tail_margin_k": 2}})
    assert "g13_peak_enabled" not in out
    assert out["_overlay_rejected"] == ["g13_peak_enabled"]
    assert out["tail_margin_k"] == 2


def test_forbidden_keys_never_overwrite_settings():
    out = effective_settings(SETTINGS, {"overrides": {"heiko_enabled": False, "pv_roi_url": "http://x"}})
    assert out["heiko_enabled"] is True
    assert "pv_roi_url" not in out
    assert sorted(out["_overlay_rejected"]) == ["heiko_enabled", "pv_roi_url"]
