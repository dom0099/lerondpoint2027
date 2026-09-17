"""D7 (suite) — d'où vient la rupture de couleur, et que donnerait l'autre règle ?

Deux règles d'attribution comparées sur la MÊME séquence :
  A. par effectif décroissant  (règle actuelle, celle du document d'identité)
  B. par identité stable C6    (A -> bleu, B -> orange, C -> violet, D -> vert)
"""
import asyncio, random, time
from sqlalchemy import select, func
from app.db import get_sessionmaker
from app.models import (ConversationState, Conversation, ModerationStatus, Participant,
                        ParticipantProjection, Statement, Vote)
from app.analysis import pipeline
from app.analysis.matching import group_name
from app.services import carte as carte_service, carte_rendu as rendu

# Partie commune au banc d'échelle, recopiée pour que ce fichier soit autonome.
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


def couleur_par_effectif(compo):
    reels = sorted((g for g in compo if len(compo[g]) >= 2), key=lambda g: (-len(compo[g]), group_name(g)))
    return {g: rendu.PALETTE[i % len(rendu.PALETTE)] for i, g in enumerate(reels)}


def couleur_par_identite(compo):
    """L'identité stable EST le rang : le groupe A prend toujours le bleu."""
    return {g: rendu.PALETTE[g % len(rendu.PALETTE)] for g in compo if len(compo[g]) >= 2}


def part_changee(avant, apres, ca, cb):
    a = {p: ca.get(g) for g, m in avant.items() for p in m}
    b = {p: cb.get(g) for g, m in apres.items() for p in m}
    communs = [p for p in a if p in b and a[p] and b[p]]
    if not communs:
        return 0.0, 0
    return sum(1 for p in communs if a[p] != b[p]) / len(communs), len(communs)


async def main():
    sm = get_sessionmaker()
    async with sm() as s:
        conv = Conversation(slug="d7b", title="D7 suite", state=ConversationState.open,
                            moderation_status=ModerationStatus.approved)
        s.add(conv); await s.flush()
        sids = []
        for i in range(N_DECLARATIONS):
            st = Statement(conversation_id=conv.id, text=f"D{i+1}",
                           moderation_status=ModerationStatus.approved)
            s.add(st); await s.flush(); sids.append(st.id)
        await s.commit()

        precedent = None
        for cible in PALIERS:
            actuels = await s.scalar(select(func.count(Participant.id)))
            await ajoute(s, conv, sids, actuels, cible)
            run = await pipeline.analyse(s, conv)
            lignes = (await s.execute(
                select(ParticipantProjection.participant_id, ParticipantProjection.stable_group_id)
                .where(ParticipantProjection.run_id == run.id,
                       ParticipantProjection.stable_group_id.isnot(None)))).all()
            compo = {}
            for pid, g in lignes:
                compo.setdefault(g, set()).add(pid)

            ce, ci = couleur_par_effectif(compo), couleur_par_identite(compo)
            noms = {"#2E6FA7": "bleu", "#D07A24": "orange", "#8E5FA8": "violet", "#2F8A78": "vert"}
            print(f"\n=== {cible} participants (run {run.id}, k={run.k}) ===")
            print(f"  {'groupe':>7} {'effectif':>9} {'rang':>5} | {'A: effectif':>12} | {'B: identité':>12}")
            for g in sorted(compo, key=lambda g: -len(compo[g])):
                rang = sorted(compo, key=lambda x: (-len(compo[x]), x)).index(g) + 1
                print(f"  {group_name(g):>7} {len(compo[g]):>9} {rang:>5} | "
                      f"{noms.get(ce.get(g), '—'):>12} | {noms.get(ci.get(g), '—'):>12}")

            if precedent is not None:
                pa, pce, pci = precedent
                ea, n = part_changee(pa, compo, pce, ce)
                eb, _ = part_changee(pa, compo, pci, ci)
                print(f"  -> couleur changée pour les {n} participants communs : "
                      f"règle A (effectif) {ea:.1%}   |   règle B (identité) {eb:.1%}")
            precedent = (compo, ce, ci)
asyncio.run(main())
