"""Ce que chaque groupe d'opinion a répondu, proposition par proposition.

Affiché en fin de parcours, sous « Merci de votre participation » : c'est la
contrepartie du vote. Quelqu'un qui vient de répondre à dix propositions a droit à
voir ce que les autres en ont fait — c'est ce que Pol.is montre après le vote, et ce
qui donne un sens à l'exercice.

**D'où viennent les deux moitiés du chiffre.** L'appartenance à un groupe est lue dans
le dernier calcul **abouti** (comme partout ailleurs : `groups.latest_ok_run`), mais
les votes sont comptés **tels qu'ils sont maintenant**, et non tels que le calcul les
avait vus. Ce n'est pas un oubli :

  - les votes qu'on vient d'émettre sont dans le total, donc l'écran ne dit pas
    « personne n'a répondu » à quelqu'un qui vient précisément de répondre ;
  - le calcul tourne au mieux toutes les heures, et un résultat vieux d'une heure
    présenté comme l'état courant serait faux d'une autre manière.

Ce qui est daté, c'est la RÉPARTITION en groupes, et la page le dit.

**Pourquoi pas `StatementStat`.** La table porte bien `n_agree`/`n_disagree`/`n_seen`,
mais seulement sur les lignes globales (`group_id NULL`). Les lignes par groupe ne sont
écrites que pour les propositions *représentatives* d'un groupe, et ne portent que
`n_agree` (voir `analysis/pipeline.py`). Elles ne permettent donc pas de remplir un
tableau complet : il faut compter les votes.

**Ne comptent que les participants classés.** Quelqu'un qui n'a pas assez voté pour
être situé n'appartient à aucun groupe ; l'ajouter quelque part reviendrait à lui en
inventer un. C'est la même règle qu'à la carte et qu'à la mini-barre de l'accueil.

CE QUE LE CHANTIER L3 AJOUTE, ET CE QU'IL N'AJOUTE PAS
======================================================
Deux blocs viennent encadrer le tableau, et ils lisent `statement_stat` — que le reste
de ce fichier évite délibérément (voir ci-dessus), parce qu'ils ne demandent pas des
comptes de votes mais des **mesures du calcul** :

  - **« Ce sur quoi nous sommes d'accord »** : les propositions les plus consensuelles
    au su des groupes (colonne `clivage`, écrite au L2) ;
  - **« Ce qui caractérise chaque groupe »** : la `repness` du C6, calculée à chaque
    passage depuis des mois et affichée nulle part jusqu'ici.

**Il n'y a pas de bloc « ce qui vous sépare », et c'est une décision du client**
(6 septembre 2026) : le site met en avant ce qui rassemble. Le clivage continue d'être
mesuré et enregistré — il sert au tri et à l'étiquette du L4 — mais aucun écran ne
range aujourd'hui des propositions sous un titre qui annonce une fracture.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.matching import group_name
from app.services import nommage
from app.models import (
    AnalysisRun,
    Conversation,
    ModerationStatus,
    ParticipantProjection,
    Statement,
    StatementStat,
    Vote,
)
from app.services import clivage
from app.services.clivage import Sens
from app.services.groups import MIN_GROUP_SIZE, latest_ok_run

#: Combien de propositions consensuelles la page met en avant. Trois, et non les cinq
#: de la moyenne du L1 : cette liste est lue, pas moyennée, et trois est ce qu'un
#: lecteur retient au bout d'un parcours de vote. Le tableau complet reste dessous.
N_CONSENSUELLES = 3

#: Combien de propositions caractérisent chaque groupe. red-dwarf en retient jusqu'à
#: cinq (`pick_max`) ; deux suffisent à faire comprendre ce qu'est un groupe, et deux
#: fois cinq lignes rendraient la section plus longue que le tableau qu'elle éclaire.
N_REPRESENTATIVES = 2


@dataclass
class PartGroupe:
    """Ce qu'un groupe a répondu à une proposition."""

    nom: str
    #: Membres du groupe ayant émis un vote (accord, désaccord ou passage) sur cette
    #: proposition. Zéro est un état normal : un groupe peut n'avoir jamais vu une
    #: proposition ajoutée après coup.
    n_votes: int
    #: Pourcentages ENTIERS, dont la somme vaut exactement 100 quand `n_votes > 0` :
    #: `passe` est le reste, pas un troisième arrondi. Trois arrondis indépendants
    #: donnent 99 % ou 101 %, et une barre qui ne remplit pas sa largeur.
    accord: int = 0
    desaccord: int = 0
    passe: int = 0


