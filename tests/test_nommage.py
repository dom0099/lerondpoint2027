"""La règle « dès que nécessaire » — chantier E1.

Ce que ces tests éprouvent n'est pas un nom : rien ne nomme encore. C'est la DÉCISION de
nommer, qui commande à elle seule le dimensionnement de tout le chantier E — à un appel
par débat et par cycle il faudrait un serveur dédié, sous cette règle personne ne sait
encore ce que ça coûte. Une règle trop bavarde ramènerait la facture qu'elle existe pour
éviter ; une règle trop avare laisserait des noms faux à l'écran.

Les calculs sont posés à la main, avec leur `finished_at` explicite : c'est lui qui
décide de l'ancienneté d'une identité, et deux calculs créés dans la même seconde de test
ne se départageraient pas autrement. Même procédé qu'au `test_clivage_liste.py`.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.analysis import pipeline
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    GroupNaming,
    MotifNommage,
    Participant,
    ParticipantProjection,
    StatementStat,
)
from app.services import nommage
from app.services.nommage import (
    DECLARATIONS_CHANGEES,
    DELAI_DE_GARDE,
    N_DECLARATIONS,
    PERSISTANCE_MINIMALE,
)
from tests.conftest import open_conversation
from tests.test_analysis import _populate

MAINTENANT = datetime(2026, 9, 6, 18, 0, 0, tzinfo=timezone.utc)


async def _participants(session, combien: int, prefixe: str) -> list[int]:
    gens = [Participant(anon_token=f"jeton-{prefixe}-{i}") for i in range(combien)]
    for gen in gens:
        session.add(gen)
    await session.flush()
    return [gen.id for gen in gens]


async def _calcul(
    session,
    conversation_id: int,
    *,
    quand: datetime,
    groupes: dict[int, list[int]],
    membres: dict[int, list[int]],
    sens: str = "agree",
    statut: AnalysisStatus = AnalysisStatus.ok,
) -> AnalysisRun:
    """Un calcul abouti, avec ses identités stables et leurs déclarations.

    `groupes` : identité stable -> déclarations représentatives, de la plus à la moins
    représentative. `membres` : identité stable -> identifiants de participants, dont
    seul le NOMBRE compte ici (c'est lui qui franchit ou non `MIN_GROUP_SIZE`). `sens` :
    la position du groupe sur ces déclarations, qui fait partie de ce qui le caractérise
    au même titre qu'elles.

    L'étiquette brute de k-means est délibérément DÉCALÉE de l'identité stable (`brut =
    stable + 10`) : c'est le détour que la règle doit faire par `group_mapping`, et un
    peuplement où les deux coïncideraient laisserait passer une implémentation qui lit
    l'étiquette brute — celle qui change à chaque calcul et ne veut rien dire.
    """
    run = AnalysisRun(
        conversation_id=conversation_id,
        status=statut,
        finished_at=quand,
        group_mapping={str(stable + 10): stable for stable in groupes},
    )
    session.add(run)
    await session.flush()

    for stable, participants in membres.items():
        for participant_id in participants:
            session.add(
                ParticipantProjection(
                    run_id=run.id,
                    participant_id=participant_id,
                    x=0.0,
                    y=0.0,
                    cluster_id=stable + 10,
                    stable_group_id=stable,
                )
            )
    for stable, declarations in groupes.items():
        # `repness` décroissante dans l'ordre donné : la règle doit retenir les
        # premières, et l'ordre d'écriture ne doit pas suffire à les retrouver.
        for rang, statement_id in enumerate(declarations):
            session.add(
                StatementStat(
                    run_id=run.id,
                    statement_id=statement_id,
                    group_id=stable + 10,
                    stable_group_id=stable,
                    repness=1.0 - rang / 100,
                    repful_for=sens,
                )
            )
    await session.commit()
    return run


async def _decisions(session, conversation_id: int) -> list[GroupNaming]:
    return list(
        await session.scalars(
            select(GroupNaming)
            .where(GroupNaming.conversation_id == conversation_id)
            .order_by(GroupNaming.decided_at, GroupNaming.stable_group_id)
        )
    )


async def _debat(moderator_client, session_factory, titre: str):
    """Un débat ouvert, ses 8 propositions, et 4 participants pour peupler les groupes."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title=titre
    )
    async with session_factory() as session:
        gens = await _participants(session, 4, titre[:12].replace(" ", "-"))
        await session.commit()
    return conversation_id, statements, gens


