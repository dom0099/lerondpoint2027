#!/bin/sh
# Renouvelle les certificats Let's Encrypt du site (aucun effet sur une lignée à plus de
# 30 jours de son expiration), puis fait relire les fichiers à nginx.
#
# Lancé deux fois par semaine par le timer systemd chantier-c-certbot (deploy/systemd/).
# Remplace ~/polis/bin/certbot-renew.sh depuis le rattachement de nginx à cette pile
# (17/09/2026).
#
# `reload` et non `restart` : nginx relit sa configuration et ses certificats sans couper
# une seule connexion. S'il échoue (configuration invalide), l'ancienne reste en service
# et le code de sortie non nul fait échouer l'unité systemd — visible dans
# `systemctl status chantier-c-certbot`.
set -e

cd "$(dirname "$0")/.."

docker compose --profile tls run --rm certbot \
  renew --webroot -w /var/www/certbot --quiet

docker compose exec -T nginx nginx -s reload
