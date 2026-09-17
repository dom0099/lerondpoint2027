"""Carte d'une conversation : positions individuelles et contours de groupe.

Lecture seule, additive : rien ici ne recalcule quoi que ce soit ni n'écrit en base.
Tout est lu dans **un seul run** — le dernier abouti (`groups.latest_ok_run`).

**Pourquoi un seul run, et jamais deux.** Le repère de la projection PCA se déplace dès
que les données bougent : le D0 a mesuré jusqu'à 113 % du rayon du nuage entre deux
calculs, et 47 % de résidu même après alignement optimal. Deux positions issues de deux
runs ne sont donc pas dans le même espace — les superposer dessinerait un mouvement qui
n'a pas eu lieu. Une enveloppe calculée sur le run N n'a aucun sens autour des points du
run N+1. Toutes les lectures de ce module filtrent donc sur le même `run_id`, et celui-ci
est renvoyé au client pour que la contrainte reste vérifiable de bout en bout.

**Ce qui n'est jamais exposé.** Les positions des participants sous le seuil de votes.
Elles existent en base (voir `ParticipantProjection`), mais ce sont des extrapolations :
les propositions non votées sont imputées par la moyenne, si bien qu'avec un vote sur
dix on ressort à une position qui ne dit rien de l'intéressé — et souvent loin du nuage.
Les afficher donnerait à voir une opinion que personne n'a exprimée. Cela vaut aussi
pour le visiteur lui-même : il reçoit un état, pas des coordonnées.

**Les identifiants de participants ne sont pas exposés non plus.** Le tableau des
positions est une nuée de points anonymes ; seul le visiteur reçoit *sa* position, et
seulement la sienne. Rendre la carte pointable participant par participant ferait d'une
opinion agrégée un fichier d'opinions individuelles.
"""

from dataclasses import dataclass, field

import numpy as np
from concave_hull import concave_hull_indexes
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.matching import group_name
from app.analysis.thresholds import min_user_votes_for
from app.config import settings
from app.models import (
    AnalysisRun,
    Conversation,
    Participant,
    ParticipantProjection,
    Statement,
    Vote,
)
from app.services.groups import latest_ok_run

#: En dessous, aucune enveloppe : deux points ne délimitent pas une surface, et un
#: point encore moins. red-dwarf saute ces groupes de la même façon
#: (`data_presenter.generate_figure`, avec un « TODO: Accomodate 2 points like Polis
#: platform does » qui dit bien que ce n'est pas résolu chez eux non plus).
MIN_POINTS_ENVELOPPE = 3


@dataclass
class Enveloppe:
    """Un groupe et son contour. `sommets` vaut None quand il n'y en a pas."""

    name: str
    size: int
    sommets: list[list[float]] | None = None
    #: Identité stable de C6. C'est elle qui porte la couleur, pas le rang d'effectif
    #: ni le nom affiché — voir `carte_rendu.attribution_des_couleurs`.
    stable_id: int | None = None


@dataclass
class Visiteur:
    """Où se trouve la personne qui regarde — ou pourquoi elle ne s'y trouve pas.

    Trois états, et ils ne se confondent pas :

    - ``situe`` : elle est dans le calcul, avec une position et un groupe ;
    - ``sous_le_seuil`` : au moment du calcul, elle n'avait pas assez de votes pour
      entrer dans l'analyse. Sa position n'existe pas, ou n'est pas montrable ;
    - ``en_attente_de_calcul`` : elle a **maintenant** assez de votes, mais ils ont été
      émis après le dernier calcul. Le prochain la situera.

    Le troisième cas est le seul « gris » réel, et il est temporel, pas géométrique
    (voir le journal D4).
    """

    etat: str
    raison: str | None = None
    x: float | None = None
    y: float | None = None
    groupe: str | None = None


@dataclass
class Carte:
    run_id: int
    computed_at: str | None
    #: Points anonymes, uniquement ceux à qui un groupe a été assigné.
    positions: list[dict] = field(default_factory=list)
    groupes: list[Enveloppe] = field(default_factory=list)
    visiteur: Visiteur | None = None


