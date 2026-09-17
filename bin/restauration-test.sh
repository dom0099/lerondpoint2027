#!/bin/sh
# Épreuve de restauration — la seule preuve qu'une sauvegarde en est une.
#
# Restaure le dernier instantané restic dans une base JETABLE, compte ce qu'on y
# trouve, puis la supprime. À lancer avant l'ouverture publique, puis tous les mois.
#
#   ./bin/restauration-test.sh              # depuis le dépôt restic (hors VPS)
#   ./bin/restauration-test.sh <fichier>    # depuis un dump local précis
set -eu

PROJET=/home/ubuntu/chantier-c
CONF=/etc/chantier-c-sauvegarde.env
BASE_JETABLE=chantierc_restauration_test
TRAVAIL=$(mktemp -d)
trap 'rm -rf "$TRAVAIL"' EXIT

# Garde-fou structurel, comme dans tests/conftest.py : cette base ne doit JAMAIS
# être celle de l'application. L'incident de migration du 1er septembre a coûté les
# données de recette, il ne se reproduira pas ici.
case "$BASE_JETABLE" in
  chantierc|chantierc_test) echo "base jetable = base réelle, refus" >&2; exit 1;;
esac

cd "$PROJET"

if [ $# -ge 1 ]; then
    DUMP="$1"
else
    [ -f "$CONF" ] || { echo "configuration absente : $CONF" >&2; exit 1; }
    # shellcheck disable=SC1090
    . "$CONF"
    export RESTIC_REPOSITORY RESTIC_PASSWORD
    [ -n "${B2_ACCOUNT_ID:-}" ] && export B2_ACCOUNT_ID B2_ACCOUNT_KEY
    echo "-- récupération du dernier instantané"
    restic restore latest --tag chantier-c --target "$TRAVAIL" --quiet
    DUMP=$(find "$TRAVAIL" -name 'chantierc-*.dump' | sort | tail -1)
fi

[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "aucun dump à restaurer" >&2; exit 1; }
echo "-- dump : $DUMP ($(wc -c < "$DUMP") octets)"

docker compose exec -T db psql -U chantierc -d postgres -q \
    -c "DROP DATABASE IF EXISTS $BASE_JETABLE WITH (FORCE)" \
    -c "CREATE DATABASE $BASE_JETABLE"

docker compose exec -T db pg_restore -U chantierc -d "$BASE_JETABLE" --no-owner < "$DUMP"

echo "-- contenu restauré"
docker compose exec -T db psql -U chantierc -d "$BASE_JETABLE" -c "
SELECT (SELECT count(*) FROM conversation) AS conversations,
       (SELECT count(*) FROM statement)    AS declarations,
       (SELECT count(*) FROM vote)         AS votes,
       (SELECT count(*) FROM participant)  AS participants,
       (SELECT count(*) FROM \"user\")       AS comptes,
       (SELECT count(*) FROM badge)        AS badges;"

docker compose exec -T db psql -U chantierc -d postgres -q \
    -c "DROP DATABASE IF EXISTS $BASE_JETABLE WITH (FORCE)"
echo "-- base jetable supprimée. Restauration éprouvée."
