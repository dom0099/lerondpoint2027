"""Les cinq indicateurs d'indépendance d'un afflux de signalements (MOD-5).

Un signalement coordonné vient presque toujours d'un seul côté de la carte des opinions,
et **c'est cela qui se voit** — bien avant le contenu des messages. Ce module produit les
indicateurs qui le montrent.

**Il n'agit sur rien.** Aucune sanction, aucun retrait, aucune pondération, aucun seuil
armé. Il regarde. C'est la même discipline que `detection.analyser()` au MOD-1 : rendre
des signaux, jamais un verdict.

**Pas de score unique, et c'est la décision structurante du module.** Un nombre de 0 à
100 est opaque, invérifiable et impossible à contester : personne ne peut dire pourquoi
il vaut 73 plutôt que 61, donc personne ne peut démontrer qu'il se trompe. On produit
**cinq indicateurs nommés**, chacun avec sa valeur, son seuil et sa phrase — et l'alerte
se déclenche quand **au moins deux** dépassent. Deux plutôt qu'un : chaque indicateur pris
seul a une explication innocente, c'est leur conjonction qui n'en a pas.

**Deux précautions, codées et non commentées :**

  - **le groupe d'opinion n'existe pas toujours.** Quelqu'un qui n'a presque pas voté
    n'est dans aucun groupe. Ce cas se compte à part (`sans_groupe`) et ne se range pas
    d'office dans « un autre groupe » — sans quoi un afflux de comptes neufs, qui ne sont
    dans aucun groupe, paraîtrait parfaitement dispersé ;
  - **les colonnes de contexte sont purgées à 30 jours** (MOD-3a). Quand elles valent
    NULL, l'indicateur qui en dépend répond **« non mesurable »**, jamais zéro. Zéro veut
    dire « mesuré, et rien trouvé » ; non mesurable veut dire « on ne sait pas ». Les
    confondre ferait passer un vieux débat pour parfaitement sain — c'est le pire des
    deux, parce que c'est rassurant.

**Ce qui ne s'affiche jamais publiquement** : ces indicateurs, leurs seuils, l'alerte, et
le fait même qu'un débat soit surveillé. La ligne du projet est de publier la méthode ;
publier le détail d'une défense anti-abus, c'est en publier la notice de contournement.
La règle est publique, les seuils de détection sont privés.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# --- les codes, nommés une fois -------------------------------------------------

DISPERSION = "dispersion_des_groupes"
RAFALE = "rafale"
FRAICHEUR = "fraicheur_des_identites"
CONCENTRATION = "concentration_technique"
NON_VOTANTS = "signalants_non_votants"

#: L'ordre d'affichage. Ce n'est ni l'alphabet ni une fréquence : c'est l'ordre dans
#: lequel on veut les lire quand on cherche à comprendre ce qui arrive — d'abord d'où
#: viennent les gens, ensuite à quelle vitesse, enfin par quel tuyau.
CODES: tuple[str, ...] = (DISPERSION, RAFALE, FRAICHEUR, CONCENTRATION, NON_VOTANTS)

LIBELLES: dict[str, str] = {
    DISPERSION: "Dispersion des groupes",
    RAFALE: "Rafale",
    FRAICHEUR: "Fraîcheur des identités",
    CONCENTRATION: "Concentration technique",
    NON_VOTANTS: "Signalants non votants",
}


# --- les seuils -----------------------------------------------------------------
#
# Réglés sur les trois scénarios de `tests/test_independance.py`, et la marge entre le
# scénario sain et les deux autres est écrite dans le journal. Ils sont ici, ensemble,
# pour qu'on puisse les relire d'un coup — et **ils ne sortent jamais du serveur**.

#: Part du plus gros groupe parmi les signalants QUI ONT un groupe. Au-delà, l'afflux
#: vient d'un seul côté de la carte. 0,80 et non 0,60 : sur un débat à deux groupes, un
#: sujet qui fâche l'un des deux produit naturellement 70 % de signalements du même côté,
#: et ce n'est pas une manœuvre.
SEUIL_DISPERSION = 0.80

#: Nombre minimum de signalants SITUÉS pour que la dispersion veuille dire quelque chose.
#: En dessous, un seul signalant de plus fait passer la part de 50 % à 67 % : on mesurerait
#: du bruit.
MINIMUM_SITUES = 5

#: Signalements de la dernière heure, rapportés au rythme habituel du débat. 5× laisse
#: passer l'emballement normal qui suit un partage sur un réseau social ; au-delà, ce
#: n'est plus une audience qui grandit.
SEUIL_RAFALE = 5.0

#: Heures d'historique exigées avant de parler de rafale. En dessous, le débat n'a pas
#: encore d'habitude, et tout y est inhabituel.
MINIMUM_HEURES_HISTORIQUE = 24.0

#: Part des signalants dont l'identité de participant a moins de 24 h. 0,60 : sur un
#: débat qui vient d'ouvrir, TOUT LE MONDE est neuf, et c'est pourquoi cet indicateur ne
#: se lit jamais seul — d'où la règle des deux indicateurs.
SEUIL_FRAICHEUR = 0.60
FENETRE_FRAICHEUR = timedelta(hours=24)

#: Part du condensé d'adresse — ou de référent — le plus fréquent. 0,50 : un foyer, un
#: bureau ou un réseau mobile partagé produisent légitimement deux ou trois signalements
#: de la même adresse, pas la moitié d'un afflux.
SEUIL_CONCENTRATION = 0.50

#: Part des signalants qui n'ont jamais voté dans le débat visé. C'est le meilleur indice
#: d'un afflux venu de l'extérieur : on ne signale pas une proposition qu'on n'a pas lue,
#: et on lit en votant. 0,70 laisse la place aux lecteurs silencieux, qui existent.
SEUIL_NON_VOTANTS = 0.70

SEUILS: dict[str, float] = {
    DISPERSION: SEUIL_DISPERSION,
    RAFALE: SEUIL_RAFALE,
    FRAICHEUR: SEUIL_FRAICHEUR,
    CONCENTRATION: SEUIL_CONCENTRATION,
    NON_VOTANTS: SEUIL_NON_VOTANTS,
}

#: Combien d'indicateurs doivent dépasser pour qu'une alerte se lève. **Deux, jamais
#: un.** Chaque indicateur pris seul a une explication innocente — un débat qui ouvre,
#: un partage qui marche, un bureau qui lit la même page. C'est leur conjonction qui n'en
#: a pas, et c'est aussi ce qui évite de crier sur un débat vivant.
INDICATEURS_POUR_ALERTE = 2


@dataclass(frozen=True, slots=True)
class SignalantObserve:
    """Ce qu'on sait d'un signalant, et rien de plus.

    Aucune donnée nouvelle n'est collectée par ce lot : tout vient de ce que le MOD-3a
    stocke déjà (`signalement`), de `participant`, et des votes. Les deux condensés sont
    ceux de la table, avec leur `None` quand la purge des 30 jours est passée.
    """

    participant_id: int | None
    #: `stable_group_id` du dernier calcul, ou `None` — **et `None` n'est pas un groupe**.
    groupe: int | None
    identite_creee_le: datetime | None
    a_vote_dans_le_debat: bool
    ip_hachee: str | None
    referent_hache: str | None
    cree_le: datetime


@dataclass(frozen=True, slots=True)
class Indicateur:
    code: str
    #: `None` quand l'indicateur n'est pas mesurable. Jamais 0 dans ce cas.
    valeur: float | None
    seuil: float
    #: Faux quand non mesurable : on ne déclenche pas sur ce qu'on ignore.
    depasse: bool
    #: La phrase qu'on lit sur l'écran. Elle dit le chiffre ET pourquoi il manque.
    detail: str

    @property
    def mesurable(self) -> bool:
        return self.valeur is not None

    @property
    def libelle(self) -> str:
        return LIBELLES[self.code]


@dataclass(frozen=True, slots=True)
class Analyse:
    indicateurs: tuple[Indicateur, ...]
    signalants: int

    @property
    def depassements(self) -> tuple[Indicateur, ...]:
        return tuple(i for i in self.indicateurs if i.depasse)

    @property
    def alerte(self) -> bool:
        """Au moins deux indicateurs au-delà de leur seuil."""
        return len(self.depassements) >= INDICATEURS_POUR_ALERTE

    @property
    def non_mesurables(self) -> tuple[Indicateur, ...]:
        return tuple(i for i in self.indicateurs if not i.mesurable)

    def par_code(self, code: str) -> Indicateur:
        return next(i for i in self.indicateurs if i.code == code)


def _non_mesurable(code: str, pourquoi: str) -> Indicateur:
    """Un indicateur qu'on ne sait pas calculer. **Valeur `None`, jamais 0.**"""
    return Indicateur(
        code=code, valeur=None, seuil=SEUILS[code], depasse=False, detail=pourquoi
    )


