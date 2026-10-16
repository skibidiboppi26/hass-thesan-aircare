"""Constants for the Thesan Aircare integration."""
from __future__ import annotations

DOMAIN = "thesan_aircare"

# --- Cloud endpoints (ricavati dall'app ufficiale) ---
BASE_URL = "https://cloud.thesan.com"
AUTH_URL = BASE_URL + "/api/v2/user/{email}/tokens"
REFRESH_URL = BASE_URL + "/api/v2/user/{email}/refresh"
THINGS_URL = BASE_URL + "/api/v2/user/{email}/things"
DESCRIBE_URL = BASE_URL + "/api/v2/thing/{uuid}/describe"

# OAuth client hardcoded nell'app
CLIENT_ID = "aircares_client"
CLIENT_SECRET = "eL{m*H.b(9uL+<5-"

# --- MQTT (comandi) ---
MQTT_HOST = "cloud.thesan.com"
MQTT_PORT = 1883  # TCP; l'app usa 8884 (wss) e 1883 (tcp). 1883 e' quello testato.
CMD_TOPIC = "api/v2/command/{uuid}/{pid}"
REPORT_TOPIC = "api/v2/report/{uuid}/{pid}"     # push di stato dalle VMC (topic ESATTI: i wildcard sono negati)
ONLINE_TOPIC = "api/v2/online/report/{uuid}"    # presenza online della VMC
# Proprieta' seguite in push (le stesse esposte come entita')
REPORT_PIDS = ("speed", "mode", "temp_int", "temp_ext", "hr_int", "hr_ext", "d_filter")

# --- Sveglia (ricavata dall'app ufficiale) ---
# Le VMC inviano report SOLO se sollecitate: l'app pubblica ogni 10 s il comando
# "transmit" con valore "10" su ogni macchina e, se entro 5 s non arriva nulla,
# la considera offline. Qui si fa lo stesso, con cadenza piu' rilassata.
TRANSMIT_PID = "transmit"
TRANSMIT_VALUE = 10        # secondi di trasmissione richiesti alla macchina (come l'app)
TRANSMIT_EVERY = 30        # ogni quanti secondi rimandare la sveglia
TRANSMIT_TIMEOUT = 8       # secondi di attesa del report prima di dichiarare la VMC offline

# --- Polling ---
DEFAULT_SCAN_INTERVAL = 60       # REST di riconciliazione (lo stato live arriva via MQTT push)
DISCOVERY_EVERY_N_POLLS = 10     # ogni ~10 min ricontrolla la lista VMC (nuove/rimosse)
STALE_AFTER = 300                # secondi senza dati (push o REST) prima di dichiarare la VMC non disponibile
OPTIMISTIC_TTL = 20              # secondi in cui un comando appena inviato "vince" sul cloud
REFRESH_AFTER_COMMAND = 4        # secondi dopo un comando prima di rileggere lo stato

# --- Proprieta' ---
# mode: 0 Auto / 1 Manuale / 2 Immissione / 3 Estrazione
MODE_MAP = {0: "Auto", 1: "Manuale", 2: "Immissione", 3: "Estrazione"}
MODE_MAP_REV = {v: k for k, v in MODE_MAP.items()}

# speed: 0 spento .. 5 massimo
SPEED_MAX = 5

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
