"""La méthode Schulze : agréger des classements ordonnés (MOD-9).

**Le cas d'usage**, et il commande tout le reste : plusieurs relecteurs classent des
reformulations concurrentes d'une même proposition, et il faut en tirer un ordre final.

**Pourquoi pas un vote ordinaire.** Cinq personnes qui reformulent la même idée écrivent
cinq textes voisins. Avec un vote à un tour, ces cinq quasi-doublons **se divisent les
voix** et une unique alternative moins soutenue l'emporte — c'est le partage des voix, et
c'est exactement ce qu'une navette de reformulation produirait naturellement, puisqu'elle
encourage les variantes. La méthode Schulze n'a pas ce défaut : elle compare les
candidats **deux à deux**, donc trois variantes d'une même idée ne s'affaiblissent pas
mutuellement face à un texte qu'une minorité préfère.

**Ce module est pur.** Aucune session, aucune base, aucune entrée/sortie — même
discipline que `signalement.router` et `grille.decision`. On peut le régler et l'éprouver
sans rien monter, et c'est ce qui permet de le livrer **avant** son consommateur.

**Il n'a aujourd'hui aucun consommateur, et c'est voulu.** MOD-10b — la navette outillée
— sera celui qui le branchera, le jour où il sera lancé. Le livrer maintenant, testé,
signifie qu'un lot ultérieur le **trouvera fiable** au lieu de le découvrir en même temps
qu'il s'en sert.

Cette absence d'appelant est **tenue par un test** —
`test_le_module_n_a_encore_aucun_appelant` — et non seulement affirmée ici : il échoue dès
que le mot « schulze » apparaît ailleurs dans le code. Le branchement de MOD-10b le fera
tomber, et il se supprime dans le commit qui branche.

---

**L'algorithme, en trois temps** :

1. **la matrice des préférences** : pour chaque paire (A, B), combien de bulletins
   placent A avant B ;
2. **les chemins les plus forts** : la force d'un chemin A → … → B est celle de son
   maillon le plus faible, et on retient le chemin le plus fort entre chaque paire
   (Floyd-Warshall). C'est ce détour par les chemins indirects qui fait toute la méthode :
   A peut battre B *par l'intermédiaire de C* ;
3. **le classement** : A passe devant B quand le chemin le plus fort de A vers B est plus
   fort que celui de B vers A.
"""

from dataclasses import dataclass, field


class BulletinInvalide(ValueError):
    """Un bulletin mal formé. Message destiné à être lu, pas seulement journalisé."""


@dataclass(frozen=True, slots=True)
class Classement:
    """Le résultat, avec de quoi l'expliquer.

    **L'ordre seul ne suffit pas dans un contexte de modération.** Le jour où un auteur
    demandera pourquoi sa reformulation est arrivée deuxième, il faudra pouvoir montrer
    autre chose qu'un rang : `forces` porte la matrice des chemins les plus forts, et
    `egalites_tranchees` dit lesquelles des places ont été départagées par l'ancienneté
    plutôt que gagnées.
    """

    ordre: tuple[str, ...]
    #: (A, B) -> force du chemin le plus fort de A vers B.
    forces: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Les paires que la méthode laissait à égalité et que l'ancienneté a départagées.
    egalites_tranchees: tuple[tuple[str, str], ...] = ()

    @property
    def gagnant(self) -> str | None:
        return self.ordre[0] if self.ordre else None


def _candidats_de(
    bulletins: list[list[str]], candidats: list[str] | None
) -> tuple[str, ...]:
    """L'univers des candidats, **dans l'ordre d'ancienneté**.

    `candidats` est la liste des reformulations dans leur ordre de dépôt. Le passer sert
    deux fois : il départage les égalités (voir `classer`), et il fait exister les
    candidats que **personne n'a classés** — une reformulation que les cinq relecteurs
    auraient ignorée doit figurer au classement, dernière, plutôt que de disparaître
    comme si elle n'avait pas été déposée.

    À défaut, l'ordre de première apparition dans les bulletins. C'est déterministe, donc
    utilisable en test, mais **ce n'est pas l'ancienneté** : un appelant qui connaît
    l'ordre de dépôt doit le passer.
    """
    if candidats is not None:
        vus: list[str] = []
        for candidat in candidats:
            if candidat in vus:
                raise BulletinInvalide(f"Candidat en double dans la liste : {candidat!r}")
            vus.append(candidat)
        connus = set(vus)
        for bulletin in bulletins:
            for candidat in bulletin:
                if candidat not in connus:
                    raise BulletinInvalide(
                        f"Le bulletin classe {candidat!r}, qui n'est pas un candidat."
                    )
        return tuple(vus)

    ordre: list[str] = []
    for bulletin in bulletins:
        for candidat in bulletin:
            if candidat not in ordre:
                ordre.append(candidat)
    return tuple(ordre)


def _valider(bulletins: list[list[str]]) -> None:
    for rang, bulletin in enumerate(bulletins):
        if len(set(bulletin)) != len(bulletin):
            raise BulletinInvalide(
                f"Le bulletin n°{rang + 1} classe deux fois le même candidat."
            )


