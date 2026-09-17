"""Commandes d'administration.

    docker compose exec api python -m app.cli create-superuser \
        --email moderateur@exemple.fr --password '…'

Créer le premier modérateur ne peut pas passer par le web : il n'existe encore aucun
compte capable d'en créer un.
"""

import argparse
import asyncio
import getpass
import statistics
import sys
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.db import get_sessionmaker
from app.services import detection, nommage
from app.services.accounts import ensure_superuser


async def create_superuser(
    email: str, password: str, display_name: str | None, reset_password: bool
) -> int:
    async with get_sessionmaker()() as session:
        user, action = await ensure_superuser(
            session, email, password, display_name, reset_password=reset_password
        )

    message = {
        "created": f"Modérateur créé : {user.email} (id {user.id})",
        "promoted": f"{user.email} existait déjà : promu modérateur.",
        "password_reset": f"Mot de passe de {user.email} réinitialisé.",
        "unchanged": f"{user.email} est déjà modérateur (mot de passe inchangé —\n  ajoutez --reset-password pour le changer).",
    }[action]
    print(message)
    return 0


async def rapport_nommage(jours: int) -> int:
    """Combien de fois la règle « dès que nécessaire » s'est déclenchée (chantier E1).

    C'est le chiffre qui manque au dimensionnement du chantier E depuis le 3 septembre,
    et le seul que ni un calcul ni une hypothèse ne donnent.
    """
    depuis = datetime.now(timezone.utc) - timedelta(days=jours)
    async with get_sessionmaker()() as session:
        m = await nommage.mesure(session, depuis)

    print(f"Décisions de nommage sur {jours} jour(s) glissant(s)")
    print(f"  total            : {m['total']}")
    for motif, combien in sorted(m["par_motif"].items()):
        print(f"    {motif:<14} : {combien}")
    print(f"  débats concernés : {m['debats']}")
    print(f"  encore en attente: {m['en_attente']}")
    if m["delai_median_s"] is None:
        print("  délai médian     : aucun nom produit — nommage désactivé ou file vide")
    else:
        heures = m["delai_median_s"] / 3600
        alerte = "  *** SEUIL DU E3 DÉPASSÉ ***" if m["seuil_depasse"] else ""
        print(f"  délai médian     : {heures:.2f} h (empilement -> nom){alerte}")
    print(f"  jours observés   : {m['jours_observes']}")
    if m["par_jour"] is None:
        print("  par jour         : moins d'une journée observée — aucun taux affiché")
    else:
        print(f"  par jour         : {m['par_jour']}")
    return 0


# --------------------------------------------------------------------------------
# rapport-detection (chantier Modération, MOD-1)
# --------------------------------------------------------------------------------

#: Ce que le cadrage pariait pour 25 s au profil économique, avant toute mesure.
#: Le banc l'affiche à côté du chiffre mesuré : un banc qui ne rappelle pas ce qu'on
#: attendait laisse conclure « ça a l'air raisonnable » sur n'importe quel nombre.
PARI_DU_CADRAGE_OCTETS = 2_500_000

#: Largeur des traits du banc. 95 colonnes : le tableau tient dans un terminal
#: standard, et se colle dans un bloc de code Markdown sans se replier.
LARGEUR_BANC = 95

#: Combien d'exemples réels montrer par signal. Dix, c'est ce qu'on relit d'une
#: traite ; au-delà on ne lit plus, on fait défiler.
EXEMPLES_PAR_SIGNAL = 10

#: À partir de combien de signaux un texte passe dans la section du bas. Trois, c'est
#: le seuil demandé : ce sont les textes les plus intéressants à relire à la main.
SIGNAUX_CUMULES = 3

#: Les marqueurs qui entourent la portion déclenchante. En ASCII et doublés, pour que
#: la sortie se colle telle quelle dans un journal Markdown sans rien casser.
OUVRANT, FERMANT = "[[", "]]"


def _souligner(texte: str, signaux: list[detection.Signal]) -> str:
    """Entoure de marqueurs les portions déclenchantes, sans chevauchement.

    Les portions se recouvrent parfois (un chiffre dans une forme « Les X + verbe ») ;
    on les fusionne plutôt que d'imbriquer des marqueurs illisibles. Les sauts de
    ligne deviennent des « ⏎ » : une proposition sur trois lignes casserait
    l'alignement du rapport, et le rapport se lit en colonne.
    """
    portions = sorted((s.debut, s.fin) for s in signaux if s.fin > s.debut)
    fusionnees: list[list[int]] = []
    for debut, fin in portions:
        if fusionnees and debut <= fusionnees[-1][1]:
            fusionnees[-1][1] = max(fusionnees[-1][1], fin)
        else:
            fusionnees.append([debut, fin])

    morceaux, curseur = [], 0
    for debut, fin in fusionnees:
        morceaux.append(texte[curseur:debut])
        morceaux.append(f"{OUVRANT}{texte[debut:fin]}{FERMANT}")
        curseur = fin
    morceaux.append(texte[curseur:])
    return "".join(morceaux).replace("\n", " ⏎ ").replace("\r", "")


def _pourcent(combien: int, total: int) -> str:
    """Un pourcentage à une décimale, virgule française, sans division par zéro."""
    if total == 0:
        return "—"
    return f"{combien * 100 / total:.1f}".replace(".", ",") + " %"


async def _corpus_de_la_base() -> list[tuple[str, str]]:
    """Toutes les propositions, **quel que soit** leur statut de modération.

    Les rejetées sont les plus précieuses du lot : ce sont les cas que le détecteur
    devrait attraper, et la seule vérité de terrain dont ce chantier dispose.

    **Lecture seule, et pas seulement par intention.** La transaction est ouverte en
    `postgresql_readonly` : une écriture qui se glisserait ici — maintenant ou dans
    six mois — serait refusée par PostgreSQL lui-même, pas par la relecture du code.
    """
    from sqlalchemy import select

    from app.models.conversation import Statement

    async with get_sessionmaker()() as session:
        await session.connection(execution_options={"postgresql_readonly": True})
        lignes = await session.execute(
            select(Statement.text, Statement.moderation_status).order_by(Statement.id)
        )
        return [
            (texte, statut.value if hasattr(statut, "value") else str(statut))
            for texte, statut in lignes.all()
        ]


