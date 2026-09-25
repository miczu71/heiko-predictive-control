"""Profile Komfort/Ekonomia dla obu pętli — parametry, nie kod sterujący.
Dwa profile liczone równolegle co cykl (patrz cycle.py); wybór aktywnego to
tylko etykieta do UI/MQTT w Etapie 1 (żadna pętla jeszcze nic nie zapisuje)."""
from __future__ import annotations

from dataclasses import dataclass

PROFILE_NAMES = ("komfort", "ekonomia")


@dataclass(frozen=True)
class HeikoBand:
    day_c: float
    night_c: float

    def for_hour(self, hour: int, day_start: int, day_end: int) -> float:
        is_day = day_start <= hour < day_end
        return self.day_c if is_day else self.night_c


@dataclass(frozen=True)
class HeikoProfiles:
    komfort: HeikoBand
    ekonomia: HeikoBand

    def band(self, profile: str) -> HeikoBand:
        return self.komfort if profile == "komfort" else self.ekonomia

    @classmethod
    def from_settings(cls, s: dict) -> "HeikoProfiles":
        return cls(
            komfort=HeikoBand(
                day_c=float(s.get("heiko_comfort_band_day_c", 1.0)),
                night_c=float(s.get("heiko_comfort_band_night_c", 1.5)),
            ),
            ekonomia=HeikoBand(
                day_c=float(s.get("heiko_economy_band_day_c", 1.5)),
                night_c=float(s.get("heiko_economy_band_night_c", 2.0)),
            ),
        )


@dataclass(frozen=True)
class AtticProfiles:
    komfort_target_c: float
    ekonomia_target_c: float

    def target(self, profile: str) -> float:
        return self.komfort_target_c if profile == "komfort" else self.ekonomia_target_c

    @classmethod
    def from_settings(cls, s: dict) -> "AtticProfiles":
        return cls(
            komfort_target_c=float(s.get("attic_comfort_target_c", 22.0)),
            ekonomia_target_c=float(s.get("attic_economy_target_c", 20.5)),
        )
