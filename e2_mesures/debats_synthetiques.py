"""Des débats français fabriqués, mais dont les groupes sont calculés par le VRAI moteur.

    docker compose exec -T -e DATABASE_URL=postgresql+asyncpg://chantierc:MDP@db:5432/chantierc_jetable \\
        api python - < e2_mesures/debats_synthetiques.py

**Ce que ce script corrige, et pourquoi il fallait le corriger.** Le corpus du premier E2
était écrit à la main : je choisissais les déclarations représentatives de chaque groupe.
Résultat, les modèles s'y débrouillaient bien et échouaient sur le seul débat réel. Un
corpus écrit par celui qui écrit l'invite mesure la facilité de son invite.

Ici, je n'écris que les déclarations et le comportement de vote de camps d'opinion. **Ce
sont red-dwarf, l'appariement du C6 et la `repness` qui décident** quels groupes existent
et ce qui les caractérise — exactement le chemin de production. Je ne connais donc plus
la réponse d'avance : je connais le camp de chaque votant, et le rattachement groupe ↔
camp se lit après coup, par majorité.

**Les déclarations imitent le vrai.** Elles commencent par « Oui… » ou « Non… », elles
sont courtes, elles ont des fautes. C'est la forme qu'ont prise les vraies déclarations
du débat sur le permis à 16 ans, et c'est très précisément ce qui a mis les deux modèles
en échec au premier E2 : l'invite produisait des lignes « CONTRE : Oui à la campagne ».
Un corpus qui gommerait ce trait masquerait le seul défaut qu'on cherche à corriger.

Tourne sur une base JETABLE, jamais sur la production — même règle que les bancs
`banc_d7_*` du chantier D.
"""

import asyncio
import json
import random

from sqlalchemy import select

from app.analysis import pipeline
from app.db import Base, get_sessionmaker, get_engine
from app.models import (
    Conversation,
    ConversationState,
    ModerationStatus,
    Participant,
    ParticipantProjection,
    Statement,
    StatementStat,
    Vote,
)

RNG = random.Random(20260906)

