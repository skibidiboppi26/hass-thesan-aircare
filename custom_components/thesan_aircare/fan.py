"""Ventola: on/off + velocita' 1-5 (mappata in percentuale)."""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .api import prop_value
from .const import DOMAIN, SPEED_MAX
from .entity import AircareEntity

SPEED_RANGE = (1, SPEED_MAX)  # 1..5


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for uuid, label in coordinator.labels.items():
        entities.append(AircareFan(coordinator, uuid, label))
    async_add_entities(entities)


class AircareFan(AircareEntity, FanEntity):
    _attr_name = None  # usa il nome del dispositivo
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = SPEED_MAX

    def __init__(self, coordinator, uuid, label) -> None:
        super().__init__(coordinator, uuid, label)
        self._attr_unique_id = f"{uuid}_fan"

    @property
    def _speed(self) -> int:
        val = prop_value(self.thing, "speed")
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    @property
    def is_on(self) -> bool:
        return self._speed > 0

    @property
    def percentage(self) -> int:
        if self._speed <= 0:
            return 0
        return ranged_value_to_percentage(SPEED_RANGE, self._speed)

    async def _async_send_speed(self, speed: int) -> None:
        await self.async_send("speed", speed)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self._async_send_speed(0)
            return
        speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        speed = max(1, min(SPEED_MAX, speed))
        await self._async_send_speed(speed)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            # riaccende all'ultima velocita' nota o a 3
            await self._async_send_speed(self._speed or 3)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send_speed(0)
