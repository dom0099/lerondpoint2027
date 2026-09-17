#!/bin/bash
#
# Restauration d'essai — éprouve que la sauvegarde en est une.
#
#   bin/restauration-essai.sh [chemin/du/dump]
#
# Sans argument, prend le dump local le plus récent de /var/backups/chantier-c.
#
# ----------------------------------------------------------------------------------
# À LANCER À LA MAIN, UNE FOIS PAR TRIMESTRE. PAS EN TÂCHE PLANIFIÉE.
# ----------------------------------------------------------------------------------
#
# C'est la décision D2 du journal, et elle n'est pas de la paresse. Une restauration
# d'essai automatique produit un courriel de plus par trimestre, qu'on cesse de lire au
# bout du deuxième ; et quand elle échoue enfin pour de bon, l'alerte se perd parmi les
# précédentes. Lancée à la main, elle oblige quelqu'un à REGARDER le résultat — ce qui
# est exactement le but de l'exercice.
#
# Ce script reprend, pas à pas, la procédure éprouvée à la main le 15 septembre 2026
# (journal, section « Restauration d'essai »). Il fait quatre choses et les fait dans cet
# ordre :
#
#   1. restaure un dump dans une base JETABLE ;
#   2. compare les décomptes de tables avec la production, en lecture seule ;
#   3. vérifie que l'APPLICATION sert dix pages types depuis la base restaurée ;
#   4. détruit tout.
#
# Le point 3 est celui qui compte. Un dump qui se restaure mais dont le schéma ne fait
# plus tourner l'application n'est pas une sauvegarde — et la suite de tests ne peut PAS
# le vérifier : `tests/conftest.py` construit son propre schéma et ne touche jamais une
# base restaurée. Elle éprouve le code, pas la sauvegarde.
#
# Ce script ne touche JAMAIS la production : elle n'est lue que pour comparer des
# décomptes, et la base cible est vérifiée avant toute écriture.
#
# Pour éprouver aussi le trajet réseau (récupération depuis Backblaze), voir le §6 de la
# même section du journal : il demande une clé B2 en LECTURE SEULE, qui n'est pas dans ce
# dépôt et ne doit jamais y être.

set -uo pipefail

PROJET=${PROJET:-/home/ubuntu/chantier-c}
CONTENEUR=${CONTENEUR:-chantier-c-db-1}
UTILISATEUR=chantierc
SOURCE=chantierc
CIBLE=chantierc_essai_restauration
TEMOIN=chantierc_essai_temoin
PORT=${PORT:-8099}
DEPOT_DUMPS=/var/backups/chantier-c

# --- garde-fou : on ne restaure que vers une base jetable --------------------------
#
# Structurel et non dérivé : la cible est écrite en dur au-dessus, et ces deux lignes
# refusent de continuer si quelqu'un la change pour une base réelle. C'est la même
# précaution que dans `tests/conftest.py`, et pour la même raison — l'incident de
# migration du chantier C, où un `downgrade` à la main a détruit les données de recette.
for interdite in "$SOURCE" chantierc_test; do
    if [ "$CIBLE" = "$interdite" ] || [ "$TEMOIN" = "$interdite" ]; then
        echo "REFUS : la base jetable coïncide avec « $interdite »." >&2
        exit 1
    fi
done

DUMP=${1:-}
if [ -z "$DUMP" ]; then
    # Le motif est développé PAR ROOT, dans le sous-shell : le répertoire est en 700,
    # donc un `sudo ls .../chantierc-*.dump` écrit tel quel laisse le shell appelant —
    # qui ne peut pas lire le répertoire — passer le motif littéral à ls, et l'on croit
    # qu'il n'y a aucune sauvegarde alors qu'il y en a trois. Trouvé en lançant ce
    # script pour la première fois.
    DUMP=$(sudo sh -c "ls -1t $DEPOT_DUMPS/chantierc-*.dump 2>/dev/null" | head -1)
fi
if [ -z "$DUMP" ] || ! sudo test -f "$DUMP"; then
    echo "Aucun dump trouvé. Donnez-en un en argument, ou vérifiez $DEPOT_DUMPS." >&2
    exit 1
fi

psql_sur() { docker exec -i "$CONTENEUR" psql -U "$UTILISATEUR" -d "$1" -tAc "$2"; }
echec=0
signaler() { echo "  ÉCHEC : $1"; echec=$((echec + 1)); }

# Une étape SAUTÉE compte comme un échec, et ce n'est pas de la sévérité de principe.
# À la première exécution, ce script a sauté les étapes 3 et 4 — faute de trouver la
# chaîne de connexion — et a conclu « la sauvegarde en est une ». Il n'avait rien vérifié
# du tout. Un contrôle qui se tait quand il n'a pas pu s'exécuter est pire que pas de
# contrôle : il rassure.
sauter() { echo "  SAUTÉE : $1"; echec=$((echec + 1)); }

