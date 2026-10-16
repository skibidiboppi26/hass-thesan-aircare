"""Client per il cloud Thesan Aircare (Domotica Labs).

Lettura stato:  REST  GET /api/v2/thing/{uuid}/describe
Comandi:        MQTT  publish su api/v2/command/{uuid}/{pid}
Auth:           OAuth2 password grant; refresh con refresh_token.

Robustezza (v1.2.x):
  - v1.2.2: stato in tempo reale via MQTT push (topic report esatti), REST come riserva
  - token salvati su disco e riusati: un riavvio di HA non rifa' login
  - backoff crescente dopo errori di auth: mai login a raffica
  - su 401 il token viene invalidato, rinnovato e la richiesta ritentata UNA volta
    (copre anche il caso "token revocato dal cloud")
  - errori temporanei -> AircareApiError (recuperabile), mai re-login forzato
  - MQTT: API paho 2.x, riconnessione automatica, lock per comandi concorrenti
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from .const import (
    TRANSMIT_PID,
    AUTH_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    CMD_TOPIC,
    DESCRIBE_URL,
    MQTT_HOST,
    MQTT_PORT,
    ONLINE_TOPIC,
    REFRESH_URL,
    REPORT_TOPIC,
    THINGS_URL,
)

_LOGGER = logging.getLogger(__name__)

# Attese crescenti (secondi) dopo errori di auth consecutivi, con tetto.
_BACKOFF_STEPS = [30, 60, 120, 300, 600]
# Margine di sicurezza prima della scadenza del token (secondi).
_TOKEN_MARGIN = 120
# Timeout HTTP.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


class AircareAuthError(Exception):
    """Credenziali non valide (errore definitivo, richiede re-login)."""


class AircareApiError(Exception):
    """Errore temporaneo di comunicazione o blocco cloud (recuperabile)."""


def _make_mqtt_client() -> mqtt.Client:
    """Crea il client MQTT con l'API corretta per la versione di paho installata."""
    try:  # paho-mqtt >= 2.0
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # type: ignore[attr-defined]
    except AttributeError:  # paho-mqtt 1.x
        return mqtt.Client()


