"""Les indicateurs d'indépendance, et les trois scénarios qui les règlent (MOD-5).

**La production n'a encore aucun signalement** — rien n'est déployé. La preuve de ce lot
ne peut donc pas venir des données réelles : elle vient de ces scénarios, construits à la
main. C'est assumé, et c'est la raison d'être du lot — le jour où une tempête arrive, on
ne veut pas commencer à écrire l'outil qui la mesure.

Trois scénarios, et le premier est le plus important :

  1. **un débat normal et animé** — beaucoup de signalements, mais dispersés, étalés, par
     des gens qui votent. **Zéro alerte est une condition de livraison** : un indicateur
     qui crie sur un débat vivant sera débranché en une semaine, et c'est ainsi qu'on se
     retrouve sans défense le jour où il y en a besoin ;
  2. **un afflux d'un seul groupe** — même volume, un seul côté de la carte ;
  3. **un import extérieur** — identités fraîches, référent commun, aucun vote préalable.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import independance
from app.services.independance import (
    CONCENTRATION,
    DISPERSION,
    FRAICHEUR,
    NON_VOTANTS,
    RAFALE,
    SignalantObserve,
)

MAINTENANT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
#: Historique disponible : trois jours, soit largement au-delà des 24 h exigées.
#:
#: C'est une HAUTEUR D'HISTORIQUE et non un rythme, depuis la correction du 15 septembre :
#: le rythme se calcule à partir des signalements eux-mêmes, dernière heure exclue. Le
#: fournir tout fait — ce que faisait la première version de ce fichier — masquait
#: justement le défaut où une rafale définissait sa propre référence.
HEURES = 72.0


def _signalant(
    *,
    groupe=None,
    age_identite=timedelta(days=60),
    a_vote=True,
    ip="ip-unique",
    referent=None,
    il_y_a=timedelta(days=1),
) -> SignalantObserve:
    return SignalantObserve(
        participant_id=1,
        groupe=groupe,
        identite_creee_le=MAINTENANT - age_identite,
        a_vote_dans_le_debat=a_vote,
        ip_hachee=ip,
        referent_hache=referent,
        cree_le=MAINTENANT - il_y_a,
    )


def _analyser(signalants, heures=HEURES):
    return independance.analyser(
        signalants, heures_observees=heures, maintenant=MAINTENANT
    )


# --- scénario 1 : un débat normal et animé ------------------------------------------


def debat_anime() -> list[SignalantObserve]:
    """Douze signalements sur un débat qui marche : trois groupes représentés, étalés sur
    plusieurs jours, par des participants installés qui votent, depuis douze adresses."""
    repartition = [0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, None]
    return [
        _signalant(
            groupe=groupe,
            ip=f"adresse-{rang}",
            il_y_a=timedelta(hours=6 * rang + 2),
            age_identite=timedelta(days=30 + rang),
        )
        for rang, groupe in enumerate(repartition)
    ]


def test_scenario_1_un_debat_anime_ne_declenche_aucune_alerte() -> None:
    """**Condition de livraison.** Zéro faux positif sur un débat vivant."""
    analyse = _analyser(debat_anime())

    assert analyse.alerte is False
    assert analyse.depassements == ()


def test_scenario_1_aucun_indicateur_ne_s_approche_de_son_seuil() -> None:
    """La marge, pas seulement le verdict : un scénario sain qui frôlerait les seuils
    serait un réglage qui a l'air de marcher."""
    analyse = _analyser(debat_anime())

    marges = {}
    for indicateur in analyse.indicateurs:
        if indicateur.mesurable:
            marges[indicateur.code] = indicateur.seuil - indicateur.valeur
    # Tous mesurables sauf aucun : le débat animé a de quoi calculer les cinq.
    assert set(marges) == set(independance.CODES), analyse.non_mesurables
    # Et chacun reste sous son seuil, avec de la marge.
    for code, marge in marges.items():
        assert marge > 0, f"{code} touche son seuil sur un débat sain"