# --- la chaîne de connexion, pour monter le témoin et lancer l'application ----------
#
# Elle n'est PAS dans .env : en production, l'application la reçoit de
# `docker-compose.yml`, qui la compose à partir de POSTGRES_PASSWORD. Depuis l'hôte, la
# base est sur localhost:5433 (voir docker-compose.yml, ports) et non sur `db:5432`,
# qui n'est joignable que depuis le réseau Docker.
BASE_URL=""
if [ -f "$PROJET/.env" ]; then
    MDP=$(grep -E '^POSTGRES_PASSWORD=' "$PROJET/.env" | head -1 | cut -d= -f2-)
    [ -n "$MDP" ] && BASE_URL="postgresql+asyncpg://$UTILISATEUR:$MDP@localhost:5433"
fi

echo "=================================================================="
echo " RESTAURATION D'ESSAI — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "=================================================================="
echo "  dump   : $DUMP"
echo "  taille : $(sudo stat -c %s "$DUMP") octets, daté du $(sudo stat -c %y "$DUMP" | cut -d. -f1)"
echo

# --- 1. restauration, chronométrée ------------------------------------------------
#
# Le chronomètre part de la première commande et s'arrête à la base interrogeable :
# c'est le chiffre qui compte le jour où il faut le faire dans l'urgence. Relevé à
# 0,74 s le 15 septembre 2026, sur une base de 95 Ko.
echo "--- 1/5 restauration dans la base jetable"
DEBUT=$(date +%s%3N)
psql_sur postgres "DROP DATABASE IF EXISTS \"$CIBLE\"" > /dev/null
psql_sur postgres "CREATE DATABASE \"$CIBLE\"" > /dev/null
sudo cat "$DUMP" | docker exec -i "$CONTENEUR" sh -c "cat > /tmp/essai-restauration.dump"
docker exec -i "$CONTENEUR" pg_restore -U "$UTILISATEUR" -d "$CIBLE" \
    --no-owner --no-privileges /tmp/essai-restauration.dump 2>&1 | tail -3
CODE=${PIPESTATUS[0]}
psql_sur "$CIBLE" "SELECT count(*) FROM vote" > /dev/null || signaler "la base restaurée ne répond pas"
FIN=$(date +%s%3N)
[ "$CODE" -eq 0 ] || signaler "pg_restore a rendu $CODE"
echo "  $((FIN - DEBUT)) ms, de la commande à la base utilisable"
REVISION=$(psql_sur "$CIBLE" "SELECT version_num FROM alembic_version")
echo "  révision restaurée : $REVISION"
echo

# --- 2. décomptes contre la production --------------------------------------------
echo "--- 2/5 décomptes, restaurée contre production (production en LECTURE SEULE)"
printf "  %-22s %12s %12s %9s\n" "table" "restaurée" "production" "écart"
for t in "user" participant conversation statement vote analysis_run group_naming \
         signalement relecture; do
    R=$(psql_sur "$CIBLE" "SELECT count(*) FROM \"$t\"" 2>/dev/null) || R="-"
    P=$(psql_sur "$SOURCE" "SELECT count(*) FROM \"$t\"" 2>/dev/null) || P="-"
    if [ "$R" = "-" ] || [ "$P" = "-" ]; then
        printf "  %-22s %12s %12s %9s\n" "$t" "$R" "$P" "?"
    else
        printf "  %-22s %12s %12s %+9d\n" "$t" "$R" "$P" "$((P - R))"
    fi
done
echo
echo "  Un écart n'est pas forcément une anomalie : c'est la FRAÎCHEUR de l'instantané."
echo "  Le 15 septembre, rate_limit_hit était à -2, ses lignes ayant été purgées par le"
echo "  worker depuis la sauvegarde. Un écart sur vote ou statement, lui, en serait une."
echo

# --- 3. le schéma restauré est-il celui des migrations ? --------------------------
#
# Comparaison avec un témoin monté par alembic jusqu'à la MÊME révision. C'est ce qui
# répond à « le dump reproduit-il ce que les migrations produisent », question que la
# suite de tests ne pose pas.
# --- 2 bis. remonter la base restaurée jusqu'à la révision du CODE ----------------
#
# Une reprise réelle ne s'arrête pas au dump : elle le restaure PUIS applique les
# migrations, parce que le code qu'on va lancer est à `head`. Sans cette étape, on
# éprouve le code d'aujourd'hui contre le schéma d'hier, et les pages d'un débat
# tombent en 500 — l'ORM y sélectionne des colonnes (`retire_le`, `jeton_contestation`)
# que le dump ne porte pas encore.
#
# Trouvé en lançant ce script après le déploiement du 15 septembre : jusque-là le
# checkout de production était resté en retard, donc son code correspondait par hasard
# au schéma du dump, et le défaut ne pouvait pas se voir.
echo "--- 2bis/5 remontée de la base restaurée jusqu'à head"
if [ -n "$BASE_URL" ]; then
    if (cd "$PROJET" && DATABASE_URL="$BASE_URL/$CIBLE" \
            "$PROJET/.venv/bin/alembic" upgrade head > /dev/null 2>&1); then
        echo "  $REVISION -> $(psql_sur "$CIBLE" "SELECT version_num FROM alembic_version")"
    else
        signaler "les migrations n'ont pas pu être appliquées à la base restaurée"
    fi
