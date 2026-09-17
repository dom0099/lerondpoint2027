"""Le détecteur de **signaux de forme** dans une proposition.

`analyser(texte)` rend une liste de `Signal`. Un signal dit « à cet endroit, cette
proposition a telle *forme* » — pas « cette proposition est mauvaise ». La distinction
n'est pas une précaution de langage : c'est le contrat de ce module, et tout le
chantier de modération repose dessus.

**Ce que ce module ne fait pas, et ne fera pas.**

- Il ne rend **jamais de verdict**. Pas de booléen, pas de score, pas de seuil, pas de
  champ « rejeter ». Un lot ultérieur qui voudrait écrire `if est_problematique(t)`
  ne trouvera rien à appeler : c'est délibéré. Ce qu'on fait d'un signal — afficher
  une phrase sous le champ de saisie, trier une file de relecture — se décide ailleurs,
  par des humains, et se lit dans le code qui appelle et non ici.
- Les **codes sont descriptifs**, jamais moraux : `cible_personnes` et non « haineux »,
  `affirmation_de_fait` et non « douteux ». Un code moral serait un verdict déguisé,
  et se mettrait à décider tout seul dès qu'on l'afficherait.
- Il ne lit **ni base, ni réseau, ni fichier**, n'appelle aucun modèle de langue et
  n'a aucun état. Deux appels sur le même texte rendent exactement la même liste, dans
  le même ordre — c'est testé.

**Il a le droit d'être grossier, et il doit l'être.** Un signal ne censure personne :
au pire il fera lire une ligne de texte que le participant ignorera. Un faux positif
coûte donc presque rien, là où une règle fine coûterait de la lisibilité et du temps.
« Les agriculteurs souffrent de la concurrence » déclenche `cible_personnes` : c'est
la forme « Les X + verbe », et c'est **voulu**, pas un défaut à corriger.

**Il doit tenir dans un formulaire.** Toutes les expressions régulières sont compilées
une seule fois, au chargement du module. `tests/test_detection.py` vérifie qu'une
proposition de longueur maximale s'analyse sous 5 ms.
"""

import re
from dataclasses import dataclass

from app.services.conversations import MAX_STATEMENT_LENGTH
from app.services.designations import DESIGNATIONS

# --------------------------------------------------------------------------------
# Les codes
# --------------------------------------------------------------------------------

#: Une désignation collective de personnes, en position de sujet, suivie d'un verbe.
CIBLE_PERSONNES = "cible_personnes"
#: Deux verbes conjugués reliés par « et », ou un point-virgule.
DEUX_IDEES = "deux_idees"
#: Une mesure (nombre + unité), une date, un « selon », un superlatif.
AFFIRMATION_DE_FAIT = "affirmation_de_fait"
#: La proposition est formulée en question.
INTERROGATION = "interrogation"
#: Trop courte, ou proche du plafond de saisie.
LONGUEUR = "longueur"

#: L'ordre d'affichage des signaux dans le rapport. Ce n'est ni l'alphabet ni une
#: fréquence : c'est l'ordre d'importance pour le chantier, `cible_personnes` en tête.
CODES: tuple[str, ...] = (
    CIBLE_PERSONNES,
    DEUX_IDEES,
    AFFIRMATION_DE_FAIT,
    INTERROGATION,
    LONGUEUR,
)


@dataclass(frozen=True, slots=True)
class Signal:
    """Un signal repéré, et **où** il l'a été.

    `debut` et `fin` indexent le texte **tel qu'il a été passé** à `analyser`, pas une
    version retravaillée : c'est ce qui permettra un jour de souligner la portion dans
    un formulaire sans avoir à réaligner quoi que ce soit.

    Volontairement dépourvu de tout champ de jugement — pas de gravité, pas de
    certitude, pas de « à rejeter ». Ajouter un tel champ ici reviendrait à rendre un
    verdict, ce que ce module refuse de faire (voir l'en-tête du fichier).
    """

    code: str
    extrait: str
    debut: int
    fin: int


# --------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------

