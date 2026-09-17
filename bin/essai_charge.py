"""Essai de charge — corpus synthétique réaliste, dans une pile jetable.

À lancer UNIQUEMENT dans la pile `deploy/compose-charge.yml`. Le garde-fou ci-dessous
refuse de démarrer si la base visée n'est pas `chantierc_charge` : ce script écrit des
milliers de lignes, il n'a rien à faire près de la production.

Ce qu'il mesure, et pourquoi :
  - le temps de `pipeline.analyse()` décomposé en extraction / red-dwarf / persistance,
    parce qu'un temps global ne dit pas quoi optimiser s'il dérive ;
  - le `k` retenu à chaque vague, en k automatique — le défaut de production ;
  - la CHURN des identités stables entre deux recalculs : les votes ne font que
    s'ajouter, donc tout changement d'identité est une instabilité d'étiquetage, pas
    un changement d'opinion. C'est le test de la méthode hongroise ;
  - la PURETÉ des groupes contre la vérité terrain semée : dit si le calcul a retrouvé
    les groupes plantés, et pas seulement qu'il n'a pas planté ;
  - la latence et la mémoire du tirage pondéré à cette échelle ;
  - (chantier H) les deux mêmes points **à l'échelle qui n'a encore jamais été
    essayée** : les vagues montent maintenant jusqu'à `VAGUES[-1]` participants — au
    moins les 400 déjà couverts côté carte 2D (chantier D), jamais éprouvés côté
    vote/calcul de groupes, dont la stabilité des identités n'avait été mesurée qu'à
    une soixantaine de participants ; et un débat à `N_CANDIDATS_GRANDE` propositions
    approuvées, le cas « trop de propositions pour toutes les montrer un jour » que le
    tirage pondéré n'avait jamais rencontré (il chargeait les objets `Statement`
    complets à chaque appel avant le H3, voir `votes_service.next_statement`).
"""

import asyncio
import os
import random
import resource
import statistics
import time
import tracemalloc
from collections import Counter, defaultdict

from sqlalchemy import func, insert, select

from app.config import settings

if not settings.database_url.endswith("/chantierc_charge"):
    raise SystemExit(
        f"base visée = {settings.database_url.rsplit('/', 1)[-1]} — "
        "cet essai n'écrit que dans chantierc_charge. Refus."
    )

from app.analysis import pipeline  # noqa: E402
from app.db import get_sessionmaker  # noqa: E402
from app.models import (  # noqa: E402
    Conversation,
    ConversationState,
    ModerationMode,
    ModerationStatus,
    Participant,
    ParticipantProjection,
    Statement,
    Vote,
)
from app.services import votes as votes_service  # noqa: E402

GRAINE = 20260903
N_DECLARATIONS = 30
N_CONSENSUS = 6           # déclarations sur lesquelles les 4 groupes s'accordent
N_GROUPES = 4
BRUIT = 0.12              # part des positions du groupe qu'un individu inverse
PASSE = 0.08              # part des déclarations sur lesquelles il passe (vote 0)
#: Participants cumulés, vague après vague. Montait à 60 jusqu'au chantier G ; porté à
#: 400 au chantier H pour rejoindre l'échelle déjà éprouvée côté carte 2D (chantier D,
#: `d0_mesures/banc_d7_echelle.py`), puis à 2000 pour aller AU-DELÀ — 400 n'avait rien
#: montré qui inquiète, la question devient jusqu'où ça tient. Réglable par
#: l'environnement pour un essai plus rapide en développement.
VAGUES = (
    [int(v) for v in os.environ["VAGUES"].split(",")]
    if os.environ.get("VAGUES")
    else [16, 32, 48, 60, 120, 240, 400, 800, 1200, 2000]
)
N_TIRAGES = 500
#: Nombre de propositions du débat dédié à la mesure du tirage pondéré à grande
#: échelle (chantier H) — le cas « trop de propositions pour toutes les montrer un
#: jour » du doc de cadrage V2, jamais essayé jusqu'ici (le corpus semé ci-dessus reste
#: à `N_DECLARATIONS`, pour ne pas fausser la mesure de pureté des groupes).
N_CANDIDATS_GRANDE = int(os.environ.get("N_CANDIDATS_GRANDE", 500))
N_TIRAGES_GRANDE = int(os.environ.get("N_TIRAGES_GRANDE", 200))
#: Nombre de groupes imposé (variable FORCE_K). Vide = k automatique, le défaut de
#: production. Sert à distinguer « red-dwarf ne sait pas séparer les groupes » de
#: « son heuristique de choix de k en compte moins qu'il n'y en a ».
FORCE_K = int(os.environ["FORCE_K"]) if os.environ.get("FORCE_K") else None