def _pourcent(part: float) -> str:
    return f"{part * 100:.0f} %".replace(".", ",")


# --- les cinq indicateurs, un par fonction --------------------------------------


def dispersion_des_groupes(signalants: list[SignalantObserve]) -> Indicateur:
    """Part du plus gros groupe d'opinion parmi les signalants **situés**.

    Les signalants sans groupe sont comptés à part et **exclus du dénominateur** : les
    ranger dans « un autre groupe » ferait passer un afflux de comptes neufs — qui ne
    sont dans aucun groupe — pour une assemblée parfaitement dispersée, c'est-à-dire pour
    l'inverse de ce qu'il est.
    """
    situes = [s for s in signalants if s.groupe is not None]
    sans_groupe = len(signalants) - len(situes)
    if len(situes) < MINIMUM_SITUES:
        return _non_mesurable(
            DISPERSION,
            f"{len(situes)} signalant(s) situé(s) sur {len(signalants)} — il en faut "
            f"{MINIMUM_SITUES} pour que la part d'un groupe veuille dire quelque chose"
            + (f" ; {sans_groupe} sans groupe" if sans_groupe else ""),
        )

    comptes: dict[int, int] = {}
    for signalant in situes:
        comptes[signalant.groupe] = comptes.get(signalant.groupe, 0) + 1
    part = max(comptes.values()) / len(situes)
    detail = (
        f"{len(comptes)} groupe(s) distinct(s) parmi {len(situes)} signalant(s) situé(s) ; "
        f"le plus gros en porte {_pourcent(part)}"
    )
    if sans_groupe:
        detail += f" ; {sans_groupe} signalant(s) sans groupe, comptés à part"
    return Indicateur(DISPERSION, part, SEUIL_DISPERSION, part > SEUIL_DISPERSION, detail)


