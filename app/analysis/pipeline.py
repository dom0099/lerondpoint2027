"""Analyse d'opinion par red-dwarf.

Le calcul est bloquant (pandas, scikit-learn) : il ne tourne jamais dans le processus
API. Il vit dans le conteneur `worker`, et même là il est exécuté dans un thread pour
ne pas figer la boucle d'événements.

**Aucune inversion de signe.** Les votes sont stockés en `agree = +1`, qui est
exactement ce que red-dwarf attend : l'extraction ci-dessous passe `Vote.value` tel
quel. C'était l'objet de la convention fixée à C1 — le chantier B avait montré que
l'inversion de Pol.is produit des groupes en miroir sans qu'aucune erreur ne le
signale.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.analysis.matching import match_groups
from app.analysis.smoothing import EtatLisseur
from app.analysis.staged import analyse_par_couches
from app.analysis.thresholds import effective_min_user_votes
from app.config import settings
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    BaseCluster,
    BaseClusterMember,
    Conversation,
    ConversationState,
    ModerationStatus,
    ParticipantProjection,
    Statement,
    StatementStat,
    Vote,
)
from app.services import clivage, nommage, recalcul

logger = logging.getLogger(__name__)

#: Espace de noms du verrou consultatif qui sérialise les analyses (chantier D).
#: Arbitraire, mais stable : deux processus doivent choisir le même.
VERROU_ANALYSE = 4242


def _approved_statements_of(conversation: Conversation):
    return select(Statement.id).where(
        Statement.conversation_id == conversation.id,
        Statement.moderation_status == ModerationStatus.approved,
    )


async def extract_votes(
    session: AsyncSession, conversation: Conversation
) -> list[dict]:
    """Votes de la conversation, dans la forme attendue par red-dwarf.

    Seules les propositions approuvées entrent : une proposition rejetée après coup
    ne doit plus peser sur l'analyse.
    """
    rows = await session.execute(
        select(Vote.participant_id, Vote.statement_id, Vote.value, Vote.modified_at)
        .where(Vote.statement_id.in_(_approved_statements_of(conversation)))
        .order_by(Vote.participant_id, Vote.statement_id)
    )
    return [
        {
            "participant_id": row.participant_id,
            "statement_id": row.statement_id,
            "vote": row.value,  # aucune conversion : déjà la convention red-dwarf
            "modified": int(row.modified_at.timestamp() * 1000),
        }
        for row in rows
    ]


async def eligible_participant_count(
    session: AsyncSession, conversation: Conversation, threshold: int
) -> int:
    """Participants ayant assez de votes pour ne pas être écartés par red-dwarf.

    Le seuil est passé en argument, pas relu de la configuration : il dépend de la
    conversation (voir app/analysis/thresholds.py), et un décalage entre le seuil
    compté ici et celui passé à red-dwarf donnerait un « insuffisant » sur une
    conversation analysable, ou l'inverse.
    """
    counted = (
        select(Vote.participant_id, func.count(Vote.id).label("n"))
        .where(Vote.statement_id.in_(_approved_statements_of(conversation)))
        .group_by(Vote.participant_id)
        .having(func.count(Vote.id) >= threshold)
        .subquery()
    )
    return await session.scalar(select(func.count()).select_from(counted))


async def meta_statement_ids(
    session: AsyncSession, conversation: Conversation
) -> list[int]:
    rows = await session.scalars(
        select(Statement.id).where(
            Statement.conversation_id == conversation.id,
            Statement.moderation_status == ModerationStatus.approved,
            Statement.is_meta.is_(True),
        )
    )
    return list(rows)


def _run_reddwarf(
    votes: list[dict],
    meta_ids: list[int],
    force_k: int | None,
    threshold: int,
    appartenances: dict[int, set[int]] | None,
    groupes: dict[int, set[int]] | None,
    etat_lisseur: EtatLisseur,
):
    """Appel bloquant, isolé pour être exécuté dans un thread.

    Depuis le chantier D, ce n'est plus `run_pipeline` d'un bloc : la projection vient
    de red-dwarf, le regroupement passe par notre couche intermédiaire, et les
    statistiques repartent chez red-dwarf. Voir app/analysis/staged.py, qui documente
    la part de plomberie reprise et la version épinglée.
    """
    return analyse_par_couches(
        votes=votes,
        meta_statement_ids=meta_ids,
        mod_out_statement_ids=[],
        min_user_vote_threshold=threshold,
        force_group_count=force_k,
        random_state=settings.analysis_random_state,
        appartenances_precedentes=appartenances,
        groupes_precedents=groupes,
        etat_lisseur=etat_lisseur,
        tours_lisseur=recalcul.tours_de_lissage(),
    )


async def vide_couche(session: AsyncSession, conversation: Conversation) -> None:
    """Efface le cache de la couche intermédiaire pour cette conversation."""
    await session.execute(
        delete(BaseCluster).where(BaseCluster.conversation_id == conversation.id)
    )
    conversation.group_partition = None


async def charge_couche(
    session: AsyncSession, conversation: Conversation
) -> tuple[dict[int, set[int]] | None, dict[int, set[int]] | None]:
    """Relit la couche du tour précédent, ou renonce.

    Cet état est un cache, pas une source de vérité : au moindre signe d'incohérence
    on le vide et on repart à froid ce tour-ci. Propager une couche corrompue
    fausserait tous les recalculs suivants en silence, ce qui est bien pire qu'un
    unique tour sans continuité.
    """
    seaux = (
        await session.execute(
            select(BaseCluster.id, BaseCluster.bucket_id).where(
                BaseCluster.conversation_id == conversation.id
            )
        )
    ).all()
    if not seaux:
        return None, _lit_partition(conversation)

    membres = (
        await session.execute(
            select(BaseClusterMember.base_cluster_id, BaseClusterMember.participant_id)
            .join(BaseCluster, BaseCluster.id == BaseClusterMember.base_cluster_id)
            .where(BaseCluster.conversation_id == conversation.id)
        )
    ).all()

    par_id = {ident: bucket_id for ident, bucket_id in seaux}
    appartenances: dict[int, set[int]] = {}
    deja_vus: set[int] = set()
    for base_cluster_id, participant_id in membres:
        bucket_id = par_id.get(base_cluster_id)
        # Un participant dans deux seaux : la couche ne partitionne plus rien.
        if bucket_id is None or participant_id in deja_vus:
            logger.warning(
                "couche incohérente pour %s : cache vidé, tour à froid", conversation.slug
            )
            await vide_couche(session, conversation)
            return None, None
        deja_vus.add(participant_id)
        appartenances.setdefault(bucket_id, set()).add(participant_id)

    # Un seau sans membre n'a pas de sens : il ne peut plus être recentré.
    if len(appartenances) != len(par_id):
        logger.warning(
            "seau vide pour %s : cache vidé, tour à froid", conversation.slug
        )
        await vide_couche(session, conversation)
        return None, None

    return appartenances, _lit_partition(conversation)


def _lit_partition(conversation: Conversation) -> dict[int, set[int]] | None:
    """Découpage groupe du tour précédent, ou None s'il est absent ou douteux."""
    brut = conversation.group_partition
    if not brut:
        return None
    partition: dict[int, set[int]] = {}
    vus: set[int] = set()
    for etiquette, participants in brut.items():
        try:
            groupe = {int(p) for p in participants}
        except (TypeError, ValueError):
            return None
        if groupe & vus:  # un participant dans deux groupes
            return None
        vus |= groupe
        partition[int(etiquette)] = groupe
    return partition or None


