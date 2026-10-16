#!/bin/zsh
# ---------------------------------------------------------------------------
#  Pubblica una nuova versione di hass-thesan-aircare su GitHub.
#
#  Uso:   /Users/fabi0/Desktop/hass-thesan-aircare/rilascia.sh 1.4.0 "note della versione"
#
#  Fa, nell'ordine:
#   1. controlla che il numero coincida con quello nel manifest
#   2. passa temporaneamente all'account GitHub skibidiboppi26
#   3. commit (se ci sono modifiche) + push
#   4. crea tag e release v<numero>
#   5. rimette l'account GitHub che era attivo prima (anche in caso di errore)
#
#  Requisiti (una volta sola): gh installato e "gh auth login" fatto con
#  ENTRAMBI gli account (vedi Guida-rilascio-Thesan-Aircare.pdf).
# ---------------------------------------------------------------------------
set -e

ACCOUNT="skibidiboppi26"
REPO="skibidiboppi26/hass-thesan-aircare"

V="$1"
NOTE="$2"
if [[ -z "$V" || -z "$NOTE" ]]; then
  echo "Uso: $0 <versione> \"note della versione\""
  echo "Es.: $0 1.4.0 \"Nuova entità Velocità 0-5\""
  exit 1
fi
V="${V#v}"   # accetta anche "v1.4.0"

cd "$(dirname "$0")"

if ! command -v gh >/dev/null 2>&1; then
  echo "Manca il programma gh: scaricalo da https://cli.github.com e riprova."
  exit 1
fi

if ! gh auth status --hostname github.com 2>&1 | grep -q "account $ACCOUNT"; then
  echo "L'account $ACCOUNT non è collegato a gh."
  echo "Esegui: gh auth login   (e nel browser entra come $ACCOUNT)"
  exit 1
fi

MV=$(python3 -c "import json;print(json.load(open('custom_components/thesan_aircare/manifest.json'))['version'])")
if [[ "$MV" != "$V" ]]; then
  echo "Il manifest dice $MV ma hai chiesto $V: controlla di aver scompattato lo zip giusto."
  exit 1
fi

if gh release view "v$V" --repo "$REPO" >/dev/null 2>&1; then
  echo "La release v$V esiste già su GitHub: niente da fare."
  exit 0
fi

PREV=$(gh api user --jq .login 2>/dev/null || echo "")
ripristina() {
  if [[ -n "$PREV" && "$PREV" != "$ACCOUNT" ]]; then
    gh auth switch --hostname github.com --user "$PREV" >/dev/null 2>&1 \
      && echo "Account GitHub attivo rimesso a: $PREV"
  fi
}
trap ripristina EXIT

gh auth switch --hostname github.com --user "$ACCOUNT" >/dev/null
echo "Account GitHub per la pubblicazione: $ACCOUNT"

git add -A
if git diff --cached --quiet; then
  echo "Nessuna modifica nuova da salvare (commit già fatto): procedo."
else
  git commit -m "$V: $NOTE"
fi
git push

gh release create "v$V" --repo "$REPO" --target main --title "$V" --notes "$NOTE"

echo ""
echo "Fatto: https://github.com/$REPO/releases/tag/v$V"
echo "In Home Assistant: HACS -> Thesan Aircare VMC -> tre puntini -> Aggiorna informazioni -> Aggiorna."
