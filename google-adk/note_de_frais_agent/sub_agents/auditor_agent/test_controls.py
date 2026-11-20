"""Test des 5 contrôles d'anomalies sur un jeu de données synthétique.

Exécution : python -m note_de_frais_agent.sub_agents.auditor_agent.test_controls
"""

import json

from .controls import run_anomaly_controls

TRANSACTIONS = [
    # --- Lot "repas" normal (sert de base statistique) ---
    *[
        {"id": f"ONEO-R{i}", "source": "ONEO", "matricule": "M001", "spender": "Alice Martin",
         "date": f"2025-03-{10 + i:02d}", "amount": 20 + i, "category": "Restauration / Repas",
         "merchant": "Paris", "country": "FR"}
        for i in range(8)
    ],
    # 1. Dépassement de seuil : repas à 180 EUR (seuil 80) — aussi outlier statistique
    {"id": "ONEO-001", "source": "ONEO", "matricule": "M002", "spender": "Bob Durand",
     "date": "2025-03-12", "amount": 180.0, "category": "Restauration / Repas", "merchant": "Lyon"},
    # 1bis. Hôtel Airplus 3 nuits à 900 EUR = 300/nuit (seuil 200)
    {"id": "AP-100", "source": "AIRPLUS", "matricule": "M003", "spender": "Chloé Petit",
     "date": "2025-03-11", "amount": 900.0, "category": "Hotel", "merchant": "Accor", "nights": 3},
    # 2. Week-end : samedi 15/03/2025
    {"id": "ONEO-002", "source": "ONEO", "matricule": "M002", "spender": "Bob Durand",
     "date": "2025-03-15", "amount": 45.0, "category": "Taxi", "merchant": "Marseille"},
    # 3a. Doublon strict ONEO
    {"id": "ONEO-003", "source": "ONEO", "matricule": "M004", "spender": "David Roux",
     "date": "2025-03-13", "amount": 62.5, "category": "Repas", "merchant": "Lille"},
    {"id": "ONEO-004", "source": "ONEO", "matricule": "M004", "spender": "David Roux",
     "date": "2025-03-13", "amount": 62.5, "category": "Repas", "merchant": "Lille"},
    # 3b. Double remboursement : billet de train en NDF ONEO ET payé via Airplus
    {"id": "ONEO-005", "source": "ONEO", "matricule": "M005", "spender": "Emma Blanc",
     "date": "2025-03-17", "amount": 120.0, "category": "Transport / Train", "merchant": "SNCF"},
    {"id": "AP-101", "source": "AIRPLUS", "matricule": "M005", "spender": "Emma Blanc",
     "date": "2025-03-17", "amount": 118.0, "category": "Rail", "merchant": "SNCF", "trip_id": "T1"},
    # 4. Fractionnement : 3 repas le même jour sous 80 EUR, total 210 EUR
    {"id": "ONEO-006", "source": "ONEO", "matricule": "M006", "spender": "Farid Nasri",
     "date": "2025-03-18", "amount": 75.0, "category": "Repas", "merchant": "Nice"},
    {"id": "ONEO-007", "source": "ONEO", "matricule": "M006", "spender": "Farid Nasri",
     "date": "2025-03-18", "amount": 70.0, "category": "Repas", "merchant": "Nice"},
    {"id": "ONEO-008", "source": "ONEO", "matricule": "M006", "spender": "Farid Nasri",
     "date": "2025-03-18", "amount": 65.0, "category": "Repas", "merchant": "Nice"},
    # Données incomplètes -> ignorée
    {"id": "ONEO-BAD", "source": "ONEO", "spender": "X", "date": None, "amount": "abc"},
]


def main() -> None:
    result = run_anomaly_controls(TRANSACTIONS)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    for a in result["anomalies"]:
        print(f"- [{a['severity']}] {a['control']}: {a['transaction_ids']} — {a['detail']}")

    by_control = {}
    for a in result["anomalies"]:
        by_control.setdefault(a["control"], []).append(set(a["transaction_ids"]))

    def triggered(control: str, *ids: str) -> bool:
        return any(set(ids) <= s for s in by_control.get(control, []))

    checks = {
        "seuil repas ONEO-001": triggered("DEPASSEMENT_SEUIL", "ONEO-001"),
        "seuil hôtel/nuit AP-100": triggered("DEPASSEMENT_SEUIL", "AP-100"),
        "weekend ONEO-002": triggered("WEEKEND", "ONEO-002"),
        "doublon ONEO-003/004": triggered("DOUBLON", "ONEO-003", "ONEO-004"),
        "double remboursement ONEO-005/AP-101": triggered("DOUBLE_REMBOURSEMENT", "ONEO-005", "AP-101"),
        "fractionnement ONEO-006/007/008": triggered("FRACTIONNEMENT", "ONEO-006", "ONEO-007", "ONEO-008"),
        "outlier ONEO-001": triggered("MONTANT_ATYPIQUE", "ONEO-001"),
        "pas de faux positif seuil sur ONEO-R0": not triggered("DEPASSEMENT_SEUIL", "ONEO-R0"),
        "ligne incomplète ignorée": "ONEO-BAD" in result["summary"]["transactions_ignorees"],
    }
    print()
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(("OK   " if ok else "FAIL ") + name)
    if failed:
        raise SystemExit(f"{len(failed)} contrôle(s) en échec : {failed}")
    print("\nTous les contrôles sont validés.")


if __name__ == "__main__":
    main()
