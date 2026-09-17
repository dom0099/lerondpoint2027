"""Appariement des groupes entre deux exécutions successives.

k-means numérote ses groupes arbitrairement : rien ne garantit que le « groupe 1 »
d'un recalcul soit le « groupe 1 » du suivant, même si les mêmes personnes s'y
retrouvent. Afficher ces numéros bruts reviendrait à dire à quelqu'un qu'il a changé
d'avis alors que seule l'étiquette a bougé.

On rattache donc chaque groupe du nouveau run au groupe du run précédent avec lequel
il partage le plus de participants. L'affectation est faite globalement (algorithme
hongrois, `scipy.optimize.linear_sum_assignment`) et non groupe par groupe : un choix
glouton peut « voler » le meilleur partenaire d'un autre groupe et dégrader
l'appariement d'ensemble.
"""

from scipy.optimize import linear_sum_assignment


def match_groups(
    new_members: dict[int, set[int]],
    previous_members: dict[int, set[int]],
    next_free_label: int,
) -> dict[int, int]:
    """Associe chaque groupe du nouveau run à une identité stable.

    Args:
        new_members: étiquette k-means -> participants du nouveau run.
        previous_members: identité stable -> participants du run précédent.
        next_free_label: première identité stable jamais utilisée sur cette
            conversation, pour les groupes réellement nouveaux.

    Returns:
        étiquette k-means -> identité stable.
    """
    new_labels = sorted(new_members)
    if not new_labels:
        return {}

    # Premier calcul de la conversation : les identités naissent ici.
    if not previous_members:
        return {label: index for index, label in enumerate(new_labels)}

    previous_labels = sorted(previous_members)
    # Coût = -recouvrement : l'algorithme minimise, on veut maximiser le partage.
    cost = [
        [-len(new_members[new] & previous_members[old]) for old in previous_labels]
        for new in new_labels
    ]
    rows, columns = linear_sum_assignment(cost)

    mapping: dict[int, int] = {}
    taken: set[int] = set()
    for row, column in zip(rows, columns):
        overlap = -cost[row][column]
        # Un appariement sans le moindre participant commun n'est pas une continuité :
        # c'est un groupe neuf qui occupe une place laissée libre.
        if overlap > 0:
            stable = previous_labels[column]
            mapping[new_labels[row]] = stable
            taken.add(stable)

    for label in new_labels:
        if label not in mapping:
            while next_free_label in taken:
                next_free_label += 1
            mapping[label] = next_free_label
            taken.add(next_free_label)
            next_free_label += 1

    return mapping


def group_name(stable_id: int | None) -> str | None:
    """Nom lisible : 0 -> « A », 1 -> « B »… puis « Z1 », « Z2 » au-delà de 26.

    Des lettres plutôt que des chiffres : un numéro suggère un classement, et
    « groupe 1 » se confond avec l'étiquette technique de k-means.
    """
    if stable_id is None:
        return None
    if stable_id < 26:
        return chr(ord("A") + stable_id)
    return f"Z{stable_id - 25}"
