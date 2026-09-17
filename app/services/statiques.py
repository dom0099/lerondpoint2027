"""Adresses versionnées des fichiers statiques, et leur durée de cache.

Le problème corrigé ici est invisible côté serveur. `styles.css` et `detection.js`
étaient liés par une adresse fixe et servis sans `Cache-Control` : le navigateur
appliquait alors sa propre heuristique (une fraction de l'âge du fichier), ce qui
peut garder une copie plusieurs jours. Après une mise en ligne, un visiteur déjà
venu voyait donc les nouvelles pages avec l'ancienne feuille, ou les anciennes
phrases du détecteur — sans qu'aucun journal ne le signale. C'est ce qui avait été
noté à l'instruction 20 du chantier I2.

La correction tient en deux gestes qui vont ensemble :

1. **L'adresse porte une empreinte du contenu** (`/static/styles.css?v=1a2b3c4d`).
   Le fichier change, l'adresse change, le navigateur redemande. Tant que le
   contenu ne bouge pas, l'adresse ne bouge pas non plus : on ne casse pas le cache
   à chaque déploiement, seulement quand le fichier est réellement différent.
2. **Une adresse qui porte une empreinte peut être mise en cache un an** (voir
   `StatiquesEnCache` dans `app/main.py`). C'est le corollaire du premier point :
   sans empreinte on n'ose pas cacher, avec empreinte on n'a plus de raison de
   s'en priver.

Pourquoi une empreinte du CONTENU et pas un numéro de version de l'application :
le numéro changerait à chaque déploiement, y compris ceux qui ne touchent pas au
CSS, et ferait retélécharger 90 ko pour rien.
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Là où `main.py` monte `/static`. Les deux doivent désigner le même dossier ;
#: si l'un bouge, l'autre aussi.
DOSSIER = Path("app/static")

#: Longueur de l'empreinte retenue. Huit caractères hexadécimaux, soit quatre
#: milliards de valeurs : largement assez pour distinguer les versions successives
#: d'un même fichier, et court à lire dans le source d'une page.
LONGUEUR_EMPREINTE = 8

#: Empreintes déjà calculées : chemin -> (signature du fichier, empreinte).
#:
#: La signature est le couple (date de modification, taille) renvoyé par `stat`.
#: Elle sert de clé de fraîcheur : tant qu'elle ne change pas, on ne relit pas les
#: 90 ko de la feuille de style à chaque page.
#:
#: Pourquoi vérifier à chaque appel plutôt que calculer une fois au démarrage —
#: parce que le conteneur `api` monte `./app` en volume. Un fichier statique
#: modifié sans redémarrage est donc un cas RÉEL sur cette machine, et une
#: empreinte figée au démarrage servirait alors une adresse périmée : le défaut
#: qu'on cherche précisément à supprimer. Un `stat` par appel est le prix de cette
#: garantie.
_empreintes: dict[str, tuple[tuple[int, int], str]] = {}


def empreinte(chemin: str) -> str | None:
    """Empreinte du contenu de `app/static/<chemin>`, ou None si illisible."""
    fichier = DOSSIER / chemin
    try:
        etat = fichier.stat()
    except OSError:
        # Fichier absent ou illisible : on ne fait pas tomber la page pour une
        # question de cache. L'appelant rendra l'adresse sans empreinte, et le
        # navigateur recevra le 404 qu'il aurait reçu de toute façon.
        logger.warning("Fichier statique introuvable, servi sans empreinte : %s", fichier)
        return None

    signature = (etat.st_mtime_ns, etat.st_size)
    connu = _empreintes.get(chemin)
    if connu is not None and connu[0] == signature:
        return connu[1]

    valeur = hashlib.sha256(fichier.read_bytes()).hexdigest()[:LONGUEUR_EMPREINTE]
    _empreintes[chemin] = (signature, valeur)
    return valeur


def url_statique(chemin: str) -> str:
    """Adresse publique d'un fichier statique, empreinte comprise.

    Appelée depuis les gabarits : `{{ url_statique('styles.css') }}`.
    """
    valeur = empreinte(chemin)
    if valeur is None:
        return f"/static/{chemin}"
    return f"/static/{chemin}?v={valeur}"


def enregistrer_globals(templates) -> None:
    """Rend `url_statique` disponible dans les gabarits d'une instance Jinja.

    À appeler sur CHAQUE `Jinja2Templates` du projet — il y en a trois, et
    `public/base.html`, qui porte le lien de la feuille de style, est rendue par
    au moins deux d'entre elles (`public.py` pour les pages ordinaires,
    `main.py` pour `indisponible.html` quand la base ne répond pas). Une instance
    oubliée ne se verrait pas aux tests des pages courantes : elle donnerait une
    erreur Jinja sur la page d'indisponibilité, c'est-à-dire le jour où tout va
    déjà mal.
    """
    templates.env.globals["url_statique"] = url_statique
