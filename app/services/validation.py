"""La validation aléatoire par les participants : la règle, la cadence, les plafonds.

**Ce que c'est.** Un participant se voit proposer, au hasard et assez fréquemment, de
regarder une proposition déposée par quelqu'un d'autre et tirée au sort. Il ne répond
que deux choses — « proposition conforme » ou « proposition à revoir ». S'il répond « à
revoir », il est renvoyé vers les mêmes cases que pour un signalement, et la suite est
le chemin habituel : ligne rouge → retrait conservatoire, mal formulé → reformulation.

**Ce que ce n'est pas : une file d'attente.** La publication n'en dépend jamais. Une
proposition paraît immédiatement (MOD-14) ; si elle est tirée pour validation, elle suit
la procédure, et sinon rien ne se passe. C'est un **contrôle par sondage après
publication**. Le mot « file » ne doit apparaître nulle part dans ce lot.

**Ce module ne touche pas la base**, comme `app/services/signalement.py` et pour la même
raison : la cadence, les plafonds et les gardes doivent pouvoir se relire d'un coup
d'œil et se tester sans base. Le tirage et l'enregistrement sont dans
`app/services/validation_tirage.py`.

**Le vocabulaire, et un mot interdit.** Le cadrage disait « niveau 0 » ; le §2 du plan
interdit précisément ce mot — « niveau » désigne la jauge de jeu du C5, et laisser croire
que voter beaucoup donne du pouvoir de modération serait faux. On écrit partout **« tout
participant »**. De même : **validation** (jamais « relecture », qui désignait la grille
supprimée au MOD-14, ni « revue », ni « review »), **conforme** / **à revoir** (jamais
« accepté » / « rejeté », qui laisseraient croire à un verdict).

**Une propriété du chantier que ce lot renverse, et il vaut mieux l'écrire que la
redécouvrir :** la grille du MOD-4 d'origine avait pour règle qu'*aucune case ne demande
un avis* — un test s'appelait `test_il_n_existe_aucune_case_d_acceptabilite`. « Conforme
/ à revoir » demande exactement un avis. C'est défendable, parce que l'avis est porté par
beaucoup de gens plutôt que par un seul, mais c'est l'opposé de la règle d'origine.
"""

import enum


class Verdict(str, enum.Enum):
    """Les deux seules réponses. **Deux, et il n'y en aura jamais une troisième.**

    « Passer » n'en est pas une : c'est refuser de répondre, et rien n'est alors
    enregistré (voir `PASSER_N_EST_PAS_UN_VERDICT` plus bas). Un troisième code, qui
    s'appellerait « sans avis » ou « je ne sais pas », transformerait une abstention en
    jugement — et l'indice d'accord du MOD-6 compterait comme désaccord le fait de
    n'avoir pas voulu trancher.
    """

    conforme = "conforme"
    a_revoir = "a_revoir"


LIBELLES: dict[Verdict, str] = {
    Verdict.conforme: "Proposition conforme",
    Verdict.a_revoir: "Proposition à revoir",
}

#: Pourquoi « passer » n'est pas un verdict, écrit dans le code et pas seulement dans le
#: journal. La carte apparaît au milieu d'un parcours de vote que personne n'a demandé à
#: interrompre : elle DOIT pouvoir être écartée. Mais l'écarter n'écrit aucune ligne —
#: ni « conforme », ni « à revoir », ni une troisième valeur. Le tirage suivant reprend
#: comme si de rien n'était, et la proposition passée reste tirable pour quelqu'un
#: d'autre.
PASSER_N_EST_PAS_UN_VERDICT = (
    "Écarter une sollicitation n'enregistre rien : ni un verdict, ni une abstention."
)


# --- la cadence, et les deux plafonds -------------------------------------------
#
# Les trois chiffres ci-dessous sont des RÉGLAGES, pas des invariants. « Assez
# fréquemment » demandait un nombre (§14.3, point 3 du plan) ; ce sont ceux-là, arrêtés
# par dom le 15 septembre 2026, et ils seront réajustés une fois l'usage observé. Ils
# sont ici, ensemble, pour qu'on puisse les relire d'un coup.

#: Une sollicitation toutes les N propositions votées dans le débat en cours.
#:
#: 7 et non 4 : la carte s'intercale dans le flux de vote, qui est ce que ce chantier
#: protège le plus depuis le MOD-3b. Une sur quatre produirait de la matière plus vite
#: et ferait du parcours de vote un parcours de modération. Une sur douze serait
#: invisible au trafic actuel. Sept laisse le parcours être un parcours.
CADENCE_VOTES = 7