def rafale(
    signalants: list[SignalantObserve],
    *,
    heures_observees: float | None,
    maintenant: datetime,
) -> Indicateur:
    """Signalements de la dernière heure, rapportés au rythme habituel du débat.

    **Le rythme de référence exclut la dernière heure**, et c'est tout l'intérêt de cet
    indicateur. Une première version prenait la médiane des heures ACTIVES, y compris
    celle en cours : une rafale de douze signalements en une heure définissait alors
    elle-même sa propre référence à six par heure, et sortait à ×2 — c'est-à-dire
    invisible. Le défaut ne s'était pas vu en test, parce que le test *fournissait* le
    rythme au lieu de le faire calculer ; il est apparu au premier essai sur une vraie
    base. Le rythme se calcule donc sur ce qui précède, jamais sur ce qu'on mesure.

    **Les heures creuses comptent.** Le dénominateur est le temps écoulé depuis
    l'ouverture du débat, pas le nombre d'heures où il s'est passé quelque chose : un
    débat qui reçoit un signalement par mois a un rythme de 0,001/h, et douze en une
    heure y est un événement. Ne compter que les heures actives revenait à dire que ce
    débat fait « un par heure », ce qui est faux 99 % du temps.

    Non mesurable sous 24 h d'historique : un débat qui vient d'ouvrir n'a pas d'habitude.
    """
    if heures_observees is None or heures_observees < MINIMUM_HEURES_HISTORIQUE:
        return _non_mesurable(
            RAFALE,
            "moins de 24 h d'historique sur ce débat — il n'a pas encore d'habitude à "
            "laquelle comparer la dernière heure",
        )
    derniere_heure = sum(
        1 for s in signalants if maintenant - s.cree_le <= timedelta(hours=1)
    )
    anciens = len(signalants) - derniere_heure
    # Plancher : « comme s'il y en avait eu UN » sur toute la période observée. Sans lui,
    # un débat qui n'a jamais rien reçu donne un rythme de zéro et une division
    # impossible — alors que c'est précisément le cas le plus parlant, celui du débat
    # tranquille sur lequel douze signalements tombent d'un coup.
    rythme = max(anciens / heures_observees, 1.0 / heures_observees)
    rapport = derniere_heure / rythme
    # Quand il n'y avait RIEN auparavant, le multiplicateur se compte en milliers — il
    # est exact, puisqu'il rapporte à un plancher fictif, mais « ×2 737 » ne se lit pas.
    # La phrase dit alors ce qui s'est réellement passé ; la valeur, elle, reste celle
    # qui se compare au seuil.
    if anciens == 0:
        detail = (
            f"{derniere_heure} dans la dernière heure ; **aucun auparavant** sur "
            f"{heures_observees:.0f} h"
        )
    else:
        detail = (
            f"{derniere_heure} dans la dernière heure ; {anciens} sur les "
            f"{heures_observees:.0f} h précédentes, soit {rythme:.3f}/h — "
            f"×{rapport:.1f}".replace(".", ",")
        )
    return Indicateur(RAFALE, rapport, SEUIL_RAFALE, rapport > SEUIL_RAFALE, detail)


