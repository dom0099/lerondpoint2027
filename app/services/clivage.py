"""Ce que « clivant » veut dire, en un nombre — et son revers, le consensus (L1).

**Ce module ne parle à personne.** Pas de base, pas de passage d'analyse, pas de
réseau : des nombres entrent, des nombres sortent. C'est délibéré, et c'est ce qui
permet d'éprouver la définition — la seule partie difficile du chantier L — sur des cas
écrits à la main, avant qu'une migration ou un écran n'en dépende.

D'où viennent les nombres qui entrent
=====================================
`red-dwarf` calcule déjà, à chaque passage d'analyse, le *group-aware consensus* : pour
chaque proposition, le **produit sur les groupes de la probabilité que le groupe soit
d'accord**, et le même produit pour le désaccord (`reddwarf/utils/stats.py`,
`calculate_comment_statistics_dataframes`, colonnes `group-aware-consensus-agree` et
`…-disagree`). `app/analysis/staged.py` les reçoit déjà ; `_persist` les jette
aujourd'hui. Le L2 cessera de les jeter. Ce module se contente de dire quoi en faire.

Les trois règles, dans l'ordre où elles s'appliquent
===================================================
1. **La moyenne géométrique.** Ces consensus sont des produits, un facteur par groupe.
   Un débat à cinq groupes multiplie cinq nombres inférieurs à 1, un débat à deux en
   multiplie deux : le premier obtient mécaniquement un consensus plus bas, donc un
   clivage plus haut, **sans être plus clivé** — juste plus fragmenté. La racine
   k-ième ramène le nombre à « la probabilité d'accord typique d'un groupe », qui se
   compare d'un débat à l'autre. Sans elle, trier les débats par clivage revient à les
   trier par nombre de groupes.

2. **Le plancher.** Quatre participants répartis en deux groupes de deux donnent des
   « 100 % pour / 0 % pour » qui ne veulent rien dire. Une proposition qu'un groupe n'a
   pas assez vue n'obtient **pas de score** — et non un score de zéro. L'absence
   d'information ne se note pas comme une information (même règle qu'au K0 pour le
   verdict `non_concluant`).

3. **L'agrégation.** Le score d'un débat n'est pas la moyenne de ses propositions : la
   plupart sont anodines, et elles noieraient les trois qui divisent vraiment. On prend
   la moyenne des `N_RETENUES` plus clivantes — « ce débat contient-il des points qui
   divisent ? », et non « ce débat est-il divisé en moyenne ? ». Symétriquement, la
   moyenne des `N_RETENUES` plus consensuelles répond à « ce débat a-t-il dégagé des
   accords ? », qui est la phrase même de la page d'accueil.

Une proposition porte donc DEUX nombres complémentaires, `clivage` et `consensus`, dont
la somme fait 1. C'est voulu : c'est la même mesure lue par les deux bouts, et le site
a besoin des deux bouts. Un débat, lui, porte deux moyennes **indépendantes** — celle du
haut du classement et celle du bas — qui ne se déduisent pas l'une de l'autre.

Ce que ces nombres ne sont pas
==============================
- **Ce n'est pas une note.** Un débat très clivant n'est ni meilleur ni pire qu'un débat
  consensuel. Le chiffre décrit, il ne juge pas.
- **Ce n'est pas de l'hostilité.** Deux groupes en désaccord sur les 80 km/h ne sont pas
  en conflit. Le vocabulaire d'affichage s'en tiendra au désaccord constaté.
- **Ce n'est pas un score à montrer.** Le continu sert à trier et à ranger ; ce qui
  s'affiche est une phrase (« Ce qui vous sépare ») ou un entier (« 4 propositions
  divisent les groupes »). Voir le L0.

Le lissage de Laplace, et pourquoi les bornes ne sont jamais atteintes
=====================================================================
`red-dwarf` calcule ses probabilités avec un pseudo-compte : `p = (1 + n_v) / (2 + n)`.
Un groupe unanime sur quatre votes donne donc `p = 5/6`, pas 1. **Sur données réelles, un
clivage ne vaut donc jamais exactement 0 ni exactement 1**, et il dépend un peu du
nombre de votes. Les cas limites ci-dessous (0 et 1) sont ceux d'entrées idéales, telles
que les tests les écrivent ; les seuils, eux, sont réglés sur ce que les vraies valeurs
produisent.
"""