@dataclass
class LigneProposition:
    """Une proposition et la réponse de chaque groupe."""

    texte: str
    #: Tous groupes confondus — les participants classés SEULEMENT, pour que ce
    #: nombre soit bien la somme des `n_votes` des groupes en face.
    n_votes: int
    groupes: list[PartGroupe] = field(default_factory=list)
    #: Chantier L3 : de quel côté penche le consensus, `Sens.accord` (les groupes
    #: l'approuvent) ou `Sens.desaccord` (ils la rejettent). `None` quand la proposition
    #: n'a pas de score — c'est le cas de toutes celles d'un calcul antérieur au L2.
    sens: Sens | None = None


@dataclass
class PropositionRepresentative:
    """Une proposition qui distingue un groupe des autres (repness, chantier C6).

    **`sens` est un ÉCART, pas une position.** `repness` est un rapport de
    probabilités entre le groupe et tous les autres : `Sens.desaccord` dit « ce groupe
    est en désaccord plus souvent que les autres », et **non** « ce groupe est contre ».
    Un groupe peut approuver une proposition à 60 % et y être malgré tout
    « représentatif du désaccord », si les autres groupes l'approuvent à 90 %.

    Constaté sur le site réel au déploiement du L3 : « Non car trop d'accidents » figure
    parmi les propositions les plus consensuelles (23 pour, 15 contre, les deux groupes
    l'approuvent) **et** caractérise le groupe B par le désaccord. Les deux sont vrais.
    C'est pourquoi `accord` accompagne toujours `sens` : le pourcentage dit où le groupe
    se tient vraiment, l'étiquette dit seulement ce qui le distingue.
    """

    texte: str
    #: L'écart qui distingue ce groupe : `Sens.accord` = d'accord plus souvent que les
    #: autres ; `Sens.desaccord` = en désaccord plus souvent que les autres.
    sens: Sens
    #: Part d'accord DANS CE GROUPE, en pourcentage entier — la même valeur que la barre
    #: du tableau, prise à la même source.
    accord: int
    #: Votes de ce groupe sur cette proposition. Zéro : on n'affiche aucun pourcentage.
    n_votes: int


@dataclass
class GroupeRepresente:
    """Ce qui caractérise un groupe, dans l'ordre de représentativité décroissante."""

    nom: str
    propositions: list[PropositionRepresentative] = field(default_factory=list)

    #: Le nom généré, **une fois validé par un modérateur** (chantier E6). `None` tant
    #: qu'aucun n'a été validé — et l'écran montre alors « Groupe B » seul, comme avant.
    #:
    #: **Il s'ajoute à la lettre, il ne la remplace pas.** « Groupe B » reste l'identité
    #: que le C6 garantit stable d'un calcul à l'autre, et que la carte, l'accueil et le
    #: routage lisent tous. Un nom qui remplacerait la lettre ferait de chaque renommage
    #: une rupture apparente de continuité — le défaut que le C6 existe pour supprimer.
    nom_genere: str | None = None
    #: Le modérateur a-t-il réécrit ce nom avant de le valider ? La mention publique
    #: doit le dire : « généré par IA, validé » et « généré par IA, corrigé et validé »
    #: ne sont pas le même fait, et une mention de transparence approximative vaut moins
    #: que pas de mention.
    nom_corrige: bool = False


@dataclass
class Resultats:
    """Le bloc entier. `calculee` faux = il n'y a rien à montrer, et pourquoi."""

    calculee: bool = False
    #: Noms des groupes retenus, dans l'ordre de leur IDENTITÉ (A, B, C…) et non de
    #: leur effectif — même règle qu'à la mini-barre de l'accueil : les colonnes ne se
    #: réordonnent pas d'un recalcul à l'autre.
    noms: list[str] = field(default_factory=list)
    lignes: list[LigneProposition] = field(default_factory=list)
    #: Chantier L3 : les `N_CONSENSUELLES` propositions sur lesquelles les groupes se
    #: rejoignent le plus. **Les mêmes objets que dans `lignes`**, pas des copies : le
    #: gabarit les rend avec exactement le même bloc, et rien ne peut diverger.
    #: Vide tant qu'aucune proposition du dernier calcul n'a de score.
    consensuelles: list[LigneProposition] = field(default_factory=list)
    #: Chantier L3 : ce qui caractérise chaque groupe (`repness`, calculée depuis C6 et
    #: affichée nulle part jusqu'ici). Vide si le calcul n'en a retenu aucune.
    representatifs: list[GroupeRepresente] = field(default_factory=list)

    @property
    def n_groupes(self) -> int:
        return len(self.noms)