#: Plafond par personne et par jour glissant. Trois : au-delà, on ne sollicite plus
#: quelqu'un qui lit, on le met au travail.
SOLLICITATIONS_PAR_JOUR = 3

#: Au-delà de ce nombre de validations, une proposition **sort du tirage**.
#:
#: Cinq avis suffisent à savoir si un texte fait difficulté ; en demander vingt userait
#: l'attention des participants sur ce qui est déjà tranché, et rendrait le tirage de
#: moins en moins aléatoire à mesure que les propositions rassasiées s'accumuleraient.
#: Ce plafond ne vaut QUE pour le tirage : il n'empêche jamais un signalement spontané.
VALIDATIONS_PAR_PROPOSITION = 5

#: La fenêtre du plafond par personne, en heures. Glissante, comme celle des
#: signalements : un plafond calé sur minuit se contourne en attendant minuit.
FENETRE_HEURES = 24


def sollicitation_due(votes_emis: int) -> bool:
    """Faut-il proposer une validation après ce vote-ci ? **Pure, sans E/S.**

    `votes_emis` est le nombre de votes de cette personne dans CE débat, celui qu'on
    vient d'enregistrer compris — c'est exactement ce que la réponse de vote calcule
    déjà pour sa barre de progression, et le réutiliser évite une seconde façon de
    compter qui finirait par diverger.

    **La cadence se lit sur les votes, jamais sur les sollicitations déjà faites.** Un
    compteur de sollicitations demanderait d'enregistrer celles qu'on écarte, donc de
    garder trace d'une non-réponse — ce que `PASSER_N_EST_PAS_UN_VERDICT` refuse. Le
    multiple de sept est vrai ou faux à un instant donné, il ne dépend d'aucun état.

    Conséquence assumée : quelqu'un qui écarte la carte du septième vote n'en reverra
    une qu'au quatorzième. C'est le bon sens du dispositif — insister sur celui qui
    vient de décliner serait l'inverse d'une sollicitation.
    """
    return votes_emis > 0 and votes_emis % CADENCE_VOTES == 0


# --- ce que la carte dit, et ce qu'elle ne dit pas -------------------------------
#
# Même discipline qu'au MOD-3b : aucun texte n'annonce un effet que le code ne produit
# pas. La carte ne promet aucun délai, ne dit pas ce que la réponse déclenchera, et
# n'apprend jamais à celui qui répond ce que les autres ont répondu — sans quoi la
# validation deviendrait un jeu où l'on compte les points, exactement comme le
# signalement l'aurait fait.

#: Le titre de la carte. Une question, et non un ordre : on sollicite, on ne convoque pas.
TITRE = "Un coup d'œil sur une proposition ?"

#: Ce qui est demandé, en une phrase. Elle dit trois choses et pas une de plus : que la
#: proposition vient de quelqu'un d'autre, qu'elle est déjà en ligne, et qu'on peut
#: passer. « Déjà en ligne » est ce qui rend le dispositif honnête : rien n'est diffusé
#: à celui qui valide qui ne le soit déjà à tout le monde.
CONSIGNE = (
    "Cette proposition a été déposée par quelqu'un d'autre et elle est déjà en ligne. "
    "Vous pouvez répondre, ou passer."
)

#: La seule réponse qu'on renvoie, quel que soit le verdict et quoi qu'il advienne.
#: Identique à l'accusé de réception du signalement, et pour la même raison : dès que
#: celui qui répond apprend ce que sa réponse a produit, il compte les points.
ACCUSE_DE_RECEPTION = "Merci, c'est enregistré."


class ValidationInvalide(ValueError):
    """Une validation refusée. Porte une phrase française affichable telle quelle."""


def verdict(code: str) -> Verdict:
    """Le verdict de ce code. Lève `ValidationInvalide` si le code est inconnu.

    Lève plutôt que de filtrer, comme `signalement.motif` : un code inconnu ici est une
    requête fabriquée à la main, et une validation qu'on enregistrerait sans savoir ce
    qu'elle dit ne serait pas une validation.
    """
    try:
        return Verdict(code)
    except ValueError:
        raise ValidationInvalide(f"Verdict inconnu : {code!r}.") from None


def exige_des_motifs(verdict_rendu: Verdict) -> bool:
    """« À revoir » renvoie aux mêmes cases qu'un signalement, « conforme » à rien.

    Un « à revoir » sans motif serait une plainte sans objet : le responsable ne saurait
    ni quoi en faire, ni si la proposition doit sortir de la circulation. Un « conforme »
    avec des motifs serait une contradiction — on ne dit pas d'un texte qu'il va bien et
    qu'il incite à la violence.
    """
    return verdict_rendu is Verdict.a_revoir
