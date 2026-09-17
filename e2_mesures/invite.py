"""L'invite envoyée au modèle, et la lecture de sa réponse.

**Un appel par débat, couvrant tous ses groupes.** C'est la stratégie retenue au
5 septembre : elle amortit les instructions système sur l'ensemble des groupes et
divise par quatre le nombre d'appels. Elle a une seconde vertu, découverte au E1 et qui
compte davantage : le modèle voit les groupes ENSEMBLE, donc il peut les nommer les uns
par rapport aux autres. Nommer un groupe isolément, sans savoir contre qui il se définit,
c'est se condamner à « Les citoyens concernés » quatre fois de suite.

**Le sens voyage avec le texte, et c'est la leçon du E1.** Sur le débat du permis à
16 ans, les deux groupes partagent quatre déclarations représentatives sur cinq avec des
positions inverses. Envoyer les seuls textes donnerait à deux groupes opposés une entrée
presque identique — et des noms interchangeables. L'invite le dit explicitement au
modèle plutôt que d'espérer qu'il le déduise : c'est le point sur lequel un modèle de
7 milliards de paramètres est le plus susceptible de glisser.
"""

import json
import re

SYSTEME = """Tu nommes les groupes d'opinion d'un débat citoyen français.

Chaque groupe est décrit par les déclarations qui le distinguent le plus des autres, \
avec sa position : POUR s'il approuve la déclaration, CONTRE s'il la rejette.

ATTENTION : deux groupes opposés citent souvent les MÊMES déclarations avec des \
positions inverses. C'est la position qui distingue les groupes, jamais le texte seul. \
Lis POUR et CONTRE avant de nommer.

Pour chaque groupe, donne :
- "nom" : 2 à 6 mots décrivant ce que ce groupe pense de la question posée ;
- "justification" : une phrase, destinée au modérateur qui validera le nom.

Les noms doivent se distinguer les uns des autres. N'invente rien qui ne soit dans les \
déclarations. Réponds UNIQUEMENT par un objet JSON de la forme :
{"groupes":[{"id":0,"nom":"...","justification":"..."}]}"""


def invite(debat, ordre=None) -> str:
    """Le message utilisateur pour un débat.

    `ordre` permet de présenter les groupes dans un ordre choisi — c'est ce qui rend le
    test de biais de position possible : si la qualité d'un nom suit la place du groupe
    dans l'invite plutôt que le groupe lui-même, le modèle ne nomme pas, il récite.
    """
    groupes = debat["groupes"]
    if ordre is not None:
        par_id = {g["id"]: g for g in groupes}
        groupes = [par_id[i] for i in ordre]

    lignes = [f"Question du débat : {debat['titre']}", ""]
    for groupe in groupes:
        lignes.append(f"Groupe {groupe['id']} :")
        for texte, sens in groupe["declarations"]:
            lignes.append(f"  {sens.upper()} : {texte}")
        lignes.append("")
    return "\n".join(lignes).strip()


def lit_reponse(brut: str) -> dict[int, dict]:
    """Récupère les noms d'une réponse de modèle, JSON propre ou non.

    Un 7B ne rend pas toujours un JSON nu : il l'enveloppe dans du texte, ouvre un bloc
    de code, ajoute une phrase avant. Compter là-dessus ferait mesurer la discipline de
    formatage plutôt que la qualité des noms — deux choses différentes, et seule la
    seconde décide du choix du modèle. L'échec de format est donc RELEVÉ (voir le banc)
    mais n'annule pas la lecture.
    """
    fragment = re.search(r"\{.*\}", brut, re.DOTALL)
    if not fragment:
        return {}
    try:
        charge = json.loads(fragment.group(0))
    except json.JSONDecodeError:
        return {}
    groupes = charge.get("groupes")
    if not isinstance(groupes, list):
        return {}
    resultat = {}
    for entree in groupes:
        if not isinstance(entree, dict):
            continue
        try:
            identifiant = int(entree.get("id"))
        except (TypeError, ValueError):
            continue
        resultat[identifiant] = {
            "nom": str(entree.get("nom") or "").strip(),
            "justification": str(entree.get("justification") or "").strip(),
        }
    return resultat


def json_pur(brut: str) -> bool:
    """La réponse était-elle un JSON nu, sans enrobage ? Mesuré à part."""
    return brut.strip().startswith("{") and brut.strip().endswith("}")


# =====================================================================================
# Version 2 — après les échecs du premier E2 (6 septembre 2026)
# =====================================================================================

