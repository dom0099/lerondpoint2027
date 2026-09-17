"""D7 — banc à l'échelle : 50 -> 150 -> 400 participants, recalculs successifs.

Une conversation qui GROSSIT, pas une charge d'un coup. À chaque palier, deux
recalculs : celui qui suit l'arrivée des nouveaux, puis un second à données
rigoureusement inchangées — c'est le cas du visiteur qui regarde la carte deux fois
dans la même heure.
"""
import asyncio, random, resource, time, tracemalloc
from sqlalchemy import select, func
from app.db import get_sessionmaker
from app.models import (AnalysisStatus, ConversationState, Conversation, ModerationStatus,
                        Participant, ParticipantProjection, Statement, Vote)
from app.analysis import pipeline
from app.services import carte as carte_service, carte_rendu as rendu, votes as votes_service

N_DECLARATIONS = 30
PALIERS = [50, 150, 400]
RNG = random.Random(20260904)

#: Quatre camps de tailles inégales. Chacun a une opinion propre sur les déclarations
#: clivantes ; les déclarations consensuelles font l'unanimité chez tous. C'est la
#: structure d'une vraie consultation : quelques sujets rassemblent, d'autres séparent.
CAMPS = [("majorité", 0.40), ("opposition", 0.30), ("modérés", 0.20), ("marginaux", 0.10)]