# --- condition 1 : un groupe neuf, et l'ancienneté qu'il doit prouver ----------------


async def test_a_young_group_is_not_named_yet(moderator_client, session_factory) -> None:
    """Moins de trois heures d'existence : rien à nommer, et surtout rien à inventer.

    Un débat qui vient de s'ouvrir n'a AUCUN calcul assez ancien pour prouver quoi que
    ce soit. Se taire est le bon défaut : nommer un groupe qui n'existera plus dans
    l'heure coûte un appel, un passage en modération, et un nom faux à l'écran.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat tout neuf"
    )
    async with session_factory() as session:
        run = await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT,
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        assert await nommage.decide(session, run, maintenant=MAINTENANT) == []
        assert await _decisions(session, conversation_id) == []


async def test_a_group_that_held_three_hours_is_named_once(
    moderator_client, session_factory
) -> None:
    """L'identité tient depuis la durée exigée : elle est nommée, et une seule fois.

    Le second calcul, aux mêmes déclarations, ne doit rien redéclencher — sans quoi la
    règle « dès que nécessaire » serait « à chaque cycle » sous un autre nom, et le
    chiffrage du 5 septembre reviendrait avec son serveur dédié.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat installé"
    )
    async with session_factory() as session:
        await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT - PERSISTANCE_MINIMALE - timedelta(minutes=10),
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        run = await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT,
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        decisions = await nommage.decide(session, run, maintenant=MAINTENANT)
        assert [d.motif for d in decisions] == [MotifNommage.nouveau]
        assert {tuple(d) for d in decisions[0].declarations} == {
            (s, "agree") for s in statements[:5]
        }

        plus_tard = MAINTENANT + DELAI_DE_GARDE + timedelta(minutes=1)
        encore = await _calcul(
            session,
            conversation_id,
            quand=plus_tard,
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        assert await nommage.decide(session, encore, maintenant=plus_tard) == []
        assert len(await _decisions(session, conversation_id)) == 1


async def test_a_group_that_vanished_and_came_back_has_not_held(
    moderator_client, session_factory
) -> None:
    """Trois heures d'ancienneté, mais pas trois heures de CONTINUITÉ.

    L'identité existait il y a trois heures et existe maintenant ; entre les deux, un
    calcul ne l'a pas vue. Ce n'est pas un groupe qui a tenu, c'est un groupe qui est
    revenu — et l'appariement du C6 lui rendra peut-être son ancienne étiquette sans
    que ce soit la même population. Une date de première apparition stockée aurait
    laissé passer ce cas ; relire l'historique le voit.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat intermittent"
    )
    async with session_factory() as session:
        await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT - PERSISTANCE_MINIMALE - timedelta(minutes=20),
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        # Le trou : un calcul abouti où l'identité 0 n'existe pas.
        await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT - timedelta(hours=1),
            groupes={1: statements[:5]},
            membres={1: gens},
        )
        run = await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT,
            groupes={0: statements[:5]},
            membres={0: gens},
        )
        assert await nommage.decide(session, run, maintenant=MAINTENANT) == []


async def test_a_group_too_small_is_not_a_group(moderator_client, session_factory) -> None:
    """Sous `MIN_GROUP_SIZE`, ce n'est pas un groupe d'opinion, c'est une personne seule.

    `group_mapping` contient tous les amas, y compris ceux d'une seule personne : le
    filtre de taille ne peut donc pas venir de lui. Le C6 avait déjà tranché qu'on ne
    confirme à personne qu'il est isolé ; lui donner un nom public serait pire.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat à solitaire"
    )
    async with session_factory() as session:
        vieux = MAINTENANT - PERSISTANCE_MINIMALE - timedelta(minutes=10)
        for quand in (vieux, MAINTENANT):
            run = await _calcul(
                session,
                conversation_id,
                quand=quand,
                groupes={0: statements[:5], 1: statements[3:8]},
                membres={0: gens[:3], 1: gens[3:4]},  # le groupe 1 n'a qu'un membre
            )
        decisions = await nommage.decide(session, run, maintenant=MAINTENANT)
        assert [d.stable_group_id for d in decisions] == [0]


# --- condition 2 : ce qui caractérise le groupe a changé ------------------------------