def _corpus_du_fichier(chemin: str) -> list[tuple[str, str]]:
    """Une proposition par ligne. Les lignes vides et les `#` sont ignorés.

    Existe pour pouvoir éprouver la liste sur des exemples écrits à la main, sans
    toucher à la base — et pour que le taux de déclenchement puisse se comparer entre
    un corpus réel et un corpus choisi.
    """
    with open(chemin, encoding="utf-8") as fichier:
        return [
            (ligne.rstrip("\n"), "fichier")
            for ligne in fichier
            if ligne.strip() and not ligne.lstrip().startswith("#")
        ]


async def rapport_detection(source: str, chemin: str | None) -> int:
    """Sur combien de propositions réelles le détecteur se déclenche, et lesquelles.

    C'est le livrable du MOD-1 : un nombre, pas un détecteur parfait. Si ce nombre
    est très au-dessus de la fourchette attendue, c'est la liste qu'il faut resserrer
    — avant que quoi que ce soit s'affiche devant un participant.
    """
    if source == "fichier":
        assert chemin is not None
        corpus = _corpus_du_fichier(chemin)
        origine = f"fichier {chemin}"
    else:
        corpus = await _corpus_de_la_base()
        origine = "base de production (lecture seule), tous statuts de modération"

    analyses = [(texte, statut, detection.analyser(texte)) for texte, statut in corpus]
    total = len(analyses)
    avec_signal = [a for a in analyses if a[2]]

    print("=" * 78)
    print("RAPPORT DE DÉTECTION — chantier Modération, MOD-1")
    print(f"Source : {origine}")
    print("=" * 78)
    print()
    print(f"Textes examinés            : {total}")
    print(
        f"Au moins un signal         : {len(avec_signal)}"
        f"  ({_pourcent(len(avec_signal), total)})"
    )
    print(
        f"Aucun signal               : {total - len(avec_signal)}"
        f"  ({_pourcent(total - len(avec_signal), total)})"
    )
    print()

    print("-" * 78)
    print("DÉTAIL PAR SIGNAL")
    print("-" * 78)
    print(f"  {'code':<22}{'textes':>8}{'part du corpus':>17}{'déclenchements':>17}")
    par_code: dict[str, list[tuple[str, str, list[detection.Signal]]]] = {}
    for code in detection.CODES:
        touches = [a for a in analyses if any(s.code == code for s in a[2])]
        par_code[code] = touches
        declenchements = sum(
            1 for _, _, signaux in analyses for s in signaux if s.code == code
        )
        print(
            f"  {code:<22}{len(touches):>8}{_pourcent(len(touches), total):>17}"
            f"{declenchements:>17}"
        )
    print()

    for code in detection.CODES:
        touches = par_code[code]
        print("-" * 78)
        print(
            f"{code.upper()} — {len(touches)} texte(s) sur {total} "
            f"({_pourcent(len(touches), total)})"
        )
        print("-" * 78)
        if not touches:
            print("  (aucun)")
            print()
            continue
        for texte, statut, signaux in touches[:EXEMPLES_PAR_SIGNAL]:
            portions = [s for s in signaux if s.code == code]
            print(f"  [{statut}] {_souligner(texte, portions)}")
        if len(touches) > EXEMPLES_PAR_SIGNAL:
            print(f"  … et {len(touches) - EXEMPLES_PAR_SIGNAL} autre(s).")
        print()

    cumules = [a for a in analyses if len({s.code for s in a[2]}) >= SIGNAUX_CUMULES]
    print("=" * 78)
    print(
        f"TEXTES PORTANT {SIGNAUX_CUMULES} SIGNAUX DISTINCTS OU PLUS — "
        f"{len(cumules)} sur {total} ({_pourcent(len(cumules), total)})"
    )
    print("=" * 78)
    if not cumules:
        print("  (aucun)")
    for texte, statut, signaux in cumules:
        codes_vus = sorted({s.code for s in signaux}, key=detection.CODES.index)
        print(f"  [{statut}] {', '.join(codes_vus)}")
        print(f"      {_souligner(texte, signaux)}")
    print()
    return 0


# --------------------------------------------------------------------------------
# rapport-signalements et purge-contexte-signalements (chantier Modération, MOD-3a)
# --------------------------------------------------------------------------------

#: Largeur des traits du rapport, comme `rapport-detection` : la sortie se colle telle
#: quelle dans un bloc de code Markdown sans se replier.
LARGEUR_SIGNALEMENTS = 78

#: Combien de propositions signalées lister, au plus. Dix, comme les exemples du
#: détecteur : c'est ce qu'on relit d'une traite.
PROPOSITIONS_LISTEES = 10

#: Longueur de la troncature du texte d'une proposition. Une proposition fait jusqu'à
#: plusieurs lignes ; le rapport se lit en colonne.
TEXTE_TRONQUE = 70


def _tronquer(texte: str, largeur: int = TEXTE_TRONQUE) -> str:
    """Le texte sur une ligne, coupé à `largeur`, sauts de ligne aplatis."""
    plat = " ".join(texte.split())
    return plat if len(plat) <= largeur else plat[: largeur - 1] + "…"


def _duree(secondes: float) -> str:
    """Une durée en français court, pour un délai de traitement."""
    if secondes < 3600:
        return f"{secondes / 60:.0f} min"
    if secondes < 86400:
        return f"{secondes / 3600:.1f} h".replace(".", ",")
    return f"{secondes / 86400:.1f} jours".replace(".", ",")


