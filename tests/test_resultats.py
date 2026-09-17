"""Résultats par proposition : ce que chaque groupe a répondu (chantier G5).

Quatre exigences, et elles répondent chacune à une manière de mentir :

  - sans calcul abouti, le bloc ne s'affiche pas — plutôt qu'une grille vide, qui se
    lirait « tout le monde a passé » ;
  - les pourcentages d'un groupe somment à 100, toujours — trois arrondis indépendants
    donnent 99 % ou 101 %, et une barre qui ne remplit pas sa largeur ;
  - un groupe qui n'a pas voté a « aucun vote », pas « 0 % d'accord » ;
  - les colonnes suivent l'IDENTITÉ des groupes, pas leur effectif.
"""

import re
from pathlib import Path

from sqlalchemy import select

from app.analysis import pipeline
from app.models import (
    Conversation,
    ModerationStatus,
    Participant,
    ParticipantProjection,
    Statement,
    StatementStat,
    Vote,
)
from app.services import resultats as resultats_service
from tests.conftest import open_conversation


async def _peupler(session, statements, effectif=10, prefixe="resultats"):
    """Deux camps francs, chacun s'écartant sur une proposition qui lui est propre.

    Repris de `tests/test_carte.py` : sans cet écart, tous les membres d'un camp se
    projettent au même point et le regroupement n'a rien à séparer.
    """
    participants = []
    for index in range(effectif):
        participant = Participant(anon_token=f"jeton-{prefixe}-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()

    for index, participant in enumerate(participants):
        camp = index % 2
        singuliere = index % len(statements)
        for position, statement_id in enumerate(statements):
            valeur = 1 if (position % 2 == camp) else -1
            if position == singuliere:
                valeur = -valeur
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement_id,
                    value=valeur,
                )
            )
    await session.commit()
    return participants


# --- l'arithmétique, qui se teste sans base ----------------------------------------


def test_the_three_shares_always_add_up_to_a_hundred() -> None:
    """Le passage prend le RESTE, il n'est pas arrondi à son tour.

    1 d'accord et 1 pas d'accord sur 3 votes : deux arrondis à 33 % laisseraient 66 %,
    et la barre s'arrêterait avant son bord. Le troisième segment absorbe l'écart.
    """
    assert sum(resultats_service.pourcentages(1, 1, 3)) == 100
    assert sum(resultats_service.pourcentages(2, 1, 3)) == 100
    assert sum(resultats_service.pourcentages(1, 0, 7)) == 100
    assert resultats_service.pourcentages(3, 1, 4) == (75, 25, 0)


def test_no_votes_is_not_zero_percent() -> None:
    """Zéro vote ne donne pas « 0 % d'accord » : il n'y a rien à répartir."""
    assert resultats_service.pourcentages(0, 0, 0) == (0, 0, 0)


# --- ce que le service lit en base --------------------------------------------------


async def test_nothing_is_shown_before_a_run_succeeds(
    moderator_client, session_factory
) -> None:
    """Sans calcul abouti, pas de bloc — et surtout pas une grille vide."""
    conversation_id, _, _ = await open_conversation(moderator_client, statements=4)
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        resultats = await resultats_service.par_proposition(session, conversation)

    assert resultats.calculee is False
    assert resultats.lignes == []
    assert resultats.n_groupes == 0


async def test_every_proposition_gets_a_line_for_every_group(
    moderator_client, session_factory
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)
        resultats = await resultats_service.par_proposition(session, conversation)

    assert resultats.calculee is True
    assert resultats.noms == ["A", "B"]
    assert len(resultats.lignes) == len(statements)

    for ligne in resultats.lignes:
        assert [g.nom for g in ligne.groupes] == ["A", "B"]
        # Les dix participants ont voté sur toutes les propositions, et tous sont
        # classés : le total de la ligne est la somme des groupes en face.
        assert ligne.n_votes == sum(g.n_votes for g in ligne.groupes) == 10
        for g in ligne.groupes:
            assert g.accord + g.desaccord + g.passe == 100