async def enregistre_couche(
    session: AsyncSession, conversation: Conversation, result
) -> None:
    """Réécrit le cache : seaux, appartenances, découpage groupe, compteurs."""
    await session.execute(
        delete(BaseCluster).where(BaseCluster.conversation_id == conversation.id)
    )
    await session.flush()
    for bucket_id, membres in result.appartenances.items():
        centre = result.centres.get(bucket_id)
        seau = BaseCluster(
            conversation_id=conversation.id,
            bucket_id=int(bucket_id),
            center_x=float(centre[0]) if centre is not None else 0.0,
            center_y=float(centre[1]) if centre is not None else 0.0,
        )
        session.add(seau)
        await session.flush()
        for participant_id in sorted(membres):
            session.add(
                BaseClusterMember(
                    base_cluster_id=seau.id, participant_id=int(participant_id)
                )
            )

    conversation.group_partition = {
        str(etiquette): sorted(int(p) for p in membres)
        for etiquette, membres in result.groupes.items()
    }
    etat = result.etat_lisseur
    conversation.smoother_last_k = etat.dernier_k
    conversation.smoother_last_k_count = etat.compte
    conversation.smoother_smoothed_k = etat.k_affiche


async def analyse(session: AsyncSession, conversation: Conversation) -> AnalysisRun:
    """Exécute une analyse et enregistre le résultat.

    Le `run` est écrit quoi qu'il arrive — succès, données insuffisantes ou erreur —
    afin qu'un modérateur puisse toujours savoir pourquoi l'affichage ne bouge pas.
    """
    votes = await extract_votes(session, conversation)
    n_statements = await session.scalar(
        select(func.count()).select_from(_approved_statements_of(conversation).subquery())
    )
    # Le seuil dépend de la taille de la conversation : sur trois propositions, exiger
    # les 7 votes de red-dwarf rendrait l'analyse impossible par construction.
    threshold = effective_min_user_votes(conversation, n_statements)
    eligible = await eligible_participant_count(session, conversation, threshold)

    run = AnalysisRun(
        conversation_id=conversation.id,
        status=AnalysisStatus.running,
        n_votes=len(votes),
        n_statements=n_statements,
        n_participants=eligible,
        params={
            "random_state": settings.analysis_random_state,
            "min_user_vote_threshold": threshold,
            "force_group_count": conversation.force_group_count,
        },
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    # Capturés maintenant : un `rollback()` expire TOUS les objets de la session,
    # `expire_on_commit=False` ou non. Relire le moindre attribut déclenche alors un
    # chargement paresseux — synchrone, donc impossible ici (`MissingGreenlet`) — et
    # cela au beau milieu du traitement d'erreur, où la panne d'origine serait masquée
    # par celle-là. Mesuré : le journal d'échec lisait `conversation.slug`.
    run_id = run.id
    slug = conversation.slug

    if eligible < settings.analysis_min_participants or n_statements < 2:
        run.status = AnalysisStatus.insufficient_data
        run.finished_at = datetime.now(timezone.utc)
        run.error_text = (
            f"{eligible} participant(s) avec au moins "
            f"{threshold} votes et {n_statements} proposition(s) : "
            f"il en faut au moins {settings.analysis_min_participants} et 2."
        )
        await session.commit()
        return run

    # Un seul calcul à la fois par conversation. Depuis le chantier D, l'analyse ne
    # fait plus qu'ajouter des lignes : elle réécrit un état (la couche intermédiaire).
    # Deux calculs simultanés — le passage horaire et un déclenchement manuel — se
    # marcheraient dessus. Le second échoue proprement plutôt que d'attendre : mieux
    # vaut un run en erreur explicite qu'un état à moitié réécrit.
    #
    # Verrou CONSULTATIF, et non `SELECT … FOR UPDATE` sur la conversation : un verrou
    # de ligne bloque aussi toute insertion qui référence cette ligne par clé
    # étrangère — à commencer par l'`analysis_run` de l'appel concurrent lui-même, qui
    # attendait alors indéfiniment au lieu d'échouer. Mesuré en écrivant le test.
    verrou = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:ns, :id)"),
        {"ns": VERROU_ANALYSE, "id": conversation.id},
    )
    if not verrou:
        run.status = AnalysisStatus.error
        run.error_text = "un autre calcul de cette conversation est déjà en cours"
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()
        logger.warning("analyse concurrente refusée pour %s", slug)
        return run

    try:
        appartenances, groupes = await charge_couche(session, conversation)
        result = await asyncio.to_thread(
            _run_reddwarf,
            votes,
            await meta_statement_ids(session, conversation),
            conversation.force_group_count,
            threshold,
            appartenances,
            groupes,
            EtatLisseur.depuis_conversation(conversation),
        )
        # L'écriture doit être DANS le try : sinon un échec de persistance laisse le
        # run figé en « running » pour toujours, puisqu'il a déjà été commité.
        await _persist(session, run, result)
        await enregistre_couche(session, conversation, result)
    except Exception as exc:  # noqa: BLE001 — on veut consigner l'échec, pas le masquer
        # Rollback d'abord : un `commit()` ici publierait les écritures partielles de
        # `_persist` et de `enregistre_couche` (projections orphelines, couche à
        # moitié réécrite). Le run, lui, a déjà été commité : il est relu après coup.
        await session.rollback()
        run = await session.get(AnalysisRun, run_id)
        run.status = AnalysisStatus.error
        run.error_text = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()
        logger.exception("analyse en échec pour %s", slug)
        return run

    run.status = AnalysisStatus.ok
    run.k = getattr(result.clusterer, "n_clusters", None)
    # Trace du lisseur : sans elle, on ne peut pas expliquer après coup pourquoi le
    # nombre de groupes affiché n'a pas suivi la silhouette du tour.
    run.params = {**(run.params or {}), "couche": result.diagnostic}
    run.finished_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info(
        "analyse de %s : %s participants, %s propositions, k=%s",
        slug,
        run.n_participants,
        run.n_statements,
        run.k,
    )

    # Le nommage vient APRÈS le commit de l'analyse, et son échec ne la défait pas.
    # C'est la contrainte du E4 (« échec silencieux si indisponible ») appliquée dès le
    # E1, alors qu'il n'y a encore rien qui puisse tomber : la règle est ici la seule
    # chose qui lira un jour un service extérieur, et l'ordre des opérations qui la
    # protège doit être en place AVANT qu'il y ait quelque chose à protéger. Un calcul
    # de groupes abouti ne doit jamais être perdu parce qu'un nom n'a pas pu être
    # décidé — ce sont deux qualités de service différentes.
    try:
        await nommage.decide(session, run)
    except Exception:  # noqa: BLE001 — voir ci-dessus : l'analyse, elle, est acquise
        await session.rollback()
        logger.exception("décision de nommage en échec pour %s ; l'analyse est gardée", slug)
    return run


