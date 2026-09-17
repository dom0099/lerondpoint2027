#!/bin/sh
# Sauvegarde quotidienne de la base du chantier C.
#
#   1. `pg_dump -Fc` depuis le conteneur `db`, dans /var/backups/chantier-c ;
#   2. dépôt du dump dans un dépôt restic (chiffré) hors du VPS ;
#   3. application de la politique de rétention ;
#   4. horodatage de la dernière réussite, pour qu'un échec se voie.
#
# Le dump contient des adresses e-mail, des empreintes de mots de passe et des votes :
# le répertoire est en 700, les fichiers en 600, et le dépôt restic est chiffré — un
# dépôt en clair chez un tiers serait une fuite en puissance.
#
# Configuration (jamais dans le dépôt Git) : /etc/chantier-c-sauvegarde.env, lisible
# par root seul, qui exporte RESTIC_REPOSITORY, RESTIC_PASSWORD et les identifiants
# Backblaze B2 (B2_ACCOUNT_ID, B2_ACCOUNT_KEY).
set -eu

PROJET=/home/ubuntu/chantier-c
DEPOT_LOCAL=/var/backups/chantier-c
CONF=/etc/chantier-c-sauvegarde.env
#: Copies locales gardées pour une restauration immédiate. Ce n'est PAS la sauvegarde :
#: un disque qui meurt les emporte avec la base.
COPIES_LOCALES=3

mkdir -p "$DEPOT_LOCAL"
chmod 700 "$DEPOT_LOCAL"

[ -f "$CONF" ] || { echo "configuration absente : $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
export RESTIC_REPOSITORY RESTIC_PASSWORD
[ -n "${B2_ACCOUNT_ID:-}" ] && export B2_ACCOUNT_ID B2_ACCOUNT_KEY

HORODATAGE=$(date -u +%Y%m%d-%H%M)
DUMP="$DEPOT_LOCAL/chantierc-$HORODATAGE.dump"

# --- 1. dump ---------------------------------------------------------------------
# Écrit d'abord en .part : un dump interrompu ne doit pas ressembler à un dump valide.
cd "$PROJET"
docker compose exec -T db pg_dump -U chantierc -Fc chantierc > "$DUMP.part"
mv "$DUMP.part" "$DUMP"
chmod 600 "$DUMP"

# --- 2. dépôt hors VPS ------------------------------------------------------------
restic backup "$DUMP" --tag chantier-c --host chantier-c --quiet

# --- 3. rétention -----------------------------------------------------------------
# `--group-by host,tags` n'est pas décoratif. Sans lui, restic groupe par `host,paths`
# et le chemin sauvegardé change à chaque exécution (chantierc-AAAAMMJJ-HHMM.dump) :
# chaque instantané formait son propre groupe d'un seul élément, que `--keep-daily 14`
# gardait consciencieusement. La politique s'appliquait parfaitement — à des groupes de
# un — et n'a rien élagué du 2 septembre au 3 septembre. Le tag est stable, lui.
restic forget --tag chantier-c --keep-daily 14 --keep-weekly 8 --keep-monthly 6 \
    --group-by host,tags --prune --quiet

# --- 4. traces --------------------------------------------------------------------
ls -1t "$DEPOT_LOCAL"/chantierc-*.dump 2>/dev/null | tail -n +$((COPIES_LOCALES + 1)) \
    | xargs -r rm -f

date -u +%Y-%m-%dT%H:%M:%SZ > "$DEPOT_LOCAL/DERNIERE-REUSSITE"

# --- 5. signal « interrupteur d'homme mort » --------------------------------------
# `OnFailure=` prévient quand la sauvegarde ÉCHOUE. Il ne prévient pas quand elle
# ne s'exécute plus du tout : une machine éteinte, un timer désactivé ou un disque
# plein n'envoient aucun e-mail, et les sauvegardes cessent en silence. Ce ping
# inverse la logique — c'est l'ABSENCE de signal qui alerte, côté healthchecks.io.
#
# L'URL vit dans $CONF (déjà chargé plus haut), jamais dans le dépôt Git :
#     HEALTHCHECKS_URL=https://hc-ping.com/<uuid-du-check>
# Non renseignée, cette étape ne fait rien : le script reste utilisable tel quel.
# `|| true` : un service de surveillance injoignable ne doit JAMAIS faire échouer
# une sauvegarde qui, elle, a réussi.
if [ -n "${HEALTHCHECKS_URL:-}" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECKS_URL" || true
fi

echo "sauvegarde terminée : $(basename "$DUMP")"