def preferences(
    bulletins: list[list[str]], candidats: tuple[str, ...]
) -> dict[tuple[str, str], int]:
    """(A, B) -> nombre de bulletins qui placent A avant B.

    **Un candidat non classé est réputé dernier, à égalité avec les autres non classés.**
    C'est la convention habituelle, et c'est la seule défendable ici : un relecteur qui
    n'a classé que trois reformulations sur cinq n'a pas dit que les deux autres se
    valaient entre elles — il a dit qu'il ne les met pas au-dessus de celles qu'il a
    classées. Les compter à égalité entre elles ne lui fait donc rien dire de plus.
    """
    compte = {(a, b): 0 for a in candidats for b in candidats if a != b}
    for bulletin in bulletins:
        rang = {candidat: place for place, candidat in enumerate(bulletin)}
        for a in candidats:
            for b in candidats:
                if a == b:
                    continue
                place_a, place_b = rang.get(a), rang.get(b)
                if place_a is None:
                    continue  # a n'est pas classé : il ne passe devant personne
                if place_b is None or place_a < place_b:
                    compte[(a, b)] += 1
    return compte


def chemins_les_plus_forts(
    compte: dict[tuple[str, str], int], candidats: tuple[str, ...]
) -> dict[tuple[str, str], int]:
    """La force du meilleur chemin entre chaque paire (Floyd-Warshall).

    La force d'un chemin est celle de son **maillon le plus faible** : un chemin
    A → C → B ne vaut que ce que vaut la plus serrée de ses deux victoires. C'est ce
    détour par les chemins indirects qui distingue Schulze d'une simple comparaison deux
    à deux, et qui lui permet de trancher un cycle de Condorcet.
    """
    force = {
        (a, b): (compte[(a, b)] if compte[(a, b)] > compte[(b, a)] else 0)
        for a in candidats
        for b in candidats
        if a != b
    }
    for milieu in candidats:
        for depart in candidats:
            if depart == milieu:
                continue
            for arrivee in candidats:
                if arrivee in (milieu, depart):
                    continue
                force[(depart, arrivee)] = max(
                    force[(depart, arrivee)],
                    min(force[(depart, milieu)], force[(milieu, arrivee)]),
                )
    return force


def classer(
    bulletins: list[list[str]], candidats: list[str] | None = None
) -> list[str]:
    """Le classement agrégé, du premier au dernier.

    `bulletins` : un classement par relecteur, chacun une liste ordonnée d'identifiants,
    éventuellement incomplète. `candidats` : les reformulations **dans leur ordre de
    dépôt** — voir `_candidats_de` pour ce que ce paramètre apporte.
    """
    return list(classer_en_detail(bulletins, candidats).ordre)


def classer_en_detail(
    bulletins: list[list[str]], candidats: list[str] | None = None
) -> Classement:
    """Comme `classer`, mais rend aussi de quoi expliquer le résultat.

    **Comment les égalités sont tranchées, et c'est un choix.** La méthode Schulze laisse
    deux candidats à égalité quand aucun chemin ne domine l'autre — un cycle parfaitement
    symétrique, par exemple. Il faut bien rendre un ordre, donc on départage par
    **l'ancienneté du dépôt** : à égalité, la reformulation proposée en premier passe
    devant.

    Ce n'est pas la seule option défendable (on pourrait tirer au sort, ou refuser de
    trancher et renvoyer une égalité au responsable), et elle a un défaut qu'il faut
    connaître : elle **avantage systématiquement celui qui a écrit le premier**. Elle est
    retenue parce qu'elle est la seule des trois qui soit à la fois déterministe — donc
    rejouable et vérifiable — et explicable à l'auteur en une phrase. Un tirage au sort ne
    se réexplique pas six mois plus tard.

    Les paires ainsi départagées sont rendues dans `egalites_tranchees` : elles ne
    disparaissent pas dans l'ordre final, on peut les montrer.
    """
    _valider(bulletins)
    univers = _candidats_de(bulletins, candidats)
    if not univers:
        return Classement(ordre=())
    if len(univers) == 1:
        return Classement(ordre=univers)

    compte = preferences(bulletins, univers)
    force = chemins_les_plus_forts(compte, univers)

    #: Combien d'adversaires ce candidat domine. La relation « A domine B » de Schulze
    #: est transitive, donc trier sur ce compte produit un ordre valide — et deux
    #: candidats à compte égal sont exactement ceux que la méthode laisse à égalité.
    domine = {
        a: sum(1 for b in univers if a != b and force[(a, b)] > force[(b, a)])
        for a in univers
    }
    anciennete = {candidat: place for place, candidat in enumerate(univers)}
    ordre = tuple(sorted(univers, key=lambda c: (-domine[c], anciennete[c])))

    tranchees = tuple(
        (a, b)
        for rang, a in enumerate(ordre)
        for b in ordre[rang + 1:]
        if domine[a] == domine[b]
        and force[(a, b)] <= force[(b, a)]
        and force[(b, a)] <= force[(a, b)]
    )
    return Classement(ordre=ordre, forces=force, egalites_tranchees=tranchees)
