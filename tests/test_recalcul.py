"""La cadence du calcul des groupes, et la grille sur laquelle elle tombe.

Chantier G9. Ce qui est éprouvé ici n'est pas la période — c'est un réglage — mais la
propriété dont dépend le message de fin de parcours : que le prochain calcul soit
prévisible, donc annonçable.

La fenêtre d'oubli des couleurs était éprouvée ici aussi, parce qu'elle se comptait en
tours et changeait donc de durée avec la cadence. Elle a disparu au G10 : la couleur
d'un groupe ne dépend plus de son histoire, mais de son effectif.

Le lisseur du nombre de groupes a pris sa place au chantier E0, pour exactement la même
raison : il se comptait en tours, et le passage de la cadence à 10 minutes aurait fait
passer son attente de 45 à 30 minutes sans qu'une seule ligne change. Les tours se
dérivent donc maintenant d'une durée, et ce qui est éprouvé plus bas est que cette
attente tienne aux trois cadences que ce projet a connues.
"""

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services import recalcul


def _avec_periode(secondes: int, lissage: int | None = None):
    """Contexte minimal : rend les réglages tels qu'ils étaient, quoi qu'il arrive."""

    class _Bascule:
        def __enter__(self):
            self.depart = settings.analysis_interval_seconds
            self.depart_lissage = settings.analysis_k_buffer_seconds
            settings.analysis_interval_seconds = secondes
            if lissage is not None:
                settings.analysis_k_buffer_seconds = lissage

        def __exit__(self, *_):
            settings.analysis_interval_seconds = self.depart
            settings.analysis_k_buffer_seconds = self.depart_lissage

    return _Bascule()


def test_the_next_run_falls_on_the_clock_grid() -> None:
    """À 900 s, les calculs ont lieu à 0, 15, 30 et 45 minutes de l'heure.

    C'est ce qui rend le compte à rebours possible. Le worker dormait auparavant N
    secondes APRÈS chaque passage : la cadence dérivait de la durée du passage, et
    « dans environ 12 minutes » n'aurait été qu'une estimation de l'estimation.
    """
    with _avec_periode(900):
        for minute, attendue in ((0, 15), (7, 15), (14, 15), (16, 30), (46, 0)):
            maintenant = datetime(2026, 9, 5, 14, minute, 30, tzinfo=timezone.utc)
            prochain = recalcul.prochain_calcul(maintenant)
            assert prochain.minute == attendue
            assert prochain.second == 0
            assert prochain > maintenant, "un calcul annoncé ne peut pas être passé"


def test_the_boundary_second_announces_the_run_after() -> None:
    """Sur la seconde exacte d'un passage, c'est le suivant qui est annoncé.

    Annoncer « dans 0 seconde » à quelqu'un dont le calcul vient de démarrer serait
    faux dans l'autre sens : ses votes n'y sont pas.
    """
    with _avec_periode(900):
        pile = datetime(2026, 9, 5, 14, 15, 0, tzinfo=timezone.utc)
        assert recalcul.prochain_calcul(pile).minute == 30
        assert recalcul.delai_avant_calcul(pile) == timedelta(minutes=15)


def test_the_hourly_setting_still_falls_on_the_hour() -> None:
    """La grille n'est pas propre au quart d'heure : elle vaut pour tout réglage."""
    with _avec_periode(3600):
        maintenant = datetime(2026, 9, 5, 14, 42, 0, tzinfo=timezone.utc)
        prochain = recalcul.prochain_calcul(maintenant)
        assert (prochain.hour, prochain.minute, prochain.second) == (15, 0, 0)


def test_the_period_sentence_follows_the_setting() -> None:
    """Elle a déménagé au G9 ; elle doit toujours suivre le réglage, pas le gabarit."""
    with _avec_periode(600):
        assert recalcul.periode_de_recalcul() == "toutes les 10 minutes"
    with _avec_periode(900):
        assert recalcul.periode_de_recalcul() == "toutes les 15 minutes"
    with _avec_periode(3600):
        assert recalcul.periode_de_recalcul() == "toutes les heures"


def test_the_ten_minute_setting_falls_on_the_clock_grid() -> None:
    """À 600 s, les calculs ont lieu à 0, 10, 20, 30, 40 et 50 minutes de l'heure.

    600 divise 3600 : la grille reste alignée sur l'heure, ce qui n'irait pas de soi
    pour une période quelconque. C'est ce qui permet d'annoncer un temps restant sans
    que la promesse dérive d'une heure à l'autre.
    """
    with _avec_periode(600):
        for minute, attendue in ((0, 10), (7, 10), (9, 10), (11, 20), (51, 0)):
            maintenant = datetime(2026, 9, 6, 14, minute, 30, tzinfo=timezone.utc)
            prochain = recalcul.prochain_calcul(maintenant)
            assert prochain.minute == attendue
            assert prochain.second == 0
            assert prochain > maintenant, "un calcul annoncé ne peut pas être passé"


def test_the_smoothing_wait_holds_across_cadences() -> None:
    """Le lisseur attend une DURÉE, pas un nombre de tours (chantier E0).

    C'est tout l'objet de la conversion : à 3 tours figés, l'attente avant qu'un groupe
    nouveau paraisse valait 3 h au rythme horaire, 45 min au quart d'heure et 30 min à
    10 minutes — trois comportements différents sous un seul réglage. Dérivée d'une
    heure, elle vaut une heure aux trois cadences.
    """
    for cadence, tours_attendus in ((600, 6), (900, 4), (3600, 2)):
        with _avec_periode(cadence, lissage=3600):
            tours = recalcul.tours_de_lissage()
            assert tours == tours_attendus
            # Le vrai invariant n'est pas le nombre de tours, c'est ce qu'il dure.
            attente = tours * cadence
            assert attente >= 3600, f"à {cadence} s, le lisseur n'attend que {attente} s"


def test_the_smoother_is_never_silently_disabled() -> None:
    """Une cible plus courte que la cadence ne doit pas éteindre le lissage.

    `lisse` élit le candidat dès que `compte >= tours` : à un tour, la condition est
    vraie à la première mesure et le k brut revient à l'écran — la silhouette varie de
    2 à 5 sur les mêmes données (D0). Le plancher fait qu'un réglage trop court rend le
    lisseur trop lent, ce qui se voit, plutôt que muet, ce qui ne se voit pas.
    """
    with _avec_periode(3600, lissage=600):
        assert recalcul.tours_de_lissage() == 2
    with _avec_periode(600, lissage=0):
        assert recalcul.tours_de_lissage() == 2
