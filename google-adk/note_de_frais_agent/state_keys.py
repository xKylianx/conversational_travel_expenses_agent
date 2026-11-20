"""Clés de l'état de session partagées entre les agents du workflow notes de frais."""

# Périmètre demandé par l'utilisateur (intake_agent) : {mode, nom, prenom, equipe, annee}
ANALYSIS_SCOPE = "analysis_scope"
# Année d'analyse validée (int)
ANNEE = "annee"
# Liste des matricules (xuid RH == matricule ONEO/Airplus) résolus par hr_agent
MATRICULES = "matricules"
# Description du périmètre résolu par hr_agent (employé ou équipe)
PERIMETRE = "perimetre"
# Numéros de voyage ONEO (== trip_counter Airplus) résolus par oneo_agent
NUMERO_VOYAGES = "numero_voyages"
# Lignes brutes extraites (listes de dict) : consommées par l'auditor sans repasser par le LLM
ONEO_LIGNES = "oneo_lignes"
AIRPLUS_TRANSACTIONS = "airplus_transactions"
# Rapport final de l'auditor_agent
AUDIT_REPORT = "audit_report"

# Clés remises à zéro à chaque nouvelle analyse
RUN_KEYS = (MATRICULES, PERIMETRE, NUMERO_VOYAGES, ONEO_LIGNES, AIRPLUS_TRANSACTIONS, AUDIT_REPORT)