def pourcentages(accord: int, desaccord: int, total: int) -> tuple[int, int, int]:
    """Trois entiers qui somment à 100. Le passage prend le reste.

    Publique depuis le L6 : l'onglet « Le plus consensuel » construit les mêmes
    `PartGroupe` que cette page pour pouvoir appeler `sens_affichable` sur des entrées
    identiques. Deux arrondis écrits séparément finiraient par répondre différemment
    sur un cas limite, et les deux écrans se contrediraient sur la même proposition.
    """
    if total <= 0:
        return (0, 0, 0)
    part_accord = round(accord / total * 100)
    part_desaccord = round(desaccord / total * 100)
    return (part_accord, part_desaccord, 100 - part_accord - part_desaccord)


async def par_proposition(
    session: AsyncSession, conversation: Conversation
) -> Resultats:
    """Réponses de chaque groupe à chaque proposition approuvée.

    Trois requêtes au total, quel que soit le nombre de propositions ou de groupes.
    Renvoie `Resultats(calculee=False)` tant qu'aucun calcul n'a abouti ou qu'aucun
    groupe n'atteint `MIN_GROUP_SIZE` — il n'y a alors rien à dire, et une grille vide
    se lirait « tout le monde a passé ».
    """
    run = await latest_ok_run(session, conversation)
    if run is None:
        return Resultats()

    # --- 1. les groupes du dernier calcul, et leur effectif
    effectifs = (
        await session.execute(
            select(
                ParticipantProjection.stable_group_id,
                func.count(ParticipantProjection.id),
            )
            .where(
                ParticipantProjection.run_id == run.id,
                ParticipantProjection.stable_group_id.isnot(None),
            )
            .group_by(ParticipantProjection.stable_group_id)
        )
    ).all()

    # Un groupe d'une seule personne est un artefact du découpage, pas une opinion
    # partagée : la carte ne le nomme pas, ce tableau ne lui donne pas de colonne non
    # plus. Sans quoi une ligne « 100 % d'accord » désignerait une personne unique.
    retenus = sorted(
        stable for stable, taille in effectifs if taille >= MIN_GROUP_SIZE
    )
    if not retenus:
        return Resultats()

    noms = {stable: group_name(stable) or "?" for stable in retenus}

    # --- 2. les propositions, dans l'ordre où elles ont été ajoutées
    propositions = list(
        await session.execute(
            select(Statement.id, Statement.text)
            .where(
                Statement.conversation_id == conversation.id,
                Statement.moderation_status == ModerationStatus.approved,
            )
            .order_by(Statement.id)
        )
    )
    if not propositions:
        return Resultats()

    # --- 3. les votes, agrégés en base et non en Python : une ligne par
    # (proposition, groupe, valeur), soit au plus 3 × propositions × groupes.
    comptes = (
        await session.execute(
            select(
                Vote.statement_id,
                ParticipantProjection.stable_group_id,
                Vote.value,
                func.count(Vote.id),
            )
            .join(
                ParticipantProjection,
                ParticipantProjection.participant_id == Vote.participant_id,
            )
            .where(
                ParticipantProjection.run_id == run.id,
                ParticipantProjection.stable_group_id.in_(retenus),
                Vote.statement_id.in_([identifiant for identifiant, _ in propositions]),
            )
            .group_by(
                Vote.statement_id,
                ParticipantProjection.stable_group_id,
                Vote.value,
            )
        )
    ).all()

    # (proposition, groupe) -> {valeur: compte}
    brut: dict[tuple[int, int], dict[int, int]] = {}
    for statement_id, stable, valeur, compte in comptes:
        brut.setdefault((statement_id, stable), {})[valeur] = compte

    lignes: list[LigneProposition] = []
    for statement_id, texte in propositions:
        parts: list[PartGroupe] = []
        for stable in retenus:
            valeurs = brut.get((statement_id, stable), {})
            accord = valeurs.get(1, 0)
            desaccord = valeurs.get(-1, 0)
            total = accord + desaccord + valeurs.get(0, 0)
            pour, contre, passe = pourcentages(accord, desaccord, total)
            parts.append(
                PartGroupe(
                    nom=noms[stable],
                    n_votes=total,
                    accord=pour,
                    desaccord=contre,
                    passe=passe,
                )
            )
        lignes.append(
            LigneProposition(
                texte=texte,
                n_votes=sum(part.n_votes for part in parts),
                groupes=parts,
            )
        )

    par_identifiant = {
        statement_id: ligne
        for (statement_id, _), ligne in zip(propositions, lignes)
    }

    return Resultats(
        calculee=True,
        noms=[noms[stable] for stable in retenus],
        lignes=lignes,
        consensuelles=await _consensuelles(session, run, par_identifiant),
        representatifs=await _representatifs(session, run, noms, par_identifiant),
    )