async def _chiffres_de_base(session) -> dict:
    """Les chiffres du §0 bis point 3 de la consigne MOD-3a.

    Ils sont dans CE rapport et pas dans un script jetable parce qu'ils ne servent pas
    une fois : ce sont eux qui disent si un seuil exprimé en pourcentage a un sens sur
    ce site, et ils changeront. Un chiffre qu'on ne peut pas rejouer est un chiffre
    qu'on finit par citer de mémoire.
    """
    from sqlalchemy import func, select

    from app.models import (
        AnalysisRun,
        AnalysisStatus,
        Conversation,
        ModerationStatus,
        Participant,
        Statement,
        User,
        Vote,
    )

    depuis = datetime.now(timezone.utc) - timedelta(days=30)
    actifs = await session.scalar(
        select(func.count(func.distinct(Participant.user_id)))
        .join(Vote, Vote.participant_id == Participant.id)
        .where(Participant.user_id.is_not(None), Vote.created_at >= depuis)
    )

    par_statut = {
        statut.value: await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.moderation_status == statut
            )
        )
        or 0
        for statut in ModerationStatus
    }

    votants = (
        select(func.count(func.distinct(Vote.participant_id)))
        .join(Statement, Statement.id == Vote.statement_id)
        .where(Statement.conversation_id == Conversation.id)
        .scalar_subquery()
    )
    debats = list(
        await session.execute(
            select(Conversation.id, Conversation.title, votants.label("votants"))
            .order_by(votants.desc(), Conversation.id)
            .limit(2)
        )
    )
    plus_fournis = []
    for conversation_id, titre, combien in debats:
        groupes = await session.scalar(
            select(AnalysisRun.k)
            .where(
                AnalysisRun.conversation_id == conversation_id,
                AnalysisRun.status == AnalysisStatus.ok,
            )
            .order_by(AnalysisRun.finished_at.desc())
            .limit(1)
        )
        plus_fournis.append((conversation_id, titre, combien, groupes))

    return {
        "comptes": await session.scalar(select(func.count(User.id))) or 0,
        "comptes_verifies": await session.scalar(
            select(func.count(User.id)).where(User.is_verified)
        )
        or 0,
        "comptes_actifs_30j": actifs or 0,
        "participants": await session.scalar(select(func.count(Participant.id))) or 0,
        "participants_sans_compte": await session.scalar(
            select(func.count(Participant.id)).where(Participant.user_id.is_(None))
        )
        or 0,
        "propositions_par_statut": par_statut,
        "votes": await session.scalar(select(func.count(Vote.id))) or 0,
        "debats": await session.scalar(select(func.count(Conversation.id))) or 0,
        "plus_fournis": plus_fournis,
    }


async def rapport_signalements() -> int:
    """Ce que le signalement a produit, et de quoi régler les seuils plus tard.

    **Lecture seule, et pas seulement par intention** : la transaction est ouverte en
    `postgresql_readonly`, comme `_corpus_de_la_base`. Une écriture qui se glisserait
    ici — maintenant ou dans six mois — serait refusée par PostgreSQL lui-même.
    """
    from app.services import signalement as regles
    from app.services import signalement_file

    async with get_sessionmaker()() as session:
        await session.connection(execution_options={"postgresql_readonly": True})
        m = await signalement_file.mesure(session)
        base = await _chiffres_de_base(session)

    trait = "=" * LARGEUR_SIGNALEMENTS
    sous_trait = "-" * LARGEUR_SIGNALEMENTS
    total = m["total"]

    print(trait)
    print("RAPPORT DE SIGNALEMENTS — chantier Modération, MOD-3a")
    print("Source : base de production (lecture seule)")
    print(trait)
    print()
    print(f"Signalements enregistrés   : {total}")
    print(f"  non traités              : {m['non_traites']}")
    print(
        "  âge du plus ancien       : "
        f"{signalement_file.age_lisible(m['age_du_plus_ancien']) or '—'}"
    )
    print()

    print(sous_trait)
    print("PAR MOTIF")
    print(sous_trait)
    print(f"  {'code':<24}{'famille':<16}{'signalements':>14}{'part':>12}")
    for motif in regles.MOTIFS:
        combien = m["par_motif"].get(motif.code, 0)
        print(
            f"  {motif.code:<24}{motif.famille.value:<16}{combien:>14}"
            f"{_pourcent(combien, total):>12}"
        )
    print()

    print(sous_trait)
    print("PAR ROUTE")
    print(sous_trait)
    for route, combien in m["par_route"].items():
        print(f"  {route.value:<24}{combien:>8}{_pourcent(combien, total):>12}")
    print()

    print(sous_trait)
    print("PAR STATUT, ET QUI SIGNALE")
    print(sous_trait)
    for statut, combien in m["par_statut"].items():
        print(f"  {statut.value:<24}{combien:>8}{_pourcent(combien, total):>12}")
    print(
        f"  {'venus d\'un compte':<24}{m['avec_compte']:>8}"
        f"{_pourcent(m['avec_compte'], total):>12}"
    )
    print(
        f"  {'venus d\'un visiteur':<24}{m['sans_compte']:>8}"
        f"{_pourcent(m['sans_compte'], total):>12}"
    )
    delais = m["delais_secondes"]
    if delais:
        print(f"  délai médian de traitement : {_duree(statistics.median(delais))}")
    else:
        print("  délai médian de traitement : aucun signalement traité")
    print(
        f"  portant encore un contexte : {m['avec_contexte']} "
        f"(purgé à {signalement_file.JOURS_CONTEXTE} jours)"
    )
    print()

    print(sous_trait)
    print(f"PROPOSITIONS LES PLUS SIGNALÉES ({PROPOSITIONS_LISTEES} au plus)")
    print(sous_trait)
    if not m["plus_signalees"]:
        print("  (aucune)")
    for statement_id, combien, statement in m["plus_signalees"][:PROPOSITIONS_LISTEES]:
        statut = statement.moderation_status.value if statement else "supprimée"
        texte = _tronquer(statement.text) if statement else "—"
        print(f"  #{statement_id:<6}{combien:>3} signalement(s)  [{statut}]  {texte}")
    print()

    print(trait)
    print("LES CHIFFRES DE LA BASE — ce qui décide de la valeur d'un seuil")
    print(trait)
    print(f"  comptes                          : {base['comptes']}")
    print(f"    dont vérifiés                  : {base['comptes_verifies']}")
    print(f"    dont actifs sur 30 jours       : {base['comptes_actifs_30j']}")
    print(f"  participants                     : {base['participants']}")
    print(
        f"    dont SANS compte               : {base['participants_sans_compte']}"
        f"  ({_pourcent(base['participants_sans_compte'], base['participants'])})"
    )
    print("  propositions par statut de modération :")
    for statut, combien in base["propositions_par_statut"].items():
        print(f"    {statut:<30} : {combien}")
    print(f"  votes                            : {base['votes']}")
    print(f"  débats                           : {base['debats']}")
    print("  les deux plus fournis :")
    for conversation_id, titre, votants, groupes in base["plus_fournis"]:
        print(
            f"    #{conversation_id} {votants} votant(s), "
            f"{groupes if groupes is not None else 'aucun calcul abouti'} groupe(s)"
        )
        print(f"        « {_tronquer(titre)} »")
    print()

    print(trait)
    print("EXPOSITION — le dénominateur qui manque pour calculer un taux")
    print(trait)
    for ligne in EXPOSITION.strip().splitlines():
        print(f"  {ligne}" if ligne else "")
    print()
    return 0


