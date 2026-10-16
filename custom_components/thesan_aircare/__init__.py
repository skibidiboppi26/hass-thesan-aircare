"""Thesan Aircare VMC integration."""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AircareApiError,
    AircareAuthError,
    AircareClient,
    parse_report_value,
    prop_value,
    set_prop_value,
)

# Pre-import delle piattaforme: cosi' HA le carica nel suo import executor
# e non nell'event loop (evita l'avviso "blocking call to import_module").
from . import binary_sensor as _binary_sensor  # noqa: F401, E402
from . import fan as _fan  # noqa: F401, E402
from . import select as _select  # noqa: F401, E402
from . import sensor as _sensor  # noqa: F401, E402

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_EVERY_N_POLLS,
    DOMAIN,
    OPTIMISTIC_TTL,
    REFRESH_AFTER_COMMAND,
    REPORT_PIDS,
    STALE_AFTER,
    TRANSMIT_EVERY,
    TRANSMIT_TIMEOUT,
    TRANSMIT_VALUE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.FAN, Platform.SELECT, Platform.SENSOR, Platform.BINARY_SENSOR]

# Proprieta' usate per capire se uno snapshot e' "vuoto"
_PLAUSIBILITY_KEYS = ("temp_int", "temp_ext", "hr_int", "speed", "mode")


def _is_plausible(thing: dict) -> bool:
    """Scarta gli snapshot vuoti che il cloud restituisce dopo una riconnessione.

    Un dispositivo reale non ha MAI temperatura interna 0.0 E umidita' 0 E
    velocita' 0 E modalita' 0 tutti insieme: e' un payload "non inizializzato".
    """
    if not thing.get("properties"):
        return False
    vals = [prop_value(thing, k) for k in _PLAUSIBILITY_KEYS]
    try:
        nums = [float(v) if v is not None else 0.0 for v in vals]
    except (TypeError, ValueError):
        return True  # dati strani ma non "vuoti": lasciamo passare
    return any(abs(n) > 0.0 for n in nums)


class AircareCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Polling REST di tutte le VMC; dati indicizzati per uuid.

    - stato live via MQTT push (report per proprieta'), REST come riserva/riconciliazione
    - letture REST in parallelo, una VMC in errore non oscura le altre
    - snapshot non plausibili scartati: si tiene l'ultimo dato buono
    - disponibilita' temporale: una VMC e' connessa se ha dato segni di vita
      (push o REST) negli ultimi STALE_AFTER secondi
    - aggiornamenti ottimistici dopo i comandi (con scadenza)
    - riscoperta periodica della lista VMC; se cambia, ricarica l'integrazione
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AircareClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.client = client
        self.labels: dict[str, str] = {}
        self._last_good: dict[str, dict] = {}
        # uuid -> pid -> (valore, scadenza)
        self._optimistic: dict[str, dict[str, tuple]] = {}
        self._poll_count = 0
        self._refresh_handle = None
        # diagnostica
        self.last_error: str | None = None          # motivo dell'ultimo ciclo fallito
        self.failed_cycles = 0                        # cicli consecutivi senza letture valide
        self.last_good_ts: dict[str, float] = {}      # uuid -> epoch ultimo dato valido (push o REST)
        self.last_push_ts: dict[str, float] = {}      # uuid -> epoch ultimo report MQTT
        self.last_source: dict[str, str] = {}         # uuid -> "mqtt" | "rest"
        self.device_reason: dict[str, str] = {}       # uuid -> motivo ultimo scarto
        self._online: dict[str, bool] = {}            # uuid -> stato da api/v2/online/report
        self._unsub_transmit = None                   # timer della sveglia periodica
        self._transmit_check_handle = None
        self.last_transmit_ts: float | None = None    # ultima sveglia inviata
        self.transmit_sent = 0                        # publish riuscite all'ultima sveglia

    # ----------------------------------------------------------- setup
    async def async_setup(self) -> None:
        things = await self.client.async_get_things()
        for t in things:
            uuid = t["uuid"]
            self.labels[uuid] = t.get("label") or uuid[:8]
            if _is_plausible(t):
                self._last_good[uuid] = t
                self.last_good_ts[uuid] = time.time()
                self.last_source[uuid] = "rest"
            else:
                self.device_reason[uuid] = "snapshot vuoto all'avvio"
                # teniamo comunque la struttura, senza spacciare zeri per veri
                self._last_good[uuid] = {**t, "connected": False}
        self.async_set_updated_data(copy.deepcopy(self._last_good))
        _LOGGER.info("Aircare: %d VMC trovate: %s", len(things), ", ".join(self.labels.values()))
        await self._async_start_push()
        # sveglia periodica: senza, le VMC non pubblicano nulla
        self._unsub_transmit = async_track_time_interval(
            self.hass, self._async_transmit_tick, timedelta(seconds=TRANSMIT_EVERY)
        )
        await self._async_transmit_tick()

    # ------------------------------------------------------- MQTT push
    async def _async_start_push(self) -> None:
        """Sottoscrive i report MQTT. Un fallimento non blocca: resta il REST."""
        self.client.set_report_callback(self._report_from_thread)
        try:
            await self.hass.async_add_executor_job(
                self.client.start_mqtt, list(self.labels), list(REPORT_PIDS)
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aircare: MQTT push non avviato (%s); uso solo REST, ritento", err)

    # ---------------------------------------------------------- sveglia
    async def _async_transmit_tick(self, _now=None) -> None:
        """Chiede alle VMC di trasmettere per TRANSMIT_VALUE secondi (come l'app)."""
        if not self.client.mqtt_connected:
            await self._async_start_push()
            if not self.client.mqtt_connected:
                return
        uuids = list(self.labels)
        sent = await self.hass.async_add_executor_job(
            self.client.request_transmit, uuids, TRANSMIT_VALUE
        )
        self.transmit_sent = sent
        self.last_transmit_ts = time.time()
        if sent:
            if self._transmit_check_handle:
                self._transmit_check_handle()
            self._transmit_check_handle = async_call_later(
                self.hass, TRANSMIT_TIMEOUT, self._transmit_check
            )

    def _transmit_check(self, _now=None) -> None:
        """Dopo la sveglia: chi non ha risposto e' offline (stesso criterio dell'app)."""
        self._transmit_check_handle = None
        sent_at = self.last_transmit_ts or 0
        if not self.last_push_ts:
            # il push non ha mai consegnato nulla: il problema e' la nostra sessione,
            # non le macchine. Non si giudica nessuno.
            return
        changed = False
        for uuid in self.labels:
            answered = (self.last_push_ts.get(uuid, 0) >= sent_at
                        or self.last_good_ts.get(uuid, 0) >= sent_at)
            if answered:
                if self._online.get(uuid) is False:
                    _LOGGER.info("Aircare: %s di nuovo online", self.labels[uuid])
                self._online[uuid] = True
            else:
                if self._online.get(uuid) is not False:
                    _LOGGER.info("Aircare: %s non risponde alla sveglia: offline", self.labels[uuid])
                    changed = True
                self._online[uuid] = False
                self.device_reason[uuid] = "nessun report dopo la sveglia (VMC spenta o senza WiFi)"
        if changed:
            self.async_set_updated_data(self._compose())

    @staticmethod
    def _suspicious_zero(thing: dict | None, pid: str, value) -> bool:
        """Uno 0 secco su temperatura/umidita' dopo un valore normale e' un dato falso."""
        if pid not in ("temp_int", "temp_ext", "hr_int", "hr_ext") or thing is None:
            return False
        try:
            new = float(value)
            old = float(prop_value(thing, pid))
        except (TypeError, ValueError):
            return False
        return new == 0.0 and abs(old) >= 3.0

    def _report_from_thread(self, uuid: str, pid: str, raw: str) -> None:
        # chiamata dal thread paho -> rientra nel loop di HA in modo thread-safe
        self.hass.loop.call_soon_threadsafe(self._handle_report, uuid, pid, raw)

    def _handle_report(self, uuid: str, pid: str, raw: str) -> None:
        if uuid not in self.labels:
            return
        now = time.time()
        if pid == "__online__":
            online = str(raw).strip().lower() not in ("0", "false", "offline", "")
            self._online[uuid] = online
            if online:
                self.last_push_ts[uuid] = now
            _LOGGER.debug("Aircare: %s online=%s", self.labels[uuid], online)
        else:
            value = parse_report_value(raw)
            thing = self._last_good.setdefault(uuid, {"uuid": uuid, "properties": []})
            if self._suspicious_zero(thing, pid, value):
                _LOGGER.debug("Aircare: %s %s=0 ignorato (valore precedente %s)",
                              self.labels[uuid], pid, prop_value(thing, pid))
            else:
                set_prop_value(thing, pid, value)
            thing["connected"] = True
            self._online[uuid] = True
            self.last_push_ts[uuid] = now
            self.last_good_ts[uuid] = now
            self.last_source[uuid] = "mqtt"
            self.device_reason.pop(uuid, None)
            # un report dal cloud conferma/sostituisce l'eventuale stato ottimistico
            self._optimistic.get(uuid, {}).pop(pid, None)
        if self.failed_cycles:
            _LOGGER.info("Aircare: dati ripresi via push dopo %d cicli REST vuoti", self.failed_cycles)
            self.failed_cycles = 0
            self.last_error = None
        self.async_set_updated_data(self._compose())

    # ---------------------------------------------------------- polling
    async def _async_update_data(self) -> dict[str, dict]:
        self._poll_count += 1
        uuids = list(self.labels)

        # riscoperta periodica: nuove VMC aggiunte dall'app ufficiale
        if self._poll_count % DISCOVERY_EVERY_N_POLLS == 0:
            await self._async_rediscover()
            uuids = list(self.labels)

        results = await asyncio.gather(
            *(self.client.async_get_thing(u) for u in uuids), return_exceptions=True
        )

        ok = 0
        empty = 0
        errors: list[str] = []
        now = time.time()
        for uuid, res in zip(uuids, results):
            label = self.labels[uuid]
            if isinstance(res, AircareAuthError):
                raise ConfigEntryAuthFailed(str(res))
            if isinstance(res, BaseException):
                reason = f"{type(res).__name__}: {res}"
                self.device_reason[uuid] = reason
                errors.append(f"{label}: {reason}")
                _LOGGER.debug("Aircare: lettura %s fallita: %s", label, reason)
                continue
            if not _is_plausible(res):
                empty += 1
                self.device_reason[uuid] = "snapshot vuoto (nessun dato dalla macchina)"
                _LOGGER.debug("Aircare: snapshot vuoto per %s, tengo l'ultimo valido", label)
                continue
            prev = self._last_good.get(uuid)
            for pid in ("temp_int", "temp_ext", "hr_int", "hr_ext"):
                if self._suspicious_zero(prev, pid, prop_value(res, pid)):
                    set_prop_value(res, pid, prop_value(prev, pid))
            self._last_good[uuid] = res
            self.last_good_ts[uuid] = now
            self.last_source[uuid] = "rest"
            self.device_reason.pop(uuid, None)
            ok += 1

        # se il push MQTT non e' attivo, prova a (ri)avviarlo: e' la fonte primaria
        if not self.client.mqtt_connected:
            await self._async_start_push()

        if ok == 0 and uuids:
            # costruisci un motivo che dica davvero cosa e' successo
            if self.client.last_error:
                reason = f"autenticazione: {self.client.last_error}"
            elif errors and not empty:
                reason = "errore di lettura REST - " + "; ".join(errors[:2])
            elif empty and not errors:
                reason = (f"REST: snapshot vuoti per tutte le {empty} VMC "
                          "(cache del cloud vuota; lo stato live arriva via MQTT)")
            else:
                reason = f"REST: {empty} snapshot vuoti, {len(errors)} errori - " + "; ".join(errors[:1])
            self.failed_cycles += 1
            self.last_error = reason
            if self.failed_cycles == 1:
                _LOGGER.info("Aircare: ciclo REST senza dati validi (%s)", reason)
            # Fallisce davvero solo se NESSUNA VMC ha dati recenti da nessuna fonte
            if not any(self._is_fresh(u, now) for u in uuids):
                if self.failed_cycles == 1:
                    _LOGGER.warning("Aircare: nessun dato recente da push ne' da REST. Motivo: %s", reason)
                raise UpdateFailed(reason)
            return self._compose()

        for uuid in uuids:
            if uuid in self._last_good and self.last_source.get(uuid) != "mqtt":
                self.last_source[uuid] = "rest"
        if self.failed_cycles:
            _LOGGER.info("Aircare: letture REST riprese dopo %d cicli senza dati", self.failed_cycles)
        self.failed_cycles = 0
        self.last_error = None
        return self._compose()

    def _is_fresh(self, uuid: str, now: float | None = None) -> bool:
        now = now or time.time()
        ts = self.last_good_ts.get(uuid)
        return bool(ts) and (now - ts) < STALE_AFTER

    def _compose(self) -> dict[str, dict]:
        """Ultimi dati buoni + overlay dei comandi ottimistici non ancora scaduti."""
        data = copy.deepcopy(self._last_good)
        now = time.time()
        for uuid, pids in list(self._optimistic.items()):
            for pid, (value, until) in list(pids.items()):
                real = prop_value(data.get(uuid, {}), pid)
                if until < now or _same(real, value):
                    del pids[pid]          # scaduto o gia' confermato dal cloud
                    continue
                if uuid in data:
                    set_prop_value(data[uuid], pid, value)
            if not pids:
                self._optimistic.pop(uuid, None)
        return data

    async def _async_rediscover(self) -> None:
        try:
            things = await self.client.async_get_things()
        except AircareApiError as err:
            _LOGGER.debug("Aircare: riscoperta saltata: %s", err)
            return
        found = {t["uuid"]: (t.get("label") or t["uuid"][:8]) for t in things}
        new = set(found) - set(self.labels)
        if new:
            _LOGGER.info("Aircare: nuove VMC rilevate (%s), ricarico l'integrazione",
                         ", ".join(found[u] for u in new))
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return
        # aggiorna eventuali rinomine fatte nell'app
        for uuid, label in found.items():
            if uuid in self.labels:
                self.labels[uuid] = label

    # --------------------------------------------------------- commands
    async def async_send_command(self, uuid: str, pid: str, value) -> None:
        """Invia un comando e mostra subito il nuovo stato, poi verifica col cloud."""
        await self.hass.async_add_executor_job(
            self.client.set_property, uuid, pid, value
        )
        self._optimistic.setdefault(uuid, {})[pid] = (value, time.time() + OPTIMISTIC_TTL)
        self.async_set_updated_data(self._compose())
        # rilettura differita (un solo timer alla volta)
        if self._refresh_handle:
            self._refresh_handle()
        self._refresh_handle = async_call_later(
            self.hass, REFRESH_AFTER_COMMAND, self._delayed_refresh
        )

    async def _delayed_refresh(self, _now) -> None:
        self._refresh_handle = None
        await self.async_request_refresh()

    # ------------------------------------------------------------ helpers
    def is_connected(self, uuid: str) -> bool:
        """Connessa se il cloud non la dichiara offline e ha dati recenti (push o REST)."""
        if self._online.get(uuid) is False:
            return False
        thing = self._last_good.get(uuid)
        if not thing or thing.get("connected", True) is False:
            return False
        return self._is_fresh(uuid)


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


# ------------------------------------------------------------------ entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    token_path = hass.config.path("thesan_aircare_tokens.json")
    client = AircareClient(
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], session, token_path
    )
    await client.async_load_tokens()
    try:
        if not client.has_valid_token():
            await client.async_login()
    except AircareAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AircareApiError as err:
        # errore temporaneo: HA riprova da solo con il suo backoff
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = AircareCoordinator(hass, entry, client)
    try:
        await coordinator.async_setup()
    except AircareAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AircareApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: AircareCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator._refresh_handle:
            coordinator._refresh_handle()
        if coordinator._unsub_transmit:
            coordinator._unsub_transmit()
        if coordinator._transmit_check_handle:
            coordinator._transmit_check_handle()
        await hass.async_add_executor_job(coordinator.client.disconnect)
    return unload_ok
