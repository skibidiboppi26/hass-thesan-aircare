# Come pubblicare su GitHub (una volta sola) — dal Mac

1. Su https://github.com/new crea un repository:
   - Owner: skibiboppi26 · Nome: **hass-thesan-aircare** · Public
   - NON spuntare "Add a README" né licenza (sono già qui) → Create repository

2. Nel Terminale del Mac (questa cartella scompattata sulla Scrivania):

   cd /Users/fabi0/Desktop/hass-thesan-aircare
   git init -b main
   git add .
   git commit -m "Thesan Aircare VMC 1.3.0"
   git remote add origin https://github.com/skibiboppi26/hass-thesan-aircare.git
   git push -u origin main

   Al push GitHub chiede le credenziali: username = skibiboppi26, password = un
   **Personal Access Token** (non la password dell'account). Si crea in
   https://github.com/settings/tokens → Generate new token (classic) → spunta "repo" →
   copia il token e incollalo come password. Il Mac lo ricorda nel portachiavi.

3. Crea la release 1.3.0 (HACS usa le release per le versioni):

   git tag v1.3.0
   git push origin v1.3.0

   poi su https://github.com/skibiboppi26/hass-thesan-aircare/releases/new
   scegli il tag v1.3.0, titolo "1.3.0", Publish release.

# Aggiornamenti futuri
Sostituisci i file, poi:
   git add . && git commit -m "1.3.1: descrizione" && git push
   git tag v1.3.1 && git push origin v1.3.1   (+ release su GitHub)
HACS mostrera' l'aggiornamento a te e a chi lo ha installato.

# Installare da HACS (anche sul tuo Pi, al posto della copia manuale)
HACS → tre puntini → Repository personalizzati → URL del repository, tipo Integration →
Aggiungi → cerca "Thesan Aircare VMC" → Scarica → riavvia HA.
La configurazione esistente (account, VMC, dashboard) resta: HACS sostituisce solo i file
nella stessa cartella /config/custom_components/thesan_aircare/.