#: La réponse à la question posée au §3.8 de la consigne : « le nombre de fois où une
#: proposition a été MONTRÉE est-il disponible quelque part ? » Elle est écrite ici, dans
#: le rapport qui en a besoin, plutôt que dans le seul journal : c'est ce texte que lira
#: celui qui, dans six mois, voudra diviser un nombre de signalements par quelque chose.
EXPOSITION = """
Le nombre d'EXPOSITIONS n'existe nulle part. Rien dans la base ne compte les fois où
une proposition a été montrée : ni `statement`, ni `vote`, ni `statement_stat`. Le
tirage pondéré (`app/services/votes.py::next_statement`) choisit une proposition et la
rend ; il n'écrit rien. Aucun compteur n'a été ajouté par ce lot — un compteur
d'expositions est sur le CHEMIN DU VOTE, c'est-à-dire sur le seul geste dont le site
mesure le résultat, et cela se réfléchit avant de s'écrire.

Le nombre de VOTES enregistrés peut en tenir lieu, « passer » compris. C'est même le
seul substitut disponible, et il n'est pas mauvais : sur ce site, une proposition
montrée est une proposition sur laquelle on répond — le parcours de vote ne permet pas
de passer à la suivante sans se prononcer, et « passer » est l'une des trois réponses.
Le rapport vote/exposition est donc proche de 1.

Il coûte trois choses, à connaître avant de s'en servir :

  1. il SOUS-COMPTE les abandons. Quelqu'un qui lit une proposition choquante et ferme
     l'onglet n'a pas voté — et c'est précisément la personne la plus susceptible
     d'avoir voulu signaler. Le dénominateur manque donc surtout là où le numérateur
     est le plus fourni, ce qui GONFLE le taux de signalement ;
  2. `statement_stat.n_seen` existe, mais ne dit pas cela : il compte les votes retenus
     par le dernier calcul d'analyse, pour les participants éligibles seulement, et il
     est figé à l'instant du calcul. L'employer comme dénominateur ferait un taux qui
     bouge quand l'analyse tourne, pas quand quelqu'un lit ;
  3. il ne vaut RIEN pour une proposition retirée. Une proposition retirée sort du
     tirage : son compteur de votes s'arrête, alors que son compteur de signalements,
     lui, s'est arrêté au même instant. Le taux d'une proposition retirée est donc figé
     et non comparable à celui d'une proposition en circulation.

En résumé : oui, on peut diviser par le nombre de votes, à condition d'écrire à côté du
chiffre qu'il s'agit d'un taux par VOTE et non par LECTURE, et de ne pas le comparer
entre propositions retirées et propositions en circulation.
"""


async def purge_contexte_signalements(jours: int) -> int:
    """Efface le référent et l'IP des signalements de plus de `jours`.

    À mettre en tâche planifiée. **Les deux colonnes passent à NULL, rien d'autre ne
    bouge** : le signalement reste, son motif reste, sa route reste, son statut reste.
    Une durée de conservation qui n'est bornée que par le hasard d'un nouvel accès n'est
    pas une durée de conservation.
    """
    from app.services import signalement_file

    async with get_sessionmaker()() as session:
        combien = await signalement_file.purger_contexte(session, jours)
    print(
        f"Contexte effacé sur {combien} signalement(s) de plus de {jours} jour(s) "
        "(référent et adresse IP). Les signalements eux-mêmes sont conservés."
    )
    return 0


# --------------------------------------------------------------------------------
# rapport-validations (chantier Modération, MOD-4)
# --------------------------------------------------------------------------------


async def rapport_validations() -> int:
    """Ce que la validation aléatoire a produit. **Lecture seule.**

    Comme `rapport-signalements`, la transaction est ouverte en `postgresql_readonly` :
    une écriture qui se glisserait ici — maintenant ou dans six mois — serait refusée par
    PostgreSQL lui-même, et pas seulement par l'intention de celui qui l'a écrit.

    **Ce rapport sert à deux questions, et il faut les avoir en tête en le lisant.**
    La première est celle du lot : est-ce que le sondage produit quelque chose, ou
    est-ce qu'il a rendu zéro ligne comme la grille qu'il remplace ? La seconde est
    celle du lot d'après : les « conforme » sont la matière des habilitations du MOD-6,
    et leur nombre par personne dit si ce chiffre veut déjà dire quelque chose.

    **Ce qu'il montre et qui ne se voit nulle part ailleurs** : la part des signalements
    qui ont été provoqués par le site. Le MOD-5 l'écarte de ses indicateurs et a raison
    de le faire ; mais quelqu'un doit bien savoir dans quelle proportion la file du
    responsable est alimentée par les questions du site plutôt que par des plaintes.
    """
    from app.services import validation as regles
    from app.services import validation_tirage

    async with get_sessionmaker()() as session:
        await session.connection(execution_options={"postgresql_readonly": True})
        m = await validation_tirage.mesure(session)

    trait = "=" * LARGEUR_SIGNALEMENTS
    sous_trait = "-" * LARGEUR_SIGNALEMENTS
    total = m["total"]

    print(trait)
    print("RAPPORT DE VALIDATIONS — chantier Modération, MOD-4")
    print("Source : base de production (lecture seule)")
    print(trait)
    print()
    print(f"Validations rendues        : {total}")
    print(
        f"  « {regles.LIBELLES[regles.Verdict.conforme]} »".ljust(29)
        + f": {m['conformes']:>6}  {_pourcent(m['conformes'], total)}"
    )
    print(
        f"  « {regles.LIBELLES[regles.Verdict.a_revoir]} »".ljust(29)
        + f": {m['a_revoir']:>6}  {_pourcent(m['a_revoir'], total)}"
    )
    print()
    print(f"Personnes ayant répondu    : {m['repondants']}")
    print(f"Propositions regardées     : {m['propositions_vues']}")
    if m["repondants"]:
        print(
            f"  validations par personne : {total / m['repondants']:.1f} en moyenne "
            f"(plafond : {regles.SOLLICITATIONS_PAR_JOUR} par jour)"
        )
    print()

    print(sous_trait)
    print("LES RÉGLAGES EN VIGUEUR")
    print(sous_trait)
    print(f"  une sollicitation toutes les       : {regles.CADENCE_VOTES} propositions votées")
    print(
        f"  par personne et par {regles.FENETRE_HEURES} h          : "
        f"{regles.SOLLICITATIONS_PAR_JOUR} au plus"
    )
    print(
        f"  au-delà de N validations, retirée   : "
        f"{regles.VALIDATIONS_PAR_PROPOSITION} du tirage"
    )
    print()

    print(sous_trait)
    print("CE QUE LES « À REVOIR » ONT DÉCLENCHÉ, PAR ROUTE")
    print(sous_trait)
    par_route = m["signalements_sollicites_par_route"]
    if not par_route:
        print("  (aucun)")
    for route, combien in sorted(par_route.items(), key=lambda paire: -paire[1]):
        print(f"  {route:<28}{combien:>8}{_pourcent(combien, m['a_revoir']):>12}")
    print()

    print(sous_trait)
    print("LA PART DU SITE DANS SA PROPRE FILE")
    print(sous_trait)
    sollicites = sum(par_route.values())
    ensemble = sollicites + m["signalements_spontanes"]
    print(
        f"  signalements spontanés     : {m['signalements_spontanes']:>6}"
        f"  {_pourcent(m['signalements_spontanes'], ensemble)}"
    )
    print(
        f"  signalements sollicités    : {sollicites:>6}"
        f"  {_pourcent(sollicites, ensemble)}"
    )
    print()
    print("  Les sollicités sont ÉCARTÉS des indicateurs d'indépendance du MOD-5 :")
    print("  ils viennent de gens que le site a choisis, sur des propositions qu'il a")
    print("  choisies. Les y laisser ferait se déclencher la détection d'afflux")
    print("  coordonné sur le sondage du site lui-même.")
    print()
    return 0