def enveloppe_concave(points: np.ndarray) -> list[list[float]] | None:
    """Sommets du contour concave, ou None s'il n'y en a pas.

    La concavité est un **réglage** (`settings.hull_concavity`), pas une constante :
    red-dwarf la fige à 4.0 dans son code de tracé, mais elle décide à quel point le
    contour épouse les creux d'un groupe. C'est une décision d'affichage, qui devra
    pouvoir être ajustée en D5 sans toucher au code.

    Rend None plutôt que de lever : un groupe trop petit ou dégénéré (tous les points
    confondus, tous alignés) est un cas normal, pas une panne. La carte doit sortir
    quand même, sans contour pour ce groupe-là.

    ATTENTION — le compte des points DISTINCTS n'est pas une précaution de style.
    `concave_hull` est une extension C++ : sur un tableau dont tous les points sont
    confondus, elle ne lève pas, elle **provoque une erreur de segmentation** (constaté :
    code de sortie 139). Un `try` ne rattrape pas un SIGSEGV — c'est le processus entier
    qui tomberait, sur une route publique en lecture. Le seul remède est de ne jamais
    l'appeler sur une telle entrée. Trois points confondus, c'est exactement un groupe
    dont les membres ont voté à l'identique : ce n'est pas un cas de laboratoire.

    Les autres dégénérescences, elles, sont sûres et rendent simplement un contour
    inutilisable, qu'on écarte ensuite : trois points alignés rendent 2 sommets, des
    doublons partiels passent sans incident.
    """
    if len(points) < MIN_POINTS_ENVELOPPE:
        return None
    distincts = len(np.unique(points, axis=0))
    if distincts < MIN_POINTS_ENVELOPPE:
        return None
    try:
        indices = concave_hull_indexes(points, concavity=settings.hull_concavity)
    except Exception:  # noqa: BLE001 — un contour manquant vaut mieux qu'une carte en panne
        return None
    # Moins de 3 sommets : des points alignés, donc une surface plate. Rien à tracer.
    if indices is None or len(indices) < MIN_POINTS_ENVELOPPE:
        return None
    return [[float(x), float(y)] for x, y in points[list(indices)]]


async def _votes_emis(
    session: AsyncSession, conversation: Conversation, participant: Participant
) -> int:
    return await session.scalar(
        select(func.count(Vote.id))
        .join(Statement, Statement.id == Vote.statement_id)
        .where(
            Vote.participant_id == participant.id,
            Statement.conversation_id == conversation.id,
        )
    )


async def _visiteur(
    session: AsyncSession,
    conversation: Conversation,
    participant: Participant,
    run: AnalysisRun,
) -> Visiteur:
    ligne = (
        await session.execute(
            select(
                ParticipantProjection.x,
                ParticipantProjection.y,
                ParticipantProjection.stable_group_id,
            ).where(
                ParticipantProjection.run_id == run.id,
                ParticipantProjection.participant_id == participant.id,
            )
        )
    ).first()

    if ligne is not None and ligne.stable_group_id is not None:
        return Visiteur(
            etat="situe",
            x=float(ligne.x),
            y=float(ligne.y),
            groupe=group_name(ligne.stable_group_id),
        )

    # Pas de groupe dans ce calcul. Reste à dire pourquoi, et les deux raisons
    # n'appellent pas la même conduite : l'une demande de voter davantage, l'autre
    # seulement d'attendre.
    seuil = await min_user_votes_for(session, conversation)
    emis = await _votes_emis(session, conversation, participant)
    if emis >= seuil:
        return Visiteur(
            etat="en_attente_de_calcul",
            raison=(
                "Vos votes ont été émis après le dernier calcul. "
                "Le prochain, qui a lieu toutes les heures, vous situera."
            ),
        )
    return Visiteur(
        etat="sous_le_seuil",
        raison=f"Il faut {seuil} votes pour être situé ; vous en avez émis {emis}.",
    )



async def carte(
    session: AsyncSession,
    conversation: Conversation,
    participant: Participant | None = None,
) -> Carte | None:
    """Carte du dernier calcul abouti, ou None si aucun n'a encore abouti."""
    run = await latest_ok_run(session, conversation)
    if run is None:
        return None

    lignes = (
        await session.execute(
            select(
                ParticipantProjection.x,
                ParticipantProjection.y,
                ParticipantProjection.stable_group_id,
            ).where(
                ParticipantProjection.run_id == run.id,
                # Le filtre qui compte : sans groupe assigné, la position ne sort pas.
                ParticipantProjection.stable_group_id.isnot(None),
            )
        )
    ).all()

    par_groupe: dict[int, list[list[float]]] = {}
    positions: list[dict] = []
    for x, y, stable in lignes:
        point = [float(x), float(y)]
        par_groupe.setdefault(stable, []).append(point)
        positions.append({"x": point[0], "y": point[1], "groupe": group_name(stable)})

    groupes = [
        Enveloppe(
            name=group_name(stable),
            size=len(points),
            sommets=enveloppe_concave(np.array(points, dtype=float)),
            stable_id=stable,
        )
        for stable, points in sorted(par_groupe.items())
    ]

    return Carte(
        run_id=run.id,
        computed_at=run.finished_at.isoformat() if run.finished_at else None,
        positions=positions,
        groupes=groupes,
        visiteur=(
            await _visiteur(session, conversation, participant, run)
            if participant is not None
            else None
        ),
    )
