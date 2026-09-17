"""Mise en forme de la carte pour l'affichage : de la géométrie à un SVG.

Sépare volontairement **le calcul du dessin** (ici, en Python, donc testable) du
**tracé** (`templates/public/_carte.html`, qui ne fait qu'écrire les balises). Le
projet n'a pas de banc de test JavaScript ; un rendu construit dans le navigateur
serait le seul morceau de l'application dont on ne pourrait rien prouver. Tout ce qui
décide — l'échelle, l'attribution des couleurs, le gris du reliquat, ce qui entre dans
la légende — est donc décidé ici, sous pytest.

Les données viennent de `app.services.carte`, c'est-à-dire de la même fonction que sert
`GET /api/conversations/{slug}/carte`. Rien n'est recalculé, rien n'est relu en base :
un seul run, celui que la carte a déjà choisi.
"""

import math
from dataclasses import dataclass, field

from app.services.carte import Carte
from app.services.groups import MIN_GROUP_SIZE

#: Palette « Groupes d'opinion », dans l'ordre des groupes par effectif décroissant.
#: Valeurs OFFICIELLES de l'identité visuelle, **choisies par le client le 5 septembre
#: 2026** en remplacement d'Okabe-Ito (adoptée la veille), elle-même successeur du jeu
#: sourd du D6 et du jeu provisoire du D5.
#:
#: Le motif du changement est d'identité, non d'accessibilité : Okabe-Ito couvrait le
#: spectre — bleu, orange, vert, pourpre, vermillon — et le site n'y ressemblait à
#: rien. Ces cinq-ci sont la palette « colorblind safe » d'IBM Design Language, du
#: même atelier qu'IBM Plex, la police du site depuis le chantier F. Elles restent
#: distinguables sous les trois formes de daltonisme, comme Okabe-Ito.
#:
#: **Aucune n'est verte, et c'est ce qui libère le vert** pour « D'accord » dans les
#: résultats par proposition : une pastille de groupe ne peut plus être confondue avec
#: un segment de réponse. Voir `styles.css`, section « Résultats par proposition ».
#:
#: **Cinq couleurs, et c'est le chiffre qui compte** : `staged.K_MAX` vaut 5, donc la
#: palette couvre tout ce que le regroupement peut produire. La lacune relevée au D5 —
#: un cinquième groupe reprenant la couleur du premier — reste close.
#: `test_the_palette_covers_every_possible_group` en tient la garde.
#:
#: C'est la SEULE source de ces couleurs : aucun gabarit ne les réécrit, et les tests
#: comparent à cette constante plutôt qu'à des valeurs en dur — les changer se fait
#: donc ici, et nulle part ailleurs.
PALETTE = ("#648FFF", "#DC267F", "#634CCF", "#FE6100", "#FFB000")

#: Gris « reliquat » : réservé à « sans position nette, jamais un vrai groupe ». Il
#: porte les participants d'un groupe trop petit pour en être un
#: (`groups.MIN_GROUP_SIZE`). Ne doit JAMAIS servir à un groupe réel, ni figurer dans
#: la légende — sa présence y ferait exactement ce qu'il sert à éviter : donner à un
#: individu isolé l'apparence d'un groupe.
GRIS_RELIQUAT = "#9199A6"

#: Côté du plus grand axe du canevas, en unités du `viewBox`. Les coordonnées de la
#: projection sont ramenées dans cet espace : les rayons peuvent alors être des nombres
#: fixes, sans dépendre de l'étendue du nuage.
CANEVAS = 1000.0
MARGE = 48.0
RAYON_POINT = 9.0
RAYON_VISITEUR = 15.0
EPAISSEUR_VISITEUR = 5.0

###### Étiquettes de groupe sur la carte (chantier E6b) ######