def _clean(value):
    """NaN et types numpy -> types Python, ou None."""
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        return None
    return value


async def previous_membership(
    session: AsyncSession, conversation: Conversation, exclude_run_id: int
) -> dict[int, set[int]]:
    """Composition des groupes du dernier calcul abouti, par identité stable."""
    previous = await session.scalar(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.conversation_id == conversation.id,
            AnalysisRun.status == AnalysisStatus.ok,
            AnalysisRun.id != exclude_run_id,
        )
        .order_by(AnalysisRun.finished_at.desc())
        .limit(1)
    )
    if previous is None:
        return {}

    rows = await session.execute(
        select(
            ParticipantProjection.stable_group_id,
            ParticipantProjection.participant_id,
        ).where(
            ParticipantProjection.run_id == previous,
            ParticipantProjection.stable_group_id.isnot(None),
        )
    )
    membership: dict[int, set[int]] = {}
    for stable_group_id, participant_id in rows:
        membership.setdefault(stable_group_id, set()).add(participant_id)
    return membership


async def next_free_label(
    session: AsyncSession, conversation: Conversation
) -> int:
    """Première identité stable jamais utilisée sur cette conversation.

    On repart du maximum historique et non du nombre de groupes courant : réutiliser
    l'identité d'un groupe disparu ferait resurgir un « groupe B » sans rapport avec
    l'ancien.
    """
    highest = await session.scalar(
        select(func.max(ParticipantProjection.stable_group_id))
        .join(AnalysisRun, AnalysisRun.id == ParticipantProjection.run_id)
        .where(AnalysisRun.conversation_id == conversation.id)
    )
    return 0 if highest is None else highest + 1