import enum
import math
from dataclasses import dataclass
from collections.abc import Sequence

#: Combien de propositions entrent dans la moyenne d'un débat, à chaque bout du
#: classement. Réglage, pas invariant : cinq points de fracture (ou cinq accords) sont
#: ce qu'un lecteur peut retenir d'un débat, et ce qu'un écran de fin de parcours peut
#: montrer sans devenir une liste. Un débat qui compte moins de propositions notées
#: prend la moyenne de ce qu'il a.
N_RETENUES = 5

#: Au-dessus, une proposition est comptée comme clivante — c'est cet entier-là qui
#: s'affiche, jamais le score continu.
#:
#: **Pourquoi 0,6 et pas 0,5.** Une foule indécise, où chaque groupe se partage à
#: 50/50, donne des probabilités voisines de 0,5 dans les deux sens, donc un clivage
#: voisin de 0,5. Ce n'est pas un clivage, c'est du bruit : deux camps constitués qui
#: répondent l'inverse l'un de l'autre montent nettement plus haut. Le seuil doit donc
#: passer au-dessus du bruit. Réglage à revoir une fois de vrais débats mesurés — c'est
#: la première chose que le L2 permettra d'observer.
SEUIL_CLIVANTE = 0.6

#: Combien de votes il faut, DANS CHAQUE GROUPE, pour qu'une proposition soit notée.
#: Aligné sur `groups.MIN_GROUP_SIZE` : un groupe de deux personnes dont une seule a vu
#: la proposition n'exprime pas une position de groupe, il exprime un avis isolé.
MIN_VUES_PAR_GROUPE = 2

#: Combien de calculs consécutifs doivent compter au moins une proposition clivante
#: avant que l'étiquette « Clivant » paraisse sur une carte (L4).
#:
#: **Pourquoi deux et non un.** Une étiquette posée sur le dernier calcul seul
#: apparaîtrait et disparaîtrait entre deux visites, sur un débat qui oscille autour du
#: seuil — et une étiquette qui clignote est pire qu'une étiquette absente. C'est la
#: règle du K0 sur les liens morts, « il faut deux échecs, pas un », transposée : dans
#: le doute, on se tait.
#:
#: **Ce réglage se compte en CALCULS, et il y reste — décision du chantier E0.** Le
#: passage de la cadence à 10 minutes a fait convertir en durée le lisseur du nombre de
#: groupes, dont on attendait une attente en minutes ; celui-ci n'est pas de la même
#: nature. Un anti-rebond n'attend pas qu'un délai passe, il attend qu'une mesure soit
#: CONFIRMÉE par une autre : ce que deux calculs consécutifs éliminent, c'est une
#: oscillation d'un tour sur deux, et cette propriété ne dépend pas de la durée qui les
#: sépare. Le convertir en durée ne l'aurait pas rendu plus juste, seulement plus
#: bavard. Conséquence assumée : l'étiquette paraît plus vite qu'avant (20 minutes au
#: lieu de 30), ce qui est le comportement voulu, pas une dérive.
#:
#: La pose demande deux calculs, le retrait un seul. L'asymétrie est voulue et elle
#: penche du même côté qu'au K0 : le côté prudent est celui qui n'affirme rien.
CALCULS_POUR_ETIQUETTE = 2

#: En dessous, la question n'a pas de sens. Le clivage retenu au L0 est le désaccord
#: **entre groupes** : à un seul groupe, il n'y a rien entre quoi que ce soit. Un débat
#: à un groupe n'a pas un clivage nul, il n'a pas de clivage.
MIN_GROUPES = 2


class Sens(str, enum.Enum):
    """De quel côté penche le consensus d'une proposition, quand il y en a un.

    Sert à l'affichage : « tous les groupes l'approuvent » et « tous les groupes la
    rejettent » sont deux accords, et le second n'est pas un clivage.
    """

    accord = "accord"
    desaccord = "desaccord"


