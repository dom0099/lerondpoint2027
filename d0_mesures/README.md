# Bancs de mesure du chantier D (D0 et D1) — archive à rejouer

Copie **verbatim** des bancs qui ont produit les chiffres du `CHANTIER_D_LOG.md`.
Ils ne font pas partie de l'application et ne sont pas exécutés par `pytest` : ce sont
des scripts d'expérimentation, conservés ici pour deux raisons.

1. **Ils justifient des choix d'architecture qui, sinon, ne se relisent plus.** Le fait
   que le recentrage seul absorbe la dérive du repère PCA — donc qu'aucun Procruste
   n'est nécessaire dans `app/analysis/buckets.py` — est un résultat *mesuré*
   (`banc_option_c_prime.py`), pas un raisonnement.
2. **Ils sont la suite à rejouer avant toute montée de version volontaire de
   red-dwarf.** L'analyse reprend à son compte l'enchaînement réducteur → clusterer →
   stats du corps de `reddwarf/implementations/base.py`, qui n'est pas une API
   documentée (voir l'en-tête de `app/analysis/staged.py`). Une montée de version peut
   le changer sans rien casser à l'import : seuls ces bancs le verraient.

## Où ça tourne

Les deux bancs `banc_d7_*.py` font exception : ils sollicitent la BASE et l'application,
pas seulement red-dwarf. Ils se jouent depuis le conteneur `api`, sur une base **jetable** :

    docker compose exec -T -e DATABASE_URL=…/chantierc_jetable api python - < d0_mesures/banc_d7_echelle.py

Emplacement canonique d'exécution des autres : **`~/red-dwarf-test/d0_mesures/`**, avec le venv de
`~/red-dwarf-test` (red-dwarf 0.4.0). Les chemins absolus des scripts sont **conservés
tels quels, volontairement** : un rejeu depuis cet emplacement reproduit exactement ce
que le journal rapporte. Cette copie-ci est une archive versionnée, pas un second
emplacement d'exécution.

    cd ~/red-dwarf-test && .venv/bin/python d0_mesures/mesures_d0.py

Ordre de dépendance : `mesures_d0_suite4.py` et `suite5.py` relisent les
`resultats_*.json` des lots précédents, présents ici. `mesures_d0_suite3.py` et
`suite4.py` lisent `~/red-dwarf-test/votes_2dcdfr5fbi.csv` (export du chantier B) ;
le dérivé pseudonymisé qu'ils écrivent a été supprimé en fin de lot et le sera de
nouveau — il n'est pas versionné.

## Ce que chacun mesure

| banc | question |
|---|---|
| `mesures_d0.py` | déterminisme, et ampleur de la dérive du repère entre deux coupes |
| `mesures_d0_suite2.py` | alignement de Procruste général, corpus flou, quatre graines |
| `mesures_d0_suite3.py` | mêmes mesures sur les votes réels ; d'où vient le résidu |
| `mesures_d0_suite4.py` / `4b.py` | un indicateur prédit-il le régime de dérive ? |
| `mesures_d0_suite5.py` | deux hypothèses restantes, toutes deux négatives |
| `banc_option_c_prime.py` | **le recentrage seul suffit-il, sans Procruste ?** (oui) |
| `banc_d7_echelle.py` | 50 → 150 → 400 participants : temps, seaux, lisseur, continuité |
| `banc_d7_couleurs.py` | d'où vient la rupture de couleur, et ce que donnerait l'autre règle |
| `banc_d8_couleurs_par_identite.py` | le même scénario après bascule sur l'identité stable : 68 % → 14 % |

Réserve à garder en tête sur le dernier : il **émule** la fusion/scission par la
stratégie `init="polis"` de `PolisKMeans`. La boucle `most-distal` réelle est
implémentée dans `app/analysis/buckets.py`, pas ici.