# --------------------------------------------------------------------------------
# rapport-independance (chantier Modération, MOD-5)
# --------------------------------------------------------------------------------


async def rapport_independance() -> int:
    """Les cinq indicateurs d'indépendance, débat par débat.

    **Lecture seule, et rien d'autre.** Ce rapport n'agit sur rien : aucune sanction,
    aucun retrait, aucune pondération, aucun seuil armé.

    **À ne pas publier.** Les seuils de détection sont privés — les publier reviendrait à
    publier la notice pour les contourner. La règle est publique, le réglage ne l'est pas.
    """
    from app.services import independance
    from app.services import independance_lecture

    async with get_sessionmaker()() as session:
        await session.connection(execution_options={"postgresql_readonly": True})
        debats = await independance_lecture.debats_surveilles(session)

    trait = "=" * LARGEUR_SIGNALEMENTS
    sous_trait = "-" * LARGEUR_SIGNALEMENTS

    print(trait)
    print("RAPPORT D'INDÉPENDANCE — chantier Modération, MOD-5")
    print("Source : base de production (lecture seule)")
    print("USAGE INTERNE — ne pas publier : les seuils sont une défense anti-abus.")
    print(trait)
    print()

    if not debats:
        print("Aucun débat n'a reçu de signalement.")
        print()
        print("  Ce n'est pas une anomalie : le signalement n'est pas déployé, et la")
        print("  production est encore à la migration 0017. Ce rapport a une valeur telle")
        print("  quel — il dit que le compteur part de zéro, et c'est le point de départ")
        print("  de toute mesure ultérieure d'afflux coordonné.")
        print()
        print(sous_trait)
        print("LES SEUILS EN VIGUEUR (réglés sur les scénarios de test)")
        print(sous_trait)
        for code in independance.CODES:
            print(
                f"  {independance.LIBELLES[code]:<28}"
                f"{independance.SEUILS[code]:>8.2f}"
            )
        print(
            f"  {'indicateurs pour une alerte':<28}"
            f"{independance.INDICATEURS_POUR_ALERTE:>8}"
        )
        print()
        return 0

    alertes = [d for d in debats if d.analyse.alerte]
    print(f"Débats ayant reçu des signalements : {len(debats)}")
    print(f"Débats en alerte                   : {len(alertes)}")
    print()

    for d in debats:
        print(sous_trait)
        etat = (
            f"ALERTE — {len(d.analyse.depassements)} indicateurs au-delà du seuil"
            if d.analyse.alerte
            else "rien à signaler"
        )
        print(f"#{d.conversation.id} {_tronquer(d.conversation.title, 50)}")
        print(f"  {etat}")
        print(
            f"  {d.signalements} signalement(s), {d.analyse.signalants} signalant(s)"
        )
        print(sous_trait)
        for i in d.analyse.indicateurs:
            if not i.mesurable:
                valeur = "non mesurable"
            elif i.code == independance.RAFALE:
                valeur = f"x{i.valeur:.1f}".replace(".", ",")
            else:
                valeur = _pourcent(round(i.valeur * 100), 100)
            marque = " <<<" if i.depasse else ""
            print(f"  {i.libelle:<28}{valeur:>16}{marque}")
            print(f"      {i.detail}")
        print()

    print(trait)
    print("COMMENT LIRE CE RAPPORT")
    print(trait)
    print("  Une alerte demande AU MOINS DEUX indicateurs au-delà de leur seuil, jamais")
    print("  un seul : chacun pris isolément a une explication innocente — un débat qui")
    print("  ouvre n'a que des identités fraîches, un partage qui marche produit une")
    print("  rafale. C'est leur conjonction qui n'en a pas.")
    print()
    print("  « Non mesurable » n'est pas zéro. Les condensés d'adresse et de référent")
    print("  sont purgés à 30 jours : passé ce délai, on ne SAIT plus, et un débat dont")
    print("  on ne sait rien n'est pas un débat sain.")
    print()
    print("  Une alerte dit « regardez », pas « agissez » : ce lot n'a aucun effet sur")
    print("  aucune proposition.")
    print()
    return 0


# --------------------------------------------------------------------------------
# Le garde-fou commun aux rapports du chantier Modération
# --------------------------------------------------------------------------------