@dataclass(frozen=True)
class Score:
    """Ce qu'une proposition vaut, une fois le plancher franchi.

    `clivage` et `consensus` font 1 à eux deux — voir l'en-tête du module.
    """

    #: 0 = les groupes s'accordent, 1 = aucun accord ne se forme, dans aucun sens.
    clivage: float
    #: La moyenne géométrique retenue : « la probabilité typique qu'un groupe adopte la
    #: position majoritaire ».
    consensus: float
    #: Le sens de ce consensus. Il reste renseigné même quand le consensus est faible :
    #: c'est le moins mauvais des deux, et l'écran de fin de parcours en a besoin pour
    #: dire « pour » ou « contre ».
    sens: Sens

    @property
    def clivante(self) -> bool:
        """Au-dessus du seuil d'affichage — c'est ce qui se compte sur une carte."""
        return self.clivage > SEUIL_CLIVANTE


@dataclass(frozen=True)
class ScoreDebat:
    """Ce qu'un débat vaut, dérivé des propositions qui ont un score.

    Les deux moyennes sont indépendantes : `clivage` regarde le haut du classement,
    `consensus` le bas. Sur un débat de moins de `2 × N_RETENUES` propositions notées
    elles portent en partie sur les mêmes propositions — c'est sans conséquence, chacune
    répond à sa propre question.
    """

    #: Moyenne des `N_RETENUES` propositions les plus clivantes.
    clivage: float
    #: Moyenne des `N_RETENUES` propositions les plus consensuelles.
    consensus: float
    #: Combien de propositions dépassent `SEUIL_CLIVANTE`. Le seul nombre affichable.
    n_clivantes: int
    #: Sur combien de propositions notées le tout est calculé — pour le journal et pour
    #: l'audit d'un résultat contesté, jamais pour l'affichage.
    n_notees: int


def _probabilite(valeur: float | None) -> float | None:
    """Ramène une entrée à une probabilité utilisable, ou à rien du tout.

    Trois cas rendent `None`, et aucun ne rend 0 : la valeur absente, la valeur `NaN`,
    la valeur hors [0, 1]. Le `NaN` n'est pas théorique — `staged.py` aligne `gac_df`
    (indexé sur la matrice **brute**) et `propositions_df` (indexé sur la matrice
    **filtrée**) par un `concat(axis=1)` : une proposition présente dans l'une et pas
    dans l'autre reçoit `NaN`. Le prendre pour un zéro donnerait un clivage maximal à
    une proposition sur laquelle on ne sait rien, et la placerait en tête du tri.
    """
    if valeur is None:
        return None
    valeur = float(valeur)
    if math.isnan(valeur) or math.isinf(valeur):
        return None
    if not 0.0 <= valeur <= 1.0:
        return None
    return valeur


def merite_un_score(vues_par_groupe: Sequence[int]) -> bool:
    """La règle du plancher : cette proposition a-t-elle été assez vue pour être notée ?

    `vues_par_groupe` compte, groupe par groupe, les votes exprimés sur la proposition
    (la colonne `ns` de red-dwarf). **Il faut y trouver exactement les groupes qui sont
    entrés dans le produit du consensus** : c'est leur nombre qui donne l'exposant de la
    moyenne géométrique, et un décompte partiel fausserait les deux.

    Le plancher porte sur **chaque** groupe, pas sur le total : une proposition vue
    trente fois dans un groupe et zéro fois dans l'autre ne dit rien du désaccord entre
    les deux — elle dit seulement qu'un groupe a voté.
    """
    if len(vues_par_groupe) < MIN_GROUPES:
        return False
    return all(vues >= MIN_VUES_PAR_GROUPE for vues in vues_par_groupe)