#: Remplacements **caractère par caractère**, donc de longueur strictement conservée.
#: C'est la propriété qui fait tout marcher : les positions trouvées dans le texte
#: normalisé sont, sans conversion, des positions dans le texte d'origine.
#: Une décomposition Unicode (NFKD) serait plus complète mais changerait les longueurs,
#: et il faudrait alors transporter une table d'index — beaucoup de code pour un
#: détecteur qui a le droit de rater. Conséquence assumée : un texte saisi en forme
#: décomposée (« e » + accent combinant, ce que ni un navigateur ni un clavier ne
#: produisent) ne verra pas ses mots accentués reconnus. C'est un raté, pas une panne.
_REMPLACEMENTS = {
    **{c: "a" for c in "àâäáãåÀÂÄÁÃÅæÆ"},
    **{c: "c" for c in "çÇ"},
    **{c: "e" for c in "éèêëÉÈÊË"},
    **{c: "i" for c in "íìîïÍÌÎÏ"},
    **{c: "n" for c in "ñÑ"},
    **{c: "o" for c in "óòôöõÓÒÔÖÕœŒ"},
    **{c: "u" for c in "úùûüÚÙÛÜ"},
    **{c: "y" for c in "ýÿÝŸ"},
    # Les trois apostrophes qu'un clavier, un traitement de texte et un téléphone
    # produisent pour la même chose. Sans cette ligne, « qu'ils » écrit au propre
    # ne ressemblerait plus à « qu'ils » écrit à la machine.
    **{c: "'" for c in "’‘ʼ´`"},
    # Toutes les espèces d'espace deviennent l'espace ordinaire. Les espaces
    # MULTIPLES ne sont pas réduites — ce serait changer la longueur ; ce sont les
    # motifs ci-dessous qui les tolèrent, en écrivant `[ ]+` partout où le français
    # met une espace.
    **{c: " " for c in "\t\n\r   ​"},
}

