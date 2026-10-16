"""Numero: velocita' VMC 0-5 come valore intero.

Esiste per comodita' nell'editor delle automazioni e nelle dashboard:
"Numero: Imposta valore -> 3" invece di "Ventola: Imposta velocita' -> 60 %".
0 = spenta. Comanda la stessa proprieta' 'speed' della ventola.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import prop_value
from .const import DOMAIN, SPEED_MAX
from .entity import AircareEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AircareSpeedNumber(coordinator, uuid, label)
        for uuid, label in coordinator.labels.items()
    )


class AircareSpeedNumber(AircareEntity, NumberEntity):
    _attr_name = "Velocità"
    _attr_icon = "mdi:fan"
    _attr_native_min_value = 0
    _attr_native_max_value = SPEED_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, uuid, label) -> None:
        super().__init__(coordinator, uuid, label)
        self._attr_unique_id = f"{uuid}_speed_level"

    @property
    def native_value(self) -> int | None:
        val = prop_value(self.thing, "speed")
        try:
            return max(0, min(SPEED_MAX, int(float(val))))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        speed = max(0, min(SPEED_MAX, int(round(value))))
        await self.async_send("speed", speed)
