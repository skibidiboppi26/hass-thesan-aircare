# Thesan Aircare VMC — integrazione Home Assistant (v1.3.0)

Controllo delle VMC Savio/Thesan Aircare ES (backend cloud Domotica Labs).

- **Stato live**: MQTT push sui topic `api/v2/report/{uuid}/{pid}` e `api/v2/online/report/{uuid}`
  (topic esatti: i wildcard sono negati dal broker)
- **Stato di riserva**: polling REST ogni 60 s (`/api/v2/thing/{uuid}/describe`), riconcilia e copre i buchi del push
- **Comandi**: MQTT publish su `api/v2/command/{uuid}/{pid}` (cloud.thesan.com:1883)
- **Auth**: OAuth2 password grant + refresh token, credenziali dell'app Aircare

## Installazione / aggiornamento

1. Copia la cartella `custom_components/thesan_aircare/` in `/config/custom_components/`
   (sostituendo eventuali versioni precedenti: **tutti** i file).
2. `rm -rf /config/custom_components/thesan_aircare/__pycache__`
3. Riavvia Home Assistant (Impostazioni → Sistema → Riavvia).
4. Solo la prima volta: Impostazioni → Dispositivi e servizi → Aggiungi integrazione →
   "Thesan Aircare" → email e password dell'app.

Aggiornando da una versione precedente **non** serve reinserire le credenziali.

## Entità per ogni VMC

| Entità | Cosa fa |
|---|---|
| `fan.vmc_<stanza>` | on/off + velocità 1–5 (percentuale 20/40/60/80/100) |
| `select.vmc_<stanza>_modalita` | Auto / Manuale / Immissione / Estrazione |
| `sensor.vmc_<stanza>_temperatura_interna/esterna` | °C |
| `sensor.vmc_<stanza>_umidita_interna/esterna` | % |
| `sensor.vmc_<stanza>_stato_filtro` | contatore filtro (`d_filter`) |
| `binary_sensor.vmc_<stanza>_connesso` | diagnostica: la VMC è vista dal cloud e letta correttamente; attributi `ultimo_errore`, `ultima_lettura_ok` |

## Comportamento a regime (perché "funziona sempre")

- **Push MQTT come fonte primaria** (v1.3.0): velocità, modalità, temperature, umidità,
  filtro e stato online arrivano dalle macchine appena cambiano, senza passare dalla
  cache REST del cloud (che periodicamente si svuota). Se la sessione MQTT cade, paho
  riconnette e risottoscrive da solo; nel frattempo il REST continua a leggere.
- **Disponibilità temporale**: una VMC è "connessa" finché ha dato un segno di vita
  (push o REST) negli ultimi 5 minuti e il cloud non l'ha dichiarata offline. I cicli
  REST vuoti non spengono più nulla se il push è vivo.

- **Token su disco** (`/config/thesan_aircare_tokens.json`): i riavvii di HA riusano il
  token, niente login ripetuti. Il file si può cancellare in qualsiasi momento: al
  riavvio viene rifatto un login pulito.
- **Backoff**: se il cloud rifiuta l'autenticazione, il componente aspetta 30 s, 60 s,
  2 min, 5 min, 10 min tra un tentativo e l'altro. Mai raffiche di login (che il cloud
  Thesan punisce con un blocco temporaneo dell'utente).
- **Token revocato** (es. login fatto altrove con lo stesso account): al primo 401 il
  token viene rinnovato e la richiesta ripetuta, senza intervento.
- **Letture per dispositivo, in parallelo**: una VMC in errore non rende "non
  disponibili" le altre.
- **Filtro anti-zeri**: dopo una riconnessione il cloud può rispondere con tutti i
  valori a 0. Questi snapshot vengono scartati e si tiene l'ultimo dato buono, così
  le automazioni su umidità non scattano su letture false.
- **Comandi ottimistici**: dopo un comando l'interfaccia mostra subito il nuovo
  valore, poi lo verifica col cloud dopo 4 s (il pulsante evidenziato non "torna
  indietro").
- **Riscoperta**: ogni ~10 min ricontrolla la lista VMC; se ne compare una nuova
  (aggiunta dall'app ufficiale) l'integrazione si ricarica da sola e la aggiunge.
  Nella dashboard il blocco della nuova stanza va aggiunto a mano.

## Cose da NON fare

- Non fare login manuali (curl, script) al cloud con lo stesso account mentre HA
  gira: il cloud revoca il token di HA. Il componente si riprende da solo, ma è
  inutile stress. Per test usare una GET con il token salvato nel file.
- Non ricaricare l'integrazione a ripetizione per "sbloccare": se il cloud sta
  rifiutando, aspettare è l'unica cosa che funziona (e il componente lo fa da solo).

## Diagnosi rapida

Dashboard con "Non disponibile"? Apri `binary_sensor.vmc_<stanza>_connesso` → Attributi:

| Attributo | Significato |
|---|---|
| `mqtt_push_connesso` | `true` = sessione MQTT attiva (fonte primaria) |
| `fonte_ultimo_dato` | `mqtt` (push) o `rest` (polling) |
| `ultimo_errore` | motivo dell'ultimo ciclo REST fallito: `autenticazione: …`, `errore di lettura REST - …`, oppure `REST: snapshot vuoti…` (cache del cloud vuota: innocuo se il push è vivo) |
| `motivo_questa_vmc` | perché l'ultima lettura REST di questa VMC è stata scartata |
| `cicli_rest_falliti_consecutivi` | cicli REST (×60 s) di fila senza dati validi |
| `ultimo_dato_ok` / `secondi_da_ultimo_dato` | età dell'ultimo dato buono, da qualunque fonte |
| `ultimo_report_push` | ultimo messaggio MQTT ricevuto per questa VMC |
| `cloud_autenticato` | `true` = token valido, nessun cooldown |
| `ultima_sveglia` / `sveglie_inviate` | quando è partito l'ultimo `transmit` e a quante VMC |

Nel log (Impostazioni → Sistema → Log, cerca `Aircare`) compare un avviso al primo
ciclo fallito con lo stesso motivo, e un'informazione quando le letture riprendono.

Regola pratica: "Connesso" = off con `mqtt_push_connesso` = true e `ultimo_report_push`
vecchio → la VMC non sta inviando report (WiFi della macchina o cloud Thesan); il
componente si riprende da solo quando tornano. `mqtt_push_connesso` = false a lungo →
il broker rifiuta la sessione: controllare la password dell'account Aircare.

## Endpoint e credenziali del client

In `const.py`. Se Thesan cambiasse backend, è l'unico file da toccare.