_TABLE = str.maketrans(_REMPLACEMENTS)


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, apostrophes et espaces unifiées — **à longueur égale**.

    `len(normaliser(t)) == len(t)` est un invariant, pas une coïncidence : tout le
    reste du module s'en sert pour rendre des positions dans le texte d'origine.
    """
    # `str.lower()` est bijectif caractère à caractère sur le français ; les rares
    # exceptions Unicode (le « İ » turc, qui se déplie en deux caractères) sont
    # laissées telles quelles plutôt que de casser l'invariant de longueur.
    minuscules = "".join(c if len(b := c.lower()) != 1 else b for c in texte)
    return minuscules.translate(_TABLE)


def _motif_mots(expression: str) -> str:
    """Un motif pour une expression de la liste, tolérant aux espaces multiples."""
    return r"[ ]+".join(re.escape(mot) for mot in normaliser(expression).split(" ") if mot)


def _alternation(expressions) -> str:
    """Une alternation, **la plus longue d'abord**.

    L'ordre compte : `re` retient la première branche qui marche, pas la plus longue.
    Sans ce tri, « ces gens-là » se ferait manger par « gens » et la portion rendue
    serait tronquée.
    """
    uniques = {normaliser(e) for e in expressions}
    return "|".join(_motif_mots(e) for e in sorted(uniques, key=lambda e: (-len(e), e)))


# --------------------------------------------------------------------------------
# Grammaire : ce qui approche « en position de sujet », et « verbe conjugué »
# --------------------------------------------------------------------------------

#: Les déterminants qui annoncent une désignation collective. Ceux qui se terminent
#: par une apostrophe (« beaucoup d' ») n'exigent pas d'espace après eux.
DETERMINANTS: tuple[str, ...] = (
    "les", "ces", "des", "certains", "certaines", "quelques", "plusieurs",
    "tous les", "toutes les", "beaucoup de", "beaucoup d'", "plein de",
    "pas mal de", "trop de", "la plupart des", "la majorité des",
    "une majorité de", "bon nombre de", "l'ensemble des", "nos", "leurs", "leur",
)

#: Ce qui, dans une phrase, ouvre une proposition — et permet donc à une désignation
#: d'y être sujet sans être en tête de texte. La liste vient de la consigne (`que`,
#: `car`, `mais`, `parce que`) ; les autres sont du même ordre grammatical.
CONJONCTIONS: tuple[str, ...] = (
    "que", "car", "mais", "parce que", "puisque", "donc", "or", "si",
    "quand", "lorsque", "alors que",
)
#: Les mêmes, élidées : elles collent au mot suivant, sans espace obligatoire.
CONJONCTIONS_ELIDEES: tuple[str, ...] = ("qu'", "parce qu'", "puisqu'", "lorsqu'")

#: Verbes conjugués que la règle des terminaisons attrape déjà pour la plupart. Ils
#: sont nommés quand même, comme le demande la consigne : le jour où la règle des
#: terminaisons bougera, cette liste dira ce qu'on ne veut en aucun cas perdre.
VERBES_IRREGULIERS: frozenset[str] = frozenset({
    "sont", "ont", "font", "vont", "veulent", "peuvent", "doivent", "prennent",
    "viennent", "savent", "croient", "voient", "boivent", "recoivent", "ecrivent",
    "vivent", "suivent", "disent", "lisent", "mettent", "tiennent", "valent",
})

#: Des mots qui finissent en `-ent` ou `-ont` **sans être des verbes**.
#:
#: La consigne ne demandait que l'exclusion des adverbes en `-ment` (« rapidement »),
#: qui est traitée par une règle et non par une liste. Cette liste-ci va un cran plus
#: loin, et c'est un écart assumé : sans elle, `cible_personnes` se déclenche sur
#: « les jeunes du continent africain » ou « les agriculteurs souvent oubliés », où il
#: n'y a aucun verbe. Le détecteur a le droit d'être grossier, mais un signal qui se
#: déclenche sur « dont » fausse le seul chiffre que ce lot doit produire.
#: Elle reste courte, et se relit d'un trait.
PAS_DES_VERBES: frozenset[str] = frozenset({
    # En -ont
    "dont", "pont", "ponts", "front", "fronts", "mont", "monts", "amont", "affront",
    # En -ent
    "souvent", "argent", "parent", "parents", "client", "clients", "president",
    "presidents", "accident", "accidents", "different", "differents", "content",
    "contents", "violent", "violents", "urgent", "urgents", "talent", "talents",
    "dent", "dents", "vent", "vents", "lent", "lents", "cent", "cents", "absent",
    "absents", "present", "presents", "evident", "evidents", "excellent",
    "excellents", "intelligent", "intelligents", "permanent", "permanents",
    "continent", "continents", "agent", "agents", "adolescent", "adolescents",
    "concurrent", "concurrents", "occident", "orient", "serpent", "torrent",
})

#: À combien de mots de la désignation on accepte encore de trouver le verbe.
#: Trois, c'est « les jeunes de banlieue **partent** » : un complément court passe,
#: une subordonnée entière ne passe pas.
FENETRE_VERBE = 3

#: Moins que ça, une proposition ne dit rien qui se vote : « Oui », « Pour »,
#: « Sur les fesses » ne se votent pas — on ne sait pas à quoi on répondrait.
#:
#: **Arrêté à 20 par le client le 12 septembre 2026** (MOD-1). J'avais proposé 15,
#: à vue de nez et sans rien pour le fonder : `app/services/conversations.py` impose
#: un plafond (`MAX_STATEMENT_LENGTH`) mais **aucun plancher**, et `/proposer` accepte
#: « Oui » sans broncher. C'est donc un seuil de jugement, pas une borne technique —
#: la seule valeur de ce module que ni le code ni une mesure ne dictent.
LONGUEUR_MIN = 20
#: 90 % du plafond réel, **lu** dans le code et non recopié : si le plafond bouge,
#: ce seuil bouge avec lui.
LONGUEUR_PROCHE_PLAFOND = MAX_STATEMENT_LENGTH * 9 // 10


def _motif_determinants() -> str:
    """Les déterminants, avec l'espace qu'ils exigent (ou non) derrière eux."""
    tries = sorted(
        {normaliser(d) for d in DETERMINANTS}, key=lambda d: (-len(d), d)
    )
    return "|".join(
        _motif_mots(d) + (r"[ ]*" if d.endswith("'") else r"[ ]+") for d in tries
    )


#: Ce qui ouvre une proposition : le début du texte, une ponctuation forte, ou une
#: conjonction. C'est l'approximation de « en position de sujet » — grossière, et
#: suffisante : elle sépare « **Les agriculteurs** souffrent » de « il faut aider
#: **les agriculteurs** », qui est le seul cas discriminant qui compte vraiment.
_DEBUT_DE_PROPOSITION = (
    r"(?:^[ ]*"
    r"|[.;:!?…][ ]*"
    rf"|\b(?:{_alternation(CONJONCTIONS)})[ ]+"
    rf"|\b(?:{_alternation(CONJONCTIONS_ELIDEES)})[ ]*)"
)

MOTIF_SUJET = re.compile(
    _DEBUT_DE_PROPOSITION
    + r"(?P<sujet>(?:"
    + _motif_determinants()
    + r")(?:"
    + _alternation(DESIGNATIONS)
    + r"))(?![a-z0-9'])"
)

#: Un mot, au sens où ce module en a besoin : de quoi compter la fenêtre de trois et
#: de quoi tester une terminaison. L'apostrophe coupe (« n'ont » donne « n » et
#: « ont »), ce qui est exactement ce qu'on veut.
MOTIF_MOT = re.compile(r"[a-z0-9]+")

MOTIF_POINT_VIRGULE = re.compile(r";")
MOTIF_ET = re.compile(r"\bet\b")

#: Ce qui transforme un nombre en **mesure**. Liste fermée et courte, à dessein.
#:
#: **Un nombre seul ne déclenche plus rien** (décision du client, 13 septembre 2026).
#: La première version prenait n'importe quel chiffre, et se déclenchait donc sur
#: « Oui car 16 ou 18 ans c'est pareil » — qui n'affirme aucun fait, c'est un avis sur
#: l'âge du permis. Le signal pesait 17,2 % de la base à lui seul, presque entièrement
#: pour cette raison.
#:
#: « ans » n'y est **pas**, et c'est le point : un âge n'est pas une statistique. C'est
#: exactement le cas que cette liste doit laisser passer.
UNITES: tuple[str, ...] = (
    "millions", "million", "milliards", "milliard", "milliers", "millier",
    "euros", "euro", "dollars", "francs",
    "personnes", "habitants", "emplois", "logements", "places", "lits",
    "deces", "morts", "naissances", "hectares", "tonnes", "kilometres",
)

MOTIF_FAIT = re.compile(
    # Le pourcentage : un nombre porteur de son unité, le cas le plus net.
    r"\d+(?:[.,]\d+)?[ ]*%"
    # Un nombre SUIVI d'une unité ou d'un ordre de grandeur — « 398 millions »,
    # « 75 000 personnes ». C'est ce qui le rend vérifiable ; seul, il ne l'est pas.
    rf"|\b\d+(?:[ .,]\d{{3}})*(?:[.,]\d+)?[ ]*(?:{'|'.join(UNITES)})\b"
    # Un millésime. Quatre chiffres entre 1900 et 2099 sont une date dans une
    # proposition, pas une quantité — « En 2014, la consommation avait doublé ».
    r"|\b(?:19|20)\d{2}\b"
    r"|\bselon\b"
    r"|\b(?:le|la|les)[ ]+(?:plus|moins|premier|premiere|premiers|premieres"
    r"|dernier|derniere|meilleur|meilleure|meilleurs|pire|pires|seul|seule)\b"
    r"|\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre"
    r"|novembre|decembre)\b"
)

MOTIF_INTERROGATION = re.compile(
    r"\?"
    # Une inversion sujet-verbe : question même sans point d'interrogation.
    r"|-t-(?:il|elle|on)\b"
    r"|\b(?:est-ce[ ]+que|est-ce[ ]+qu'|qu'est-ce|faut-il|doit-on|peut-on"
    r"|devrait-on|pourquoi|comment|combien)\b"
    # En tête seulement : ailleurs, « qui » et « quel » sont des relatifs ordinaires.
    r"|^[ ]*(?:qui|quel|quelle|quels|quelles|quand|ou)\b"
)


def _est_verbe(mot: str) -> bool:
    """Le verbe conjugué, à l'oreille plutôt qu'au dictionnaire.

    Une terminaison en `-ent` ou `-ont`, moins les adverbes en `-ment` — le piège
    classique, « rapidement » — moins une courte liste de noms et de pronoms qui
    finissent pareil, plus les irréguliers nommés. Aucune conjugaison n'est chargée :
    ce module doit tenir dans un formulaire.
    """
    if mot in PAS_DES_VERBES:
        return False
    if mot in VERBES_IRREGULIERS:
        return True
    if len(mot) < 4 or mot.endswith("ment"):
        return False
    return mot.endswith("ent") or mot.endswith("ont")


def _verbes(norme: str) -> list[re.Match[str]]:
    """Tous les mots du texte que `_est_verbe` reconnaît, dans l'ordre."""
    return [m for m in MOTIF_MOT.finditer(norme) if _est_verbe(m.group())]


# --------------------------------------------------------------------------------
# Les cinq signaux
# --------------------------------------------------------------------------------


def _cible_personnes(texte: str, norme: str) -> list[Signal]:
    """« Les X + verbe » : une désignation collective sujet, suivie d'un verbe.

    La portion rendue va du déterminant jusqu'au verbe inclus — c'est la forme
    entière qu'on voudra souligner, pas le seul nom de groupe, qui n'a rien de fautif
    en lui-même.
    """
    signaux = []
    for sujet in MOTIF_SUJET.finditer(norme):
        fin_sujet = sujet.end("sujet")
        for rang, mot in enumerate(MOTIF_MOT.finditer(norme, fin_sujet)):
            if rang >= FENETRE_VERBE:
                break
            if _est_verbe(mot.group()):
                debut, fin = sujet.start("sujet"), mot.end()
                signaux.append(Signal(CIBLE_PERSONNES, texte[debut:fin], debut, fin))
                break
    return signaux


def _deux_idees(texte: str, norme: str) -> list[Signal]:
    """Deux verbes conjugués reliés par « et », ou un point-virgule.

    Une proposition qui porte deux idées rend le vote ininterprétable : on ne saura
    pas à laquelle la personne a répondu, et le désaccord qu'elle exprime ne dira rien.
    """
    signaux = [
        Signal(DEUX_IDEES, texte[m.start():m.end()], m.start(), m.end())
        for m in MOTIF_POINT_VIRGULE.finditer(norme)
    ]
    verbes = _verbes(norme)
    for et in MOTIF_ET.finditer(norme):
        avant = [v for v in verbes if v.end() <= et.start()]
        apres = [v for v in verbes if v.start() >= et.end()]
        if avant and apres:
            debut, fin = avant[-1].start(), apres[0].end()
            signaux.append(Signal(DEUX_IDEES, texte[debut:fin], debut, fin))
    return signaux