async def _avec_les_tables(rapport) -> int:
    """Lance un rapport, et explique plutôt que de tomber si les tables manquent.

    Les rapports du chantier Modération lisent des tables créées par ses migrations.
    Tant qu'elles ne sont pas appliquées, ils tombaient sur une trace d'exécution de
    quarante lignes se terminant par « relation "signalement" does not exist ».

    **Le numéro de migration n'est plus écrit ici**, et c'est le MOD-4 qui l'a appris :
    le texte disait « 0018 à 0020 », `rapport-validations` a besoin de la 0025, et un
    message d'aide qui nomme la mauvaise migration envoie chercher là où il n'y a rien —
    exactement ce que ce garde-fou existe pour éviter. La commande donnée plus bas dit
    où en est la base ; c'est elle qui répond, et elle ne vieillit pas.

    Ce n'est pas une panne, c'est un état attendu, et il mérite une phrase. Une trace
    d'exécution laisse croire à un défaut et envoie chercher là où il n'y a rien.
    """
    from sqlalchemy.exc import ProgrammingError

    try:
        return await rapport()
    except ProgrammingError as erreur:
        if "does not exist" not in str(erreur):
            raise
        manquante = str(erreur).split('relation "')[-1].split('"')[0]
        print(
            f"La table « {manquante} » n'existe pas dans cette base.",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        print(
            "  Ce rapport a besoin des migrations du chantier Modération, qui ne sont\n"
            "  pas toutes appliquées ici. Pour voir où en est la base :\n"
            "\n"
            "      docker compose exec -T db psql -U chantierc -d chantierc -tAc \\\n"
            "          \"SELECT version_num FROM alembic_version\"\n"
            "\n"
            "  Le mode opératoire de déploiement est dans NOTES-CHANTIER-MODERATION.md,\n"
            "  section « Restauration d'essai », §5.",
            file=sys.stderr,
        )
        return 1


# --------------------------------------------------------------------------------
# video-joindre et banc-video (chantier Vidéo, VIDEO-1)
# --------------------------------------------------------------------------------


def _mo(octets: float) -> str:
    """Des méga-octets à deux décimales, virgule française."""
    return f"{octets / 1_000_000:.2f}".replace(".", ",")


def _nombre(valeur: float, decimales: int = 2) -> str:
    """Un nombre à la française — la sortie se colle dans un journal en français."""
    return f"{valeur:.{decimales}f}".replace(".", ",")


async def video_joindre(chemin: str, debat_id: int, profil_nom: str) -> int:
    """Le pipeline complet sur un fichier local, écriture en base comprise.

    Point d'entrée de DÉVELOPPEMENT : il n'y a pas d'écran d'envoi dans ce lot, et il
    n'y en aura pas avant le VIDEO-2. Cette commande sert à voir le mécanisme marcher
    de bout en bout sur un vrai fichier — et c'est aussi elle qui écrit réellement les
    quatre colonnes, ce que le banc de mesure, lui, ne fait jamais.
    """
    from sqlalchemy import select

    from app.models.conversation import Conversation
    from app.services import video

    profil = video.PROFILS.get(profil_nom)
    if profil is None:
        connus = ", ".join(sorted(video.PROFILS))
        print(f"Profil inconnu : {profil_nom!r} (connus : {connus})", file=sys.stderr)
        return 1

    async with get_sessionmaker()() as session:
        debat = await session.scalar(
            select(Conversation).where(Conversation.id == debat_id)
        )
        if debat is None:
            print(f"Aucun débat d'identifiant {debat_id}.", file=sys.stderr)
            return 1

        print(f"Débat {debat.id} — {debat.title}")
        print(f"Fichier  : {chemin}")
        print(f"Profil   : {profil}")
        print()

        try:
            resultat = await video.preparer(
                chemin, profil=profil, sous_dossier=str(debat.id)
            )
        except video.VideoTropLongue as erreur:
            print(f"Refusé : {erreur}", file=sys.stderr)
            return 1
        except video.ErreurVideo as erreur:
            print(f"Échec : {erreur}", file=sys.stderr)
            return 1

        # Un débat n'a qu'une vidéo : remplacer la précédente laisserait sinon sur le
        # disque un fichier que plus rien ne désigne, et que personne n'irait chercher.
        ancienne = (debat.video_chemin, debat.video_miniature_chemin)

        debat.video_chemin = resultat.chemin_video
        debat.video_miniature_chemin = resultat.chemin_miniature
        debat.video_duree_secondes = resultat.duree_s
        debat.video_profil = resultat.profil
        await session.commit()

        for vieux in ancienne:
            if vieux and vieux not in (resultat.chemin_video, resultat.chemin_miniature):
                video.supprimer(vieux)

    source = resultat.sonde_source
    print(f"Durée mesurée       : {_nombre(resultat.duree_s)} s")
    print(
        f"Source              : {_mo(resultat.octets_source)} Mo — "
        f"{source.largeur}x{source.hauteur}, {source.codec_video}"
        f"/{source.codec_audio or 'sans son'}"
    )
    print(
        f"Transcodée          : {_mo(resultat.octets_video)} Mo — "
        f"{resultat.largeur}x{resultat.hauteur}, "
        f"{resultat.debit_effectif_kbps or '?'} kbit/s effectifs"
    )
    print(f"Miniature           : {_mo(resultat.octets_miniature)} Mo")
    print(f"Taux de compression : {_nombre(resultat.taux_compression, 1)} x")
    print(f"Temps d'encodage    : {_nombre(resultat.secondes_encodage)} s")
    print()
    print(f"Écrit en base sur le débat {debat_id} :")
    print(f"  video_chemin           = {resultat.chemin_video}")
    print(f"  video_miniature_chemin = {resultat.chemin_miniature}")
    print(f"  video_duree_secondes   = {resultat.duree_s}")
    print(f"  video_profil           = {resultat.profil}")
    print(f"(fichiers sous {video.racine_media()} — la base ne porte que ces chemins)")
    return 0


def _machine() -> str:
    """De quoi relire un temps d'encodage dans six mois sans se demander « sur quoi ? »."""
    import os
    import platform

    modele = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fichier:
            for ligne in fichier:
                if ligne.startswith("model name"):
                    modele = ligne.split(":", 1)[1].strip()
                    break
    except OSError:  # pragma: no cover - dépend du système
        pass
    return f"{modele or platform.processor() or 'processeur inconnu'} — {os.cpu_count()} cœurs"


def _version_ffmpeg() -> str:
    """La première ligne de `ffmpeg -version` : un temps d'encodage se lit avec elle."""
    import subprocess

    try:
        sortie = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return "ffmpeg introuvable"
    return sortie.splitlines()[0] if sortie else "version inconnue"


async def banc_video(dossier: str, profil_nom: str) -> int:
    """Combien pèse une vidéo de 25 s au profil retenu, et combien de temps elle coûte.

    C'est la question que le cadrage a laissée ouverte, et le livrable de ce lot. Le banc
    passe un corpus de vraies vidéos par le MÊME pipeline que la production — pas par une
    ligne `ffmpeg` recopiée pour l'occasion, qui finirait par en diverger — et n'écrit
    rien en base : il transcode dans un répertoire temporaire, mesure, puis efface.

    La résolution et le débit rapportés sont les EFFECTIFS, relus sur le fichier produit :
    x264 vise un débit moyen, il ne le tient pas au kilobit près, et c'est justement cet
    écart qu'il faut voir avant de conclure quoi que ce soit sur le poids d'un clip.
    """
    import tempfile
    from pathlib import Path

    from app.services import video

    profil = video.PROFILS.get(profil_nom)
    if profil is None:
        connus = ", ".join(sorted(video.PROFILS))
        print(f"Profil inconnu : {profil_nom!r} (connus : {connus})", file=sys.stderr)
        return 1

    if not video.outils_disponibles():
        print(
            "ffmpeg/ffprobe introuvables — dépendance SYSTÈME, à installer sur la "
            "machine (apt install ffmpeg), pas par pip.",
            file=sys.stderr,
        )
        return 1

    racine = Path(dossier).expanduser()
    if not racine.is_dir():
        print(f"Répertoire introuvable : {racine}", file=sys.stderr)
        return 1

    extensions = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".ogv"}
    fichiers = sorted(f for f in racine.iterdir() if f.suffix.lower() in extensions)
    if not fichiers:
        print(f"Aucun fichier vidéo dans {racine}.", file=sys.stderr)
        return 1

    print("=" * LARGEUR_BANC)
    print("BANC-VIDÉO — chantier Vidéo, VIDEO-1")
    print("=" * LARGEUR_BANC)
    print(f"Corpus  : {racine} ({len(fichiers)} fichiers)")
    print(f"Profil  : {profil}")
    print(
        f"Cadre   : borne {profil.cote_court}x{profil.cote_long} en portrait — forme "
        f"de la source conservée, jamais agrandie"
    )
    print(f"Machine : {_machine()}")
    print(f"Outil   : {_version_ffmpeg()}")
    print(
        f"Seuil   : {video.DUREE_MAX_SECONDES:.0f} s + "
        f"{video.TOLERANCE_DUREE_SECONDES:.0f} s de marge d'arrondi = rejet au-delà de "
        f"{video.SEUIL_REJET_SECONDES:.0f} s"
    )
    print()

    entete = (
        f"{'fichier':<26}{'durée':>8}{'source':>22}{'sortie':>22}"
        f"{'taux':>7}{'encodage':>10}"
    )
    print(entete)
    print("-" * len(entete))

    resultats: list[tuple[str, object]] = []
    refus: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="banc-video-") as temporaire:
        for fichier in fichiers:
            try:
                resultat = await video.preparer(
                    fichier, profil=profil, racine=Path(temporaire)
                )
            except video.VideoTropLongue as erreur:
                refus.append((fichier.name, f"{_nombre(erreur.duree, 1)} s — hors seuil"))
                continue
            except video.ErreurVideo as erreur:
                refus.append((fichier.name, str(erreur)))
                continue

            resultats.append((fichier.name, resultat))
            s = resultat.sonde_source
            print(
                f"{fichier.name:<26.26}"
                f"{_nombre(resultat.duree_s) + ' s':>8}"
                f"{_mo(s.octets) + ' Mo':>11}{f'{s.largeur}x{s.hauteur}':>11}"
                f"{_mo(resultat.octets_video) + ' Mo':>11}"
                f"{f'{resultat.largeur}x{resultat.hauteur}':>11}"
                f"{_nombre(resultat.taux_compression, 0) + 'x':>7}"
                f"{_nombre(resultat.secondes_encodage) + ' s':>10}"
            )

    print()
    if refus:
        print("-" * LARGEUR_BANC)
        print("REFUSÉS PAR LE PIPELINE")
        print("-" * LARGEUR_BANC)
        for nom, motif in refus:
            print(f"  {nom:<26.26} {motif}")
        print()

    if not resultats:
        print("Aucun fichier transcodé — rien à conclure.")
        return 1

    print("-" * LARGEUR_BANC)
    print("DÉTAIL PAR FICHIER")
    print("-" * LARGEUR_BANC)
    for nom, r in resultats:
        s = r.sonde_source
        debit_mesure = int(r.octets_video * 8 / 1000 / r.duree_s) if r.duree_s else 0
        print(f"  {nom}")
        print(
            f"      source   : {s.largeur}x{s.hauteur} {s.codec_video}"
            f"/{s.codec_audio or 'sans son'} — {s.debit_kbps or '?'} kbit/s, "
            f"{(s.images_par_seconde or 0):.0f} i/s, {_mo(s.octets)} Mo"
        )
        print(
            f"      sortie   : {r.largeur}x{r.hauteur} h264/aac — "
            f"{r.debit_effectif_kbps or '?'} kbit/s annoncés par le conteneur, "
            f"{debit_mesure} kbit/s recalculés sur le fichier, {_mo(r.octets_video)} Mo "
            f"(+ {_mo(r.octets_miniature)} Mo de miniature)"
        )
        print(
            f"      encodage : {_nombre(r.secondes_encodage)} s pour "
            f"{_nombre(r.duree_s)} s de vidéo "
            f"({_nombre(r.secondes_encodage / r.duree_s)} x le temps réel)"
        )
    print()

    tailles = [r.octets_video for _, r in resultats]
    durees = [r.duree_s for _, r in resultats]
    encodages = [r.secondes_encodage for _, r in resultats]
    # Ramené à 25 s : les clips du corpus ne durent pas tous pile la durée maximale, et
    # la question posée porte sur « une vidéo de 25 secondes ». À débit visé constant, le
    # poids d'un fichier est proportionnel à sa durée — la règle de trois est donc
    # légitime ici. Elle ne le serait PAS sur le temps d'encodage, qui dépend aussi de ce
    # que la machine faisait par ailleurs ; celui-là est rapporté brut.
    a_25s = [r.octets_video * video.DUREE_MAX_SECONDES / r.duree_s for _, r in resultats]

    print("=" * LARGEUR_BANC)
    print(f"SYNTHÈSE — {len(resultats)} fichiers, profil {profil.nom}")
    print("=" * LARGEUR_BANC)
    ramene = f"Ramené à {video.DUREE_MAX_SECONDES:.0f} s"
    print(
        f"  {'Poids produit':<24}: médian {_mo(statistics.median(tailles))} Mo, "
        f"min {_mo(min(tailles))} Mo, max {_mo(max(tailles))} Mo"
    )
    print(
        f"  {ramene:<24}: médian {_mo(statistics.median(a_25s))} Mo, "
        f"min {_mo(min(a_25s))} Mo, max {_mo(max(a_25s))} Mo"
    )
    print(
        f"  {'Durées du corpus':<24}: de {_nombre(min(durees))} s "
        f"à {_nombre(max(durees))} s"
    )
    print(
        f"  {'Temps d’encodage':<24}: médian {_nombre(statistics.median(encodages))} s, "
        f"min {_nombre(min(encodages))} s, max {_nombre(max(encodages))} s"
    )
    print(
        f"  {'Rapport au temps réel':<24}: médian "
        f"{_nombre(statistics.median([e / d for e, d in zip(encodages, durees)]))} x"
    )
    médiane_25 = statistics.median(a_25s)
    ecart = (médiane_25 - PARI_DU_CADRAGE_OCTETS) / PARI_DU_CADRAGE_OCTETS * 100
    print(
        f"  {'Pari du cadrage':<24}: {_mo(PARI_DU_CADRAGE_OCTETS)} Mo pour "
        f"{video.DUREE_MAX_SECONDES:.0f} s — mesuré {_mo(médiane_25)} Mo "
        f"({ecart:+.0f} %)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-superuser", help="crée ou promeut un modérateur")
    create.add_argument("--email", required=True)
    create.add_argument("--password", help="demandé de façon masquée si omis")
    create.add_argument("--display-name", default=None)
    create.add_argument(
        "--reset-password",
        action="store_true",
        help="remplace le mot de passe si le compte existe déjà",
    )

    rapport = sub.add_parser(
        "rapport-nommage", help="ce que la règle de nommage a déclenché (chantier E1)"
    )
    rapport.add_argument("--jours", type=int, default=7)

    detect = sub.add_parser(
        "rapport-detection",
        help="ce que le détecteur de signaux déclenche sur un corpus (MOD-1)",
    )
    # Deux sources, jamais les deux à la fois : un rapport qui mélangerait la base et
    # un fichier ne voudrait rien dire, puisque c'est un TAUX qu'il produit.
    origine = detect.add_mutually_exclusive_group()
    origine.add_argument(
        "--base",
        action="store_true",
        help="toutes les propositions en base, tous statuts (défaut, lecture seule)",
    )
    origine.add_argument(
        "--fichier", metavar="CHEMIN", help="un fichier texte, une proposition par ligne"
    )

    sub.add_parser(
        "rapport-independance",
        help="les cinq indicateurs d'afflux coordonné, par débat — INTERNE (MOD-5)",
    )

    sub.add_parser(
        "rapport-validations",
        help="ce que la validation aléatoire par les participants a produit (MOD-4)",
    )

    sub.add_parser(
        "rapport-signalements",
        help="ce que le signalement a produit, et les chiffres de la base (MOD-3a)",
    )

    purge = sub.add_parser(
        "purge-contexte-signalements",
        help="efface référent et IP des signalements de plus de 30 jours (MOD-3a)",
    )
    # Réglable, mais avec le défaut de la règle annoncée : une purge dont la durée se
    # choisit à chaque appel finirait par tourner avec la valeur d'un ancien essai.
    purge.add_argument("--jours", type=int, default=30)

    joindre = sub.add_parser(
        "video-joindre",
        help="transcode une vidéo locale et l'attache à un débat (VIDEO-1)",
    )
    joindre.add_argument("--fichier", required=True, metavar="CHEMIN")
    joindre.add_argument("--debat", required=True, type=int, metavar="ID")
    joindre.add_argument("--profil", default="economique")

    banc = sub.add_parser(
        "banc-video",
        help="poids et temps d'encodage d'un corpus, au profil retenu (VIDEO-1)",
    )
    banc.add_argument(
        "--dossier",
        required=True,
        metavar="CHEMIN",
        help="répertoire de vidéos sources — rien n'y est modifié, rien n'est écrit en base",
    )
    banc.add_argument("--profil", default="economique")

    args = parser.parse_args(argv)
    if args.command == "banc-video":
        return asyncio.run(banc_video(args.dossier, args.profil))
    if args.command == "video-joindre":
        return asyncio.run(video_joindre(args.fichier, args.debat, args.profil))
    if args.command == "purge-contexte-signalements":
        return asyncio.run(purge_contexte_signalements(args.jours))
    if args.command == "rapport-independance":
        return asyncio.run(_avec_les_tables(rapport_independance))
    if args.command == "rapport-validations":
        return asyncio.run(_avec_les_tables(rapport_validations))
    if args.command == "rapport-signalements":
        return asyncio.run(_avec_les_tables(rapport_signalements))
    if args.command == "rapport-detection":
        source = "fichier" if args.fichier else "base"
        return asyncio.run(rapport_detection(source, args.fichier))
    if args.command == "rapport-nommage":
        return asyncio.run(rapport_nommage(args.jours))
    if args.command == "create-superuser":
        password = args.password or getpass.getpass("Mot de passe : ")
        if len(password) < 8:
            print("Mot de passe trop court (8 caractères minimum).", file=sys.stderr)
            return 1
        try:
            return asyncio.run(
                create_superuser(
                    args.email, password, args.display_name, args.reset_password
                )
            )
        except ValidationError as erreur:
            # Une adresse mal formée est une faute de frappe, pas un incident :
            # elle mérite une phrase, pas une trace d'exécution.
            for detail in erreur.errors():
                champ = ".".join(str(p) for p in detail["loc"]) or "valeur"
                print(f"{champ} : {detail['msg']}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
