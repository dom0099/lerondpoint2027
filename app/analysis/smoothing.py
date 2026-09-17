"""Lisseur du nombre de groupes affiché.

Le D0 a mesuré que le k choisi par silhouette **varie de 2 à 5 entre répliques du même
jeu de données** : c'est l'arbitrage lui-même qui est instable, pas seulement les
étiquettes. Afficher ce k brut ferait apparaître et disparaître des groupes sans qu'aucun
avis n'ait changé.

On reprend donc le `group-k-smoother` de Pol.is : un k candidat doit être élu plusieurs
fois **consécutivement** avant de remplacer le k affiché. La différence avec Pol.is est
que notre candidat n'est pas un calcul à froid — il sort déjà de la couche intermédiaire,
donc il est moins bruité en entrée.

Le nombre de tours est un réglage, pas un invariant, et depuis le chantier E0 il n'est
plus réglé directement : il se DÉRIVE d'une durée (`analysis_k_buffer_seconds`, une
heure) et de la cadence du worker, via `recalcul.tours_de_lissage()`. Pol.is recalcule à
la seconde, donc ses 4 tours ne coûtent rien ; nous recalculions à l'heure quand ce
module a été écrit, et 4 tours y valaient jusqu'à 4 heures d'attente avant qu'un groupe
réellement nouveau n'apparaisse.

C'est cette attente-là qu'on tient constante, et non le nombre de tours : à trois tours
figés, elle valait 3 heures au rythme horaire, 45 minutes au quart d'heure et n'aurait
plus valu que 30 minutes à 10 minutes. Dérivée d'une heure, elle donne 6 tours à la
cadence actuelle — assez pour éteindre une oscillation d'un tour sur deux, assez peu
pour qu'un changement réel se voie dans l'heure.

`tours` reste un paramètre de `lisse` plutôt qu'une lecture de la configuration : ce
module ne connaît ni le worker ni la cadence, et c'est ce qui le rend éprouvable tour
par tour.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EtatLisseur:
    """Les trois compteurs, persistés sur la conversation."""

    dernier_k: int | None = None
    compte: int = 0
    k_affiche: int | None = None

    @classmethod
    def depuis_conversation(cls, conversation) -> "EtatLisseur":
        return cls(
            dernier_k=conversation.smoother_last_k,
            compte=conversation.smoother_last_k_count or 0,
            k_affiche=conversation.smoother_smoothed_k,
        )


def lisse(etat: EtatLisseur, candidat: int, disponibles: set[int], tours: int) -> EtatLisseur:
    """Décide du k à afficher.

    Args:
        etat: les compteurs du tour précédent.
        candidat: le k que la silhouette retient pour ce tour.
        disponibles: les k réellement calculés ce tour-ci.
        tours: nombre de tours consécutifs d'accord exigés.

    La soupape de fin est celle de Pol.is : si le k mémorisé n'existe plus parmi les
    candidats de ce tour — la couche a rétréci, ou `max_k` a bougé — on retombe sur le
    candidat du moment plutôt que d'afficher un groupe qui n'a pas été calculé.
    """
    identique = etat.dernier_k is not None and candidat == etat.dernier_k
    compte = etat.compte + 1 if identique else 1

    if compte >= tours or etat.k_affiche is None:
        k_affiche = candidat
    else:
        k_affiche = etat.k_affiche

    if k_affiche not in disponibles:
        k_affiche = candidat

    return EtatLisseur(dernier_k=candidat, compte=compte, k_affiche=k_affiche)