def profils(n_declarations):
    """Pour chaque camp, sa probabilité d'être d'accord avec chaque déclaration."""
    consensuelles = set(RNG.sample(range(n_declarations), k=n_declarations // 5))
    profs = []
    for index, (_, _) in enumerate(CAMPS):
        p = []
        for d in range(n_declarations):
            if d in consensuelles:
                p.append(0.88)                       # tout le monde d'accord
            else:
                # Chaque camp a sa position, franche mais pas absolue.
                base = [0.85, 0.12, 0.55, 0.30][index]
                p.append(base if (d + index) % 3 else 1 - base)
        profs.append(p)
    return profs, consensuelles


PROFILS, CONSENSUELLES = profils(N_DECLARATIONS)


def camp_de(i):
    seuil, cumul = RNG.random(), 0.0
    for index, (_, part) in enumerate(CAMPS):
        cumul += part
        if seuil < cumul:
            return index
    return len(CAMPS) - 1


async def ajoute(session, conv, sids, depuis, jusqua):
    """Ajoute des participants avec des votes structurés et incomplets."""
    for i in range(depuis, jusqua):
        p = Participant(anon_token=f"d7-{i}")
        session.add(p)
        await session.flush()
        camp = camp_de(i)
        prof = PROFILS[camp]
        # Personne ne vote sur tout : 60 à 100 % des déclarations, comme en vrai.
        vus = RNG.sample(range(len(sids)), k=RNG.randint(int(len(sids) * 0.6), len(sids)))
        for d in vus:
            r = RNG.random()
            if r < 0.06:
                v = 0                                 # « passer »
            elif r < 0.06 + prof[d] * 0.94:
                v = 1
            else:
                v = -1
            session.add(Vote(participant_id=p.id, statement_id=sids[d], value=v))
        if i % 50 == 0:
            await session.flush()
    await session.commit()


async def etat(session, conv):
    """Composition par groupe STABLE et couleur de chaque groupe, au dernier run."""
    run = await pipeline.latest_ok_run(session, conv)
    lignes = (await session.execute(
        select(ParticipantProjection.participant_id, ParticipantProjection.stable_group_id)
        .where(ParticipantProjection.run_id == run.id,
               ParticipantProjection.stable_group_id.isnot(None)))).all()
    compo = {}
    for pid, g in lignes:
        compo.setdefault(g, set()).add(pid)
    carte = await carte_service.carte(session, conv, None)
    couleurs = rendu.couleurs_par_groupe(carte.groupes)
    # group_name() : 0 -> A. On refait le lien identité stable -> nom -> couleur.
    from app.analysis.matching import group_name
    par_stable = {g: couleurs[group_name(g)] for g in compo if group_name(g) in couleurs}
    return run, compo, par_stable, carte


def jaccard_commun(avant, apres):
    """Part des participants communs qui gardent leur identité de groupe."""
    place_avant = {p: g for g, m in avant.items() for p in m}
    place_apres = {p: g for g, m in apres.items() for p in m}
    communs = set(place_avant) & set(place_apres)
    if not communs:
        return 0.0, 0
    memes = sum(1 for p in communs if place_avant[p] == place_apres[p])
    return memes / len(communs), len(communs)


def couleurs_changees(avant, apres, coul_avant, coul_apres):
    """Part des participants communs pour qui la COULEUR affichée change."""
    ca = {p: coul_avant.get(g) for g, m in avant.items() for p in m}
    cb = {p: coul_apres.get(g) for g, m in apres.items() for p in m}
    communs = [p for p in ca if p in cb and ca[p] and cb[p]]
    if not communs:
        return 0.0, 0
    change = sum(1 for p in communs if ca[p] != cb[p])
    return change / len(communs), len(communs)


DUREES = {}
_vrai = pipeline._run_reddwarf
def _chrono(*a, **k):
    t = time.perf_counter()
    r = _vrai(*a, **k)
    DUREES["calcul"] = time.perf_counter() - t
    return r
pipeline._run_reddwarf = _chrono


async def main():
    sm = get_sessionmaker()
    async with sm() as s:
        conv = Conversation(slug="d7", title="Banc à l'échelle", state=ConversationState.open,
                            moderation_status=ModerationStatus.approved)
        s.add(conv); await s.flush()
        sids = []
        for i in range(N_DECLARATIONS):
            st = Statement(conversation_id=conv.id, text=f"Déclaration {i+1}.",
                           moderation_status=ModerationStatus.approved)
            s.add(st); await s.flush(); sids.append(st.id)
        await s.commit()

        print(f"{N_DECLARATIONS} déclarations, dont {len(CONSENSUELLES)} consensuelles ; "
              f"{len(CAMPS)} camps latents\n")
        print(f"{'palier':>7} {'votes':>7} {'run':>4} {'k':>2} {'seaux':>6} "
              f"{'calcul':>8} {'total':>8}  {'ident.':>7} {'couleur':>8}  note")

        precedent = None
        for cible in PALIERS:
            actuels = await s.scalar(select(func.count(Participant.id)))
            await ajoute(s, conv, sids, actuels, cible)
            n_votes = await s.scalar(select(func.count(Vote.id)))

            for passe in ("croissance", "immédiat"):
                t0 = time.perf_counter()
                run = await pipeline.analyse(s, conv)
                total = time.perf_counter() - t0
                assert run.status is AnalysisStatus.ok, run.error_text
                run, compo, coul, carte = await etat(s, conv)

                if precedent is None:
                    ident, coul_chg, n_comm = "—", "—", 0
                else:
                    j, n_comm = jaccard_commun(precedent[0], compo)
                    c, _ = couleurs_changees(precedent[0], compo, precedent[1], coul)
                    ident, coul_chg = f"{j:.1%}", f"{c:.1%}"
                d = run.params["couche"]
                print(f"{cible:>7} {n_votes:>7} {run.id:>4} {run.k:>2} "
                      f"{d.get('seaux', '—'):>6} {DUREES['calcul']:>7.2f}s {total:>7.2f}s  "
                      f"{ident:>7} {coul_chg:>8}  {passe} ({n_comm} communs)")
                precedent = (compo, coul)

        # --- la carte, à 400 participants ---
        t0 = time.perf_counter()
        carte = await carte_service.carte(s, conv, None)
        t_carte = time.perf_counter() - t0
        t0 = time.perf_counter()
        d = rendu.dessin(carte)
        t_dessin = time.perf_counter() - t0
        sommets = sum(len(c.points.split()) for c in d.contours)
        print(f"\ncarte : {t_carte*1000:.0f} ms (lecture) + {t_dessin*1000:.0f} ms (dessin) ; "
              f"{len(d.points)} points, {len(d.contours)} contours, {sommets} sommets, "
              f"légende {len(d.legende)}")

        # --- tirage pondéré des déclarations ---
        quelquun = await s.scalar(select(Participant).limit(1))
        tracemalloc.start()
        t0 = time.perf_counter()
        for _ in range(50):
            await votes_service.next_statement(s, conv, quelquun)
        t_tirage = (time.perf_counter() - t0) / 50
        _, pic = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"tirage pondéré : {t_tirage*1000:.1f} ms/appel, pic mémoire {pic/1024:.0f} Kio "
              f"(50 appels) — borné par les {N_DECLARATIONS} déclarations, pas par les participants")
        print(f"mémoire résidente max du processus : "
              f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} Mio")

asyncio.run(main())
