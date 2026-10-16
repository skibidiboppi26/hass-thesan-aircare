"""Base entity condivisa: raggruppa le entita' per dispositivo VMC."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class AircareEntity(CoordinatorEntity):
    """Entita' base legata a una VMC (uuid)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, uuid: str, label: str) -> None:
        super().__init__(coordinator)
        self._uuid = uuid
        self._label = label

    @property
    def thing(self) -> dict:
        return (self.coordinator.data or {}).get(self._uuid, {})

    @property
    def available(self) -> bool:
        # disponibile solo se il polling funziona E la VMC risulta connessa al cloud
        return super().available and self.coordinator.is_connected(self._uuid)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=f"VMC {self._label}",
            manufacturer="Thesan / Savio",
            model="Aircare ES",
        )

    async def async_send(self, pid: str, value) -> None:
        """Invia un comando tramite il coordinator (stato ottimistico + verifica)."""
        await self.coordinator.async_send_command(self._uuid, pid, value)