def _affirmation_de_fait(texte: str, norme: str) -> list[Signal]:
    """Une mesure, une date, un « selon », un superlatif.

    Ce signal ne dit pas que l'affirmation est fausse — il dit qu'elle est
    **vérifiable**, ce qui n'est pas la même chose et prépare l'exigence de source
    d'un lot ultérieur.

    Un nombre nu n'est pas une mesure : il lui faut une unité, un ordre de grandeur
    ou la forme d'un millésime (voir `UNITES`). C'est ce qui distingue « 398 millions
    d'euros » de « 16 ou 18 ans », et c'est la correction du 13 septembre 2026.
    """
    return [
        Signal(AFFIRMATION_DE_FAIT, texte[m.start():m.end()], m.start(), m.end())
        for m in MOTIF_FAIT.finditer(norme)
    ]


def _interrogation(texte: str, norme: str) -> list[Signal]:
    """La proposition est formulée en question.

    Sur ce site on vote pour ou contre une affirmation ; « d'accord » avec une
    question ne veut rien dire.
    """
    return [
        Signal(INTERROGATION, texte[m.start():m.end()], m.start(), m.end())
        for m in MOTIF_INTERROGATION.finditer(norme)
    ]


def _longueur(texte: str, norme: str) -> list[Signal]:
    """Trop courte pour dire quelque chose, ou au bord du plafond de saisie."""
    debut = len(texte) - len(texte.lstrip())
    fin = len(texte.rstrip())
    utile = fin - debut
    if utile < LONGUEUR_MIN or utile >= LONGUEUR_PROCHE_PLAFOND:
        return [Signal(LONGUEUR, texte[debut:fin], debut, max(fin, debut))]
    return []


#: Les cinq détecteurs, dans l'ordre de `CODES`. Une liste fermée : ajouter un signal
#: se voit ici, et se discute avant.
_DETECTEURS = (
    _cible_personnes,
    _deux_idees,
    _affirmation_de_fait,
    _interrogation,
    _longueur,
)


def analyser(texte: str) -> list[Signal]:
    """Les signaux de forme d'une proposition, **sans aucun verdict**.

    L'ordre est celui de la lecture — par position dans le texte, puis par code à
    position égale — et il est stable : deux appels rendent la même liste.
    """
    norme = normaliser(texte)
    signaux: list[Signal] = []
    for detecteur in _DETECTEURS:
        signaux.extend(detecteur(texte, norme))
    signaux.sort(key=lambda s: (s.debut, s.fin, s.code))
    return signaux