async def _consensuelles(
    session: AsyncSession,
    run: AnalysisRun,
    par_identifiant: dict[int, LigneProposition],
) -> list[LigneProposition]:
    """Les propositions sur lesquelles les groupes se rejoignent le plus (L3).

    **On classe, on ne seuille pas.** Le L2 l'a mesuré sur les deux débats réels : le
    consensus des propositions s'étale entre 0,37 et 0,65, sans jamais approcher 1. Un
    seuil absolu (« au-dessus de 0,8 ») ne retiendrait donc rien, aujourd'hui comme
    demain — le lissage de red-dwarf interdit les valeurs extrêmes. « Les trois plus
    consensuelles » dit quelque chose sur tous les débats, y compris ceux qui divisent
    beaucoup, et c'est précisément là que la phrase d'accueil du site se vérifie.

    **Le classement propose, le titre dispose.** Une proposition n'entre dans le bloc
    que si les groupes penchent tous du même côté (`sens_affichable`) : autrement elle
    serait rangée sous « ce sur quoi nous sommes d'accord » alors qu'un groupe y est à
    32 %. Le bloc peut donc n'en montrer qu'une, ou disparaître, sur un débat très
    divisé — c'est voulu, et c'est moins cher qu'un titre qui affirme plus que ses
    chiffres.

    Le score lui-même n'est jamais rendu : il ordonne, il ne s'affiche pas.
    """
    scores = (
        await session.execute(
            select(
                StatementStat.statement_id,
                StatementStat.consensus_accord,
                StatementStat.consensus_desaccord,
            )
            .where(
                StatementStat.run_id == run.id,
                StatementStat.group_id.is_(None),
                StatementStat.clivage.isnot(None),
            )
            # Le plus consensuel est le moins clivant : même colonne, autre bout.
            # Pas de `LIMIT` : le classement est parcouru jusqu'à trouver
            # `N_CONSENSUELLES` propositions qui MÉRITENT le titre du bloc (voir la
            # règle ci-dessous). Un `LIMIT` aurait tronqué avant le tri utile.
            .order_by(StatementStat.clivage)
        )
    ).all()

    retenues: list[LigneProposition] = []
    for statement_id, accord, desaccord in scores:
        if len(retenues) >= N_CONSENSUELLES:
            break
        ligne = par_identifiant.get(statement_id)
        # Une proposition rejetée par la modération APRÈS le calcul a gardé sa
        # statistique et perdu sa ligne : elle ne doit pas reparaître ici.
        if ligne is None:
            continue
        sens = sens_affichable(clivage.sens_de(accord, desaccord), ligne)
        # **Le titre du bloc décide de ce qui y entre** (décision du client du
        # 6 septembre 2026, prise en voyant le rendu réel). Une proposition dont les
        # groupes ne penchent pas tous du même côté est peut-être la moins clivante du
        # débat ; elle n'est pas pour autant quelque chose « sur quoi nous sommes
        # d'accord ». Elle reste dans le tableau complet, avec ses barres.
        #
        # Le bloc peut donc n'afficher qu'une ligne, ou disparaître — et c'est la règle
        # du site partout ailleurs : pas de score plutôt qu'un zéro (L1), deux échecs
        # plutôt qu'un (K0), la mini-barre grise plutôt qu'une barre vide (G3). Un bloc
        # qui naît presque vide et se remplit avec l'audience a des précédents ici ; un
        # titre qui affirme plus que ses chiffres n'en a aucun.
        if sens is None:
            continue
        ligne.sens = sens
        retenues.append(ligne)
    return retenues


