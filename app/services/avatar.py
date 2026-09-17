"""Avatar généré — un identicon déterministe, pas un tirage.

Comme sur GitHub : la même personne voit toujours le même dessin, calculé à partir
de `User.id` (un UUID déjà stable et unique). Personne ne le choisit, personne ne le
change en rechargeant la page — « aléatoire » veut dire ici « que la personne n'a pas
choisi », pas « qui varie à chaque affichage ».

Aucune colonne, aucun fichier stocké : le rendu part de ce qui existe déjà sur le
compte, donc aucune migration ne l'accompagne (voir `0014_bio_du_profil.py`, qui ne
touche qu'à la bio).

**Aucune couleur n'est écrite ici.** Le SVG ne porte qu'un numéro de variante
(0 à 3) sous forme de classe CSS ; c'est `app/static/styles.css` qui décide de la
teinte réelle, dans les NEUF valeurs du document d'identité. Écrire un hex ici
créerait une seconde source pour la même décision — et c'est justement ce que le
fichier de styles interdit en tête (« toute valeur qui n'y figure pas est un bug,
pas une variante »).
"""

import hashlib

#: Nombre de variantes de couleur déclarées côté CSS (`.identicon--0` à `--3`).
VARIANTES = 4

#: Grille 5x5, symétrique horizontalement : seules les colonnes 0, 1 et 2 sont
#: tirées au hasard, les colonnes 3 et 4 recopient les colonnes 1 et 0. C'est
#: l'algorithme des identicons GitHub/GitLab — un motif reconnaissable sans
#: bibliothèque ni dépendance ajoutée.
_LIGNES = 5
_COLONNES_TIREES = 3


def identicon_svg(seed: str, taille: int = 64) -> str:
    """Le SVG d'un avatar, déterministe pour un `seed` donné.

    `seed` est un identifiant stable (l'UUID du compte, en texte) — jamais une
    valeur qui changerait d'une requête à l'autre, sous peine de perdre la seule
    propriété qui compte : la même personne doit toujours voir le même dessin.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    variante = digest[0] % VARIANTES

    cellules: list[str] = []
    bit = 0
    for ligne in range(_LIGNES):
        moitie: list[bool] = []
        for _ in range(_COLONNES_TIREES):
            octet = digest[1 + bit // 8]
            moitie.append(bool((octet >> (bit % 8)) & 1))
            bit += 1
        # Miroir : colonne 3 = colonne 1, colonne 4 = colonne 0.
        rangee = moitie + moitie[-2::-1]
        for colonne, rempli in enumerate(rangee):
            if rempli:
                cellules.append(f'<rect x="{colonne}" y="{ligne}" width="1" height="1"/>')

    return (
        f'<svg class="identicon identicon--{variante}" '
        f'viewBox="0 0 {_LIGNES} {_LIGNES}" width="{taille}" height="{taille}" '
        f'aria-hidden="true" focusable="false">'
        f'<rect class="identicon-fond" x="0" y="0" width="{_LIGNES}" height="{_LIGNES}"/>'
        f'<g class="identicon-motif">{"".join(cellules)}</g>'
        f"</svg>"
    )