alea = random.Random(GRAINE)


def rss_mo() -> float:
    with open("/proc/self/status") as f:
        for ligne in f:
            if ligne.startswith("VmRSS:"):
                return int(ligne.split()[1]) / 1024
    return 0.0


def pic_rss_mo() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# --- instrumentation, sans toucher au code de production -------------------------
CHRONO: dict[str, float] = {}


def chronometre(nom, fonction, asynchrone=True):
    if asynchrone:
        async def enveloppe(*a, **kw):
            debut = time.perf_counter()
            try:
                return await fonction(*a, **kw)
            finally:
                CHRONO[nom] = time.perf_counter() - debut
        return enveloppe

    def enveloppe_sync(*a, **kw):
        debut = time.perf_counter()
        try:
            return fonction(*a, **kw)
        finally:
            CHRONO[nom] = time.perf_counter() - debut
    return enveloppe_sync


pipeline.extract_votes = chronometre("extraction", pipeline.extract_votes)
pipeline._persist = chronometre("persistance", pipeline._persist)
pipeline._run_reddwarf = chronometre("reddwarf", pipeline._run_reddwarf, asynchrone=False)


# --- corpus synthétique -----------------------------------------------------------
def positions_des_groupes() -> list[list[int]]:
    """Position latente de chaque groupe sur chaque déclaration.

    Les `N_CONSENSUS` premières font consensus : toute vraie conversation en contient,
    et ce sont précisément celles que la priorité de red-dwarf déclasse. Sans elles,
    le corpus serait plus facile à séparer que le réel.

    Deux corpus, et la différence entre les deux est tout l'intérêt de la mesure :

    `aleatoire` (défaut) — chaque groupe tire ses positions indépendamment. Réaliste,
    mais deux groupes peuvent sortir presque confondus par accident : c'est ce qui
    est arrivé à la première exécution (groupes 1 et 2 à 8 divergences sur 24). Les
    fusionner est alors le comportement CORRECT, pas un défaut.

    `separe` — les 24 déclarations clivantes sont réparties en 4 blocs de 6, chaque
    groupe défendant le sien. Toutes les paires divergent alors sur exactement 12
    déclarations sur 24. C'est l'expérience de CONTRÔLE : si le calcul ne retrouve
    pas 4 groupes ici, le problème est dans le calcul et non dans le corpus.
    """
    if os.environ.get("CORPUS") == "separe":
        positions = []
        clivantes = N_DECLARATIONS - N_CONSENSUS
        bloc = clivantes // N_GROUPES
        for groupe in range(N_GROUPES):
            ligne = [1] * N_CONSENSUS
            ligne += [1 if (i // bloc) == groupe else -1 for i in range(clivantes)]
            positions.append(ligne)
        return positions
    positions = []
    for groupe in range(N_GROUPES):
        ligne = [1] * N_CONSENSUS
        ligne += [alea.choice((1, -1)) for _ in range(N_DECLARATIONS - N_CONSENSUS)]
        positions.append(ligne)
    return positions


POSITIONS = positions_des_groupes()


def votes_dun_participant(groupe: int) -> dict[int, int]:
    """Votes d'un individu : la position de son groupe, bruitée."""
    couverture = alea.uniform(0.80, 1.0)
    indices = alea.sample(
        range(N_DECLARATIONS), k=max(8, round(N_DECLARATIONS * couverture))
    )
    resultat = {}
    for i in indices:
        tirage = alea.random()
        if tirage < PASSE:
            resultat[i] = 0
        elif tirage < PASSE + BRUIT:
            resultat[i] = -POSITIONS[groupe][i]
        else:
            resultat[i] = POSITIONS[groupe][i]
    return resultat


async def main() -> int:
    fabrique = get_sessionmaker()
    print(f"== essai de charge — graine {GRAINE} ==")
    print(f"{N_DECLARATIONS} déclarations, {N_GROUPES} groupes semés, "
          f"vagues {VAGUES} puis complément de votes")
    print(f"k = {'imposé à ' + str(FORCE_K) if FORCE_K else 'automatique (défaut de production)'}\n")

    async with fabrique() as session:
        conversation = Conversation(
            slug="essai-de-charge",
            title="Essai de charge",
            state=ConversationState.open,
            moderation_mode=ModerationMode.pre,
            moderation_status=ModerationStatus.approved,
            force_group_count=FORCE_K,
        )
        session.add(conversation)
        await session.flush()
        declarations = [
            Statement(
                conversation_id=conversation.id,
                text=f"Déclaration synthétique n°{i + 1}.",
                moderation_status=ModerationStatus.approved,
                is_seed=True,
            )
            for i in range(N_DECLARATIONS)
        ]
        for d in declarations:
            session.add(d)
        await session.commit()
        ids_declarations = [d.id for d in declarations]
        conversation_id = conversation.id

    verite_terrain: dict[int, int] = {}   # participant_id -> groupe semé
    votes_prevus: dict[int, dict[int, int]] = {}
    deja_ecrits: dict[int, set[int]] = defaultdict(set)
    ids_participants: list[int] = []
    historique: list[dict] = []

    total_precedent = 0
    for numero, cible in enumerate(VAGUES, start=1):
        async with fabrique() as session:
            nouveaux = []
            for rang in range(total_precedent, cible):
                groupe = rang % N_GROUPES
                p = Participant(anon_token=f"charge-{rang}")
                session.add(p)
                nouveaux.append((p, groupe))
            await session.flush()
            for p, groupe in nouveaux:
                verite_terrain[p.id] = groupe
                votes_prevus[p.id] = votes_dun_participant(groupe)
                ids_participants.append(p.id)

            lignes = []
            for p, _ in nouveaux:
                for i, valeur in votes_prevus[p.id].items():
                    lignes.append({
                        "participant_id": p.id,
                        "statement_id": ids_declarations[i],
                        "value": valeur,
                    })
                    deja_ecrits[p.id].add(i)
            await session.execute(insert(Vote), lignes)
            await session.commit()
        total_precedent = cible
        historique.append(await une_vague(fabrique, conversation_id, numero,
                                          verite_terrain, f"{cible} participants"))

    # Vague 5 : mêmes participants, votes complétés — la conversation mûrit sans
    # accueillir personne. C'est le cas où les identités DOIVENT tenir.
    async with fabrique() as session:
        lignes = []
        for pid in ids_participants:
            manquants = [i for i in range(N_DECLARATIONS) if i not in deja_ecrits[pid]]
            groupe = verite_terrain[pid]
            for i in manquants:
                tirage = alea.random()
                valeur = (0 if tirage < PASSE
                          else -POSITIONS[groupe][i] if tirage < PASSE + BRUIT
                          else POSITIONS[groupe][i])
                lignes.append({"participant_id": pid,
                               "statement_id": ids_declarations[i],
                               "value": valeur})
        if lignes:
            await session.execute(insert(Vote), lignes)
            await session.commit()
    historique.append(await une_vague(
        fabrique, conversation_id, len(VAGUES) + 1, verite_terrain,
        f"{VAGUES[-1]} participants, votes complétés"))

    stabilite(historique)
    await tirage_pondere(fabrique, conversation_id, ids_declarations)
    await tirage_a_grande_echelle(fabrique)
    return 0


async def une_vague(fabrique, conversation_id, numero, verite_terrain, etiquette):
    CHRONO.clear()
    async with fabrique() as session:
        conversation = await session.get(Conversation, conversation_id)
        n_votes = await session.scalar(
            select(func.count()).select_from(Vote.__table__)
        )
        tracemalloc.start()
        rss_avant = rss_mo()
        debut = time.perf_counter()
        run = await pipeline.analyse(session, conversation)
        total = time.perf_counter() - debut
        _, pic = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_apres = rss_mo()

        lignes = await session.execute(
            select(ParticipantProjection.participant_id,
                   ParticipantProjection.cluster_id,
                   ParticipantProjection.stable_group_id)
            .where(ParticipantProjection.run_id == run.id)
        )
        projections = {p: (c, s) for p, c, s in lignes}

    print(f"--- vague {numero} : {etiquette} ---")
    print(f"  statut={run.status.value}  k={run.k}  votes={n_votes}  "
          f"participants retenus={run.n_participants}  seuil={run.params['min_user_vote_threshold']}")
    print(f"  temps total analyse()  : {total * 1000:8.1f} ms")
    print(f"     extraction des votes: {CHRONO.get('extraction', 0) * 1000:8.1f} ms")
    print(f"     red-dwarf (PCA+kmeans){CHRONO.get('reddwarf', 0) * 1000:8.1f} ms")
    print(f"     persistance          : {CHRONO.get('persistance', 0) * 1000:8.1f} ms")
    print(f"  mémoire : pic tracemalloc {pic / 1024 / 1024:.1f} Mo — "
          f"RSS {rss_avant:.0f} -> {rss_apres:.0f} Mo (pic processus {pic_rss_mo():.0f} Mo)")

    tailles = Counter(s for _, s in projections.values() if s is not None)
    print(f"  groupes (identité stable -> effectif) : {dict(sorted(tailles.items()))}")

    croise = defaultdict(Counter)
    for pid, (_, stable) in projections.items():
        if stable is not None:
            croise[stable][verite_terrain[pid]] += 1
    puretes = []
    for stable, compte in sorted(croise.items()):
        majoritaire, n = compte.most_common(1)[0]
        purete = n / sum(compte.values())
        puretes.append(purete)
        print(f"    groupe {stable} : {sum(compte.values()):2d} membres, "
              f"{purete * 100:5.1f} % issus du groupe semé {majoritaire} — {dict(compte)}")
    if puretes:
        pondere = sum(p * sum(croise[s].values()) for p, s in zip(puretes, sorted(croise)))
        print(f"  PURETÉ GLOBALE : {pondere / sum(tailles.values()) * 100:.1f} %")
    print(f"  mapping k-means -> identité stable : {run.group_mapping}")
    print(f"  diagnostic du lisseur de k : {run.params.get('couche')}\n")
    return {"numero": numero, "projections": projections, "k": run.k,
            "mapping": run.group_mapping, "etiquette": etiquette}


def stabilite(historique):
    print("=== STABILITÉ DES IDENTITÉS ENTRE RECALCULS (méthode hongroise) ===")
    print("Les votes ne font que s'ajouter : tout changement d'identité stable est")
    print("une churn d'étiquetage, pas un changement d'opinion.\n")
    for avant, apres in zip(historique, historique[1:]):
        communs = set(avant["projections"]) & set(apres["projections"])
        changes = [p for p in communs
                   if avant["projections"][p][1] != apres["projections"][p][1]]
        print(f"  vague {avant['numero']} -> {apres['numero']} : "
              f"{len(communs):2d} participants comparables, "
              f"{len(changes):2d} ont changé d'identité "
              f"({len(changes) / len(communs) * 100 if communs else 0:.1f} %)")
        if changes:
            detail = Counter((avant["projections"][p][1], apres["projections"][p][1])
                             for p in changes)
            print(f"      mouvements : {dict(detail)}")
    print()


async def tirage_pondere(fabrique, conversation_id, ids_declarations):
    print("=== TIRAGE PONDÉRÉ DES DÉCLARATIONS À CETTE ÉCHELLE ===")
    async with fabrique() as session:
        conversation = await session.get(Conversation, conversation_id)
        neufs = [Participant(anon_token=f"charge-tirage-{i}") for i in range(10)]
        for p in neufs:
            session.add(p)
        await session.commit()
        for p in neufs:
            await session.refresh(p)

        # Cas le plus coûteux : un arrivant qui n'a rien vu, donc 30 candidates.
        tracemalloc.start()
        rss_avant = rss_mo()
        latences = []
        for i in range(N_TIRAGES):
            p = neufs[i % len(neufs)]
            debut = time.perf_counter()
            choisie = await votes_service.next_statement(session, conversation, p)
            latences.append((time.perf_counter() - debut) * 1000)
            assert choisie is not None
        _, pic = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_apres = rss_mo()

    latences.sort()
    print(f"  {N_TIRAGES} tirages, 30 candidates à chaque appel (pire cas)")
    print(f"     min    {latences[0]:6.2f} ms")
    print(f"     médiane{statistics.median(latences):6.2f} ms")
    print(f"     p95    {latences[int(len(latences) * 0.95)]:6.2f} ms")
    print(f"     max    {latences[-1]:6.2f} ms")
    print(f"     moyenne{statistics.fmean(latences):6.2f} ms")
    print(f"  mémoire sur les {N_TIRAGES} tirages : pic tracemalloc "
          f"{pic / 1024 / 1024:.1f} Mo — RSS {rss_avant:.0f} -> {rss_apres:.0f} Mo")
    print(f"  (chaque appel refait la requête latest_priorities() : c'est voulu, "
          f"une déclaration devenue prioritaire remonte au tirage suivant)")


async def tirage_a_grande_echelle(fabrique) -> None:
    """Tirage pondéré sur un débat à `N_CANDIDATS_GRANDE` propositions (chantier H).

    Débat séparé du corpus semé ci-dessus : celui-là doit rester à `N_DECLARATIONS`
    pour ne pas fausser la mesure de pureté des groupes. Deux profils de participant,
    parce que `next_statement` a deux coûts distincts (voir `votes_service.py`) :

      - un ARRIVANT sans aucun vote : la liste de candidates elle-même est maximale
        (`N_CANDIDATS_GRANDE` objets `Statement` COMPLETS, texte inclus, chargés en
        mémoire à chaque appel — c'est le point que le doc de cadrage V2 identifie
        comme jamais vérifié à cette échelle) ;
      - un VÉTÉRAN qui a déjà voté sur presque tout : la liste de candidates est
        petite, mais le `NOT IN` qui l'exclut porte sur un ensemble presque aussi
        grand que le débat lui-même — un coût différent, côté requête SQL plutôt que
        côté objets Python.
    """
    print("=== TIRAGE PONDÉRÉ SUR UN DÉBAT À BEAUCOUP DE PROPOSITIONS (chantier H) ===")
    async with fabrique() as session:
        conversation = Conversation(
            slug="essai-de-charge-grande-echelle",
            title="Essai de charge — beaucoup de propositions",
            state=ConversationState.open,
            moderation_mode=ModerationMode.pre,
            moderation_status=ModerationStatus.approved,
        )
        session.add(conversation)
        await session.flush()
        declarations = [
            Statement(
                conversation_id=conversation.id,
                text=f"Déclaration synthétique grande échelle n°{i + 1}.",
                moderation_status=ModerationStatus.approved,
                is_seed=True,
            )
            for i in range(N_CANDIDATS_GRANDE)
        ]
        session.add_all(declarations)
        arrivant = Participant(anon_token="charge-grande-echelle-arrivant")
        veteran = Participant(anon_token="charge-grande-echelle-veteran")
        session.add_all([arrivant, veteran])
        await session.commit()
        ids_declarations = [d.id for d in declarations]

        # Le vétéran a voté sur tout sauf les 10 dernières : peu de candidates, mais
        # un NOT IN qui porte sur presque tout le débat.
        lignes = [
            {"participant_id": veteran.id, "statement_id": sid, "value": 1}
            for sid in ids_declarations[:-10]
        ]
        await session.execute(insert(Vote), lignes)
        await session.commit()

        for profil, participant in (("arrivant (0 vote)", arrivant),
                                     ("vétéran (voté sur tout sauf 10)", veteran)):
            tracemalloc.start()
            rss_avant = rss_mo()
            latences = []
            for _ in range(N_TIRAGES_GRANDE):
                debut = time.perf_counter()
                choisie = await votes_service.next_statement(session, conversation, participant)
                latences.append((time.perf_counter() - debut) * 1000)
                assert choisie is not None
            _, pic = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rss_apres = rss_mo()

            latences.sort()
            print(f"  profil {profil} — {N_CANDIDATS_GRANDE} propositions au débat")
            print(f"     min    {latences[0]:6.2f} ms")
            print(f"     médiane{statistics.median(latences):6.2f} ms")
            print(f"     p95    {latences[int(len(latences) * 0.95)]:6.2f} ms")
            print(f"     max    {latences[-1]:6.2f} ms")
            print(f"     moyenne{statistics.fmean(latences):6.2f} ms")
            print(f"  mémoire sur les {N_TIRAGES_GRANDE} tirages : pic tracemalloc "
                  f"{pic / 1024 / 1024:.1f} Mo — RSS {rss_avant:.0f} -> {rss_apres:.0f} Mo\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