# --- scénario 2 : un afflux d'un seul groupe ----------------------------------------


def afflux_d_un_groupe() -> list[SignalantObserve]:
    """Même volume, un seul côté de la carte, et resserré dans l'heure. Des participants
    réels et installés : c'est ce qui rend ce cas difficile et ce qui le distingue du
    scénario 3."""
    return [
        _signalant(
            groupe=1,
            ip=f"adresse-{rang}",
            il_y_a=timedelta(minutes=5 * rang),
            age_identite=timedelta(days=40 + rang),
        )
        for rang in range(12)
    ]


def test_scenario_2_un_afflux_d_un_seul_groupe_declenche_une_alerte() -> None:
    analyse = _analyser(afflux_d_un_groupe())

    assert analyse.alerte is True
    codes = {i.code for i in analyse.depassements}
    assert DISPERSION in codes
    assert RAFALE in codes


def test_scenario_2_ne_declenche_pas_les_indicateurs_d_import() -> None:
    """Ce sont de vrais participants du débat : ni identités fraîches, ni adresses
    concentrées, ni non-votants. C'est ce qui rend ce scénario distinct du troisième —
    et c'est pourquoi l'alerte à deux indicateurs est le bon réglage."""
    analyse = _analyser(afflux_d_un_groupe())

    codes = {i.code for i in analyse.depassements}
    assert FRAICHEUR not in codes
    assert CONCENTRATION not in codes
    assert NON_VOTANTS not in codes


# --- scénario 3 : un import extérieur -----------------------------------------------


def import_exterieur() -> list[SignalantObserve]:
    """Identités fraîches, référent commun, aucun vote préalable dans le débat — donc
    aucun groupe d'opinion, puisqu'on n'est situé qu'en votant."""
    return [
        _signalant(
            groupe=None,
            age_identite=timedelta(minutes=20 + rang),
            a_vote=False,
            ip=f"adresse-{rang}",
            referent="referent-partage",
            il_y_a=timedelta(minutes=2 * rang),
        )
        for rang in range(12)
    ]


def test_scenario_3_un_import_exterieur_declenche_le_plus_franchement() -> None:
    """« C'est le scénario qui doit déclencher le plus franchement. »"""
    analyse = _analyser(import_exterieur())

    assert analyse.alerte is True
    codes = {i.code for i in analyse.depassements}
    assert {RAFALE, FRAICHEUR, CONCENTRATION, NON_VOTANTS} <= codes
    assert len(analyse.depassements) > len(_analyser(afflux_d_un_groupe()).depassements)


def test_scenario_3_la_dispersion_n_est_pas_mesurable_et_ne_ment_pas() -> None:
    """**La précaution qui compte.** Aucun signalant n'est situé : la dispersion est
    NON MESURABLE, et surtout pas « parfaitement dispersée ». Les ranger d'office dans
    « un autre groupe » ferait passer cet afflux pour le plus sain des trois."""
    analyse = _analyser(import_exterieur())
    dispersion = analyse.par_code(DISPERSION)

    assert dispersion.mesurable is False
    assert dispersion.valeur is None
    assert dispersion.depasse is False
    assert "sans groupe" in dispersion.detail


# --- la marge entre les scénarios ----------------------------------------------------


def test_la_marge_entre_le_sain_et_les_deux_autres() -> None:
    """Le chiffre qui dit si le réglage tient, plutôt que le seul verdict."""
    sain = _analyser(debat_anime())
    groupe = _analyser(afflux_d_un_groupe())
    exterieur = _analyser(import_exterieur())

    assert len(sain.depassements) == 0
    assert len(groupe.depassements) == 2
    assert len(exterieur.depassements) == 4
    # Sur l'indicateur qui sépare le mieux les scénarios 1 et 2 : 36 % contre 100 %,
    # pour un seuil à 80 %.
    assert sain.par_code(DISPERSION).valeur < 0.5
    assert groupe.par_code(DISPERSION).valeur == 1.0


# --- « non mesurable » n'est pas zéro ------------------------------------------------


