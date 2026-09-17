"""L2 — le clivage écrit en base, du passage d'analyse jusqu'aux colonnes.

Le pendant du fichier pur `test_clivage.py` : celui-là éprouve la règle sur des nombres
écrits à la main, celui-ci fait tourner un **vrai** passage d'analyse et va lire ce qui
s'est écrit. C'est le même partage qu'au chantier K entre `test_liens_verification.py`
et `test_liens_passage.py`.

Le peuplement (`_populate`, repris de `test_analysis.py`) fabrique **deux camps
parfaits** : sur chaque proposition, un camp répond oui et l'autre non. C'est le cas
extrême que le chantier L existe pour reconnaître, et il rend les attentes lisibles.
"""

from sqlalchemy import func, select

from app.analysis import pipeline
from app.config import settings
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    StatementStat,
)
from app.services.clivage import MIN_VUES_PAR_GROUPE, N_RETENUES, SEUIL_CLIVANTE
from tests.conftest import open_conversation
from tests.test_analysis import _populate


async def _statistiques_globales(session, run_id: int) -> list[StatementStat]:
    """Les lignes `group_id NULL` : les seules que le chantier L renseigne."""
    return list(
        await session.scalars(
            select(StatementStat).where(
                StatementStat.run_id == run_id, StatementStat.group_id.is_(None)
            )
        )
    )


async def test_a_full_run_writes_both_faces_of_every_statement(
    moderator_client, session_factory
) -> None:
    """Ce que `_persist` jetait à chaque quart d'heure depuis le chantier C.

    Trois colonnes par proposition : le score, et les deux produits bruts dont il est
    tiré. Les bruts sont écrits parce que ce sont les données — c'est eux qui
    permettront de déplacer le seuil sans attendre un nouveau passage.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Clivage"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=10)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        globales = await _statistiques_globales(session, run.id)
        par_groupe = list(
            await session.scalars(
                select(StatementStat).where(
                    StatementStat.run_id == run.id,
                    StatementStat.group_id.is_not(None),
                )
            )
        )

    assert run.status is AnalysisStatus.ok, run.error_text
    assert len(globales) == 8
    assert all(stat.consensus_accord is not None for stat in globales)
    assert all(stat.consensus_desaccord is not None for stat in globales)
    assert all(stat.clivage is not None for stat in globales)
    # Le clivage est une mesure SUR les groupes : elle n'appartient à aucun d'eux, et
    # les lignes de représentativité ne la portent pas.
    assert par_groupe and all(stat.clivage is None for stat in par_groupe)


async def test_the_score_is_the_geometric_mean_of_what_was_stored(
    moderator_client, session_factory
) -> None:
    """Les colonnes se tiennent entre elles, sur des valeurs venues de red-dwarf.

    C'est le seul test qui relie le brut au score sur de vraies données : si la racine
    k-ième disparaissait de `_persist`, ou si `k` cessait d'être le nombre de groupes
    entrés dans le produit, il tomberait.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Racine"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=10)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        globales = await _statistiques_globales(session, run.id)
        k = run.k

    assert k is not None and k >= 2
    for stat in globales:
        attendu = 1.0 - max(stat.consensus_accord, stat.consensus_desaccord) ** (1 / k)
        assert abs(stat.clivage - attendu) < 1e-9


async def test_two_perfect_camps_make_a_divisive_debate(
    moderator_client, session_factory
) -> None:
    """Le débat qui divise, mesuré de bout en bout.

    Cinq votants par camp : le partage parfait vaut alors 0,65, au-dessus du seuil, et
    les huit propositions sont comptées. Le débat est **entièrement** clivant, donc ses
    deux moyennes sont complémentaires — c'est un artefact de ce peuplement synthétique,
    pas une propriété générale (voir le test suivant du fichier pur : un vrai débat peut
    être clivant et consensuel à la fois).
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Deux camps"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=10)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.clivage > SEUIL_CLIVANTE
    assert run.n_clivantes == 8
    assert abs(run.clivage + run.consensus - 1.0) < 1e-9
    assert run.params["clivage"] == {
        "n_notees": 8,
        "seuil": SEUIL_CLIVANTE,
        "n_retenues": N_RETENUES,
        "min_vues_par_groupe": MIN_VUES_PAR_GROUPE,
    }


async def test_a_thin_debate_is_measured_but_not_yet_labelled(
    moderator_client, session_factory
) -> None:
    """Là où le seuil rencontre le lissage, et pourquoi c'est le bon comportement.

    À **trois** votants par camp, un partage pourtant parfait ne vaut que 0,60 : les
    probabilités de red-dwarf sont lissées (`p = (1 + n_v) / (2 + n)`), et à trois votes
    « tous d'accord » ne vaut encore que 4/5. Le score est écrit, il est bien au-dessus
    d'une foule indécise (0,50), mais il n'atteint pas le seuil d'affichage : aucune
    proposition n'est **annoncée** clivante.

    C'est délibéré, et c'est la règle du K0 sous une autre forme : dans le doute, on se
    tait. Un débat de six personnes ne mérite pas encore d'étiquette ; le chiffre se
    remplira avec l'audience.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Débat mince"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=6)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        globales = await _statistiques_globales(session, run.id)

    assert run.status is AnalysisStatus.ok, run.error_text
    assert all(stat.clivage is not None for stat in globales)
    assert 0.59 < run.clivage < 0.61
    assert run.n_clivantes == 0
    assert run.params["clivage"]["n_notees"] == 8


async def test_a_debate_without_a_successful_run_has_no_score_at_all(
    moderator_client, session_factory
) -> None:
    """« Pas encore mesuré » n'est pas « pas clivant » : les colonnes restent NULL.

    C'est l'état de tous les débats du site le jour de la mise en ligne, et c'est ce sur
    quoi le L4 s'appuiera pour ranger les débats sans score plutôt que de les déclarer
    consensuels.
    """
    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=2, title="Trop peu"
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.status is AnalysisStatus.insufficient_data
    assert run.clivage is None
    assert run.consensus is None
    assert run.n_clivantes is None


async def test_the_debate_score_outlives_the_statements_it_came_from(
    moderator_client, session_factory
) -> None:
    """La raison d'être du choix de table : `analysis_run` survit à la purge.

    Le détail par proposition suit le sort du calcul qui l'a produit ; le score du débat,
    lui, reste lisible pour toujours. C'est ce qui interdisait de le poser sur
    `conversation`, où il aurait fallu le maintenir à jour.
    """
    from datetime import datetime, timedelta, timezone

    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Purge du clivage"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=10)
        conversation = await session.get(Conversation, conversation_id)
        vieux = await pipeline.analyse(session, conversation)
        recent = await pipeline.analyse(session, conversation)
        assert vieux.status is AnalysisStatus.ok and recent.status is AnalysisStatus.ok
        score_avant = vieux.clivage
        assert score_avant is not None

        ancien = datetime.now(timezone.utc) - timedelta(
            hours=settings.analysis_history_retention_hours + 24
        )
        vieux.finished_at = ancien - timedelta(hours=1)
        recent.finished_at = ancien
        await session.commit()

        await pipeline.purge_calculs_perimes(session)

        restant = await session.scalar(
            select(func.count(StatementStat.id)).where(
                StatementStat.run_id == vieux.id
            )
        )
        relu = await session.get(AnalysisRun, vieux.id)

    assert restant == 0, "le détail du calcul dépassé n'a pas été purgé"
    assert relu.clivage == score_avant
    assert relu.n_clivantes == 8