async def _persist(session: AsyncSession, run: AnalysisRun, result) -> None:
    conversation = await session.get(Conversation, run.conversation_id)

    # --- appariement des groupes avec le calcul précédent -------------------------
    new_members: dict[int, set[int]] = {}
    for participant_id, row in result.participants_df.iterrows():
        cluster = _clean(row.get("cluster_id"))
        if cluster is not None:
            new_members.setdefault(int(cluster), set()).add(int(participant_id))

    mapping = match_groups(
        new_members,
        await previous_membership(session, conversation, run.id),
        await next_free_label(session, conversation),
    )
    run.group_mapping = {str(k): v for k, v in mapping.items()}

    for participant_id, row in result.participants_df.iterrows():
        cluster = _clean(row.get("cluster_id"))
        cluster = int(cluster) if cluster is not None else None
        session.add(
            ParticipantProjection(
                run_id=run.id,
                participant_id=int(participant_id),
                x=float(row["x"]),
                y=float(row["y"]),
                cluster_id=cluster,
                stable_group_id=mapping.get(cluster) if cluster is not None else None,
            )
        )

    # Statistiques globales par proposition (group_id NULL), dont `priority`,
    # que le routage de C7 consommera, et le clivage du chantier L.
    scores: list[clivage.Score] = []
    for statement_id, row in result.statements_df.iterrows():
        priority = _clean(row.get("priority"))
        # Les deux produits de red-dwarf, écrits tels quels : ce sont les données, et
        # les garder permettra de déplacer seuil ou plancher sans attendre un nouveau
        # passage d'analyse. `_clean` neutralise le NaN de l'alignement `gac_df` /
        # `propositions_df` — indexés respectivement sur la matrice brute et sur la
        # matrice filtrée, une proposition présente dans l'une seulement en reçoit un.
        accord = _clean(row.get("group-aware-consensus-agree"))
        desaccord = _clean(row.get("group-aware-consensus-disagree"))
        score = clivage.score_proposition(
            accord, desaccord, result.vues_par_groupe.get(int(statement_id), ())
        )
        if score is not None:
            scores.append(score)
        session.add(
            StatementStat(
                run_id=run.id,
                statement_id=int(statement_id),
                group_id=None,
                n_agree=int(_clean(row.get("n_agree")) or 0),
                n_disagree=int(_clean(row.get("n_disagree")) or 0),
                n_seen=int(_clean(row.get("n_total")) or 0),
                priority=float(priority) if priority is not None else None,
                clivage=score.clivage if score is not None else None,
                consensus_accord=float(accord) if accord is not None else None,
                consensus_desaccord=float(desaccord) if desaccord is not None else None,
            )
        )

    # Le score du débat, dérivé des propositions notées. `agrege` rend None quand
    # aucune ne l'est : les trois colonnes restent alors NULL, et « pas encore mesuré »
    # se distingue de « pas clivant ».
    debat = clivage.agrege(scores)
    if debat is not None:
        run.clivage = debat.clivage
        run.consensus = debat.consensus
        run.n_clivantes = debat.n_clivantes
    # Les réglages en vigueur au moment du calcul, avec le nombre de propositions
    # notées : sans eux, un score relu dans six mois ne serait pas interprétable — le
    # seuil et le nombre de retenues sont faits pour bouger, et la purge aura emporté
    # le détail par proposition.
    run.params = {
        **(run.params or {}),
        "clivage": {
            "n_notees": debat.n_notees if debat is not None else 0,
            "seuil": clivage.SEUIL_CLIVANTE,
            "n_retenues": clivage.N_RETENUES,
            "min_vues_par_groupe": clivage.MIN_VUES_PAR_GROUPE,
        },
    }

    # Propositions représentatives, par groupe.
    for group_id, statements in result.repness.items():
        for entry in statements:
            session.add(
                StatementStat(
                    run_id=run.id,
                    statement_id=int(entry["tid"]),
                    group_id=int(group_id),
                    repness=float(entry["repness"]),
                    p_test=float(entry["p-test"]),
                    repful_for=str(entry["repful-for"]),
                    n_agree=int(entry.get("n-agree") or 0),
                )
            )
    await session.flush()


