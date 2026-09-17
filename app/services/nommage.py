"""Quand un groupe d'opinion mérite un (nouveau) nom — chantier E1.

**Ce module ne nomme rien.** Il décide qu'un groupe *devrait* être nommé, écrit pourquoi,
et s'arrête là. Le modèle qui produira le nom viendra au E4 ; ce qui manque avant lui
n'est pas du code de génération, c'est un CHIFFRE : combien de fois la règle se déclenche
réellement. Tout le dimensionnement du chantier E en dépend — à un appel par débat et par
cycle, il faudrait tenir 20 tokens/seconde et un serveur dédié ; sous la règle « dès que
nécessaire », personne ne sait encore ce que ça coûte (`chantier-e-decisions.md`, §2).
Ce module produit ce chiffre sans dépenser un appel.

## La règle (E0, §5)

Un groupe est (re)nommé quand **l'une** de ces deux conditions est vraie, jamais autrement :

1. il n'a **jamais été nommé** et son identité stable tient depuis `PERSISTANCE_MINIMALE` ;
2. **ce qui le caractérise a changé** : au moins `DECLARATIONS_CHANGEES` de ses
   `N_DECLARATIONS` déclarations représentatives n'étaient pas là au nommage précédent —
   déclarations prises AVEC LEUR SENS, voir ci-dessous.

Trois choses ne déclenchent rien, et chacune est un piège qu'on a écarté exprès :

- **l'ordre des déclarations.** On compare des ensembles, pas des classements : deux
  déclarations qui échangent leur rang décrivent le même groupe ;
- **la taille du groupe.** Un groupe qui grossit ou maigrit sans changer d'opinions n'a
  aucune raison de changer de nom. C'est un écart assumé avec la COULEUR, qui suit le
  rang d'effectif depuis le G10 : la couleur dit « lequel est le plus gros », le nom dit
  « de quoi celui-ci parle », et ces deux questions n'ont pas les mêmes réponses ;
- **un simple recalcul.** C'est tout l'objet du découplage : les groupes se recalculent
  toutes les dix minutes, les noms suivent le changement réel.

## Le sens fait partie de la déclaration

Ce qu'on compare n'est pas un identifiant de déclaration mais un couple
`(déclaration, sens)`. La raison a été mesurée, pas supposée : sur le débat du permis à
16 ans (calcul 27, 6 septembre), les deux groupes d'opinion partagent **quatre
déclarations représentatives sur cinq**, avec des sens exactement inverses — l'un est
contre ce que l'autre approuve. C'est le cas normal, pas une curiosité : la `repness`
retient ce qui SÉPARE les groupes, et deux camps se séparent en s'opposant sur les mêmes
sujets.

Il en découle qu'un groupe qui retournerait sa position sur toutes ses déclarations — le
changement le plus radical qu'un groupe puisse subir — présenterait un ensemble
d'identifiants rigoureusement inchangé. Comparés nus, ces identifiants n'auraient rien
déclenché, et le nom serait resté en place pendant que le groupe devenait son contraire.

**Le même relevé porte un avertissement pour le E4** : envoyer au modèle les seuls textes
des déclarations lui donnerait, pour deux groupes opposés, une entrée presque identique.
Le sens doit voyager avec le texte, sans quoi les deux groupes recevraient des noms
interchangeables — et c'est précisément le mécanisme du biais mesuré chez Pol.is, dont
les résumés se sont révélés statistiquement plus proches d'un groupe que de l'autre.

## Ce que « au moins deux déclarations sur cinq » veut dire, exactement

`len(nouvelles - anciennes) >= 2` : **deux couples `(déclaration, sens)` qui n'y étaient
pas**. Une déclaration conservée mais retournée compte donc pour un couple nouveau, ce
qui est le comportement voulu — le groupe n'en dit plus la même chose.

Ce n'est pas la même chose que la différence symétrique, et la confusion coûterait cher.
Entre deux ensembles de même taille, la différence symétrique est toujours PAIRE : un
seul échange en donne déjà deux. Le seuil se déclencherait donc au moindre remplacement,
c'est-à-dire à peu près à chaque calcul — la règle « dès que nécessaire » redeviendrait
« à chaque cycle », et avec elle le serveur dédié qu'on cherche à éviter.

## L'ancienneté ne se stocke pas, elle se relit

La condition 1 demande depuis quand une identité de groupe existe. Rien n'est écrit pour
ça : `analysis_run.group_mapping` porte, calcul par calcul, quelles identités existaient,
et le G9 l'a explicitement exclue de la purge (« jamais effacée »). Un groupe « tient
depuis trois heures » s'il figure dans le dernier calcul abouti d'il y a au moins trois
heures ET dans tous ceux qui ont suivi.

Prendre le calcul de référence dans le passé plutôt que de mesurer une durée à partir
d'une date stockée a un avantage qui n'est pas évident : c'est robuste à un arrêt du
worker. Si rien n'a tourné pendant deux heures, la référence recule d'autant, et la
continuité reste vérifiée sur les calculs réellement faits — là où une date de première
apparition aurait laissé croire à une persistance que personne n'a observée.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalysisRun,
    AnalysisStatus,
    GroupNaming,
    ModerationStatus,
    MotifNommage,
    ParticipantProjection,
    Statement,
    StatementStat,
)
from app.services.groups import MIN_GROUP_SIZE

logger = logging.getLogger(__name__)

#: Depuis combien de temps une identité de groupe doit tenir avant d'être nommée.
#: Trois heures — décision du client du 6 septembre, qui remplace les 12 heures du
#: 3 septembre. À la cadence de dix minutes cela fait 18 calculs traversés : le filtre
#: reste sérieux contre les groupes éphémères, et l'affichage devient nettement plus
#: vivant. C'est le bas de la fourchette envisagée, donc le réglage le plus susceptible
#: de bouger après observation — d'où une DURÉE, jamais un nombre de calculs.
PERSISTANCE_MINIMALE = timedelta(hours=3)

#: Délai minimum entre deux nommages du MÊME groupe. Un nom qui change tous les quarts
#: d'heure n'est plus un nom, c'est un scintillement — et chaque changement coûtera au
#: E5 un passage en modération humaine. Ce garde-fou porte d'autant plus que le seuil de
#: persistance a été raccourci à trois heures.
DELAI_DE_GARDE = timedelta(hours=1)

#: Combien de déclarations représentatives décrivent un groupe pour le nommer. Cinq —
#: hypothèse du chiffrage du 5 septembre, qu'il faut donc tenir si l'on veut que le
#: volume de jetons mesuré corresponde à celui qui a été budgété.
#:
#: **Volontairement distinct de `resultats.N_REPRESENTATIVES`**, qui vaut 2. Celui-là
#: dit ce qu'un LECTEUR peut absorber dans une carte ; celui-ci dit ce qu'un modèle a
#: besoin de lire pour trouver un nom juste. Les confondre ferait qu'un jour où l'on
#: allège l'affichage, on dégraderait le nommage sans le voir.
N_DECLARATIONS = 5

#: Le seuil de réexamen de l'architecture, écrit au E3. Deux heures parce que c'est
#: l'ordre de grandeur du délai de modération humaine : en deçà, le nommage reste
#: invisible dans la chaîne ; au-delà, il en devient le goulot. Ce n'est pas une alarme
#: technique — la file peut fort bien tenir — c'est le moment de rouvrir la question de
#: la machine.
SEUIL_DELAI = timedelta(hours=2)

#: Combien de déclarations doivent être NOUVELLES pour que le nom soit refait. Voir la
#: note de l'en-tête : c'est `nouvelles - anciennes`, pas la différence symétrique.
DECLARATIONS_CHANGEES = 2


async def _identites_du_calcul(session: AsyncSession, run: AnalysisRun) -> set[int]:
    """Les identités stables assez grandes pour être un groupe, dans ce calcul.

    `group_mapping` contient TOUS les amas, y compris ceux d'une seule personne : le
    filtre de taille se fait donc ici, et seulement sur le calcul courant. Un groupe
    passé sous le seuil un après-midi puis remonté n'a pas changé d'identité, et lui
    refaire purger son ancienneté serait le punir d'un aléa de découpage.
    """
    if not run.group_mapping:
        return set()
    tailles: dict[int, int] = {}
    lignes = await session.execute(
        select(ParticipantProjection.stable_group_id).where(
            ParticipantProjection.run_id == run.id,
            ParticipantProjection.stable_group_id.isnot(None),
        )
    )
    for (stable,) in lignes:
        tailles[stable] = tailles.get(stable, 0) + 1
    return {stable for stable, n in tailles.items() if n >= MIN_GROUP_SIZE}


async def declarations_par_groupe(
    session: AsyncSession, run: AnalysisRun, n: int = N_DECLARATIONS
) -> dict[int, list[tuple[int, str]]]:
    """Les `n` déclarations les plus représentatives de chaque groupe, AVEC LEUR SENS.

    Aucun calcul nouveau : la `repness` est écrite à chaque passage depuis le C6. C'est
    exactement ce que le E4 enverra au modèle, et ce que la règle compare d'un nommage
    au suivant — les deux usages doivent lire la même chose, sans quoi on comparerait
    autre chose que ce qu'on a nommé.

    **`statement_stat` porte l'étiquette BRUTE de k-means**, qui change à chaque calcul
    et ne veut rien dire d'un tour à l'autre ; `group_mapping` la traduit en identité
    stable. C'est le même détour que `resultats._representatifs`, et l'oublier ferait
    comparer les déclarations du groupe A d'aujourd'hui à celles du groupe C d'hier.
    """
    mapping = {str(brut): stable for brut, stable in (run.group_mapping or {}).items()}
    lignes = (
        await session.execute(
            select(
                StatementStat.group_id,
                StatementStat.statement_id,
                StatementStat.repful_for,
            )
            .where(
                StatementStat.run_id == run.id,
                StatementStat.group_id.isnot(None),
                StatementStat.repness.isnot(None),
            )
            .order_by(StatementStat.group_id, StatementStat.repness.desc())
        )
    ).all()

    par_groupe: dict[int, list[tuple[int, str]]] = {}
    for brut, statement_id, sens in lignes:
        stable = mapping.get(str(brut))
        if stable is None:
            continue
        retenues = par_groupe.setdefault(stable, [])
        if len(retenues) < n:
            retenues.append((statement_id, sens or ""))
    return par_groupe


async def _identites_persistantes(
    session: AsyncSession,
    conversation_id: int,
    run: AnalysisRun,
    maintenant: datetime,
) -> set[int]:
    """Les identités présentes sans interruption depuis `PERSISTANCE_MINIMALE`.

    Lues dans l'historique des calculs, jamais dans une date stockée — voir l'en-tête.
    Rendre l'ensemble vide quand aucun calcul assez ancien n'existe est le bon défaut :
    un débat de moins de trois heures ne peut prouver aucune persistance de trois
    heures, et se taire vaut mieux que nommer sur une intuition.
    """
    reference = (
        await session.execute(
            select(AnalysisRun.id, AnalysisRun.finished_at, AnalysisRun.group_mapping)
            .where(
                AnalysisRun.conversation_id == conversation_id,
                AnalysisRun.status == AnalysisStatus.ok,
                AnalysisRun.finished_at.isnot(None),
                AnalysisRun.finished_at <= maintenant - PERSISTANCE_MINIMALE,
            )
            .order_by(AnalysisRun.finished_at.desc())
            .limit(1)
        )
    ).first()
    if reference is None:
        return set()

    _, borne, mapping_reference = reference
    persistantes = set((mapping_reference or {}).values())
    if not persistantes:
        return set()

    # Tous les calculs aboutis depuis la référence : l'identité doit être dans CHACUN.
    # Une seule absence casse la continuité — c'est un groupe qui a disparu puis
    # reparu, donc une identité qui n'a pas « tenu » trois heures.
    suivants = (
        await session.execute(
            select(AnalysisRun.group_mapping)
            .where(
                AnalysisRun.conversation_id == conversation_id,
                AnalysisRun.status == AnalysisStatus.ok,
                AnalysisRun.finished_at.isnot(None),
                AnalysisRun.finished_at > borne,
                AnalysisRun.id != run.id,
            )
            .order_by(AnalysisRun.finished_at)
        )
    ).all()
    for (mapping,) in suivants:
        persistantes &= set((mapping or {}).values())
        if not persistantes:
            return set()
    return persistantes


async def _dernieres_decisions(
    session: AsyncSession, conversation_id: int
) -> dict[int, GroupNaming]:
    """La décision la plus récente de chaque groupe du débat.

    Une seule requête pour tout le débat, et non une par groupe : c'est la boucle qui
    tourne à chaque calcul, sur chaque conversation recalculée. Le N+1 que le G9 avait
    trouvé dans le balayage du worker est né exactement comme ça.
    """
    lignes = (
        await session.execute(
            select(GroupNaming)
            .where(GroupNaming.conversation_id == conversation_id)
            .order_by(GroupNaming.stable_group_id, GroupNaming.decided_at.desc())
        )
    ).scalars()
    dernieres: dict[int, GroupNaming] = {}
    for ligne in lignes:
        dernieres.setdefault(ligne.stable_group_id, ligne)
    return dernieres


async def decide(
    session: AsyncSession,
    run: AnalysisRun,
    maintenant: datetime | None = None,
) -> list[GroupNaming]:
    """Applique la règle au calcul qui vient d'aboutir, et journalise ses décisions.

    N'écrit rien d'autre que des lignes de `group_naming` : aucun affichage ne change,
    aucun participant ne voit quoi que ce soit. C'est ce qui rend le E1 déployable en
    production sans validation d'écran — il mesure, il ne montre pas.

    Rend les décisions prises, pour que l'appelant les journalise ou les compte.
    """
    if run.status is not AnalysisStatus.ok:
        return []
    maintenant = maintenant or datetime.now(timezone.utc)

    reelles = await _identites_du_calcul(session, run)
    if not reelles:
        return []
    declarations = await declarations_par_groupe(session, run)
    dernieres = await _dernieres_decisions(session, run.conversation_id)
    persistantes: set[int] | None = None  # calculé au premier besoin seulement

    decisions: list[GroupNaming] = []
    for stable in sorted(reelles):
        nouvelles = declarations.get(stable) or []
        # Un groupe sans déclaration représentative n'est pas nommable : il n'y a
        # rien à lire pour trouver son nom. Se taire, plutôt que d'envoyer un
        # ensemble vide à un modèle qui inventerait quelque chose.
        if not nouvelles:
            continue

        precedente = dernieres.get(stable)
        if precedente is None:
            if persistantes is None:
                persistantes = await _identites_persistantes(
                    session, run.conversation_id, run, maintenant
                )
            if stable not in persistantes:
                continue
            motif = MotifNommage.nouveau
        else:
            if maintenant - precedente.decided_at < DELAI_DE_GARDE:
                continue
            # JSONB rend des listes ; la comparaison porte sur des couples. Sans cette
            # normalisation, `set(listes) - set(tuples)` ne partagerait aucun élément et
            # la règle se déclencherait à chaque calcul.
            anciennes = {tuple(d) for d in (precedente.declarations or [])}
            if len(set(nouvelles) - anciennes) < DECLARATIONS_CHANGEES:
                continue
            motif = MotifNommage.changement

        decision = GroupNaming(
            conversation_id=run.conversation_id,
            stable_group_id=stable,
            run_id=run.id,
            decided_at=maintenant,
            motif=motif,
            declarations=[[statement_id, sens] for statement_id, sens in nouvelles],
        )
        session.add(decision)
        decisions.append(decision)

    if decisions:
        await session.commit()
        logger.info(
            "nommage : %d groupe(s) à (re)nommer sur le calcul %s — %s",
            len(decisions),
            run.id,
            ", ".join(f"{d.stable_group_id}:{d.motif.value}" for d in decisions),
        )
    return decisions


async def mesure(session: AsyncSession, depuis: datetime | None = None) -> dict:
    """Ce que la règle a déclenché — le livrable réel du E1.

    Le chiffrage du 5 septembre prenait le PLAFOND (un appel par débat à chaque cycle)
    parce qu'on dimensionne sur un plafond, pas sur une moyenne. Cette fonction rend
    l'autre chiffre, celui que seule l'observation donne : ce que la règle « dès que
    nécessaire » laisse réellement passer. Le rapport des deux décide s'il faut monter
    d'un cran de machine au E3, ou si la VPS actuelle suffit.

    `par_jour` est ramené aux jours réellement observés et non à la fenêtre demandée :
    diviser par 7 une mesure de deux jours donnerait un chiffre trois fois trop bas, et
    c'est précisément le chiffre sur lequel on s'apprêterait à commander un serveur.
    """
    depuis = depuis or datetime.now(timezone.utc) - timedelta(days=7)
    lignes = (
        await session.execute(
            select(
                GroupNaming.conversation_id,
                GroupNaming.motif,
                GroupNaming.decided_at,
                GroupNaming.named_at,
                GroupNaming.nom,
            ).where(GroupNaming.decided_at >= depuis)
        )
    ).all()

    par_motif: dict[str, int] = {motif.value: 0 for motif in MotifNommage}
    debats: set[int] = set()
    premiere: datetime | None = None
    derniere: datetime | None = None
    delais: list[float] = []
    en_attente = 0
    for conversation_id, motif, quand, nomme_le, nom in lignes:
        par_motif[motif.value] += 1
        debats.add(conversation_id)
        premiere = quand if premiere is None or quand < premiere else premiere
        derniere = quand if derniere is None or quand > derniere else derniere
        if nom and nomme_le:
            delais.append((nomme_le - quand).total_seconds())
        elif not nom:
            en_attente += 1

    total = len(lignes)
    jours = 0.0
    if premiere is not None and derniere is not None:
        jours = max((derniere - premiere).total_seconds() / 86400, 0.0)
    return {
        "total": total,
        "par_motif": par_motif,
        "debats": len(debats),
        "jours_observes": round(jours, 2),
        # Sous une journée d'observation, aucun taux n'a de sens : le dire vaut mieux
        # que rendre un nombre qu'on citerait ensuite comme s'il était mesuré.
        "par_jour": round(total / jours, 1) if jours >= 1 else None,
        "en_attente": en_attente,
        # Le délai médian entre l'empilement d'une demande et la production du nom.
        # C'est la mesure sur laquelle le E3 a écrit son seuil de réexamen de
        # l'architecture : au-delà de deux heures de médiane sur une semaine, la file ne
        # tient plus le rythme et il faut monter d'un cran de machine (un GPU, pas un
        # serveur CPU intermédiaire — le E2 a mesuré que le CPU ne se rattrape pas en
        # ajoutant des cœurs).
        "delai_median_s": (
            round(sorted(delais)[len(delais) // 2]) if delais else None
        ),
        "seuil_depasse": (
            bool(delais) and sorted(delais)[len(delais) // 2] > SEUIL_DELAI.total_seconds()
        ),
    }


# =====================================================================================
# Modération avant affichage — chantier E5
# =====================================================================================


async def a_moderer(session: AsyncSession, limite: int = 50) -> list[GroupNaming]:
    """Les noms produits qui attendent un humain — **le plus récent par groupe seulement**.

    Un débat actif peut empiler plusieurs décisions pour le même groupe avant qu'un
    modérateur ne passe : les déclarations ont changé deux fois, la règle du E1 a produit
    deux noms. Les montrer tous ferait trancher sur des états périmés, et le modérateur
    validerait un nom déjà démenti par le calcul suivant.

    Seule la décision la plus récente de chaque groupe est donc proposée. Les autres
    restent en base — elles sont l'historique — mais ne seront jamais ni modérées ni
    affichées. C'est le même principe que la purge du G9 : ce qu'on garde sert à
    expliquer après coup, pas à décider.
    """
    lignes = (
        await session.scalars(
            select(GroupNaming)
            .where(
                GroupNaming.nom.isnot(None),
                GroupNaming.statut == ModerationStatus.pending,
            )
            .order_by(
                GroupNaming.conversation_id,
                GroupNaming.stable_group_id,
                GroupNaming.decided_at.desc(),
            )
        )
    ).all()
    vues: set[tuple[int, int]] = set()
    retenues: list[GroupNaming] = []
    for ligne in lignes:
        cle = (ligne.conversation_id, ligne.stable_group_id)
        if cle in vues:
            continue
        vues.add(cle)
        retenues.append(ligne)
        if len(retenues) >= limite:
            break
    return retenues


async def declarations_lisibles(
    session: AsyncSession, ligne: GroupNaming
) -> list[tuple[str, str]]:
    """Les déclarations d'une décision, textes résolus, pour l'écran de modération.

    **Le modérateur doit voir exactement ce que le modèle a vu.** Un nom seul ne se juge
    pas : « Les opposants à la baisse de l'âge du permis B » peut être juste ou
    exactement à l'envers, et rien dans la phrase ne le dit. Ce qui le dit, ce sont les
    déclarations approuvées et rejetées — le chantier E a mesuré trois fois qu'un
    résultat bien formé peut être entièrement faux.

    Le sens est normalisé ici comme il l'est pour le modèle (`nommage_llm.SENS`), pour
    la même raison qu'il l'est là-bas : `repful_for` parle anglais, l'écran parle
    français, et le décalage entre les deux a déjà produit une invite vide en
    production.
    """
    from app.services.nommage_llm import SENS

    couples = [(int(i), s) for i, s in (ligne.declarations or [])]
    if not couples:
        return []
    textes = dict(
        (
            await session.execute(
                select(Statement.id, Statement.text).where(
                    Statement.id.in_([i for i, _ in couples])
                )
            )
        ).all()
    )
    return [
        (textes[i], SENS[s]) for i, s in couples if i in textes and s in SENS
    ]


async def modere(
    session: AsyncSession,
    ligne: GroupNaming,
    moderateur,
    *,
    approuve: bool,
    nom_corrige: str | None = None,
    maintenant: datetime | None = None,
) -> GroupNaming:
    """Tranche sur un nom : validé (éventuellement corrigé), ou refusé.

    **Un refus n'est pas suivi d'une nouvelle tentative, et c'est délibéré.** Le modèle
    tourne à température basse et graine fixe : sur la même entrée il produirait
    exactement le même nom. Réessayer brûlerait du calcul pour obtenir le refus
    précédent. Le groupe attend donc le prochain changement réel de ses déclarations —
    c'est la règle du E1 qui le rouvrira — et garde « Groupe B » entre-temps.

    C'est aussi pourquoi la correction existe : sans elle, un nom presque juste serait
    perdu pour des jours.
    """
    ligne.statut = ModerationStatus.approved if approuve else ModerationStatus.rejected
    ligne.modere_par_id = getattr(moderateur, "id", None)
    ligne.modere_le = maintenant or datetime.now(timezone.utc)
    if approuve:
        propose = (nom_corrige or "").strip()
        ligne.nom_valide = propose[:120] if propose else ligne.nom
    await session.commit()
    return ligne


async def noms_affichables(
    session: AsyncSession, conversation_id: int
) -> dict[int, dict]:
    """Le nom à afficher pour chaque groupe d'un débat, ou rien.

    **Le dernier nom VALIDÉ, jamais le dernier produit.** Un nom en attente ne remplace
    pas celui qui est déjà à l'écran : sans cela, chaque déclenchement de la règle du E1
    ferait retomber le groupe sur « Groupe B » le temps qu'un modérateur passe, et
    l'affichage clignoterait au rythme de la modération. C'est la même exigence de
    stabilité que le C6 sur l'identité des groupes.

    Un refus ne fait rien disparaître non plus : le nom validé précédent, s'il existe,
    reste. Refuser un nouveau nom, c'est refuser un changement, pas effacer l'ancien.
    """
    lignes = (
        await session.scalars(
            select(GroupNaming)
            .where(
                GroupNaming.conversation_id == conversation_id,
                GroupNaming.statut == ModerationStatus.approved,
                GroupNaming.nom_valide.isnot(None),
            )
            .order_by(GroupNaming.stable_group_id, GroupNaming.decided_at.desc())
        )
    ).all()
    affichables: dict[int, dict] = {}
    for ligne in lignes:
        if ligne.stable_group_id in affichables:
            continue
        affichables[ligne.stable_group_id] = {
            "nom": ligne.nom_valide,
            # Ce que la mention publique du E6 devra dire : « validé » ou « corrigé et
            # validé ». Deux phrases différentes, parce que ce ne sont pas deux fois le
            # même fait.
            "corrige": ligne.corrige,
        }
    return affichables