async def _groupe_nomme(session, conversation_id, statements, gens):
    """Amène un groupe jusqu'à son premier nommage, et rend l'instant de ce nommage."""
    await _calcul(
        session,
        conversation_id,
        quand=MAINTENANT - PERSISTANCE_MINIMALE - timedelta(minutes=10),
        groupes={0: statements[:5]},
        membres={0: gens},
    )
    run = await _calcul(
        session,
        conversation_id,
        quand=MAINTENANT,
        groupes={0: statements[:5]},
        membres={0: gens},
    )
    assert len(await nommage.decide(session, run, maintenant=MAINTENANT)) == 1
    return MAINTENANT + DELAI_DE_GARDE + timedelta(minutes=1)


async def test_reordering_the_same_statements_changes_nothing(
    moderator_client, session_factory
) -> None:
    """Les mêmes déclarations, dans un autre ordre, décrivent le même groupe.

    La `repness` bouge à chaque calcul ; si son classement décidait, la règle
    déclencherait à peu près tout le temps. On compare des ensembles, pas des rangs.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui permute"
    )
    async with session_factory() as session:
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        run = await _calcul(
            session,
            conversation_id,
            quand=apres,
            groupes={0: list(reversed(statements[:5]))},
            membres={0: gens},
        )
        assert await nommage.decide(session, run, maintenant=apres) == []


async def test_one_statement_out_of_five_is_not_enough(
    moderator_client, session_factory
) -> None:
    """Le piège de la différence symétrique, tenu par un test.

    Entre deux ensembles de même taille, la différence symétrique est toujours PAIRE :
    un seul échange en donne déjà deux. Un seuil posé sur elle se déclencherait donc au
    moindre remplacement — soit, en pratique, à chaque calcul. Le seuil porte sur les
    déclarations NOUVELLES, et une seule ne suffit pas.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui bouge peu"
    )
    async with session_factory() as session:
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        une_seule = statements[:4] + [statements[5]]
        assert len(set(une_seule) - set(statements[:5])) == 1
        run = await _calcul(
            session,
            conversation_id,
            quand=apres,
            groupes={0: une_seule},
            membres={0: gens},
        )
        assert await nommage.decide(session, run, maintenant=apres) == []


async def test_two_new_statements_earn_a_new_name(
    moderator_client, session_factory
) -> None:
    """Deux déclarations qui n'y étaient pas : le groupe ne parle plus de la même chose."""
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui tourne"
    )
    async with session_factory() as session:
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        deux = statements[:3] + statements[5:7]
        assert len(set(deux) - set(statements[:5])) == DECLARATIONS_CHANGEES
        run = await _calcul(
            session,
            conversation_id,
            quand=apres,
            groupes={0: deux},
            membres={0: gens},
        )
        decisions = await nommage.decide(session, run, maintenant=apres)
        assert [d.motif for d in decisions] == [MotifNommage.changement]
        assert {tuple(d) for d in decisions[0].declarations} == {
            (s, "agree") for s in deux
        }


async def test_growing_alone_never_renames(moderator_client, session_factory) -> None:
    """La taille ne nomme pas. C'est un écart assumé avec la couleur.

    Depuis le G10 la couleur suit le rang d'effectif : elle répond à « lequel est le
    plus gros ». Le nom répond à « de quoi celui-ci parle », et un groupe qui double
    sans changer d'avis parle toujours de la même chose.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui grossit"
    )
    async with session_factory() as session:
        renforts = await _participants(session, 6, "renfort")
        await session.commit()
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        run = await _calcul(
            session,
            conversation_id,
            quand=apres,
            groupes={0: statements[:5]},
            membres={0: gens + renforts},
        )
        assert await nommage.decide(session, run, maintenant=apres) == []


async def test_the_cooldown_holds_a_real_change_back(
    moderator_client, session_factory
) -> None:
    """Un changement réel, mais trop tôt : le délai de garde le retient.

    Sans lui, un débat agité renommerait son groupe à chaque calcul — un nom qui change
    toutes les dix minutes n'est plus un nom, et chaque changement coûtera au E5 un
    passage en modération humaine.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat agité"
    )
    async with session_factory() as session:
        await _groupe_nomme(session, conversation_id, statements, gens)
        trop_tot = MAINTENANT + DELAI_DE_GARDE - timedelta(minutes=1)
        run = await _calcul(
            session,
            conversation_id,
            quand=trop_tot,
            groupes={0: statements[3:8]},
            membres={0: gens},
        )
        assert await nommage.decide(session, run, maintenant=trop_tot) == []

        # ... et le même changement passe une fois le délai écoulé.
        assez_tard = MAINTENANT + DELAI_DE_GARDE + timedelta(minutes=1)
        run = await _calcul(
            session,
            conversation_id,
            quand=assez_tard,
            groupes={0: statements[3:8]},
            membres={0: gens},
        )
        assert len(await nommage.decide(session, run, maintenant=assez_tard)) == 1