#: Taille du texte d'une étiquette, en unités du canevas. Le canevas fait 1000 de côté
#: et occupe toute la largeur disponible : 34 donne environ 20 px sur un écran large et
#: 13 px sur un téléphone de 390 px. En dessous, le nom cesse d'être lisible là où la
#: carte est la plus petite — c'est-à-dire là où elle est le plus souvent regardée.
TAILLE_ETIQUETTE = 34.0
INTERLIGNE_ETIQUETTE = 40.0

#: Largeur moyenne d'un caractère, en fraction de la taille du texte. **Une estimation,
#: et elle ne peut pas être autre chose** : SVG ne mesure pas le texte avant de le
#: tracer, et le projet n'exécute pas de JavaScript pour le faire à sa place. 0,52 est
#: la valeur usuelle pour une fonte proportionnelle comme IBM Plex ; elle sert à
#: dimensionner le fond, pas à couper le texte, donc une erreur de quelques pour cent
#: élargit ou rétrécit un cartouche sans jamais tronquer un mot.
LARGEUR_CARACTERE = 0.52

#: Au-delà, on passe à la ligne. Trois lignes de vingt caractères couvrent soixante
#: signes, soit exactement `nommage_llm.NOM_MAX` : un nom validé tient donc toujours en
#: entier, et la troncature reste un cas de repli plutôt qu'un comportement courant.
CARACTERES_PAR_LIGNE = 20
LIGNES_MAX = 3

MARGE_ETIQUETTE = 16.0
RAYON_CARTOUCHE = 12.0


@dataclass
class Etiquette:
    """Le nom d'un groupe, posé sur la carte, avec le cartouche qui le porte."""

    lignes: list[str]
    #: Coin haut-gauche du cartouche, et sa taille : le gabarit n'a rien à calculer.
    x: float
    y: float
    largeur: float
    hauteur: float
    #: Centre horizontal, pour le `text-anchor="middle"` de chaque ligne.
    cx: float
    couleur: str


def _coupe(nom: str) -> list[str]:
    """Découpe un nom en lignes courtes, sans jamais couper un mot en deux.

    Un mot plus long que la ligne déborde plutôt que d'être scindé : « constitutionnel »
    coupé en « constitution- / nel » se lit plus mal qu'un cartouche un peu large.
    """
    lignes: list[str] = []
    courante = ""
    for mot in nom.split():
        essai = f"{courante} {mot}".strip()
        if courante and len(essai) > CARACTERES_PAR_LIGNE:
            lignes.append(courante)
            courante = mot
        else:
            courante = essai
        if len(lignes) == LIGNES_MAX:
            break
    if courante and len(lignes) < LIGNES_MAX:
        lignes.append(courante)
    # Repli : un nom qui ne tient pas est tronqué sur la dernière ligne, jamais rendu
    # à moitié sans le dire. Ne devrait pas arriver — voir CARACTERES_PAR_LIGNE.
    reste = len(nom) - sum(len(ligne) + 1 for ligne in lignes) + 1
    if reste > 0 and lignes:
        lignes[-1] = lignes[-1][: CARACTERES_PAR_LIGNE - 1].rstrip() + "…"
    return lignes