#: Quatre débats, chacun avec ses camps. `profil` donne, pour chaque déclaration, la
#: probabilité que ce camp l'approuve. Les déclarations sont dans l'ordre ; un camp qui
#: vaut 0.9 sur une déclaration l'approuve presque toujours.
DEBATS = [
    {
        "cle": "pietonnisation",
        "titre": "Faut-il piétonniser le centre-ville ?",
        "declarations": [
            "Oui, on respire enfin dans les rues",
            "Oui mais il faut garder l'accès aux livraisons",
            "Non les commerces vont fermer",
            "Non c'est impossible pour les personnes agées",
            "Oui si il y a un parking relais gratuit",
            "Non j'habite en centre ville je fais comment",
            "Oui les enfants pourront jouer dehors",
            "Non ça va reporter les bouchons ailleurs",
            "Oui mais seulement le week-end",
            "Il faudrait d'abord améliorer le bus",
            "Non les artisans ne pourront plus travailler",
            "Oui, comme dans les villes du nord de l'europe",
        ],
        "camps": {
            # pour la piétonnisation, franchement
            "pour": [0.92, 0.70, 0.08, 0.15, 0.60, 0.10, 0.90, 0.12, 0.30, 0.65, 0.10, 0.88],
            # contre, au nom des commerces et de l'accès
            "contre": [0.08, 0.55, 0.90, 0.88, 0.30, 0.92, 0.15, 0.85, 0.45, 0.55, 0.90, 0.10],
            # ni l'un ni l'autre : d'abord les transports, ensuite on verra
            "transports_dabord": [0.35, 0.60, 0.35, 0.40, 0.85, 0.40, 0.45, 0.55, 0.75, 0.95, 0.35, 0.30],
        },
        "parts": [0.42, 0.35, 0.23],
    },
    {
        "cle": "cameras",
        "titre": "Faut-il installer des caméras de surveillance dans le bourg ?",
        "declarations": [
            "Oui ça rassure les commerçants",
            "Non on n'est pas dans un état policier",
            "Oui il y a eu trop de dégradations cette année",
            "Non ça coûte cher et ça ne résout rien",
            "Oui mais uniquement devant l'école et la mairie",
            "Non les caméras déplacent la délinquance c'est tout",
            "Oui si les images sont effacées au bout d'une semaine",
            "Il vaudrait mieux payer un policier municipal de plus",
            "Non je refuse d'être filmé quand je vais au pain",
            "Oui ça a marché dans la commune d'à côté",
            "Non aucune preuve que ça marche vraiment",
        ],
        "camps": {
            "pour": [0.90, 0.10, 0.88, 0.12, 0.70, 0.15, 0.60, 0.35, 0.08, 0.85, 0.10],
            "contre": [0.10, 0.92, 0.20, 0.88, 0.25, 0.90, 0.30, 0.60, 0.90, 0.12, 0.88],
            "humain_plutot": [0.35, 0.55, 0.50, 0.70, 0.45, 0.60, 0.40, 0.95, 0.45, 0.30, 0.55],
        },
        "parts": [0.40, 0.38, 0.22],
    },
    {
        "cle": "mercredi",
        "titre": "Faut-il rétablir l'école le mercredi matin ?",
        "declarations": [
            "Oui les enfants sont moins fatigués sur 5 matinées",
            "Non le mercredi c'est le sport et la musique",
            "Oui c'est mieux pour les apprentissages, les chercheurs le disent",
            "Non ça complique tout pour les parents qui travaillent",
            "Oui mais alors il faut raccourcir les journées",
            "Non on vient de changer, on arrête de changer tout le temps",
            "Ça devrait être à chaque école de décider",
            "Oui, les autres pays font 5 jours",
            "Non les assos du village vont perdre leurs créneaux",
            "Le vrai sujet c'est la cantine pas le mercredi",
        ],
        "camps": {
            "pour": [0.90, 0.20, 0.88, 0.25, 0.75, 0.15, 0.40, 0.85, 0.20, 0.30],
            "contre": [0.12, 0.90, 0.18, 0.85, 0.30, 0.88, 0.55, 0.15, 0.88, 0.35],
            "autonomie": [0.45, 0.55, 0.45, 0.50, 0.50, 0.45, 0.95, 0.40, 0.50, 0.60],
            "hors_sujet": [0.40, 0.45, 0.35, 0.55, 0.40, 0.40, 0.50, 0.35, 0.45, 0.95],
        },
        "parts": [0.32, 0.30, 0.22, 0.16],
    },
    {
        "cle": "eolien_local",
        "titre": "Faut-il installer un parc éolien sur la commune ?",
        "declarations": [
            "Oui il faut bien produire l'électricité quelque part",
            "Non ça défigure complètement le paysage",
            "Oui les retombées fiscales financeraient la salle des fêtes",
            "Non et les oiseaux alors, personne n'en parle",
            "Oui mais loin des habitations",
            "Non, refusons ces machines imposées par paris",
            "Le vrai sujet c'est de consommer moins, pas de produire plus",
            "Oui, refuser chez nous ce qu'on accepte ailleurs c'est facile",
            "Non ça va faire baisser le prix des maisons",
            "Il faudrait plutôt du solaire sur les toits",
        ],
        "camps": {
            "pour": [0.90, 0.12, 0.85, 0.20, 0.70, 0.10, 0.35, 0.88, 0.15, 0.55],
            "contre_paysage": [0.15, 0.92, 0.25, 0.88, 0.35, 0.85, 0.50, 0.12, 0.85, 0.50],
            "sobriete": [0.40, 0.55, 0.35, 0.60, 0.45, 0.45, 0.95, 0.35, 0.45, 0.75],
        },
        "parts": [0.40, 0.38, 0.22],
    },
]

N_PARTICIPANTS = 90


