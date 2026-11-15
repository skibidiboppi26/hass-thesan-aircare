"""Thesan Aircare VMC integration."""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import local as vmc_local
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
from . import number as _number  # noqa: F401, E402
from . import select as _select  # noqa: F401, E402
from . import sensor as _sensor  # noqa: F401, E402

from .const import (
    CONF_EMAIL,
    CONF_LOCAL,
    CONF_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_EVERY_N_POLLS,
    DOMAIN,
    LOCAL_EVERY,
    LOCAL_FAIL_RESCAN,
    LOCAL_RESCAN_GAP,
    LOCAL_SLOW_EVERY_N,
    MQTT_RESTART_GAPS,
    OFFLINE_AFTER,
    OPTIMISTIC_TTL,
    REFRESH_AFTER_COMMAND,
    REPORT_PIDS,
    STALE_AFTER,
    TRANSMIT_EVERY,
    TRANSMIT_TIMEOUT,
    TRANSMIT_VALUE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.FAN, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.BINARY_SENSOR]

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
        self.all_silent = False                       # tutte le VMC zitte insieme (cloud/sessione)
        self.mqtt_restarts = 0                        # sessioni MQTT riaperte da noi
        self._silent_restarts = 0                     # riaperture nel silenzio in corso
        self._last_mqtt_restart = 0.0
        # rete locale
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}") if entry else None
        self._hosts: dict[str, str] = {}              # seriale -> indirizzo IP
        self.local_serial: dict[str, str] = {}        # uuid -> seriale
        self.last_local_ts: dict[str, float] = {}     # uuid -> epoch ultima lettura locale
        self._local_fails: dict[str, int] = {}
        self._local_cycle = 0
        self._local_busy = False
        self._last_local_scan = 0.0
        self._unsub_local = None

    # ----------------------------------------------------------- setup
    async def async_setup(self) -> None:
        saved = (await self._store.async_load()) or {}
        self._hosts = dict(saved.get("hosts") or {})
        try:
            things = await self.client.async_get_things()
        except AircareApiError as err:
            # cloud giu' all'avvio: si parte con le VMC gia' note (i dati arrivano in locale)
            things = self._cached_things(saved)
            if not things:
                raise
            _LOGGER.warning("Aircare: cloud non raggiungibile all'avvio (%s): parto con le "
                            "%d VMC gia' note", err, len(things))
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
        self._save_store()
        self.local_serial = {
            u: s for u, s in (self.entry.options.get(CONF_LOCAL) or {}).items() if u in self.labels
        }
        if self.local_serial:
            self._unsub_local = async_track_time_interval(
                self.hass, self._async_local_tick, timedelta(seconds=LOCAL_EVERY)
            )
            await self._async_local_tick()
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

    # ----------------------------------------------------------- locale
    def _cached_things(self, saved: dict) -> list[dict]:
        """Le VMC note dall'ultimo avvio riuscito (o dal registro dispositivi di HA)."""
        labels = dict(saved.get("labels") or {})
        if not labels:
            reg = dr.async_get(self.hass)
            for dev in dr.async_entries_for_config_entry(reg, self.entry.entry_id):
                for dom, ident in dev.identifiers:
                    if dom == DOMAIN:
                        name = dev.name or ident[:8]
                        labels[ident] = name[4:] if name.startswith("VMC ") else name
        return [{"uuid": u, "label": l, "properties": []} for u, l in labels.items()]

    def _save_store(self) -> None:
        if self._store:
            self._store.async_delay_save(
                lambda: {"labels": dict(self.labels), "hosts": dict(self._hosts)}, 5
            )

    def set_hosts(self, hosts: dict[str, str]) -> None:
        """Indirizzi trovati da una ricerca in rete ({seriale: IP})."""
        self._hosts.update(hosts)
        self._save_store()

    def local_host(self, uuid: str) -> str | None:
        return self._hosts.get(self.local_serial.get(uuid, ""))

    def _local_fresh(self, uuid: str, now: float | None = None) -> bool:
        return (now or time.time()) - self.last_local_ts.get(uuid, 0) <= OFFLINE_AFTER

    def _all_local(self) -> bool:
        """Tutte le VMC stanno rispondendo in locale: il cloud non serve."""
        return bool(self.labels) and all(self._local_fresh(u) for u in self.labels)

    async def _async_local_tick(self, _now=None) -> None:
        """Legge le VMC in rete locale; se una non risponde piu', la ricerca per seriale."""
        if self._local_busy:
            return
        self._local_busy = True
        try:
            self._local_cycle += 1
            slow = self._local_cycle % LOCAL_SLOW_EVERY_N == 1
            pids = vmc_local.FAST_PIDS + (vmc_local.SLOW_PIDS if slow else ())
            todo = [(u, s, self._hosts.get(s)) for u, s in self.local_serial.items()]
            reach = [t for t in todo if t[2]]
            missing = [s for _, s, h in todo if not h]
            results = await asyncio.gather(
                *(self.hass.async_add_executor_job(vmc_local.poll, h, s if slow else None, pids)
                  for _, s, h in reach),
                return_exceptions=True,
            )
            now = time.time()
            changed = False
            for (uuid, serial, host), res in zip(reach, results):
                if isinstance(res, BaseException):
                    fails = self._local_fails[uuid] = self._local_fails.get(uuid, 0) + 1
                    _LOGGER.debug("Aircare: lettura locale di %s (%s) fallita: %s",
                                  self.labels.get(uuid, uuid[:8]), host, res)
                    if isinstance(res, vmc_local.WrongDevice) or fails >= LOCAL_FAIL_RESCAN:
                        self._hosts.pop(serial, None)
                        missing.append(serial)
                    continue
                self._local_fails[uuid] = 0
                self._apply_local(uuid, res, now)
                changed = True
            if missing and now - self._last_local_scan >= LOCAL_RESCAN_GAP:
                self._last_local_scan = now
                found = await self.hass.async_add_executor_job(vmc_local.scan)
                back = {s: h for s, h in found.items() if s in missing}
                if back:
                    _LOGGER.info("Aircare: VMC ritrovate in rete locale: %s", ", ".join(back.values()))
                    self.set_hosts(back)
            if changed:
                self.async_set_updated_data(self._compose())
        finally:
            self._local_busy = False

    @callback
    def _apply_local(self, uuid: str, values: dict[str, str], now: float) -> None:
        thing = self._last_good.setdefault(uuid, {"uuid": uuid, "properties": []})
        for pid, raw in values.items():
            value = parse_report_value(raw)
            if not self._suspicious_zero(thing, pid, value):
                set_prop_value(thing, pid, value)
        thing["connected"] = True
        self._online[uuid] = True
        self.last_good_ts[uuid] = now
        self.last_local_ts[uuid] = now
        self.last_source[uuid] = "locale"
        self.device_reason.pop(uuid, None)

    # ---------------------------------------------------------- sveglia
    async def _async_transmit_tick(self, _now=None) -> None:
        """Valuta chi ha risposto, poi chiede alle VMC di trasmettere (come l'app)."""
        if self._all_local():
            return  # rispondono tutte in casa: niente sveglie ne' giudizi dal cloud
        if self._evaluate_online():
            await self._async_restart_push()
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

    async def _async_restart_push(self) -> None:
        """Riapre la sessione MQTT quando tutte le VMC tacciono insieme."""
        self._last_mqtt_restart = time.time()
        self._silent_restarts += 1
        self.mqtt_restarts += 1
        _LOGGER.info("Aircare: riapro la sessione MQTT (tentativo %d in questo silenzio)",
                     self._silent_restarts)
        try:
            await self.hass.async_add_executor_job(self.client.restart_mqtt)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Aircare: riapertura MQTT fallita (%s); ritento alla prossima sveglia", err)

    @callback
    def _evaluate_online(self) -> bool:
        """Decide chi e' offline; ritorna True se conviene riaprire la sessione MQTT.

        - Offline solo se da OFFLINE_AFTER secondi non arriva nessun report: una
          risposta in ritardo non conta come disconnessione.
        - Se TUTTE le VMC tacciono insieme il problema e' il cloud o la sessione,
          non le macchine: nessuna viene segnata offline (resta il limite
          STALE_AFTER, cosi' un blackout vero si vede lo stesso). La sessione si
          riapre solo se il silenzio dura a lungo (MQTT_RESTART_GAPS).
        Gira nel loop di HA (prima di ogni sveglia): niente scritture di stato da
        altri thread.
        """
        if not self.last_push_ts or not self.last_transmit_ts or not self.client.mqtt_connected:
            # il push non consegna nulla o la sessione e' giu': il problema e' nostro,
            # non delle macchine. Non si giudica nessuno.
            return False
        now = time.time()
        if len(self.labels) > 1:
            newest = max(self.last_push_ts.get(u, 0) for u in self.labels)
            if now - newest > OFFLINE_AFTER:
                if not self.all_silent:
                    _LOGGER.info("Aircare: nessuna VMC risponde da %d s: problema del cloud "
                                 "o della sessione MQTT, non delle macchine", int(now - newest))
                    self.all_silent = True
                    self.async_set_updated_data(self._compose())
                gap = MQTT_RESTART_GAPS[min(self._silent_restarts, len(MQTT_RESTART_GAPS) - 1)]
                # dall'inizio del silenzio o dall'ultima riapertura, se piu' recente
                return now - max(newest, self._last_mqtt_restart) >= gap
            if self.all_silent:
                _LOGGER.info("Aircare: i report sono ripresi")
                self.all_silent = False
                self._silent_restarts = 0
        changed = False
        for uuid in self.labels:
            last = max(self.last_push_ts.get(uuid, 0), self.last_good_ts.get(uuid, 0))
            if now - last <= OFFLINE_AFTER:
                if self._online.get(uuid) is False:
                    _LOGGER.info("Aircare: %s di nuovo online", self.labels[uuid])
                    self._online[uuid] = True
                    changed = True
            elif self._online.get(uuid) is not False:
                _LOGGER.info("Aircare: %s non risponde da %d s: offline",
                             self.labels[uuid], int(now - last))
                self._online[uuid] = False
                self.device_reason[uuid] = (
                    f"nessun report da oltre {OFFLINE_AFTER} s (VMC spenta o senza WiFi)"
                )
                changed = True
        if changed:
            self.async_set_updated_data(self._compose())
        return False

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
            if online:
                self._online[uuid] = True
                self.last_push_ts[uuid] = now
            elif now - self.last_push_ts.get(uuid, 0) <= OFFLINE_AFTER:
                # il cloud la dice offline ma ha appena mandato dati: non ci si crede
                _LOGGER.debug("Aircare: %s offline dal cloud ignorato (report di %d s fa)",
                              self.labels[uuid], int(now - self.last_push_ts.get(uuid, 0)))
            else:
                self._online[uuid] = False
                self.device_reason[uuid] = "il cloud la segnala offline e non manda report"
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

        if self._all_local():
            # le letture locali sono piu' fresche della cache del cloud: non serve il REST
            self.failed_cycles = 0
            self.last_error = None
            return self._compose()

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
            if self._local_fresh(uuid, now):
                ok += 1      # la lettura locale e' piu' fresca: il REST non la sovrascrive
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
        host = self.local_host(uuid)
        sent = False
        if host:
            try:
                await self.hass.async_add_executor_job(vmc_local.write, host, pid, value)
                sent = True
            except vmc_local.LocalError as err:
                _LOGGER.warning("Aircare: comando locale a %s fallito (%s): provo dal cloud",
                                self.labels.get(uuid, uuid[:8]), err)
        if not sent:
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
        if self.local_serial:
            await self._async_local_tick()
        await self.async_request_refresh()

    # ------------------------------------------------------------ helpers
    def is_connected(self, uuid: str) -> bool:
        """Connessa se ha mandato report da poco; altrimenti decidono i segnali del
        cloud (topic online, campo connected del REST) e l'eta' dei dati.
        """
        if self._local_fresh(uuid):
            return True      # risponde in rete locale: e' accesa, qualunque cosa dica il cloud
        if time.time() - self.last_push_ts.get(uuid, 0) <= OFFLINE_AFTER:
            return True
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
        # cloud giu': si prova lo stesso, le VMC note rispondono in rete locale
        _LOGGER.warning("Aircare: login al cloud non riuscito (%s)", err)

    coordinator = AircareCoordinator(hass, entry, client)
    try:
        await coordinator.async_setup()
    except AircareAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AircareApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Opzioni cambiate (abbinamento locale): si ricarica l'integrazione."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: AircareCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator._refresh_handle:
            coordinator._refresh_handle()
        if coordinator._unsub_transmit:
            coordinator._unsub_transmit()
        if coordinator._unsub_local:
            coordinator._unsub_local()
        if coordinator._transmit_check_handle:
            coordinator._transmit_check_handle()
        await hass.async_add_executor_job(coordinator.client.disconnect)
    return unload_ok