# --- garde-fous ----------------------------------------------------------------------


async def test_a_failed_run_decides_nothing(moderator_client, session_factory) -> None:
    """Un calcul en échec n'a pas de groupes : il ne peut rien y avoir à nommer."""
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat en panne"
    )
    async with session_factory() as session:
        run = await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT,
            groupes={0: statements[:5]},
            membres={0: gens},
            statut=AnalysisStatus.error,
        )
        assert await nommage.decide(session, run, maintenant=MAINTENANT) == []


async def test_a_group_without_representative_statements_is_left_alone(
    moderator_client, session_factory
) -> None:
    """Rien à lire pour trouver un nom : on se tait plutôt que d'en faire inventer un.

    C'est le seul cas où un groupe réel, assez ancien et assez grand, n'est pas nommé.
    Envoyer un ensemble vide à un modèle lui ferait produire un nom tout de même — et
    ce nom ne s'appuierait sur rien.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat sans repness"
    )
    async with session_factory() as session:
        vieux = MAINTENANT - PERSISTANCE_MINIMALE - timedelta(minutes=10)
        for quand in (vieux, MAINTENANT):
            run = await _calcul(
                session,
                conversation_id,
                quand=quand,
                groupes={0: []},
                membres={0: gens},
            )
        assert await nommage.decide(session, run, maintenant=MAINTENANT) == []


async def test_the_rule_reads_at_most_five_statements(
    moderator_client, session_factory
) -> None:
    """Cinq, et pas ce que l'affichage montre.

    `resultats.N_REPRESENTATIVES` vaut 2 : c'est ce qu'un lecteur absorbe dans une
    carte. La règle en lit cinq, parce que c'est l'hypothèse du chiffrage du
    5 septembre — si les deux constantes n'en faisaient qu'une, alléger l'affichage
    dégraderait le nommage sans que rien ne le dise.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat bavard"
    )
    async with session_factory() as session:
        run = await _calcul(
            session,
            conversation_id,
            quand=MAINTENANT,
            groupes={0: statements},  # huit déclarations proposées
            membres={0: gens},
        )
        lues = await nommage.declarations_par_groupe(session, run)
        assert [statement_id for statement_id, _ in lues[0]] == statements[:N_DECLARATIONS]


# --- le raccordement au vrai passage d'analyse ---------------------------------------


async def test_a_real_analysis_pass_records_its_naming_decisions(
    moderator_client, session_factory
) -> None:
    """Un vrai passage écrit ses décisions, et un échec de nommage ne le défait pas.

    Le pendant en base des tests ci-dessus : ceux-là posent les calculs à la main,
    celui-ci fait tourner `pipeline.analyse` et va lire ce qui s'est écrit. C'est le
    même partage qu'au chantier L entre `test_clivage.py` et `test_clivage_passage.py`.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Passage complet"
    )
    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=8)

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        assert run.status is AnalysisStatus.ok
        # Aucune décision : le débat vient de naître, il n'a aucun calcul de trois
        # heures d'âge. C'est bien la règle qui se tait, pas le raccordement qui manque
        # — le test suivant le prouve en vieillissant ce calcul.
        assert await _decisions(session, conversation_id) == []

        # On vieillit le calcul, on en refait un : les identités ont alors tenu.
        # Vieilli par rapport à l'heure RÉELLE, et non par rapport au `MAINTENANT` fixe
        # des tests ci-dessus : ici c'est `pipeline.analyse` qui appelle la règle, donc
        # elle lit l'horloge. Une date d'ancrage figée aurait placé la référence dans
        # le futur, et la règle se serait tue pour la mauvaise raison.
        run.finished_at = (
            datetime.now(timezone.utc) - PERSISTANCE_MINIMALE - timedelta(minutes=10)
        )
        await session.commit()
        second = await pipeline.analyse(session, conversation)
        assert second.status is AnalysisStatus.ok

    async with session_factory() as session:
        combien = await session.scalar(
            select(func.count())
            .select_from(GroupNaming)
            .where(GroupNaming.conversation_id == conversation_id)
        )
        assert combien >= 1, "un vrai passage doit consigner ses décisions de nommage"


async def test_the_measurement_refuses_a_rate_it_has_not_observed(
    moderator_client, session_factory
) -> None:
    """Sous une journée d'observation, aucun taux quotidien n'est affiché.

    C'est le chiffre sur lequel on s'apprêterait à commander un serveur dédié à
    200-325 $/mois. Extrapoler « 3 décisions en deux heures » en « 36 par jour » serait
    une invention présentée comme une mesure — et le §8 du E0 dit explicitement que
    cette fréquence n'est connue de personne tant qu'elle n'a pas été observée.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat mesuré"
    )
    async with session_factory() as session:
        await _groupe_nomme(session, conversation_id, statements, gens)
        m = await nommage.mesure(session, depuis=MAINTENANT - timedelta(days=7))
        assert m["total"] == 1
        assert m["par_motif"]["nouveau"] == 1
        assert m["debats"] == 1
        assert m["par_jour"] is None, "un taux sur moins d'un jour n'est pas une mesure"