async def construis(session, debat) -> tuple[Conversation, dict[int, str]]:
    """Crée le débat, ses déclarations, ses votants, et rend camp de chaque votant."""
    conversation = Conversation(
        slug=debat["cle"],
        title=debat["titre"],
        description="",
        state=ConversationState.open,
    )
    session.add(conversation)
    await session.flush()

    statements = []
    for texte in debat["declarations"]:
        statement = Statement(
            conversation_id=conversation.id,
            text=texte,
            moderation_status=ModerationStatus.approved,
        )
        session.add(statement)
        statements.append(statement)
    await session.flush()

    noms = list(debat["camps"])
    camp_de = {}
    for index in range(N_PARTICIPANTS):
        # Répartition par parts cumulées : les camps n'ont pas la même taille, et
        # c'est ce qui rend le découpage réaliste (le G10 fait dépendre la couleur du
        # rang d'effectif, donc des groupes égaux masqueraient un défaut d'affichage).
        tirage, cumul, camp = RNG.random(), 0.0, noms[-1]
        for nom, part in zip(noms, debat["parts"]):
            cumul += part
            if tirage <= cumul:
                camp = nom
                break
        participant = Participant(anon_token=f"{debat['cle']}-{index}")
        session.add(participant)
        await session.flush()
        camp_de[participant.id] = camp

        profil = debat["camps"][camp]
        for statement, probabilite in zip(statements, profil):
            # Tout le monde ne voit pas tout : c'est le cas réel, et cela fait varier
            # `n_seen`, donc la `repness`. Un vote sur chaque déclaration par chacun
            # donnerait une matrice pleine qu'aucune consultation ne produit.
            if RNG.random() < 0.15:
                continue
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement.id,
                    value=1 if RNG.random() < probabilite else -1,
                )
            )
    await session.commit()
    return conversation, camp_de


async def extrait(session, conversation, run, camp_de) -> dict:
    """Ce que le moteur a trouvé : groupes, camp majoritaire, déclarations retenues."""
    mapping = {str(b): s for b, s in (run.group_mapping or {}).items()}

    # Camp majoritaire de chaque identité stable — c'est ça, la vérité de terrain :
    # elle est CONSTATÉE après le calcul, pas décidée avant.
    membres: dict[int, list[str]] = {}
    for stable, participant_id in await session.execute(
        select(ParticipantProjection.stable_group_id, ParticipantProjection.participant_id)
        .where(ParticipantProjection.run_id == run.id,
               ParticipantProjection.stable_group_id.isnot(None))
    ):
        membres.setdefault(stable, []).append(camp_de[participant_id])

    lignes = (await session.execute(
        select(StatementStat.group_id, Statement.text, StatementStat.repful_for)
        .join(Statement, Statement.id == StatementStat.statement_id)
        .where(StatementStat.run_id == run.id,
               StatementStat.group_id.isnot(None),
               StatementStat.repness.isnot(None))
        .order_by(StatementStat.group_id, StatementStat.repness.desc())
    )).all()

    par_groupe: dict[int, list] = {}
    for brut, texte, sens in lignes:
        stable = mapping.get(str(brut))
        if stable is None:
            continue
        retenues = par_groupe.setdefault(stable, [])
        if len(retenues) < 5:
            retenues.append([texte, "pour" if sens == "agree" else "contre"])

    groupes = []
    for stable in sorted(par_groupe):
        camps = membres.get(stable, [])
        if len(camps) < 2:
            continue  # sous MIN_GROUP_SIZE : pas un groupe d'opinion
        dominant = max(set(camps), key=camps.count)
        purete = camps.count(dominant) / len(camps)
        groupes.append({
            "id": stable,
            "camp_majoritaire": dominant,
            "purete": round(purete, 2),
            "effectif": len(camps),
            "declarations": par_groupe[stable],
        })
    return {"cle": conversation.slug, "titre": conversation.title, "groupes": groupes}


async def principal() -> None:
    async with get_engine().begin() as connexion:
        await connexion.run_sync(Base.metadata.drop_all)
        await connexion.run_sync(Base.metadata.create_all)

    sortie = []
    async with get_sessionmaker()() as session:
        for debat in DEBATS:
            conversation, camp_de = await construis(session, debat)
            run = await pipeline.analyse(session, conversation)
            if run.status.value != "ok":
                print(f"  {debat['cle']:<16} ÉCHEC : {run.error_text}")
                continue
            resultat = await extrait(session, conversation, run, camp_de)
            sortie.append(resultat)
            print(f"  {debat['cle']:<16} k={run.k}  "
                  f"{len(resultat['groupes'])} groupe(s) retenus : "
                  + ", ".join(f"{g['id']}={g['camp_majoritaire']}({g['purete']})"
                              for g in resultat["groupes"]))
    print("---JSON---")
    print(json.dumps(sortie, ensure_ascii=False))


asyncio.run(principal())