async def test_a_group_that_never_saw_a_proposition_has_no_votes(
    moderator_client, session_factory
) -> None:
    """Une proposition ajoutée après le vote : zéro vote, pas « 0 % d'accord »."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

        # Approuvée, donc visible et votable — mais personne ne l'a encore vue.
        tardive = Statement(
            conversation_id=conversation_id,
            text="Ajoutée après le calcul, jamais votée.",
            moderation_status=ModerationStatus.approved,
        )
        session.add(tardive)
        await session.commit()

        resultats = await resultats_service.par_proposition(session, conversation)

    ligne = next(
        l for l in resultats.lignes if l.texte == "Ajoutée après le calcul, jamais votée."
    )
    assert ligne.n_votes == 0
    for g in ligne.groupes:
        assert g.n_votes == 0
        assert (g.accord, g.desaccord, g.passe) == (0, 0, 0)


async def test_the_counts_follow_the_actual_votes(
    moderator_client, session_factory
) -> None:
    """Un vote émis APRÈS le calcul est compté : les votes sont lus au présent.

    C'est la moitié mouvante du chiffre — l'appartenance à un groupe est datée du
    dernier calcul, les votes ne le sont pas. Sans quoi quelqu'un qui vient de voter
    verrait un écran où sa réponse n'existe pas.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        participants = await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

        avant = await resultats_service.par_proposition(session, conversation)

        # Le premier participant change d'avis sur la première proposition.
        vote = await session.scalar(
            select(Vote).where(
                Vote.participant_id == participants[0].id,
                Vote.statement_id == statements[0],
            )
        )
        vote.value = 0  # il passe
        await session.commit()

        apres = await resultats_service.par_proposition(session, conversation)

    # Aucun nouveau calcul n'a tourné : les groupes sont les mêmes, la ligne a bougé.
    assert apres.noms == avant.noms
    assert avant.lignes[0].groupes != apres.lignes[0].groupes
    assert sum(g.passe for g in apres.lignes[0].groupes) > sum(
        g.passe for g in avant.lignes[0].groupes
    )


async def test_a_group_of_one_gets_no_column(
    moderator_client, session_factory
) -> None:
    """Sous `MIN_GROUP_SIZE`, pas de colonne : « 100 % d'accord » désignerait une
    personne unique, ce que la carte se refuse déjà à faire."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)

        # On isole artificiellement une personne dans un troisième groupe.
        projection = await session.scalar(
            select(ParticipantProjection).where(ParticipantProjection.run_id == run.id)
        )
        projection.stable_group_id = 2
        await session.commit()

        resultats = await resultats_service.par_proposition(session, conversation)

    assert "C" not in resultats.noms


# --- la page ------------------------------------------------------------------------


async def test_the_page_carries_the_block_hidden_until_the_end(
    moderator_client, client, session_factory
) -> None:
    """Rendu par le serveur, mais masqué : on ne souffle pas ses réponses à quelqu'un
    qui n'a pas encore voté."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    page = (await client.get(f"/c/{slug}")).text

    compact = " ".join(page.split())
    assert "Ce que chaque groupe a répondu" in page
    assert 'id="resultats" hidden' in compact
    # La couleur de groupe vient de la carte, pas de la feuille de style.
    assert "background:#" in page


async def test_a_closed_conversation_shows_the_results_at_once(
    moderator_client, client, session_factory
) -> None:
    """Plus rien à voter, donc plus rien à souffler : le bloc est visible d'emblée."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Conversation ouverte",
            "description": "",
            "state": "closed",
            "themes": ["institutions"],
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )

    page = (await client.get(f"/c/{slug}")).text
    compact = " ".join(page.split())
    assert "Ce que chaque groupe a répondu" in page
    assert 'id="resultats"' in compact
    assert 'id="resultats" hidden' not in compact


async def test_the_block_is_absent_without_a_run(moderator_client, client) -> None:
    _, slug, _ = await open_conversation(moderator_client, statements=4)
    page = (await client.get(f"/c/{slug}")).text
    assert 'id="resultats"' not in page


# --- les couleurs des deux réponses ------------------------------------------------


FEUILLE = Path("app/static/styles.css")


def _jeton(nom: str) -> str:
    """Valeur d'une variable CSS, lue dans la feuille réellement servie."""
    trouve = re.search(rf"--{nom}:\s*(#[0-9A-Fa-f]{{6}})", FEUILLE.read_text())
    assert trouve, f"--{nom} est introuvable dans {FEUILLE}"
    return trouve.group(1).upper()