else
    sauter "remontée jusqu'à head — chaîne de connexion introuvable"
fi
echo

echo "--- 3/5 conformité du schéma, et l'application qui tourne dessus"
psql_sur postgres "DROP DATABASE IF EXISTS \"$TEMOIN\"" > /dev/null
psql_sur postgres "CREATE DATABASE \"$TEMOIN\"" > /dev/null
if [ -n "$BASE_URL" ]; then
    (cd "$PROJET" && DATABASE_URL="$BASE_URL/$TEMOIN" \
        "$PROJET/.venv/bin/alembic" upgrade head > /dev/null 2>&1) \
        || signaler "le témoin n'a pas pu être monté par alembic"
    for quoi in \
        "colonnes:SELECT table_name||'.'||column_name||':'||data_type||':'||is_nullable FROM information_schema.columns WHERE table_schema='public' ORDER BY 1" \
        "index:SELECT indexname||' :: '||indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1" \
        "contraintes:SELECT conrelid::regclass::text||'.'||conname||':'||contype::text FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY 1"
    do
        nom=${quoi%%:*}; sql=${quoi#*:}
        A=$(psql_sur "$CIBLE" "$sql" | sort | md5sum)
        B=$(psql_sur "$TEMOIN" "$sql" | sort | md5sum)
        if [ "$A" = "$B" ]; then
            echo "  $nom : identiques au schéma des migrations"
        else
            signaler "$nom : ÉCART entre la base restaurée et le schéma des migrations"
        fi
    done
else
    sauter "comparaison de schéma — POSTGRES_PASSWORD introuvable dans $PROJET/.env"
fi
psql_sur postgres "DROP DATABASE IF EXISTS \"$TEMOIN\"" > /dev/null
echo

# --- 4. l'application sert-elle le site depuis la base restaurée ? ----------------
#
# LE POINT QUI COMPTE. On lance l'application du checkout de production contre la base
# restaurée, sur un port à part, et on demande de vraies pages. C'est la répétition de
# la panne — remonter le site à partir d'une sauvegarde.
if [ -n "$BASE_URL" ] && [ -x "$PROJET/.venv/bin/python" ]; then
    echo "--- 4/5 l'application, servie depuis la base restaurée"
    (cd "$PROJET" && DATABASE_URL="$BASE_URL/$CIBLE" PYTHONPATH="$PROJET" \
        "$PROJET/.venv/bin/python" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "$PORT" --log-level warning \
        > /tmp/essai-restauration.log 2>&1) &
    SERVEUR=$!
    for _ in $(seq 40); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null)
        [ "$code" = "200" ] && break
        sleep 0.5
    done

    SLUG=$(psql_sur "$CIBLE" \
        "SELECT slug FROM conversation WHERE moderation_status='approved' AND state='open' ORDER BY id LIMIT 1")
    for chemin in /health /health/db / /debats /api/conversations /manifeste \
                  "/c/$SLUG" "/api/conversations/$SLUG" \
                  "/api/conversations/$SLUG/next-statement" "/c/$SLUG/chiffres"; do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$chemin")
        if [ "$code" = "200" ]; then
            printf "  %s  %s\n" "$code" "$chemin"
        else
            signaler "$chemin a rendu $code"
        fi
    done
    kill "$SERVEUR" 2>/dev/null
    wait "$SERVEUR" 2>/dev/null
    rm -f /tmp/essai-restauration.log
else
    echo "--- 4/5 l'application, servie depuis la base restaurée"
    sauter "l'application n'a pas été éprouvée — venv ou chaîne de connexion manquants"
fi
echo

# --- destruction ------------------------------------------------------------------
echo "--- 5/5 destruction de ce qui a été créé"
# WITH (FORCE) : le serveur d'essai vient d'être arrêté, mais sa connexion peut
# survivre une seconde, et un DROP poli échoue alors en laissant la base derrière.
psql_sur postgres "DROP DATABASE IF EXISTS \"$CIBLE\" WITH (FORCE)" > /dev/null
psql_sur postgres "DROP DATABASE IF EXISTS \"$TEMOIN\" WITH (FORCE)" > /dev/null
docker exec -i "$CONTENEUR" rm -f /tmp/essai-restauration.dump
echo "  bases jetables supprimées, copie du dump effacée"
echo "  bases restantes : $(psql_sur postgres "SELECT string_agg(datname, ', ' ORDER BY datname) FROM pg_database WHERE datname LIKE 'chantierc%'")"
echo

echo "=================================================================="
if [ "$echec" -eq 0 ]; then
    echo " VERDICT : la sauvegarde en est une. Le site se remonte depuis elle."
else
    echo " VERDICT : $echec POINT(S) EN ÉCHEC OU NON VÉRIFIÉ(S)."
    echo " La sauvegarde n'est PAS déclarée bonne : une étape sautée n'est pas une"
    echo " étape réussie."
fi
echo "=================================================================="
exit "$echec"