def etiquettes(positions, place, couleurs, noms: dict[int, str], lettres: dict[int, str]):
    """Une étiquette par groupe nommé, posée au centre de son nuage.

    **Le centre du nuage, et non celui du contour.** Un groupe en croissant a un contour
    dont le centre géométrique tombe dans le vide, entre ses deux branches ; la moyenne
    des points, elle, tombe toujours là où il y a du monde. C'est le seul endroit où
    poser un nom sans qu'il désigne un espace vide.

    Les étiquettes qui se chevauchent sont décalées vers le bas, dans l'ordre où elles
    apparaissent : deux groupes voisins produiraient sinon deux cartouches superposés,
    illisibles tous les deux. Décaler plutôt que masquer — un nom qu'on ne montre pas
    est une information perdue, un nom déplacé de quelques dizaines d'unités reste juste.
    """
    posees: list[Etiquette] = []
    for stable in sorted(noms):
        lettre = lettres.get(stable)
        if lettre is None:
            continue
        siens = [p for p in positions if p.get("groupe") == lettre]
        if not siens:
            continue
        lignes = _coupe(noms[stable])
        if not lignes:
            continue
        largeur = (
            max(len(ligne) for ligne in lignes) * TAILLE_ETIQUETTE * LARGEUR_CARACTERE
            + 2 * MARGE_ETIQUETTE
        )
        hauteur = len(lignes) * INTERLIGNE_ETIQUETTE + MARGE_ETIQUETTE
        # La moyenne est prise dans l'espace des DONNÉES puis placée, et non l'inverse :
        # `place` est une homothétie, donc les deux ordres donnent le même point — mais
        # celui-ci n'a besoin de connaître que les positions brutes, ce qui évite
        # d'ajouter au dessin des points une étiquette de groupe que le gabarit n'a pas
        # à écrire. `PointDessine` ne porte aucun attribut identifiant, et ce n'est pas
        # un hasard : ce qui n'est pas là ne peut pas fuir dans le HTML.
        cx, cy = place(
            sum(p["x"] for p in siens) / len(siens),
            sum(p["y"] for p in siens) / len(siens),
        )
        posees.append(
            Etiquette(
                lignes=lignes,
                x=cx - largeur / 2,
                y=cy - hauteur / 2,
                largeur=largeur,
                hauteur=hauteur,
                cx=cx,
                couleur=couleurs.get(lettre, GRIS_RELIQUAT),
            )
        )

    # Désempilement : chaque étiquette qui recouvre une précédente descend juste assez
    # pour la dégager. L'ordre est celui des identités (A, B, C…), donc stable d'un
    # calcul à l'autre — un désempilement qui dépendrait de l'effectif ferait sauter
    # les noms d'un recalcul au suivant, exactement ce que le G10 a coûté aux couleurs.
    for rang, etiquette in enumerate(posees):
        for precedente in posees[:rang]:
            chevauche = (
                etiquette.x < precedente.x + precedente.largeur
                and precedente.x < etiquette.x + etiquette.largeur
                and etiquette.y < precedente.y + precedente.hauteur
                and precedente.y < etiquette.y + etiquette.hauteur
            )
            if chevauche:
                etiquette.y = precedente.y + precedente.hauteur + 8.0
    return posees

#: Un nuage parfaitement plat (tout le monde sur une ligne) donnerait un canevas d'une
#: hauteur nulle. On lui laisse cette fraction de la plus grande étendue, plutôt que
#: d'étirer les distances — étirer mentirait sur les écarts entre participants.
APLATISSEMENT_MIN = 0.18


#: Lissage des contours : fraction de la tangente Catmull-Rom effectivement appliquée.
#: `0.0` redonne exactement le polygone à sommets vifs, `1.0` la spline complète. La
#: valeur est délibérément **en dessous de 1** : le contour n'est pas un objet réel,
#: c'est un résumé de l'endroit où sont les gens. Plus il se courbe, plus il affirme
#: une forme que les données ne portent pas.
LISSAGE = 0.7

#: Exposant de la paramétrisation Catmull-Rom. `0.5` = **centripète**, et ce n'est pas
#: un réglage d'ambiance : c'est celui des trois exposants usuels qui ne produit ni
#: boucle ni rebroussement quand les sommets sont inégalement espacés. Une enveloppe
#: concave enchaîne précisément des arêtes très courtes et très longues ; en uniforme
#: (`0.0`), les courtes feraient des boucles.
ALPHA_CENTRIPETE = 0.5

#: Plafond de la longueur d'une poignée de Bézier, en fraction de l'arête qu'elle
#: gouverne. C'est le garde-fou, et il est géométrique : une courbe de Bézier reste
#: dans l'enveloppe convexe de ses quatre points de contrôle, donc à moins d'un tiers
#: d'arête du segment droit. Un contour ne peut pas partir chercher un point qui n'est
#: pas à lui.
POIGNEE_MAX = 1.0 / 3.0

