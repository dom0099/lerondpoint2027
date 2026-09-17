"""Les liens qu'un participant attache à sa proposition : contrôle et affichage.

Deux liens au plus par proposition (décision du client, 5 septembre 2026), écrits par
l'auteur de la proposition au moment où il l'écrit, propositions d'amorce comprises.

**Rien n'est demandé au site distant.** Le titre de la page liée n'est pas récupéré :
un champ d'URL public qui déclenche une requête sortante depuis le serveur est le cas
d'école de la SSRF (`http://169.254.169.254/`, une redirection vers une adresse
interne…), et s'en protéger correctement coûte bien plus cher que le confort obtenu.
Ce qui s'affiche à la place est **le domaine, extrait de l'URL par nous**, plus le
libellé que l'auteur a écrit. Le lecteur voit où il va avant de cliquer, et rien de ce
qui s'affiche ne vient d'un tiers.

Le domaine affiché doit donc être *vraiment* celui vers lequel le lien pointe — c'est
tout l'objet de ce module, et la raison des trois refus ci-dessous.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

#: Deux liens au plus par proposition. La contrainte est aussi portée par la base
#: (`position` dans (1, 2), unique par proposition) : celle-ci ne rattrape que la
#: saisie, la base rattrape tout le reste.
LIENS_MAX = 2

URL_MAX = 2000
LIBELLE_MAX = 120

SCHEMAS = ("http", "https")


class LienInvalide(ValueError):
    """Saisie refusée. Le message est destiné à l'auteur, il est donc en français."""


@dataclass(frozen=True)
class Lien:
    url: str
    libelle: str | None
    #: Ce qui sera affiché au lecteur. Toujours dérivé de `url`, jamais saisi.
    domaine: str


def _domaine(hote: str) -> str:
    """Le nom d'hôte tel qu'il sera montré : ASCII, minuscule, sans « www. ».

    L'encodage IDNA n'est pas cosmétique. `urlsplit` rend l'hôte en Unicode, et deux
    domaines distincts peuvent s'y écrire de la même façon à l'œil — c'est l'attaque
    par homographe. Affiché en punycode, le domaine est laid mais vrai ; c'est le bon
    arbitrage sur un lien qu'un lecteur va suivre.
    """
    try:
        ascii_hote = hote.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError) as erreur:
        raise LienInvalide("ce nom de domaine n'est pas valide") from erreur
    return ascii_hote[4:] if ascii_hote.startswith("www.") else ascii_hote


def domaine_de(url: str) -> str | None:
    """Le domaine d'une adresse DÉJÀ stockée, pour l'affichage. None si illisible.

    Séparé de `valider_lien` parce que les deux n'ont pas le même devoir : celui-là
    refuse une saisie, celui-ci relit ce qui est en base. Une ligne écrite avant un
    durcissement de la validation ne doit pas faire tomber la page qui l'affiche.

    **None n'est pas « affiche l'adresse brute ».** Une adresse dont on ne sait pas
    nommer la destination ne doit pas être présentée du tout : montrer un lien sans
    dire où il mène est exactement ce que la règle du domaine visible interdit.
    """
    try:
        morceaux = urlsplit(url)
        if morceaux.scheme.lower() not in SCHEMAS or not morceaux.hostname:
            return None
        if morceaux.username or morceaux.password:
            return None
        return _domaine(morceaux.hostname)
    except (ValueError, LienInvalide):
        return None


def valider_lien(url: str | None, libelle: str | None = None) -> Lien | None:
    """Contrôle une saisie et rend ce qu'il faut stocker, ou None si le champ est vide.

    Lève `LienInvalide` sur une saisie refusée : un lien mal formé est une erreur de
    l'auteur, à lui montrer, et non une valeur à corriger en silence.
    """
    url = (url or "").strip()
    libelle = (libelle or "").strip() or None

    if not url:
        # Un libellé sans lien n'a rien à décrire : c'est un champ oublié, pas un
        # lien. Le dire plutôt que de jeter la saisie sans un mot.
        if libelle:
            raise LienInvalide("un libellé sans adresse : renseignez l'adresse ou videz le libellé")
        return None

    if len(url) > URL_MAX:
        raise LienInvalide(f"adresse trop longue (maximum {URL_MAX} caractères)")
    if libelle and len(libelle) > LIBELLE_MAX:
        raise LienInvalide(f"libellé trop long (maximum {LIBELLE_MAX} caractères)")

    # Espaces et caractères de contrôle : une adresse qui en contient a été recopiée
    # de travers, ou fabriquée pour casser l'attribut dans lequel elle atterrit.
    if any(caractere.isspace() or ord(caractere) < 0x20 for caractere in url):
        raise LienInvalide("l'adresse ne doit pas contenir d'espace")

    morceaux = urlsplit(url)

    if morceaux.scheme.lower() not in SCHEMAS:
        # Attrape `javascript:`, `data:`, `file:` — et l'adresse sans schéma, que l'on
        # refuse plutôt que de la compléter d'office : préfixer « https:// » à
        # « exemple.fr » revient à deviner, et l'auteur ne verrait pas ce qui a été
        # décidé pour lui.
        raise LienInvalide("l'adresse doit commencer par http:// ou https://")

    if morceaux.username or morceaux.password:
        # `https://insee.fr@exemple.net/` mène à exemple.net. Le refuser est plus sûr
        # que d'afficher le bon domaine sous une adresse que personne ne lit ainsi.
        raise LienInvalide("l'adresse ne doit pas contenir d'identifiant")

    if not morceaux.hostname:
        raise LienInvalide("cette adresse n'a pas de nom de domaine")

    return Lien(url=url, libelle=libelle, domaine=_domaine(morceaux.hostname))


def valider_liens(saisies: list[tuple[str | None, str | None]]) -> list[Lien]:
    """Contrôle les couples (adresse, libellé) d'une proposition, dans l'ordre.

    Les champs vides sont ignorés — remplir le second sans le premier est fréquent et
    ne veut rien dire de particulier — mais leur nombre est plafonné avant tout autre
    contrôle : un formulaire qui en envoie trois est un formulaire trafiqué, et il ne
    mérite pas un message d'erreur sur le troisième lien.
    """
    if len(saisies) > LIENS_MAX:
        raise LienInvalide(f"au plus {LIENS_MAX} liens par proposition")

    liens = [lien for url, libelle in saisies if (lien := valider_lien(url, libelle))]

    # Deux fois la même adresse : ce sont deux lignes qui montreront le même domaine
    # sous la même proposition. C'est une faute de recopie, pas une source de plus.
    vues = [lien.url for lien in liens]
    if len(set(vues)) != len(vues):
        raise LienInvalide("deux fois la même adresse")

    return liens