def score_proposition(
    consensus_accord: float | None,
    consensus_desaccord: float | None,
    vues_par_groupe: Sequence[int],
) -> Score | None:
    """Le score d'une proposition, ou `None` quand elle n'en mérite pas.

    `consensus_accord` et `consensus_desaccord` sont les deux produits calculés par
    red-dwarf, tels quels : la racine k-ième est prise ici, pas avant.

    Rendre `None` n'est pas un échec — c'est le cas courant sur un débat jeune, et c'est
    la réponse juste. Un appelant qui en ferait un zéro annulerait la règle du plancher.
    """
    if not merite_un_score(vues_par_groupe):
        return None

    accord = _probabilite(consensus_accord)
    desaccord = _probabilite(consensus_desaccord)
    if accord is None or desaccord is None:
        return None

    k = len(vues_par_groupe)
    moyenne_accord = accord ** (1 / k)
    moyenne_desaccord = desaccord ** (1 / k)

    # Le consensus retenu est le plus fort des deux : « tous les groupes l'approuvent »
    # et « tous les groupes la rejettent » sont deux accords. Le clivage est ce qui
    # reste quand AUCUN des deux ne se forme.
    if moyenne_accord >= moyenne_desaccord:
        consensus, sens = moyenne_accord, Sens.accord
    else:
        consensus, sens = moyenne_desaccord, Sens.desaccord

    return Score(clivage=1.0 - consensus, consensus=consensus, sens=sens)


def sens_de(
    consensus_accord: float | None, consensus_desaccord: float | None
) -> Sens | None:
    """De quel côté penche le consensus d'une proposition déjà notée (L3).

    Prend les deux **produits bruts** relus en base, et non le score : la racine k-ième
    étant croissante et l'exposant le même pour les deux, comparer les bruts donne
    exactement le même verdict que comparer les moyennes géométriques.

    Ce n'est pas la brèche que la règle du L2 interdit. Elle interdit de **recalculer un
    score** à la lecture, ce qui contournerait le plancher ; connaître la direction d'un
    score déjà accordé n'est pas la même chose. Un affichage a besoin de dire « les
    groupes l'approuvent » plutôt que « les groupes la rejettent », et ces deux phrases
    portent le même consensus.
    """
    accord = _probabilite(consensus_accord)
    desaccord = _probabilite(consensus_desaccord)
    if accord is None or desaccord is None:
        return None
    return Sens.accord if accord >= desaccord else Sens.desaccord


def agrege(scores: Sequence[Score]) -> ScoreDebat | None:
    """Les deux moyennes d'un débat et son compte de propositions clivantes.

    Rend `None` quand aucune proposition n'a de score — c'est l'état de tous les débats
    du site à ce jour, et il doit rester distinct d'un score nul : « pas encore mesuré »
    n'est pas « pas clivant ». Les écrans du L4 s'appuient là-dessus pour ranger les
    débats sans score après les autres plutôt que de les déclarer consensuels.
    """
    if not scores:
        return None

    clivages = sorted((score.clivage for score in scores), reverse=True)
    hautes = clivages[:N_RETENUES]
    # Les plus consensuelles sont les moins clivantes : même classement, autre bout.
    basses = clivages[-N_RETENUES:]

    return ScoreDebat(
        clivage=sum(hautes) / len(hautes),
        consensus=sum(1.0 - clivage for clivage in basses) / len(basses),
        n_clivantes=sum(1 for score in scores if score.clivante),
        n_notees=len(scores),
    )


def porte_etiquette(comptes: Sequence[int | None]) -> bool:
    """L'étiquette « Clivant » est-elle méritée, vu les derniers calculs du débat ?

    `comptes` donne le `n_clivantes` des calculs aboutis du débat, **du plus récent au
    plus ancien**. Un `None` est un calcul qui n'a rien mesuré (trop peu de groupes,
    plancher jamais franchi) : ce n'est pas un zéro, mais pour l'étiquette les deux
    empêchent de la poser — on n'annonce pas ce qu'on n'a pas mesuré.

    **L'étiquette repose sur l'entier affiché, jamais sur le score continu du débat.**
    C'est la conséquence directe de la règle laissée par le L3 : aucun texte ne doit
    pouvoir être démenti par le chiffre imprimé à côté de lui. Une étiquette accrochée
    au score moyen dirait « Clivant » sur une carte qui, deux lignes plus bas, ne
    compte aucune proposition clivante — et l'inverse, car le score d'un débat est une
    moyenne quand le compte est un seuil. En la faisant reposer sur le compte lui-même,
    le mot et le nombre ne peuvent plus se contredire.
    """
    recents = list(comptes[:CALCULS_POUR_ETIQUETTE])
    if len(recents) < CALCULS_POUR_ETIQUETTE:
        return False
    return all(compte is not None and compte >= 1 for compte in recents)