#: En dessous, aucun lissage : le contour est rendu droit. Un contour a au moins
#: trois sommets par construction (`carte.MIN_POINTS_ENVELOPPE`) ; ce plancher ne
#: sert qu'au cas où le retrait des doublons en laisserait moins.
MIN_SOMMETS_LISSAGE = 3

#: En deçà de cette distance, dans l'espace du canevas, deux sommets sont LE MÊME
#: point. Le seuil n'est pas prudentiel, il est celui de l'écriture : le `d` sort au
#: centième d'unité, donc rien de plus fin n'atteint jamais l'écran.
#:
#: Il n'est pas non plus théorique. L'enveloppe concave rend couramment des sommets
#: séparés de 1e-15 — deux personnes ayant voté à l'identique, dont la projection ne
#: diffère que par l'arrondi du calcul. Une égalité stricte les laisse passer, et ils
#: coûtent deux fois : des segments de longueur nulle dans le tracé, et surtout des
#: tangentes calculées sur des distances de l'ordre de 1e-8, qui écrasent le lissage
#: des arêtes voisines — un angle vif réapparaît là où deux participants coïncident.
EPSILON_SOMMET = 0.01


@dataclass
class Contour:
    """Une enveloppe, déjà écrite dans la forme attendue par `<path d=…>`."""

    chemin: str
    couleur: str


@dataclass
class PointDessine:
    cx: float
    cy: float
    couleur: str


@dataclass
class Dessin:
    """Tout ce dont le gabarit a besoin, et rien de plus.

    Aucun identifiant de participant n'y figure — ni dans les points, ni dans un
    attribut de survol. Ce qui n'est pas ici ne peut pas fuir dans le HTML.
    """

    largeur: float = CANEVAS
    hauteur: float = CANEVAS
    contours: list[Contour] = field(default_factory=list)
    points: list[PointDessine] = field(default_factory=list)
    visiteur: PointDessine | None = None
    #: (nom du groupe, couleur), uniquement les groupes réels.
    legende: list[tuple[str, str]] = field(default_factory=list)
    #: Message à afficher À LA PLACE du point du visiteur, quand il n'en a pas.
    message_visiteur: str | None = None
    #: Noms de groupe validés, posés sur le nuage (chantier E6b). Vide tant qu'aucun
    #: n'a été validé par un modérateur — la carte est alors celle d'avant.
    etiquettes: list[Etiquette] = field(default_factory=list)


def couleurs_par_groupe(groupes) -> dict[str, str]:
    """Couleur affichée pour chaque groupe, par NOM — **au rang d'effectif**.

    Le plus grand groupe porte la première teinte de la palette, le deuxième la
    suivante, et ainsi de suite. Décision du client du 5 septembre 2026 (chantier G10),
    qui remplace la règle du D7 — la couleur suivait alors l'identité du groupe à
    travers toute son histoire.

    **Ce que ce retour coûte, pour qu'il ne soit pas redécouvert comme un bug.** Le D7
    avait chiffré exactement ce cas : quand deux groupes échangent leur rang de taille,
    **68 %** des participants voient la couleur de leur groupe changer alors que
    **86 %** d'entre eux n'ont pas changé de groupe. Le changement de rang est pourtant
    réel — ce n'est pas du bruit, et aucun amortissement ne le corrigerait. C'est le
    prix d'une couleur accrochée à une grandeur qui a le droit de bouger. Le NOM du
    groupe, lui, continue de suivre son identité stable : « groupe A » reste le même
    groupe d'un calcul à l'autre, seule sa teinte peut changer de main.

    **Ce que ce retour rapporte.** La couleur ne dépend plus que du calcul courant.
    L'accueil n'a plus à rejouer, pour chaque débat affiché, une ligne par calcul jamais
    effectué sur ce débat — un coût qui croissait sans borne et n'était plafonné par
    rien (mesuré au G9 : 2,7 s pour 300 débats de 1 000 calculs).

    Les groupes en dessous de `MIN_GROUP_SIZE` reçoivent le gris du reliquat : ce ne
    sont pas des groupes d'opinion, ce sont des personnes que le découpage a laissées
    seules. Ils ne consomment donc pas de teinte.

    Le nom départage deux groupes de même effectif : sans lui, l'ordre dépendrait de
    celui des lignes rendues par la base, et la carte changerait de couleurs à données
    identiques.
    """
    reels = sorted(
        (g for g in groupes if g.size >= MIN_GROUP_SIZE),
        key=lambda g: (-g.size, g.name),
    )
    couleurs = {g.name: PALETTE[rang % len(PALETTE)] for rang, g in enumerate(reels)}
    for groupe in groupes:
        couleurs.setdefault(groupe.name, GRIS_RELIQUAT)
    return couleurs


