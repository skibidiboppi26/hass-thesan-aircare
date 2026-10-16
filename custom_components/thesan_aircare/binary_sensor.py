"""Binary sensor diagnostico: la VMC risulta connessa al cloud Thesan?

Attributi (visibili nella scheda dell'entita'):
  - mqtt_push_connesso            : sessione MQTT attiva (fonte primaria dello stato)
  - fonte_ultimo_dato             : "mqtt" (push) oppure "rest" (polling)
  - ultimo_errore                 : motivo dell'ultimo ciclo REST fallito (globale)
  - motivo_questa_vmc             : perche' l'ultima lettura REST di QUESTA VMC e' stata scartata
  - cicli_rest_falliti_consecutivi: cicli REST di fila senza dati validi (innocui se il push e' vivo)
  - ultimo_dato_ok / secondi_da_ultimo_dato : eta' dell'ultimo dato valido, da qualunque fonte
  - ultimo_report_push            : ultimo messaggio MQTT ricevuto per questa VMC
  - cloud_autenticato             : token valido e nessun cooldown in corso
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import AircareEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AircareConnected(coordinator, uuid, label)
        for uuid, label in coordinator.labels.items()
    )


class AircareConnected(AircareEntity, BinarySensorEntity):
    """On = la VMC ha dati recenti (push o REST) e il cloud non la dichiara offline."""

    _attr_name = "Connesso"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, uuid, label) -> None:
        super().__init__(coordinator, uuid, label)
        self._attr_unique_id = f"{uuid}_connected"

    @property
    def available(self) -> bool:
        # sempre disponibile: deve poter dire "off" anche quando il cloud e' giu'
        return True

    @property
    def is_on(self) -> bool:
        return bool(
            self.coordinator.last_update_success
            and self.coordinator.is_connected(self._uuid)
        )

    @property
    def extra_state_attributes(self) -> dict:
        c = self.coordinator
        client = c.client
        attrs: dict = {
            "mqtt_push_connesso": client.mqtt_connected,
            "fonte_ultimo_dato": c.last_source.get(self._uuid),
            "ultimo_errore": c.last_error or client.last_error,
            "motivo_questa_vmc": c.device_reason.get(self._uuid),
            "cicli_rest_falliti_consecutivi": c.failed_cycles,
            "cloud_autenticato": client.has_valid_token() and not client.in_cooldown(),
            "sveglie_inviate": c.transmit_sent,
        }
        if c.last_transmit_ts:
            attrs["ultima_sveglia"] = _iso(c.last_transmit_ts)
        ts = c.last_good_ts.get(self._uuid)
        if ts:
            attrs["ultimo_dato_ok"] = _iso(ts)
            attrs["secondi_da_ultimo_dato"] = int(time.time() - ts)
        pts = c.last_push_ts.get(self._uuid)
        if pts:
            attrs["ultimo_report_push"] = _iso(pts)
        return attrs


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
