"""Select: modalita' VMC (Auto / Manuale / Immissione / Estrazione)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import prop_value
from .const import DOMAIN, MODE_MAP, MODE_MAP_REV
from .entity import AircareEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for uuid, label in coordinator.labels.items():
        entities.append(AircareModeSelect(coordinator, uuid, label))
    async_add_entities(entities)


class AircareModeSelect(AircareEntity, SelectEntity):
    _attr_name = "Modalità"
    _attr_options = list(MODE_MAP.values())

    def __init__(self, coordinator, uuid, label) -> None:
        super().__init__(coordinator, uuid, label)
        self._attr_unique_id = f"{uuid}_mode"

    @property
    def current_option(self) -> str | None:
        val = prop_value(self.thing, "mode")
        try:
            return MODE_MAP.get(int(val))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        await self.async_send("mode", MODE_MAP_REV[option])