def _poignee(
    depart: tuple[float, float],
    vecteur: tuple[float, float],
    facteur: float,
    arete: float,
) -> tuple[float, float]:
    """Point de contrôle de Bézier, longueur plafonnée à `POIGNEE_MAX` × l'arête."""
    dx, dy = vecteur[0] * facteur, vecteur[1] * facteur
    longueur = math.hypot(dx, dy)
    plafond = arete * POIGNEE_MAX
    if longueur > plafond > 0:
        dx, dy = dx * plafond / longueur, dy * plafond / longueur
    return depart[0] + dx, depart[1] + dy


def chemin_lisse(sommets: list[tuple[float, float]]) -> str:
    """Le contour d'un groupe, en `d` de `<path>` : une spline fermée sur ses sommets.

    **Ce que le lissage change, et ce qu'il ne change pas.** La courbe passe
    EXACTEMENT par chacun des sommets de l'enveloppe — c'est la propriété
    d'interpolation de Catmull-Rom, et c'est la raison de l'avoir choisie plutôt qu'un
    adoucissement de coins (Chaikin, B-splines). Or un sommet d'enveloppe concave
    n'est pas un point de dessin : c'est la position d'un participant réel, calculée
    par `carte.enveloppe_concave` sur le nuage lui-même. Une méthode qui rabote les
    coins pousserait ces personnes-là HORS du contour de leur propre groupe. Ici,
    seules les portions ENTRE deux sommets bougent, et le tracé continue de dire la
    même chose : voilà où sont les gens de ce groupe.

    **Trois garde-fous, parce qu'un contour trop dessiné devient un faux signal.**

    1. la paramétrisation est centripète (`ALPHA_CENTRIPETE`), la seule qui ne boucle
       pas sur des arêtes de longueurs très inégales — le cas normal d'une enveloppe
       concave ;
    2. les tangentes sont atténuées d'un facteur `LISSAGE` < 1 : on arrondit, on ne
       gonfle pas ;
    3. chaque poignée est plafonnée à `POIGNEE_MAX` de son arête, ce qui borne
       géométriquement l'écart entre la courbe et le segment droit qu'elle remplace.

    Les sommets consécutifs confondus sont écartés d'abord : ils annuleraient une
    tangente (division par zéro) et n'ajoutent rien au tracé. S'il en reste moins de
    `MIN_SOMMETS_LISSAGE`, on rend le polygone droit, fermé — un triangle dégénéré ne
    se lisse pas, il se trace.
    """
    points = _sans_doublons(sommets)
    if len(points) < MIN_SOMMETS_LISSAGE:
        return _chemin_droit(points)

    n = len(points)
    arêtes = [
        math.dist(points[i], points[(i + 1) % n]) ** ALPHA_CENTRIPETE for i in range(n)
    ]

    morceaux = [f"M {points[0][0]:.2f},{points[0][1]:.2f}"]
    for i in range(n):
        p0, p1 = points[i - 1], points[i]
        p2, p3 = points[(i + 1) % n], points[(i + 2) % n]
        d1, d2, d3 = arêtes[i - 1], arêtes[i], arêtes[(i + 1) % n]
        arete = math.dist(p1, p2)

        # Tangentes de Catmull-Rom non uniforme, converties en poignées de Bézier :
        # b1 = p1 + m1·Δt/3 et b2 = p2 − m2·Δt/3, avec m1 = (p2−p0)/(d1+d2) et
        # m2 = (p3−p1)/(d2+d3). Le facteur Δt = d2 se simplifie dans le quotient.
        f1 = LISSAGE * d2 / (3 * (d1 + d2)) if d1 + d2 else 0.0
        f2 = LISSAGE * d2 / (3 * (d2 + d3)) if d2 + d3 else 0.0
        b1 = _poignee(p1, (p2[0] - p0[0], p2[1] - p0[1]), f1, arete)
        b2 = _poignee(p2, (p1[0] - p3[0], p1[1] - p3[1]), f2, arete)

        morceaux.append(
            f"C {b1[0]:.2f},{b1[1]:.2f} {b2[0]:.2f},{b2[1]:.2f} "
            f"{p2[0]:.2f},{p2[1]:.2f}"
        )
    morceaux.append("Z")
    return " ".join(morceaux)


