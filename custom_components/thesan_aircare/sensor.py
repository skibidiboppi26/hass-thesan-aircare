"""Sensori: temperatura int/est, umidita' int/est, stato filtro."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import prop_value
from .const import DOMAIN
from .entity import AircareEntity


@dataclass(frozen=True, kw_only=True)
class AircareSensorDesc:
    key: str          # uid nella proprieta'
    name: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None


SENSORS: tuple[AircareSensorDesc, ...] = (
    AircareSensorDesc(
        key="temp_int", name="Temperatura interna",
        unit=UnitOfTemperature.CELSIUS, device_class=SensorDeviceClass.TEMPERATURE,
    ),
    AircareSensorDesc(
        key="temp_ext", name="Temperatura esterna",
        unit=UnitOfTemperature.CELSIUS, device_class=SensorDeviceClass.TEMPERATURE,
    ),
    AircareSensorDesc(
        key="hr_int", name="Umidità interna",
        unit=PERCENTAGE, device_class=SensorDeviceClass.HUMIDITY,
    ),
    AircareSensorDesc(
        key="hr_ext", name="Umidità esterna",
        unit=PERCENTAGE, device_class=SensorDeviceClass.HUMIDITY,
    ),
    AircareSensorDesc(key="d_filter", name="Stato filtro"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for uuid, label in coordinator.labels.items():
        for desc in SENSORS:
            entities.append(AircareSensor(coordinator, uuid, label, desc))
    async_add_entities(entities)


class AircareSensor(AircareEntity, SensorEntity):
    def __init__(self, coordinator, uuid, label, desc: AircareSensorDesc) -> None:
        super().__init__(coordinator, uuid, label)
        self._desc = desc
        self._attr_name = desc.name
        self._attr_unique_id = f"{uuid}_{desc.key}"
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class
        if desc.device_class in (
            SensorDeviceClass.TEMPERATURE, SensorDeviceClass.HUMIDITY
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return prop_value(self.thing, self._desc.key)