def sens_affichable(sens: Sens | None, ligne: LigneProposition) -> Sens | None:
    """N'affirme une direction que si les groupes penchent tous du même côté.

    **C'est la règle (b), retenue par le client le 6 septembre 2026**, et elle vaut
    maintenant pour deux écrans : le bloc de fin de parcours (L3) et l'onglet « Le plus
    consensuel » (L6). Elle est publique pour cette raison — l'écrire deux fois aurait
    laissé une liste affirmer ce que l'autre refuse d'affirmer sur la même proposition.


    Le classement du bloc est fondé sur une **moyenne géométrique** entre groupes : une
    proposition peut être la moins clivante d'un débat sans que tous les groupes
    l'approuvent. Constaté sur le site réel : « Non car trop d'accidents » sort dans les
    trois plus consensuelles, avec le groupe A à 100 % d'accord et le groupe B à 32 % —
    et l'écran annonçait « Les groupes l'approuvent » au-dessus de ces deux barres-là.

    La mesure n'était pas fausse ; la phrase l'était. Une direction n'est donc annoncée
    que si **chaque groupe qui s'est exprimé** penche de ce côté. Sinon, aucune
    étiquette : le classement suffit, et les barres disent le reste. Aucun texte de cette
    page ne doit pouvoir être démenti par le chiffre imprimé trois centimètres plus bas.
    """
    if sens is None:
        return None
    exprimes = [part for part in ligne.groupes if part.n_votes]
    if not exprimes:
        return None
    if sens is Sens.accord and all(p.accord > p.desaccord for p in exprimes):
        return Sens.accord
    if sens is Sens.desaccord and all(p.desaccord > p.accord for p in exprimes):
        return Sens.desaccord
    return None


async def _representatifs(
    session: AsyncSession,
    run: AnalysisRun,
    noms: dict[int, str],
    par_identifiant: dict[int, LigneProposition],
) -> list[GroupeRepresente]:
    """Ce qui caractérise chaque groupe — la `repness` du C6, enfin montrée.

    Calculée à chaque passage depuis des mois et lue par aucun écran jusqu'ici. Elle
    tient dans la même table, sur le même calcul, et parle au même lecteur que le bloc
    ci-dessus : les afficher séparément aurait demandé de concevoir deux fois la même
    grille.

    **L'identité stable se relit dans `run.group_mapping`.** `_persist` renseigne
    `stable_group_id` sur les projections de participants, mais pas sur ces lignes-là :
    elles portent l'étiquette brute de k-means, qui change à chaque calcul et ne veut
    rien dire pour un lecteur. Le mapping du calcul la traduit ; un groupe absent du
    mapping ou dont l'identité n'a pas été retenue (moins de `MIN_GROUP_SIZE` membres)
    est simplement sauté.
    """
    mapping = {str(brut): stable for brut, stable in (run.group_mapping or {}).items()}
    # Les noms validés, par identité stable. Rien n'est lu qui ne soit passé par un
    # modérateur : `noms_affichables` ne rend que les décisions approuvées (E5).
    valides = await nommage.noms_affichables(session, run.conversation_id)
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

    par_groupe: dict[int, list[PropositionRepresentative]] = {}
    for brut, statement_id, repful_for in lignes:
        stable = mapping.get(str(brut))
        ligne = par_identifiant.get(statement_id)
        if stable is None or stable not in noms or ligne is None:
            continue
        retenues = par_groupe.setdefault(stable, [])
        if len(retenues) >= N_REPRESENTATIVES:
            continue
        # La position réelle du groupe, prise à la même source que la barre du tableau.
        # Sans elle, l'étiquette d'écart se lirait comme une position — voir la note de
        # `PropositionRepresentative`.
        part = next(
            (part for part in ligne.groupes if part.nom == noms[stable]), None
        )
        retenues.append(
            PropositionRepresentative(
                texte=ligne.texte,
                sens=Sens.accord if repful_for == "agree" else Sens.desaccord,
                accord=part.accord if part is not None else 0,
                n_votes=part.n_votes if part is not None else 0,
            )
        )

    # Dans l'ordre des identités (A, B, C…), comme les colonnes du tableau.
    return [
        GroupeRepresente(
            nom=noms[stable],
            propositions=par_groupe[stable],
            nom_genere=(valides.get(stable) or {}).get("nom"),
            nom_corrige=bool((valides.get(stable) or {}).get("corrige")),
        )
        for stable in sorted(par_groupe)
    ]