async def latest_ok_run(
    session: AsyncSession, conversation: Conversation
) -> AnalysisRun | None:
    """Dernier calcul abouti — l'interface ne lit jamais autre chose."""
    return await session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.conversation_id == conversation.id,
            AnalysisRun.status == AnalysisStatus.ok,
        )
        .order_by(AnalysisRun.finished_at.desc())
        .limit(1)
    )


async def purge_calculs_perimes(session: AsyncSession) -> int:
    """Rend les grosses tables des calculs dépassés. Renvoie le nombre de lignes ôtées.

    **Ce qui est effacé, et ce qui ne l'est pas.** `participant_projection` compte une
    ligne par personne et par calcul, `statement_stat` une par proposition et par
    groupe : ce sont elles qui grossissent sans fin. Le `analysis_run`, lui, est
    conservé : quelques octets par calcul, et c'est la trace qu'on relit quand un
    résultat est contesté — combien de groupes, quel seuil, quel appariement.

    **Deux protections, et la seconde n'est pas redondante.** Le dernier calcul abouti
    de chaque conversation est exclu quel que soit son âge : c'est celui que la carte,
    la mini-barre de l'accueil, le routage des propositions et l'annonce de groupe
    lisent tous. Sur une conversation qu'on ne vote plus depuis un mois, la seule borne
    d'âge l'aurait emporté, et le site aurait perdu ses groupes en silence.

    Confiée au worker, comme la purge des traces de plafond : elle doit avoir lieu même
    sans trafic, et un `DELETE` de cette taille n'a rien à faire dans le processus qui
    sert les participants.
    """
    limite = datetime.now(timezone.utc) - timedelta(
        hours=settings.analysis_history_retention_hours
    )
    # « Dépassé » se dit par la NÉGATIVE : un calcul l'est quand un calcul abouti plus
    # récent existe pour la même conversation. Écrite dans l'autre sens — « garder le
    # dernier abouti » — la règle passe par un `DISTINCT ON` qui doit départager deux
    # calculs de même `finished_at`, et son arbitrage n'est pas celui des lecteurs, qui
    # ordonnent tous par `finished_at` seul. Le site aurait alors pu lire un calcul dont
    # la purge venait de rendre les lignes. Ici, deux calculs à égalité sont tous deux
    # protégés : conserver de trop est sans conséquence, l'inverse aveugle une page.
    plus_recent = aliased(AnalysisRun)
    depasse = (
        select(plus_recent.id)
        .where(
            plus_recent.conversation_id == AnalysisRun.conversation_id,
            plus_recent.status == AnalysisStatus.ok,
            plus_recent.finished_at > AnalysisRun.finished_at,
        )
        .exists()
    )
    perimes = (
        select(AnalysisRun.id)
        .where(
            AnalysisRun.finished_at.isnot(None),
            AnalysisRun.finished_at < limite,
            depasse,
        )
        .scalar_subquery()
    )
    otees = 0
    for table in (ParticipantProjection, StatementStat):
        resultat = await session.execute(
            delete(table).where(table.run_id.in_(perimes))
        )
        otees += resultat.rowcount or 0
    await session.commit()
    return otees