async def test_a_group_that_reverses_itself_is_renamed(
    moderator_client, session_factory
) -> None:
    """Mêmes déclarations, position inverse : le groupe dit le contraire de son nom.

    **Ce test vient d'un relevé, pas d'une hypothèse.** Sur le débat du permis à 16 ans
    (calcul 27, 6 septembre), les deux groupes d'opinion partagent quatre déclarations
    représentatives sur cinq, avec des sens exactement inverses — c'est le cas normal,
    puisque la `repness` retient ce qui SÉPARE les groupes et que deux camps se séparent
    en s'opposant sur les mêmes sujets.

    Il en découle qu'une règle comparant des identifiants nus aurait vu un ensemble
    rigoureusement inchangé là où le groupe s'est entièrement retourné : le changement le
    plus radical possible n'aurait rien déclenché, et le nom serait resté en place
    pendant que le groupe devenait son contraire.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui se retourne"
    )
    async with session_factory() as session:
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        run = await _calcul(
            session,
            conversation_id,
            quand=apres,
            groupes={0: statements[:5]},  # les MÊMES déclarations…
            membres={0: gens},
            sens="disagree",              # …et la position inverse
        )
        decisions = await nommage.decide(session, run, maintenant=apres)
        assert [d.motif for d in decisions] == [MotifNommage.changement]
        assert {tuple(d) for d in decisions[0].declarations} == {
            (s, "disagree") for s in statements[:5]
        }


async def test_flipping_a_single_statement_is_not_enough(
    moderator_client, session_factory
) -> None:
    """Une seule position retournée compte pour un couple nouveau, et un ne suffit pas.

    Le pendant du test sur la déclaration remplacée : le sens entre bien dans la clé,
    mais il n'abaisse pas le seuil. Sans ce test, on pourrait durcir la règle sur le
    sens en croyant l'assouplir.
    """
    conversation_id, statements, gens = await _debat(
        moderator_client, session_factory, "Débat qui hésite"
    )
    async with session_factory() as session:
        apres = await _groupe_nomme(session, conversation_id, statements, gens)
        run = AnalysisRun(
            conversation_id=conversation_id,
            status=AnalysisStatus.ok,
            finished_at=apres,
            group_mapping={"10": 0},
        )
        session.add(run)
        await session.flush()
        for participant_id in gens:
            session.add(
                ParticipantProjection(
                    run_id=run.id,
                    participant_id=participant_id,
                    x=0.0,
                    y=0.0,
                    cluster_id=10,
                    stable_group_id=0,
                )
            )
        # Quatre déclarations inchangées, la cinquième retournée : un couple nouveau.
        for rang, statement_id in enumerate(statements[:5]):
            session.add(
                StatementStat(
                    run_id=run.id,
                    statement_id=statement_id,
                    group_id=10,
                    stable_group_id=0,
                    repness=1.0 - rang / 100,
                    repful_for="disagree" if rang == 4 else "agree",
                )
            )
        await session.commit()
        assert await nommage.decide(session, run, maintenant=apres) == []
