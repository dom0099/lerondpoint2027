"""La liste fermée des **désignations collectives de personnes**.

Elle ne sert qu'à une chose : permettre à `app/services/detection.py` de reconnaître
la **forme** « Les X + verbe » dans une proposition. Elle ne qualifie rien, ne juge
rien, et n'entraîne à elle seule aucune décision de modération.

**Ce que cette liste n'est pas.**

1. **Ce n'est pas une liste de termes à proscrire.** Toutes les entrées sont des mots
   neutres du français courant — « les agriculteurs », « les musulmans », « les
   Parisiens », « les retraités ». On peut parfaitement écrire une proposition
   irréprochable avec n'importe laquelle. Le signal porte sur la place du mot dans la
   phrase, pas sur le mot.
2. **Ce n'est pas la liste d'injures du chantier E.** Celle-là filtre les noms de
   groupes produits par un modèle de langue ; elle répond à un besoin opposé (des mots
   qu'on refuse) et ne doit pas être fusionnée avec celle-ci (des mots qu'on constate).
   Aucune insulte ne doit jamais entrer ici : elle rendrait le signal moral, alors
   qu'il est grammatical.

**La règle de symétrie est une contrainte, pas une intention.** Partout où une notion
a deux bords, les deux sont dans la liste : « les riches » et « les pauvres », « les
patrons » et « les syndicats », « les fonctionnaires » et « les indépendants », « les
Parisiens » et « les ruraux », et de même pour les religions et les opinions
politiques. Une liste qui n'attraperait la forme « Les X + verbe » que pour certains
groupes serait indéfendable le jour où quelqu'un la lira — et quelqu'un la lira.
`tests/test_detection.py` vérifie une dizaine de couples nommément.

**Pourquoi une constante Python et non une table.** Même raison qu'`app/services/
themes.py` : une liste administrable serait un écran de plus à écrire, à protéger et
à tester, pour une donnée qui doit au contraire bouger lentement et se relire d'un
coup d'œil. Le versionnement de cette liste, c'est `git log`, et c'est ce qu'on veut
pour un objet aussi sensible.

**Forme des entrées.** Le **nom seul**, au pluriel, sans déterminant : le déterminant
(`les`, `ces`, `certains`…) est fourni par la grammaire dans `detection.py`. Elles
s'écrivent ici en français lisible, avec accents et majuscules ; `detection.py` les
normalise au chargement. Une entrée peut compter plusieurs mots.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Famille:
    """Un groupement de désignations, pour la relecture humaine seulement.

    La détection ne se sert pas de la famille : elle aplatit tout dans une seule
    alternation. La famille existe pour qu'on puisse vérifier la symétrie d'un
    regard, famille par famille, et pour que le rapport puisse dire d'où vient une
    entrée si on le lui demande un jour.
    """

    code: str
    libelle: str
    designations: tuple[str, ...]


FAMILLES: tuple[Famille, ...] = (
    Famille(
        "origine",
        "Origine et nationalité",
        (
            "Français",
            "étrangers",
            "immigrés",
            "émigrés",
            "natifs",
            "Français de souche",
            "binationaux",
            "naturalisés",
            "réfugiés",
            "demandeurs d'asile",
            "sans-papiers",
            "descendants d'immigrés",
            "Européens",
            "non-Européens",
            "Africains",
            "Maghrébins",
            "Noirs",
            "Blancs",
            "Arabes",
            "Roms",
            "gens du voyage",
        ),
    ),
    Famille(
        "religion",
        "Religion et conviction",
        (
            "musulmans",
            "chrétiens",
            "catholiques",
            "protestants",
            "juifs",
            "bouddhistes",
            "hindous",
            "athées",
            "croyants",
            "non-croyants",
            "pratiquants",
            "non-pratiquants",
            "laïcs",
            "islamistes",
            "imams",
            "prêtres",
            "rabbins",
            "pasteurs",
        ),
    ),
    Famille(
        "age",
        "Âge et génération",
        (
            "jeunes",
            "vieux",
            "seniors",
            "retraités",
            "personnes âgées",
            "boomers",
            "millennials",
        ),
    ),
    Famille(
        "metier",
        "Métier et statut",
        (
            "fonctionnaires",
            "indépendants",
            "patrons",
            "syndicats",
            "policiers",
            "gendarmes",
            "militaires",
            "magistrats",
            "juges",
            "avocats",
            "politiciens",
            "politiques",
            "élus",
            "banquiers",
            "propriétaires",
            "locataires",
            "bailleurs",
            "syndicalistes",
        ),
    ),
    Famille(
        "economie",
        "Situation économique",
        (
            "riches",
            "pauvres",
            "milliardaires",
            "classes populaires",
            "classes moyennes",
            "classes aisées",
            "bourgeois",
            "précaires",
            "chômeurs",
            "sans-abri",
            "mal-logés",
            "bénéficiaires du RSA",
        ),
    ),
    Famille(
        "territoire",
        "Territoire",
        (
            "Parisiens",
            "ruraux",
            "urbains",
            "banlieusards",
            "habitants des quartiers",
            "ultramarins",
            "Corses",
            "Bretons",
            "Alsaciens",
        ),
    ),
    Famille(
        "genre",
        "Genre et orientation",
        (
            "femmes",
            "hommes",
            "mères",
            "pères",
            "mères célibataires",
            "pères célibataires",
            "homosexuels",
            "hétérosexuels",
            "personnes trans",
            "transgenres",
            "cisgenres",
            "non-binaires",
            "féministes",
            "masculinistes",
        ),
    ),
    Famille(
        "politique",
        "Opinion politique",
        (
            "gens de gauche",
            "gens de droite",
            "complotistes",
            "gilets jaunes",
            "nationalistes",
            "réactionnaires",
            "progressistes",
            "conservateurs",
            "militants",
            "manifestants",
            "souverainistes",
            "européistes",
        ),
    ),
    Famille(
        "sante",
        "Santé et handicap",
        (
            "personnes handicapées",
            "handicapés",
            "valides",
            "invalides",
            "malades",
            "aidants",
            "aidés",
            "autistes",
            "sourds",
            "aveugles",
            "obèses",
            "usagers de drogues",
            "consommateurs d'alcool",
        ),
    ),
    Famille(
        "generique",
        "Désignations génériques et d'usage",
        (
            "gens",
            "gens-là",
            "citoyens",
        ),
    ),
)
#: Toutes les désignations, à plat, dans l'ordre de lecture du fichier. C'est ce que
#: `detection.py` consomme ; `FAMILLES` reste la source de l'ORDRE et du regroupement.
DESIGNATIONS: tuple[str, ...] = tuple(
    designation for famille in FAMILLES for designation in famille.designations
)

#: **La dérogation à la règle de symétrie, tenue à jour ici et nulle part ailleurs.**
#:
#: Ces trois entrées n'ont **pas de contraire** à leur opposer : personne ne se désigne
#: ainsi soi-même, et il n'existe pas de mot pour le bord d'en face. Elles violent donc
#: la règle qui fonde cette liste, et elles y sont quand même.
#:
#: C'est une **décision de Julien, prise le 12 septembre 2026** (MOD-1), contre l'avis
#: qui les avait d'abord écartées : ces tournures existent dans le débat réel, et une
#: liste qui les rate laisse passer exactement le genre de proposition que le signal
#: cherche. L'argument opposé — qu'une liste asymétrique est attaquable le jour où
#: quelqu'un la lit — reste vrai et n'a pas disparu ; il a été mis en balance et pesé
#: moins lourd.
#:
#: Ce registre existe pour que la dérogation soit **visible et réversible en une
#: minute**, plutôt que noyée dans 253 lignes. Un test vérifie qu'il contient
#: exactement ces trois entrées : une quatrième exigera la même décision explicite,
#: et ne pourra pas s'ajouter par habitude.
SANS_CONTRAIRE: frozenset[str] = frozenset({
    "Français de souche",
    "islamistes",
    "complotistes",
})

#: Index inverse, construit une fois : d'une désignation vers le code de sa famille.
#: Sert au rapport et aux tests, jamais à la détection elle-même.
FAMILLE_DE: dict[str, str] = {
    designation: famille.code
    for famille in FAMILLES
    for designation in famille.designations
}