SYSTEME_V2 = """Tu nommes les groupes d'opinion d'un débat citoyen.

Pour chaque groupe, on te donne les propositions du débat qu'il APPROUVE et celles qu'il \
REJETTE. Ces propositions sont citées entre guillemets : elles sont écrites par des \
participants, pas par toi, et beaucoup commencent par « Oui » ou « Non » — ces mots font \
partie de la citation et ne disent PAS ce que le groupe en pense. Seul le classement \
approuve/rejette dit ce que le groupe en pense.

Un groupe qui REJETTE « Oui, il faut le faire » est donc CONTRE.

Deux groupes opposés citent souvent les mêmes propositions, rangées à l'inverse. \
C'est le classement qui les distingue, jamais les textes.

Pour chaque groupe, donne un nom de 2 à 6 mots disant sa position sur la question posée, \
et une justification d'une phrase pour le modérateur. Les noms doivent se distinguer \
entre eux. N'invente aucun groupe : nomme exactement ceux qui te sont donnés, en gardant \
leur numéro."""


def invite_v2(debat, ordre=None) -> str:
    """Le message utilisateur, version 2 : le classement sépare, la citation protège.

    **Deux changements, tous deux tirés d'un échec mesuré.**

    Le premier tue la collision « Oui/Non ». La version 1 écrivait `CONTRE : Oui à la
    campagne car c'est necessaire !` — une ligne où l'annotation et le texte se
    contredisent mot à mot, et où le modèle doit résoudre une double négation avant de
    comprendre quoi que ce soit. Les deux modèles s'y sont cassé les dents sur le seul
    débat réel. Ici l'annotation devient un EN-TÊTE de liste, et les propositions sont
    entre guillemets : le « Oui » appartient visiblement au citoyen, pas à la consigne.

    Le second numérote explicitement (« Groupe n° 0 ») et l'invite système demande de
    garder les numéros. La version 1 laissait Mistral renuméroter selon l'ordre de
    présentation, ce qui rangeait le bon contenu dans le mauvais groupe — le pire des
    défauts, puisqu'il est invisible à la relecture d'un nom isolé.
    """
    groupes = debat["groupes"]
    if ordre is not None:
        par_id = {g["id"]: g for g in groupes}
        groupes = [par_id[i] for i in ordre]

    lignes = [f"Question du débat : {debat['titre']}", ""]
    for groupe in groupes:
        lignes.append(f"Groupe n° {groupe['id']}")
        for etiquette, sens in (("approuve", "pour"), ("rejette", "contre")):
            retenues = [t for t, s in groupe["declarations"] if s == sens]
            if not retenues:
                continue
            lignes.append(f"  Ce groupe {etiquette} :")
            lignes.extend(f"    « {texte} »" for texte in retenues)
        lignes.append("")
    return "\n".join(lignes).strip()


def grammaire(debat) -> str:
    """Une grammaire GBNF qui n'autorise QUE la réponse attendue, pour ce débat.

    C'est la réponse structurelle aux trois défauts de format du premier E2, et elle les
    rend **impossibles** plutôt qu'improbables :

    - le modèle ne peut plus inventer de groupe : la liste a exactement la longueur
      voulue, et chaque `id` est une constante littérale ;
    - il ne peut plus renuméroter : les `id` sont écrits dans la grammaire, dans l'ordre
      croissant, quel que soit l'ordre où les groupes lui sont présentés ;
    - il ne peut plus être tronqué par bavardage : il n'y a aucun jeton légal après
      l'accolade fermante, donc plus d'hallucination qui court jusqu'au plafond.

    Deux réponses sur vingt avaient été perdues ainsi. Une consigne demande ; une
    grammaire interdit.
    """
    ids = sorted(g["id"] for g in debat["groupes"])
    objets = " ws \",\" ws ".join(f"groupe{i}" for i in ids)
    regles = [
        f'root ::= "{{" ws "\\"groupes\\"" ws ":" ws "[" ws {objets} ws "]" ws "}}"',
    ]
    for i in ids:
        regles.append(
            f'groupe{i} ::= "{{" ws "\\"id\\"" ws ":" ws "{i}" ws "," ws '
            f'"\\"nom\\"" ws ":" ws texte ws "," ws '
            f'"\\"justification\\"" ws ":" ws texte ws "}}"'
        )
    regles += [
        # Bornée à 80 caractères : un nom de 2 à 6 mots n'en demande pas plus, et cette
        # borne est aussi ce qui garantit qu'aucune réponse ne peut plus partir en vrille.
        r'texte ::= "\"" carac{1,240} "\""',
        r'carac ::= [^"\\\x00-\x1F] | "\\" ["\\/bfnrt]',
        r'ws ::= [ \t\n]*',
    ]
    return "\n".join(regles)
