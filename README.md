# Thesan Aircare VMC — Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
![Version](https://img.shields.io/badge/version-1.4.0-blue)

Integrazione **non ufficiale** per le VMC decentralizzate **Savio / Thesan Aircare ES** (versione Wi‑Fi, app "Aircare", backend cloud Domotica Labs).
Controllo di velocità e modalità, lettura di temperatura e umidità interna/esterna, stato filtro, con aggiornamento in tempo reale.

*English version below.*

---

## Italiano

### Cosa fa
- **Ventola** `fan.vmc_<stanza>`: on/off e velocità 1–5
- **Velocità** `number.vmc_<stanza>_velocita`: la velocità come numero 0–5 (0 = spenta), comoda nell'editor visuale delle automazioni
- **Modalità** `select.vmc_<stanza>_modalita`: Auto / Manuale / Immissione / Estrazione
- **Sensori**: temperatura interna ed esterna (°C), umidità interna ed esterna (%), stato filtro
- **Connesso** (diagnostica): la VMC risponde al cloud; attributi che spiegano cosa succede
- Stato in **tempo reale** via MQTT; polling REST di riserva ogni 60 s
- **Sveglia automatica** delle macchine: le Aircare trasmettono solo se sollecitate, come fa l'app ufficiale. L'integrazione se ne occupa da sola.
- Nuove VMC aggiunte dall'app compaiono in HA entro ~10 minuti

### Installazione con HACS (consigliata)
1. HACS → tre puntini in alto a destra → **Repository personalizzati**
2. URL: `https://github.com/skibidiboppi26/hass-thesan-aircare` · Tipo: **Integration** → Aggiungi
3. Cerca "Thesan Aircare VMC" → **Scarica** → riavvia Home Assistant
4. Impostazioni → Dispositivi e servizi → **Aggiungi integrazione** → "Thesan Aircare"
5. Inserisci **email e password dell'app Aircare**. Le VMC compaiono con i nomi dati nell'app.

### Installazione manuale
Copia `custom_components/thesan_aircare/` in `/config/custom_components/`, riavvia, poi passo 4–5.

### Dashboard (opzionale)
In `dashboards/vmc-dashboard.yaml` c'è una dashboard pronta: quadranti per velocità, pulsanti − / +,
pannello umidità con soglie, grafici per stanza. Richiede **button-card** (da HACS).
Gli id delle entità nel file seguono i nomi delle stanze (`salone`, `cucina`, `cameretta`, `camera`):
adattali ai tuoi.

### Cose da sapere
- Il cloud Thesan **revoca il token** se si fa login con lo stesso account da un altro client
  (es. script o curl). L'integrazione si riprende da sola, ma evitate login manuali.
- Non ricaricare l'integrazione a ripetizione: il cloud blocca temporaneamente l'utente dopo login a raffica.
  L'integrazione ha già il suo backoff.
- Il file `/config/thesan_aircare_tokens.json` contiene i token di sessione: non condividerlo.

### Diagnostica
Apri `binary_sensor.vmc_<stanza>_connesso` → Attributi. Dettagli in [docs-componente.md](docs-componente.md).

### Come funziona (per curiosi)
API ricavata dall'app ufficiale (Cordova): OAuth2 su `cloud.thesan.com`, MQTT su porta 1883 per
comandi (`api/v2/command/{uuid}/{pid}`) e report (`api/v2/report/{uuid}/{pid}`). Le macchine
pubblicano solo dopo il comando `transmit`, che l'app manda ogni 10 s; qui ogni 30 s.

Progetto indipendente, non affiliato a Savio, Thesan o Domotica Labs.

---

## English

Unofficial Home Assistant integration for **Savio / Thesan Aircare ES** decentralised ventilation
units (Wi‑Fi version, "Aircare" app, Domotica Labs cloud backend).

**Features**: fan on/off and speed 1–5, speed as a 0–5 number entity (handy in the automation editor), mode select (Auto / Manual / Supply / Extract), indoor and
outdoor temperature and humidity, filter counter, connectivity diagnostics. Real‑time state via
MQTT with REST fallback. The integration periodically sends the `transmit` wake‑up command, exactly
like the official app does, so data keeps flowing with the app closed.

**Install (HACS)**: HACS → ⋮ → Custom repositories → `https://github.com/skibidiboppi26/hass-thesan-aircare`,
type *Integration* → Download → restart → Settings → Devices & services → Add integration →
"Thesan Aircare" → your Aircare app e‑mail and password.

**Notes**: the Thesan cloud revokes the token when another client logs in with the same account;
the integration recovers automatically. Avoid manual logins and repeated reloads (temporary
account block). Example dashboard in `dashboards/` (needs *button-card*).

Independent project, not affiliated with Savio, Thesan or Domotica Labs. MIT licence.