def test_le_contexte_purge_rend_la_concentration_non_mesurable() -> None:
    """**Exigé par la consigne.** Les colonnes de contexte sont purgées à 30 jours : quand
    elles valent NULL, l'indicateur doit dire « non mesurable » et non « zéro ».

    Confondre les deux ferait passer un vieux débat pour parfaitement sain — c'est le pire
    des deux, parce que c'est rassurant.
    """
    purges = [
        _signalant(groupe=rang % 3, ip=None, referent=None) for rang in range(12)
    ]

    indicateur = _analyser(purges).par_code(CONCENTRATION)

    assert indicateur.mesurable is False
    assert indicateur.valeur is None
    assert indicateur.valeur != 0
    assert indicateur.depasse is False
    assert "purgé" in indicateur.detail


def test_un_indicateur_non_mesurable_ne_compte_jamais_comme_un_depassement() -> None:
    """On ne déclenche pas sur ce qu'on ignore."""
    purges = [_signalant(groupe=1, ip=None, referent=None) for _ in range(12)]

    analyse = _analyser(purges)

    assert all(i.mesurable for i in analyse.depassements)
    assert CONCENTRATION not in {i.code for i in analyse.depassements}


def test_un_debat_entierement_purge_n_est_pas_declare_sain() -> None:
    """Le cas qui motive toute la précaution : trente jours plus tard, on ne sait plus.
    L'écran doit pouvoir le dire, donc l'analyse doit distinguer « rien trouvé » de
    « rien su »."""
    purges = [
        SignalantObserve(
            participant_id=None,
            groupe=None,
            identite_creee_le=None,
            a_vote_dans_le_debat=True,
            ip_hachee=None,
            referent_hache=None,
            cree_le=MAINTENANT - timedelta(days=40),
        )
        for _ in range(8)
    ]

    analyse = _analyser(purges, heures=None)

    assert analyse.alerte is False
    # Trois indicateurs sur cinq sont muets, et l'écran le saura.
    assert len(analyse.non_mesurables) == 4
    assert {i.code for i in analyse.non_mesurables} == {
        DISPERSION,
        RAFALE,
        FRAICHEUR,
        CONCENTRATION,
    }


# --- le contrat du module -------------------------------------------------------------


def test_il_n_existe_aucun_score_unique() -> None:
    """**Le contrat à protéger.** Un nombre de 0 à 100 est opaque, invérifiable et
    impossible à contester. Cinq indicateurs nommés, et rien qui les résume."""
    analyse = _analyser(debat_anime())

    champs = set(type(analyse).__dataclass_fields__)
    assert champs == {"indicateurs", "signalants"}
    for interdit in ("score", "note", "total", "gravite", "risque"):
        assert not any(interdit in champ for champ in champs)
    assert len(analyse.indicateurs) == 5


def test_une_alerte_demande_deux_indicateurs_jamais_un() -> None:
    """Chaque indicateur pris seul a une explication innocente : un débat qui ouvre, un
    partage qui marche, un bureau qui lit la même page."""
    un_seul = [
        _signalant(groupe=1, ip=f"adresse-{rang}", il_y_a=timedelta(days=3))
        for rang in range(12)
    ]

    analyse = _analyser(un_seul)

    assert len(analyse.depassements) == 1
    assert analyse.par_code(DISPERSION).depasse is True
    assert analyse.alerte is False


def test_aucun_signalant_ne_leve_aucune_alerte() -> None:
    analyse = _analyser([], heures=None)

    assert analyse.alerte is False
    assert analyse.signalants == 0


@pytest.mark.parametrize("code", list(independance.CODES))
def test_chaque_indicateur_porte_son_seuil_et_une_phrase(code) -> None:
    """L'écran doit pouvoir expliquer, pas seulement colorer."""
    indicateur = _analyser(debat_anime()).par_code(code)

    assert indicateur.seuil == independance.SEUILS[code]
    assert indicateur.detail
    assert indicateur.libelle