def _sans_doublons(sommets: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sommets consécutifs distincts À L'ÉCHELLE DU TRACÉ, contour refermé compris.

    La comparaison se fait à `EPSILON_SOMMET` et non à zéro : voir cette constante,
    l'égalité stricte ne filtre rien sur des données réelles.
    """
    points: list[tuple[float, float]] = []
    for sommet in sommets:
        if not points or math.dist(points[-1], sommet) > EPSILON_SOMMET:
            points.append(sommet)
    while len(points) > 1 and math.dist(points[0], points[-1]) <= EPSILON_SOMMET:
        points.pop()
    return points


def _chemin_droit(points: list[tuple[float, float]]) -> str:
    """Repli : le polygone tel quel, sans lissage."""
    if not points:
        return ""
    trace = " ".join(f"L {x:.2f},{y:.2f}" for x, y in points[1:])
    return f"M {points[0][0]:.2f},{points[0][1]:.2f} {trace} Z".replace("  ", " ")


def _cadre(valeurs: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Boîte englobante, jamais dégénérée. Renvoie (x0, y0, largeur, hauteur)."""
    xs = [x for x, _ in valeurs]
    ys = [y for _, y in valeurs]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    largeur, hauteur = x1 - x0, y1 - y0

    etendue = max(largeur, hauteur)
    if etendue <= 0:  # tout le monde exactement au même endroit
        return x0 - 0.5, y0 - 0.5, 1.0, 1.0

    minimum = etendue * APLATISSEMENT_MIN
    if largeur < minimum:
        x0 -= (minimum - largeur) / 2
        largeur = minimum
    if hauteur < minimum:
        y0 -= (minimum - hauteur) / 2
        hauteur = minimum
    return x0, y0, largeur, hauteur


def contexte(carte: Carte | None, noms: dict[int, str] | None = None) -> dict:
    """Contexte de gabarit prêt à l'emploi, rayons compris.

    Les tailles voyagent avec le dessin plutôt que d'être réécrites dans le HTML : une
    seule source, et le gabarit reste un gabarit.
    """
    return {
        "dessin": dessin(carte, noms),
        "rayon_point": RAYON_POINT,
        "taille_etiquette": TAILLE_ETIQUETTE,
        "interligne_etiquette": INTERLIGNE_ETIQUETTE,
        "marge_etiquette": MARGE_ETIQUETTE,
        "rayon_cartouche": RAYON_CARTOUCHE,
        "rayon_visiteur": RAYON_VISITEUR,
        "epaisseur_visiteur": EPAISSEUR_VISITEUR,
    }


def dessin(carte: Carte | None, noms: dict[int, str] | None = None) -> Dessin | None:
    """Traduit une carte en instructions de tracé, ou None s'il n'y a rien à tracer.

    `noms` porte les noms de groupe VALIDÉS (chantier E6b), par identité stable. Absent
    ou vide, la carte est exactement celle d'avant : c'est le cas de loin le plus
    fréquent, et il ne doit rien coûter.
    """
    if carte is None:
        return None

    couleurs = couleurs_par_groupe(carte.groupes)

    brut: list[tuple[float, float]] = [(p["x"], p["y"]) for p in carte.positions]
    for groupe in carte.groupes:
        if groupe.sommets:
            brut.extend((x, y) for x, y in groupe.sommets)
    if not brut:
        return None

    x0, y0, largeur_donnees, hauteur_donnees = _cadre(brut)
    echelle = (CANEVAS - 2 * MARGE) / max(largeur_donnees, hauteur_donnees)

    def place(x: float, y: float) -> tuple[float, float]:
        """Vers l'espace du canevas.

        L'axe vertical n'est PAS retourné pour compenser le sens du SVG : les signes
        d'une projection en composantes principales sont arbitraires (mesuré au D0), si
        bien que « en haut » ne veut rien dire. Retourner l'axe donnerait l'illusion
        d'une orientation qui a un sens.
        """
        return (
            MARGE + (x - x0) * echelle,
            MARGE + (y - y0) * echelle,
        )

    dess = Dessin(
        largeur=round(2 * MARGE + largeur_donnees * echelle, 2),
        hauteur=round(2 * MARGE + hauteur_donnees * echelle, 2),
    )

    # Les contours d'abord : ils passent SOUS les points.
    for groupe in carte.groupes:
        if not groupe.sommets:
            continue
        # Le lissage se fait APRÈS la mise à l'échelle, dans l'espace du canevas :
        # le plafond des poignées s'exprime alors dans les mêmes unités que le tracé.
        # La transformation étant une homothétie, lisser avant donnerait le même
        # dessin — mais le garde-fou serait exprimé en unités de projection, qui ne
        # veulent rien dire à l'écran.
        sommets = [place(x, y) for x, y in groupe.sommets]
        dess.contours.append(
            Contour(
                chemin=chemin_lisse(sommets),
                couleur=couleurs[groupe.name],
            )
        )

    dess.etiquettes = etiquettes(
        carte.positions,
        place,
        couleurs,
        noms or {},
        {g.stable_id: g.name for g in carte.groupes if g.stable_id is not None},
    )

    for point in carte.positions:
        cx, cy = place(point["x"], point["y"])
        dess.points.append(
            PointDessine(
                cx=round(cx, 2),
                cy=round(cy, 2),
                couleur=couleurs.get(point["groupe"], GRIS_RELIQUAT),
            )
        )

    visiteur = carte.visiteur
    if visiteur is not None:
        if visiteur.etat == "situe":
            cx, cy = place(visiteur.x, visiteur.y)
            dess.visiteur = PointDessine(
                cx=round(cx, 2),
                cy=round(cy, 2),
                couleur=couleurs.get(visiteur.groupe, GRIS_RELIQUAT),
            )
        else:
            # Ni point, ni position approchée, ni point grisé « quelque part » : les
            # deux autres états veulent dire qu'on ne SAIT pas où cette personne se
            # situe. Un message, donc, à la place du point.
            dess.message_visiteur = visiteur.raison

    dess.legende = [
        (groupe.name, couleurs[groupe.name])
        for groupe in sorted(carte.groupes, key=lambda g: (-g.size, g.name))
        if groupe.size >= MIN_GROUP_SIZE
    ]
    return dess