class AircareClient:
    """Gestisce auth, lettura REST e invio comandi MQTT."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
        token_path: str | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._token_path = token_path

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._access_expires: float = 0.0

        # backoff / cooldown auth
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._auth_lock = asyncio.Lock()

        # stato diagnostico esposto alle entita'
        self.last_error: str | None = None
        self.last_success: float = 0.0

        # MQTT per i comandi (username/password = credenziali utente)
        self._mqtt = _make_mqtt_client()
        self._mqtt.username_pw_set(email, password)
        self._mqtt.reconnect_delay_set(min_delay=1, max_delay=30)
        self._mqtt_started = False
        self._mqtt_lock = threading.Lock()
        self.mqtt_connected = False           # stato live della sessione MQTT
        self._report_topics: list[str] = []   # topic a cui (ri)sottoscriversi
        self._report_cb = None                # fn(uuid, pid, raw_value) thread-safe
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message = self._on_mqtt_message

    # ------------------------------------------------------------ token file
    async def async_load_tokens(self) -> None:
        """Carica i token salvati su disco, se presenti."""
        if not self._token_path:
            return
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, self._read_token_file)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Token file non leggibile: %s", err)
            return
        if not data:
            return
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._access_expires = self._parse_expires(data.get("access_token_expires"))
        _LOGGER.debug("Token caricati da disco (validi ancora %.0fs)",
                      self._access_expires - time.time())

    def _read_token_file(self) -> dict | None:
        if self._token_path and os.path.exists(self._token_path):
            with open(self._token_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return None

    def _write_token_file(self) -> None:
        if not self._token_path:
            return
        tmp = self._token_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "access_token_expires": int(self._access_expires),
                },
                fh,
            )
        os.replace(tmp, self._token_path)  # scrittura atomica

    async def _async_store_tokens(self, data: dict[str, Any]) -> None:
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        self._access_expires = self._parse_expires(data.get("access_token_expires"))
        # autenticati: azzera il backoff
        self._fail_count = 0
        self._cooldown_until = 0.0
        self.last_error = None
        if self._token_path:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._write_token_file
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Impossibile salvare i token su disco: %s", err)

    @staticmethod
    def _parse_expires(raw: Any) -> float:
        """L'API restituisce un epoch assoluto; tolleriamo anche un delta in secondi."""
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return time.time() + 3000
        if val < 1_000_000_000:  # non e' un epoch: e' una durata
            return time.time() + val
        return val

    # ------------------------------------------------------------- backoff
    def _enter_cooldown(self, reason: str) -> None:
        step = _BACKOFF_STEPS[min(self._fail_count, len(_BACKOFF_STEPS) - 1)]
        self._fail_count += 1
        self._cooldown_until = time.time() + step
        self.last_error = reason
        _LOGGER.warning(
            "Aircare: auth fallita (%s). Nuovo tentativo tra %ss (fallimenti: %s)",
            reason, step, self._fail_count,
        )

    def _in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def in_cooldown(self) -> bool:
        """True se siamo in attesa dopo un errore di autenticazione."""
        return self._in_cooldown()

    def has_valid_token(self) -> bool:
        return bool(self._access_token) and time.time() < (
            self._access_expires - _TOKEN_MARGIN
        )

    # ---------------------------------------------------------------- AUTH
    async def async_login(self, raise_auth: bool = False) -> None:
        """Login (password grant).

        raise_auth=True  -> credenziali errate sollevano AircareAuthError
                            (solo in fase di configurazione).
        raise_auth=False -> qualunque fallimento entra in cooldown e solleva
                            AircareApiError (a regime: recuperabile).
        """
        payload = {
            "password": self._password,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "password",
        }
        url = AUTH_URL.format(email=self._email)
        try:
            async with self._session.post(url, json=payload, timeout=_HTTP_TIMEOUT) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            if raise_auth:
                raise AircareApiError(str(err)) from err
            self._enter_cooldown(f"rete: {err}")
            raise AircareApiError(str(err)) from err

        if isinstance(data, dict) and "access_token" in data:
            await self._async_store_tokens(data)
            _LOGGER.info("Aircare: login riuscito")
            return

        desc = (data or {}).get("description", "Login fallito") if isinstance(data, dict) else "Login fallito"
        if raise_auth:
            raise AircareAuthError(desc)
        self._enter_cooldown(f"login rifiutato: {desc}")
        raise AircareApiError(f"Login rifiutato dal cloud: {desc}")

    async def _async_refresh(self) -> None:
        """Rinnova l'access token; se fallisce, un solo login di fallback."""
        if self._refresh_token:
            payload = {
                "refresh_token": self._refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
            }
            url = REFRESH_URL.format(email=self._email)
            try:
                async with self._session.post(url, json=payload, timeout=_HTTP_TIMEOUT) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                data = None
            if isinstance(data, dict) and "access_token" in data:
                await self._async_store_tokens(data)
                _LOGGER.debug("Aircare: token rinnovato")
                return
            _LOGGER.debug("Aircare: refresh fallito, provo il login")
        await self.async_login()

    async def _async_valid_token(self) -> str:
        """Ritorna un token valido, rispettando il cooldown."""
        async with self._auth_lock:
            if self.has_valid_token():
                return self._access_token  # type: ignore[return-value]
            if self._in_cooldown():
                wait = int(self._cooldown_until - time.time())
                raise AircareApiError(f"cloud in cooldown, riprovo tra {wait}s")
            await self._async_refresh()
            if not self._access_token:
                raise AircareApiError("token non disponibile")
            return self._access_token

    def invalidate_token(self) -> None:
        """Forza il rinnovo al prossimo giro (es. dopo un 401)."""
        self._access_expires = 0.0

    # ---------------------------------------------------------------- READ
    async def _async_get_json(self, url: str) -> Any:
        """GET autenticata. Su 401 invalida il token, rinnova e ritenta UNA volta."""
        last_err = "richiesta fallita"
        for attempt in (1, 2):
            token = await self._async_valid_token()
            try:
                async with self._session.get(
                    url, headers={"authorization": f"Bearer {token}"}, timeout=_HTTP_TIMEOUT
                ) as resp:
                    if resp.status == 401:
                        self.invalidate_token()
                        last_err = f"401 su {url}"
                        if attempt == 1:
                            _LOGGER.debug("Aircare: 401, rinnovo token e ritento")
                            continue
                        break
                    if resp.status >= 400:
                        raise AircareApiError(f"HTTP {resp.status} su {url}")
                    data = await resp.json(content_type=None)
                    self.last_success = time.time()
                    self.last_error = None
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise AircareApiError(str(err)) from err
        raise AircareApiError(last_err)

    async def async_get_things(self) -> list[dict[str, Any]]:
        data = await self._async_get_json(THINGS_URL.format(email=self._email))
        if not isinstance(data, list):
            raise AircareApiError("risposta /things inattesa")
        return data

    async def async_get_thing(self, uuid: str) -> dict[str, Any]:
        data = await self._async_get_json(DESCRIBE_URL.format(uuid=uuid))
        if not isinstance(data, dict):
            raise AircareApiError("risposta /describe inattesa")
        return data

    # -------------------------------------------------------- MQTT (push)
    def set_report_callback(self, cb) -> None:
        """cb(uuid, pid, raw) viene chiamata dal thread MQTT: deve essere thread-safe."""
        self._report_cb = cb

    def start_mqtt(self, uuids: list[str], pids: list[str]) -> None:
        """Connette (bloccante, in executor) e sottoscrive i report delle VMC.

        Le sottoscrizioni vengono rifatte in on_connect, quindi sopravvivono
        alle riconnessioni automatiche di paho.
        """
        self._report_topics = [
            REPORT_TOPIC.format(uuid=u, pid=p) for u in uuids for p in pids
        ] + [ONLINE_TOPIC.format(uuid=u) for u in uuids]
        self._ensure_mqtt()

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, *args) -> None:
        # paho v2: reason_code e' un ReasonCode; v1: int rc
        ok = getattr(reason_code, "is_failure", None)
        failed = ok if isinstance(ok, bool) else (reason_code != 0)
        if failed:
            self.mqtt_connected = False
            _LOGGER.warning("Aircare: connessione MQTT rifiutata (%s)", reason_code)
            return
        self.mqtt_connected = True
        if self._report_topics:
            client.subscribe([(t, 0) for t in self._report_topics])
            _LOGGER.info("Aircare: MQTT connesso, sottoscritti %d topic di report",
                         len(self._report_topics))
        else:
            _LOGGER.debug("Aircare: MQTT connesso")

    def _on_mqtt_disconnect(self, client, userdata, *args) -> None:
        self.mqtt_connected = False
        _LOGGER.debug("Aircare: MQTT disconnesso, paho ritentera' da solo")

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        if not self._report_cb:
            return
        parts = msg.topic.split("/")
        try:
            raw = msg.payload.decode("utf-8", "ignore").strip()
        except Exception:  # noqa: BLE001
            return
        # api/v2/report/{uuid}/{pid}   oppure   api/v2/online/report/{uuid}
        if len(parts) == 5 and parts[2] == "report":
            self._report_cb(parts[3], parts[4], raw)
        elif len(parts) == 5 and parts[2] == "online":
            self._report_cb(parts[4], "__online__", raw)

    def _ensure_mqtt(self) -> None:
        """Avvia il client MQTT (una volta); paho gestisce la riconnessione."""
        with self._mqtt_lock:
            if self._mqtt_started:
                return
            self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            self._mqtt.loop_start()
            self._mqtt_started = True
            _LOGGER.debug("Aircare: MQTT avviato")

    # ------------------------------------------------------------ COMMANDS
    def set_property(self, uuid: str, pid: str, value: Any) -> None:
        """Pubblica un comando (bloccante: va eseguito in executor).

        QoS 0 come l'app ufficiale: il broker Thesan non conferma in modo
        affidabile i QoS 1, quindi non si attende alcun ack.
        """
        try:
            self._ensure_mqtt()
        except OSError as err:
            raise AircareApiError(f"MQTT non raggiungibile: {err}") from err
        # se la sessione si sta ancora aprendo, aspetta al massimo 3 s
        deadline = time.time() + 3
        while not self.mqtt_connected and time.time() < deadline:
            time.sleep(0.1)
        if not self.mqtt_connected:
            raise AircareApiError("MQTT non connesso: comando non inviato")
        topic = CMD_TOPIC.format(uuid=uuid, pid=pid)
        info = self._mqtt.publish(topic, str(value), qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise AircareApiError(f"comando non inviato (rc={info.rc})")
        try:
            info.wait_for_publish(timeout=3)   # QoS 0: attende solo l'uscita dal socket
        except Exception:  # noqa: BLE001
            pass
        _LOGGER.debug("Aircare: comando %s=%s -> %s", pid, value, uuid)

    def request_transmit(self, uuids: list[str], seconds: int) -> int:
        """Sveglia le VMC: pubblica il comando 'transmit' (come fa l'app ogni 10 s).

        Ritorna quante publish sono partite. Non alza eccezioni: se la sessione
        MQTT non e' viva, semplicemente non manda nulla (ci pensa il coordinator
        a riavviarla).
        """
        if not self._mqtt or not self.mqtt_connected:
            return 0
        sent = 0
        for uuid in uuids:
            try:
                info = self._mqtt.publish(
                    CMD_TOPIC.format(uuid=uuid, pid=TRANSMIT_PID), str(seconds), qos=0
                )
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    sent += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Aircare: transmit a %s non inviato: %s", uuid[:8], err)
        return sent

    def disconnect(self) -> None:
        with self._mqtt_lock:
            if self._mqtt_started:
                self._mqtt.loop_stop()
                try:
                    self._mqtt.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._mqtt_started = False
                self.mqtt_connected = False


def prop_value(thing: dict[str, Any], uid: str) -> Any:
    """Estrae il value di una proprieta' dal describe/thing."""
    for p in thing.get("properties", []):
        if p.get("uid") == uid:
            return p.get("value")
    return None


def set_prop_value(thing: dict[str, Any], uid: str, value: Any) -> None:
    """Imposta il value di una proprieta' (la crea se manca)."""
    props = thing.setdefault("properties", [])
    for p in props:
        if p.get("uid") == uid:
            p["value"] = value
            return
    props.append({"uid": uid, "value": value})


def parse_report_value(raw: str) -> Any:
    """I report MQTT sono valori grezzi ('3', '25.8', 'true')."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw
