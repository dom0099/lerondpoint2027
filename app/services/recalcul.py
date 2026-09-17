"""La cadence du calcul des groupes, en un seul endroit.

Trois pages annoncent cette cadence (l'accueil, la page d'un débat, et depuis le G9 le
message de fin de parcours), et le worker l'applique. Écrite quatre fois à la main,
elle devient fausse dès que `analysis_interval_seconds` change — et personne ne pense à
relire un gabarit quand il touche à une variable d'environnement.

**Le calage sur l'horloge, et pourquoi il est nécessaire.** Le worker dormait
`analysis_interval_seconds` secondes *après* chaque passage : la cadence dérivait de la
durée du passage. Mesuré sur la production avant le G9, les calculs horaires tombaient
à 20:59:23, 21:59:23, 22:59:24, 23:59:24 — un quart de seconde de retard par tour, et
un décalage qui n'a aucune raison de se stabiliser. Tant qu'on ne faisait qu'annoncer
« toutes les heures », c'était sans conséquence. Annoncer « dans environ 12 minutes »
demande en revanche de savoir *quand* a lieu le prochain calcul, ce qu'une dérive rend
impossible. Les passages sont donc calés sur une grille absolue, à partir de l'époque
Unix : à 600 secondes, les calculs ont lieu à 0, 10, 20, 30, 40 et 50 minutes de chaque
heure.

**Et ce qui se compte en tours vit ici aussi.** Un réglage exprimé en nombre de tours
change de durée à chaque changement de cadence, sans qu'aucun test ne rougisse : c'est
le défaut que le G9 avait trouvé sur la fenêtre d'oubli des couleurs, et que le E0 a
retrouvé sur le lisseur du nombre de groupes. La règle retenue est qu'un réglage dont on
attend une DURÉE est écrit en secondes et converti ici, à un seul endroit.
"""

from datetime import datetime, timedelta, timezone

from app.config import settings


def periode_de_recalcul() -> str:
    """Phrase disant à quelle fréquence les groupes sont recalculés."""
    secondes = settings.analysis_interval_seconds
    if secondes % 3600 == 0:
        heures = secondes // 3600
        return "toutes les heures" if heures == 1 else f"toutes les {heures} heures"
    if secondes % 60 == 0:
        minutes = secondes // 60
        return "toutes les minutes" if minutes == 1 else f"toutes les {minutes} minutes"
    return f"toutes les {secondes} secondes"


def prochain_calcul(maintenant: datetime | None = None) -> datetime:
    """Instant du prochain passage du worker, calé sur la grille absolue.

    Strictement postérieur à `maintenant` : sur la seconde exacte d'un passage, c'est le
    passage SUIVANT qui est annoncé. Annoncer « dans 0 seconde » à quelqu'un dont le
    calcul vient de commencer serait faux dans l'autre sens — ses votes n'y sont pas.
    """
    maintenant = maintenant or datetime.now(timezone.utc)
    periode = max(1, settings.analysis_interval_seconds)
    epoch = maintenant.timestamp()
    prochain = (int(epoch) // periode + 1) * periode
    return datetime.fromtimestamp(prochain, tz=timezone.utc)


def delai_avant_calcul(maintenant: datetime | None = None) -> timedelta:
    """Temps restant avant le prochain passage."""
    maintenant = maintenant or datetime.now(timezone.utc)
    return prochain_calcul(maintenant) - maintenant


# Le temps restant N'EST PAS mis en français ici, et c'est délibéré. Le seul endroit
# qui l'affiche est le message de fin de parcours, où il doit se rafraîchir tant que la
# page reste ouverte : c'est donc le navigateur qui l'écrit, à partir de la date rendue
# par `prochain_calcul`. Une seconde formulation côté serveur, même juste le jour où on
# l'écrit, finirait par annoncer un autre délai que celle qui est réellement affichée.


def tours_de_lissage() -> int:
    """Tours consécutifs d'accord exigés par le lisseur du nombre de groupes.

    Dérivé de `analysis_k_buffer_seconds` — une durée — et de la cadence courante,
    pour que l'attente avant qu'un groupe nouveau paraisse ne dépende pas du réglage
    de fréquence. Voir la note de `app/config.py` sur les deux faces de ce réglage.

    **Le plancher est à deux tours, et il n'est pas décoratif.** À un seul tour, la
    condition d'élection du lisseur (`compte >= tours`) est vraie dès la première
    mesure : le lissage n'est pas raccourci, il est *désactivé*, et le k brut revient
    à l'écran — exactement le défaut mesuré au D0, où la silhouette varie de 2 à 5 sur
    les mêmes données. Une durée plus courte que la cadence produirait ce cas sans
    prévenir (à 3600 s de cadence, une cible d'une heure donne un tour). Le plancher
    fait qu'un mauvais réglage rend le lisseur trop lent — visible — plutôt que muet.
    """
    periode = max(1, settings.analysis_interval_seconds)
    return max(2, round(settings.analysis_k_buffer_seconds / periode))