def _luminance(couleur: str) -> float:
    canaux = [int(couleur[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    canaux = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canaux]
    return 0.2126 * canaux[0] + 0.7152 * canaux[1] + 0.0722 * canaux[2]


def _contraste(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _deuteranope(couleur: str) -> str:
    """Simulation Viénot-Brettel-Mollon (1999) : ce que voit un œil deutéranope."""
    r, v, b = (int(couleur[i : i + 2], 16) / 255 for i in (1, 3, 5))
    r, v, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, v, b))
    L = 17.8824 * r + 43.5161 * v + 4.11935 * b
    S = 0.0299566 * r + 0.184309 * v + 1.46709 * b
    M = 0.494207 * L + 1.24827 * S  # le canal moyen, reconstruit depuis les deux autres
    canaux = (
        0.080944 * L - 0.130504 * M + 0.116721 * S,
        -0.0102485 * L + 0.0540194 * M - 0.113615 * S,
        -0.000365294 * L - 0.00412163 * M + 0.693513 * S,
    )
    sortie = []
    for c in canaux:
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        sortie.append(round(c * 255))
    return "#{:02X}{:02X}{:02X}".format(*sortie)


def test_agree_and_disagree_stay_apart_without_their_hue() -> None:
    """Vert et rouge est la paire que le daltonisme rouge-vert efface.

    Le client les a demandés le 5 septembre 2026, et c'est légitime : ce sont les
    couleurs que tout le monde attend, et celles de Pol.is. Mais le premier couple
    essayé — `#198038` et `#DA1E28` — avait exactement la MÊME clarté (1,00:1). Sans la
    teinte, les deux segments d'une barre devenaient le même gris.

    Ce test garde ce qui a été retenu à la place : un écart de clarté suffisant pour
    que « d'accord » et « pas d'accord » se distinguent par le clair et le foncé, la
    teinte n'étant plus qu'un renfort. Changer l'une des deux valeurs sans regarder
    l'autre doit le faire tomber.
    """
    vert, rouge = _jeton("vert-accord"), _jeton("rouge-desaccord")

    assert _contraste(vert, rouge) >= 1.5
    assert _contraste(_deuteranope(vert), _deuteranope(rouge)) >= 1.8


def test_both_answer_colours_hold_against_the_page_and_the_third_segment() -> None:
    """Trois segments, et le troisième est presque blanc.

    « Passer » est en `--trait` (rebaptisé `--bordure` avant le chantier I — même
    rôle, même jeton de séparation neutre). Les deux autres doivent s'en détacher,
    sans quoi une barre à trois parts se lirait comme une barre à deux. Le seuil est
    celui des objets graphiques porteurs de sens (WCAG 1.4.11), 3:1 — et non les
    4,5:1 du texte, puisque aucun texte n'est posé dessus.
    """
    for nom in ("vert-accord", "rouge-desaccord"):
        couleur = _jeton(nom)
        assert _contraste(couleur, "#FFFFFF") >= 3.0, nom
        assert _contraste(couleur, _jeton("trait")) >= 3.0, nom


def test_the_error_red_stays_the_error_red() -> None:
    """La levée de la réserve du rouge n'est pas une mise en commun.

    Le document d'identité disait « rien d'autre n'est rouge » ; le désaccord l'est
    désormais. `--erreur` garde pour autant sa propre valeur : un champ invalide et une
    part de désaccord ne doivent pas être la même couleur, sinon la première perd ce
    qui la signale.
    """
    assert _jeton("erreur") != _jeton("rouge-desaccord")


# --- ce que le chantier L3 met en avant ---------------------------------------------


async def _debat_analyse(moderator_client, session_factory, prefixe="l3"):
    """Un débat de dix votants, deux groupes forcés, calculé une fois."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe=prefixe)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)
        resultats = await resultats_service.par_proposition(session, conversation)
    return slug, run, resultats


async def _debat_avec_accord(moderator_client, session_factory, prefixe="accord"):
    """Le même débat, plus UNE proposition que tout le monde approuve.

    Le peuplement de ce fichier oppose les deux camps sur chacune des huit propositions.
    Depuis que le titre du bloc filtre ce que le classement propose (décision du client
    du 6 septembre 2026), un tel débat n'a plus rien à mettre en avant — c'est le
    comportement attendu, et `test_a_debate_where_the_groups_never_meet_shows_nothing`
    l'exige. Éprouver le bloc lui-même demande donc un accord dans le débat.
    """
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        participants = await _peupler(session, statements, prefixe=prefixe)
        commune = Statement(
            conversation_id=conversation_id,
            text="Celle-ci, tout le monde l'approuve.",
            moderation_status=ModerationStatus.approved,
        )
        session.add(commune)
        await session.flush()
        for participant in participants:
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=commune.id,
                    value=1,
                )
            )
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)
        resultats = await resultats_service.par_proposition(session, conversation)
    return slug, run, resultats


async def test_a_debate_where_the_groups_never_meet_shows_nothing(
    moderator_client, client, session_factory
) -> None:
    """Un débat où les camps s'opposent sur TOUT : aucun accord à mettre en avant.

    Le bloc disparaît plutôt que de ranger sous « ce sur quoi nous sommes d'accord »
    des propositions dont un groupe est à 32 %. Le tableau complet, lui, reste : il
    montre le débat tel qu'il est.
    """
    slug, _, resultats = await _debat_analyse(
        moderator_client, session_factory, prefixe="jamais-daccord"
    )

    assert resultats.consensuelles == []
    assert resultats.lignes != []

    page = (await client.get(f"/c/{slug}")).text
    assert 'id="consensus"' not in page
    assert "Ce que chaque groupe a répondu" in page


async def test_the_consensual_block_shows_the_least_divisive_propositions(
    moderator_client, session_factory
) -> None:
    """On CLASSE, on ne seuille pas — mais le titre filtre ce que le classement propose.

    Le L2 a mesuré pourquoi on ne seuille pas : sur les débats réels le consensus
    s'étale entre 0,37 et 0,65 et n'approche jamais 1, si bien qu'un seuil absolu ne
    retiendrait rien. Et le déploiement du L3 a montré pourquoi le classement ne suffit
    pas : ses premières places peuvent être des propositions sur lesquelles un groupe
    est à 32 %. Le bloc parcourt donc le classement et ne garde que celles dont tous
    les groupes penchent du même côté.
    """
    _, run, resultats = await _debat_avec_accord(moderator_client, session_factory)

    assert len(resultats.consensuelles) <= resultats_service.N_CONSENSUELLES
    # Ce sont les MÊMES objets que dans le tableau, pas des copies : un chiffre ne peut
    # pas différer d'un bloc à l'autre de la même page.
    for ligne in resultats.consensuelles:
        assert any(ligne is autre for autre in resultats.lignes)
        assert ligne.sens is not None

    async with session_factory() as session:
        classement = list(
            await session.scalars(
                select(StatementStat.statement_id)
                .where(
                    StatementStat.run_id == run.id,
                    StatementStat.group_id.is_(None),
                    StatementStat.clivage.isnot(None),
                )
                .order_by(StatementStat.clivage)
            )
        )
        textes = dict(
            (await session.execute(select(Statement.id, Statement.text))).all()
        )

    # La même règle, redérivée des SEULS chiffres affichés : le classement en base, dont
    # on retire ce que les barres démentent, puis les trois premières qui restent.
    par_texte = {ligne.texte: ligne for ligne in resultats.lignes}
    attendues = []
    for identifiant in classement:
        ligne = par_texte.get(textes[identifiant])
        if ligne is None:
            continue
        exprimes = [part for part in ligne.groupes if part.n_votes]
        unanimes = exprimes and (
            all(p.accord > p.desaccord for p in exprimes)
            or all(p.desaccord > p.accord for p in exprimes)
        )
        if unanimes:
            attendues.append(ligne.texte)
        if len(attendues) == resultats_service.N_CONSENSUELLES:
            break

    assert [ligne.texte for ligne in resultats.consensuelles] == attendues


def _ligne(*groupes: tuple[int, int, int]) -> resultats_service.LigneProposition:
    """Une ligne de résultats montée à la main : (accord, désaccord, votes) par groupe."""
    return resultats_service.LigneProposition(
        texte="peu importe",
        n_votes=sum(votes for _, _, votes in groupes),
        groupes=[
            resultats_service.PartGroupe(
                nom=chr(65 + rang),
                n_votes=votes,
                accord=accord,
                desaccord=desaccord,
                passe=100 - accord - desaccord if votes else 0,
            )
            for rang, (accord, desaccord, votes) in enumerate(groupes)
        ],
    )


def test_a_direction_needs_every_group_to_lean_that_way() -> None:
    """La règle qui empêche une phrase d'être démentie par la barre du dessous."""
    from app.services.clivage import Sens

    affichable = resultats_service.sens_affichable

    # Les deux groupes approuvent : la phrase tient.
    assert affichable(Sens.accord, _ligne((80, 20, 5), (60, 40, 5))) is Sens.accord
    # Le second groupe ne suit pas : plus d'étiquette du tout.
    assert affichable(Sens.accord, _ligne((100, 0, 5), (32, 68, 5))) is None
    # Symétrique pour le rejet.
    assert affichable(Sens.desaccord, _ligne((10, 90, 5), (20, 80, 5))) is Sens.desaccord
    assert affichable(Sens.desaccord, _ligne((10, 90, 5), (70, 30, 5))) is None
    # Un groupe qui n'a pas voté ne pèse ni dans un sens ni dans l'autre.
    assert affichable(Sens.accord, _ligne((80, 20, 5), (0, 0, 0))) is Sens.accord
    # Personne n'a voté : rien à annoncer.
    assert affichable(Sens.accord, _ligne((0, 0, 0), (0, 0, 0))) is None
    # Sans mesure, pas de direction, quoi que disent les barres.
    assert affichable(None, _ligne((80, 20, 5), (60, 40, 5))) is None


async def test_a_run_without_scores_highlights_nothing(
    moderator_client, session_factory
) -> None:
    """Un calcul antérieur au L2 n'a pas de score : le bloc disparaît, le tableau reste.

    C'est l'état de tous les calculs déjà en base le jour de la mise en ligne, et il ne
    doit pas faire disparaître l'écran de fin de parcours du G5.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="sans-score")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)

        # Comme si le calcul datait d'avant la migration 0012.
        for stat in await session.scalars(
            select(StatementStat).where(StatementStat.run_id == run.id)
        ):
            stat.clivage = None
        await session.commit()

        resultats = await resultats_service.par_proposition(session, conversation)

    assert resultats.calculee is True
    assert resultats.lignes != []
    assert resultats.consensuelles == []


async def test_what_characterises_each_group_is_finally_read(
    moderator_client, session_factory
) -> None:
    """`repness` est calculée depuis le C6 et n'était lue par aucun écran.

    L'identité stable de ces lignes n'est pas en base : elles portent l'étiquette brute
    de k-means, et c'est `run.group_mapping` qui la traduit. Si cette traduction
    tombait, la section serait vide sans erreur — d'où ce test.
    """
    _, _, resultats = await _debat_analyse(
        moderator_client, session_factory, prefixe="repness"
    )

    assert resultats.representatifs != []
    for groupe in resultats.representatifs:
        assert groupe.nom in resultats.noms
        assert 1 <= len(groupe.propositions) <= resultats_service.N_REPRESENTATIVES
        for proposition in groupe.propositions:
            assert proposition.texte
            assert proposition.sens.value in {"accord", "desaccord"}
    # Dans l'ordre des identités, comme les colonnes du tableau.
    assert [g.nom for g in resultats.representatifs] == sorted(
        g.nom for g in resultats.representatifs
    )


async def test_a_direction_is_never_contradicted_by_the_bars_below_it(
    moderator_client, session_factory
) -> None:
    """« Les groupes l'approuvent » n'est écrit que si tous les groupes l'approuvent.

    Le classement repose sur une moyenne géométrique : une proposition peut être la
    moins clivante d'un débat sans que chaque groupe l'approuve. Constaté sur le site
    réel — groupe A à 100 % d'accord, groupe B à 32 %, et l'écran annonçait « Les
    groupes l'approuvent » juste au-dessus de ces deux barres. La mesure était juste,
    la phrase ne l'était pas.
    """
    _, _, resultats = await _debat_avec_accord(
        moderator_client, session_factory, prefixe="direction"
    )

    for ligne in resultats.consensuelles:
        exprimes = [part for part in ligne.groupes if part.n_votes]
        if ligne.sens is None:
            continue
        if ligne.sens.value == "accord":
            assert all(part.accord > part.desaccord for part in exprimes)
        else:
            assert all(part.desaccord > part.accord for part in exprimes)


async def test_a_distinctive_group_is_not_an_opposed_group(
    moderator_client, session_factory
) -> None:
    """`repness` est un ÉCART, jamais une position — et l'écran doit dire les deux.

    Constaté sur le site réel au déploiement : « Non car trop d'accidents » figure parmi
    les propositions les plus consensuelles (23 pour, 15 contre, les deux groupes
    l'approuvent) **et** caractérise le groupe B par le désaccord, parce que ce groupe
    est en désaccord plus souvent que l'autre. Les deux sont vrais. L'étiquette seule se
    lisait « ce groupe est contre » : le pourcentage de ce groupe l'accompagne donc
    toujours, pris à la même source que la barre du tableau.
    """
    _, _, resultats = await _debat_analyse(
        moderator_client, session_factory, prefixe="ecart"
    )

    par_texte = {ligne.texte: ligne for ligne in resultats.lignes}
    for groupe in resultats.representatifs:
        for proposition in groupe.propositions:
            ligne = par_texte[proposition.texte]
            part = next(p for p in ligne.groupes if p.nom == groupe.nom)
            # Le chiffre montré est celui du tableau, pas un second calcul.
            assert proposition.accord == part.accord
            assert proposition.n_votes == part.n_votes


async def test_the_page_puts_the_agreement_before_the_table(
    moderator_client, client, session_factory
) -> None:
    """L'ordre est le message : ce qui rassemble d'abord, le détail ensuite."""
    slug, _, _ = await _debat_avec_accord(
        moderator_client, session_factory, prefixe="page-l3"
    )
    page = (await client.get(f"/c/{slug}")).text
    compact = " ".join(page.split())

    assert "Ce sur quoi nous sommes d'accord" in page
    assert "Ce qui caractérise chaque groupe" in page
    assert page.index("Ce sur quoi nous sommes d'accord") < page.index(
        "Ce que chaque groupe a répondu"
    )
    # Masqués comme le tableau : on ne souffle pas ses réponses à qui n'a pas voté.
    assert 'id="consensus" hidden' in compact
    assert 'id="representatifs" hidden' in compact
    # Et c'est la CLASSE qui les découvre, pas trois identifiants à tenir à jour.
    assert "bloc-fin" in page


async def test_no_block_announces_a_fracture(
    moderator_client, client, session_factory
) -> None:
    """Décision du client du 6 septembre 2026 : pas de section « ce qui vous sépare ».

    Le clivage reste mesuré et enregistré — il sert au tri du L4 — mais aucun écran ne
    range des propositions sous un titre qui annonce une fracture, et aucun score
    continu n'est rendu.
    """
    slug, _, _ = await _debat_analyse(
        moderator_client, session_factory, prefixe="pas-de-fracture"
    )
    page = (await client.get(f"/c/{slug}")).text.lower()

    assert "ce qui vous sépare" not in page
    assert "clivant" not in page
    assert "clivage" not in page