def fraicheur_des_identites(
    signalants: list[SignalantObserve], *, maintenant: datetime
) -> Indicateur:
    """Part des signalants dont l'identité de participant a moins de 24 heures."""
    connues = [s for s in signalants if s.identite_creee_le is not None]
    if not connues:
        return _non_mesurable(
            FRAICHEUR, "aucune date de création d'identité connue pour ces signalants"
        )
    fraiches = sum(
        1 for s in connues if maintenant - s.identite_creee_le < FENETRE_FRAICHEUR
    )
    part = fraiches / len(connues)
    return Indicateur(
        FRAICHEUR,
        part,
        SEUIL_FRAICHEUR,
        part > SEUIL_FRAICHEUR,
        f"{fraiches} identité(s) de moins de 24 h sur {len(connues)} — {_pourcent(part)}",
    )


def concentration_technique(signalants: list[SignalantObserve]) -> Indicateur:
    """Concentration des condensés d'adresse et de référent.

    **Non mesurable après la purge des 30 jours**, et c'est le cas qui compte : les deux
    colonnes passent alors à NULL, et rendre 0 ferait passer un vieux débat pour
    parfaitement propre. On dit qu'on ne sait pas.
    """
    adresses = [s.ip_hachee for s in signalants if s.ip_hachee]
    referents = [s.referent_hache for s in signalants if s.referent_hache]
    if not adresses and not referents:
        return _non_mesurable(
            CONCENTRATION,
            "contexte purgé ou absent — les condensés d'adresse et de référent sont "
            "effacés au bout de 30 jours (MOD-3a), la concentration n'est pas calculable",
        )

    def part_du_plus_frequent(valeurs: list[str]) -> tuple[float, int]:
        comptes: dict[str, int] = {}
        for valeur in valeurs:
            comptes[valeur] = comptes.get(valeur, 0) + 1
        plus = max(comptes.values())
        return plus / len(valeurs), plus

    morceaux, parts = [], []
    if adresses:
        part, combien = part_du_plus_frequent(adresses)
        parts.append(part)
        morceaux.append(
            f"{combien}/{len(adresses)} depuis la même adresse ({_pourcent(part)})"
        )
    if referents:
        part, combien = part_du_plus_frequent(referents)
        parts.append(part)
        morceaux.append(
            f"{combien}/{len(referents)} depuis le même référent externe "
            f"({_pourcent(part)})"
        )
    # La PLUS FORTE des deux concentrations : un afflux qui passe par un seul lien
    # partagé se voit sur le référent même si les adresses sont toutes différentes.
    part = max(parts)
    return Indicateur(
        CONCENTRATION,
        part,
        SEUIL_CONCENTRATION,
        part > SEUIL_CONCENTRATION,
        " ; ".join(morceaux),
    )


def signalants_non_votants(signalants: list[SignalantObserve]) -> Indicateur:
    """Part des signalants qui n'ont jamais voté dans le débat visé.

    Le meilleur indice d'un afflux venu de l'extérieur : sur ce site, on lit en votant.
    Quelqu'un qui signale une proposition d'un débat où il n'a jamais voté ne l'a
    probablement pas rencontrée en lisant le débat.
    """
    if not signalants:
        return _non_mesurable(NON_VOTANTS, "aucun signalant")
    non_votants = sum(1 for s in signalants if not s.a_vote_dans_le_debat)
    part = non_votants / len(signalants)
    return Indicateur(
        NON_VOTANTS,
        part,
        SEUIL_NON_VOTANTS,
        part > SEUIL_NON_VOTANTS,
        f"{non_votants} signalant(s) sur {len(signalants)} n'ont jamais voté dans ce "
        f"débat — {_pourcent(part)}",
    )


def analyser(
    signalants: list[SignalantObserve],
    *,
    heures_observees: float | None,
    maintenant: datetime,
) -> Analyse:
    """Les cinq indicateurs, et l'alerte s'il y en a une. **Pure, sans entrée/sortie.**

    L'alerte se lève à deux indicateurs dépassés, jamais un. Un indicateur non mesurable
    ne dépasse pas : on ne déclenche pas sur ce qu'on ignore, et on ne rassure pas non
    plus — l'écran affiche séparément ce qui n'a pas pu être calculé.
    """
    return Analyse(
        indicateurs=(
            dispersion_des_groupes(signalants),
            rafale(
                signalants,
                heures_observees=heures_observees,
                maintenant=maintenant,
            ),
            fraicheur_des_identites(signalants, maintenant=maintenant),
            concentration_technique(signalants),
            signalants_non_votants(signalants),
        ),
        signalants=len(signalants),
    )