async def conversations_needing_analysis(session: AsyncSession) -> list[Conversation]:
    """Conversations dont le dernier calcul est antérieur au dernier vote.

    Évite de recalculer toutes les heures des conversations où rien n'a bougé.
    """
    candidates = await session.scalars(
        select(Conversation).where(Conversation.state != ConversationState.draft)
    )
    stale: list[Conversation] = []
    for conversation in candidates:
        last_vote = await session.scalar(
            select(func.max(Vote.modified_at)).where(
                Vote.statement_id.in_(_approved_statements_of(conversation))
            )
        )
        if last_vote is None:
            continue
        last_run = await session.scalar(
            select(func.max(AnalysisRun.started_at)).where(
                AnalysisRun.conversation_id == conversation.id
            )
        )
        if last_run is None or last_run < last_vote:
            stale.append(conversation)
            continue

        # Un changement de réglage doit être pris en compte sans attendre un nouveau
        # vote : sinon le réglage du modérateur reste sans effet visible pendant une
        # heure, voire indéfiniment sur une conversation close. Le seuil compte au
        # même titre que k, et il bouge aussi tout seul — approuver une proposition
        # change la borne automatique sans créer le moindre vote.
        last_params = await session.scalar(
            select(AnalysisRun.params)
            .where(AnalysisRun.conversation_id == conversation.id)
            .order_by(AnalysisRun.started_at.desc())
            .limit(1)
        )
        last_params = last_params or {}
        n_statements = await session.scalar(
            select(func.count()).select_from(
                _approved_statements_of(conversation).subquery()
            )
        )
        if last_params.get("force_group_count") != conversation.force_group_count or (
            last_params.get("min_user_vote_threshold")
            != effective_min_user_votes(conversation, n_statements)
        ):
            stale.append(conversation)
    return stale
