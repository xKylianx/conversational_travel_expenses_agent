import os
from typing import Optional

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import ToolContext

from dotenv import load_dotenv
import litellm

from .controls import THRESHOLDS, run_anomaly_controls
from ... import state_keys

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

litellm.use_litellm_proxy = True
llm = LiteLlm(model=os.getenv("LITE_LLM_MODEL"),
              api_key=os.getenv("LITE_LLM_API_KEY"),
              base_url=os.getenv("LITE_LLM_BASE_URL"))

_seuils = "\n".join(f"  - {famille} : {seuil:.0f} EUR" for famille, seuil in THRESHOLDS.items())


def _oneo_to_tx(row: dict, i: int) -> dict:
    cat = " / ".join(str(v) for v in (row.get("categorie_frais"), row.get("nature_frais")) if v)
    return {
        "id": str(row.get("numero_ligne_de_frais") or row.get("numero_ndf") or f"ONEO-{i}"),
        "source": "ONEO",
        "matricule": row.get("matricule"),
        "spender": row.get("spender"),
        "date": row.get("date_depense"),
        "amount": row.get("montant_soumis"),
        "category": cat or None,
        "merchant": row.get("lieu_depense"),
        "country": row.get("pays"),
        "trip_id": row.get("numero_voyage"),
        "approval_date": row.get("date_approbation"),
        "nights": None,
    }


def _airplus_to_tx(row: dict, i: int) -> dict:
    return {
        "id": str(row.get("document_number") or row.get("invoice_number") or f"AIRPLUS-{i}"),
        "source": "AIRPLUS",
        "matricule": row.get("matricule"),
        "spender": row.get("spender"),
        "date": row.get("sale_date") or row.get("departure_date"),
        "amount": row.get("transaction_amount"),
        "category": row.get("service_type"),
        "merchant": row.get("services_provider"),
        "country": row.get("sale_country"),
        "trip_id": row.get("trip_counter"),
        "approval_date": None,
        "nights": row.get("nights"),
    }


def transactions_from_state(tool_context: ToolContext) -> list[dict]:
    """Normalise les lignes ONEO / Airplus déposées en état par les agents d'extraction."""
    oneo = tool_context.state.get(state_keys.ONEO_LIGNES) or []
    airplus = tool_context.state.get(state_keys.AIRPLUS_TRANSACTIONS) or []
    return [_oneo_to_tx(r, i) for i, r in enumerate(oneo, 1)] + [_airplus_to_tx(r, i) for i, r in enumerate(airplus, 1)]


def audit_transactions(tool_context: ToolContext, transactions: Optional[list[dict]] = None) -> dict:
    """Applique les 5 contrôles d'anomalies aux transactions ONEO et Airplus de l'analyse en cours.

    Sans argument, l'outil lit directement les lignes extraites par les agents ONEO et Airplus
    (état de session) et les normalise lui-même : c'est le mode à privilégier. Le paramètre
    `transactions` ne sert qu'en secours, si l'état est vide et que les transactions doivent être
    fournies manuellement (liste de dict avec les clés id, source, matricule, spender, date, amount,
    category, merchant, country, trip_id, approval_date, nights).

    Returns:
        dict avec la provenance des données (state | argument), le nombre de lignes par source,
        puis le résultat des contrôles : summary, anomalies, risk_by_transaction, thresholds.
    """
    if transactions:
        source, txs = "argument", list(transactions)
    else:
        source, txs = "state", transactions_from_state(tool_context)
    result = run_anomaly_controls(txs)
    result["provenance"] = source
    result["nb_par_source"] = {
        "ONEO": sum(1 for t in txs if str(t.get("source", "")).upper() == "ONEO"),
        "AIRPLUS": sum(1 for t in txs if str(t.get("source", "")).upper() == "AIRPLUS"),
    }

    # Synthèse par voyageur (utile pour les équipes) : calculée ici, pas par le LLM
    person_of: dict[str, str] = {}
    par_voyageur: dict[str, dict] = {}
    for t in txs:
        key = str(t.get("matricule") or t.get("spender") or "inconnu")
        person_of[str(t.get("id"))] = key
        entry = par_voyageur.setdefault(key, {"matricule": t.get("matricule"), "spender": t.get("spender"),
                                              "nb_transactions": 0, "total_eur": 0.0, "nb_anomalies": 0, "score_risque": 0})
        entry["nb_transactions"] += 1
        try:
            entry["total_eur"] += float(t.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    for a in result["anomalies"]:
        for tx_id in set(a["transaction_ids"]):
            key = person_of.get(str(tx_id))
            if key in par_voyageur:
                par_voyageur[key]["nb_anomalies"] += 1
    for tx_id, score in result["risk_by_transaction"].items():
        key = person_of.get(str(tx_id))
        if key in par_voyageur:
            par_voyageur[key]["score_risque"] += score
    for entry in par_voyageur.values():
        entry["total_eur"] = round(entry["total_eur"], 2)
    result["par_voyageur"] = sorted(par_voyageur.values(), key=lambda e: (-e["score_risque"], -e["total_eur"]))
    return result


auditor_agent = Agent(
    name="auditor_agent",
    model=llm,
    description="Agent d'audit : détecte les anomalies sur les transactions ONEO et Airplus",
    instruction=f"""
Tu es un auditeur interne spécialisé dans le contrôle des notes de frais et des dépenses de voyage.
Périmètre analysé : {{perimetre?}} — année {{annee?}}.

Tu reçois en entrée un JSON contenant les comptes rendus des agents d'extraction :
- "oneo_agent" : notes de frais ONEO (remboursements aux salariés)
- "airplus_agent" : transactions Airplus (paiements par carte logée : avion, train, hôtel, location, frais d'agence)
Ces comptes rendus servent de contexte ; les données brutes sont déjà disponibles pour l'outil d'audit.

## Étape 1 — Lancer les contrôles
Appelle l'outil `audit_transactions` SANS argument : il lit lui-même les lignes ONEO et Airplus extraites
et applique les contrôles. Ne recopie pas les transactions et ne fais JAMAIS les calculs toi-même.
Uniquement si l'outil indique 0 transaction alors que les comptes rendus en contiennent, rappelle-le en
passant `transactions` : une liste d'objets avec exactement ces clés :
  id            : identifiant (ONEO : numero_ligne_de_frais ou numero_ndf ; Airplus : document_number ou invoice_number ; sinon "ONEO-n"/"AIRPLUS-n")
  source        : "ONEO" ou "AIRPLUS"
  matricule     : matricule / xuid (ou null)
  spender       : nom du voyageur (ou null)
  date          : date de la dépense au format YYYY-MM-DD (ONEO : date_depense ; Airplus : sale_date, sinon departure_date)
  amount        : montant en EUR, nombre décimal positif (ONEO : montant_soumis ; Airplus : transaction_amount)
  category      : ONEO : "categorie_frais / nature_frais" ; Airplus : service_type
  merchant      : ONEO : lieu_depense ; Airplus : services_provider
  country       : pays (ou null)
  trip_id       : ONEO : numero_voyage ; Airplus : trip_counter (ou null)
  approval_date : ONEO : date_approbation au format YYYY-MM-DD (ou null)
  nights        : Airplus : nights (ou null)
N'invente aucune valeur : mets null si l'information est absente.

Les 5 contrôles sont implémentés de façon déterministe dans l'outil :
  1. DEPASSEMENT_SEUIL  : montant unitaire au-dessus du seuil de sa famille de dépense (hôtel : par nuitée). Seuils :
{_seuils}
  2. WEEKEND            : dépense effectuée un samedi ou un dimanche
  3. DOUBLON / DOUBLE_REMBOURSEMENT : même dépense saisie deux fois, ou dépense présente à la fois en NDF ONEO et en paiement carte Airplus
  4. FRACTIONNEMENT     : plusieurs dépenses d'une même famille le même jour, chacune sous le seuil mais dont la somme le dépasse
  5. MONTANT_ATYPIQUE   : montant statistiquement aberrant par rapport aux autres dépenses de la même famille (IQR ou multiple de la médiane)

## Étape 2 — Rédiger le rapport d'audit
À partir du résultat de l'outil, produis un rapport en français, structuré ainsi :
  ### Synthèse
  Périmètre et année, nombre de transactions analysées (par source), nombre d'anomalies par contrôle, transactions ignorées (données incomplètes).
  Si le périmètre est une équipe : ajoute un tableau par voyageur à partir de `par_voyageur` (nb transactions, total EUR, nb anomalies, score de risque cumulé).
  ### Anomalies détectées
  Un tableau trié par sévérité (HAUTE > MOYENNE > BASSE) : Sévérité | Contrôle | Transaction(s) | Voyageur | Montant | Détail.
  ### Transactions à revoir en priorité
  Les transactions avec le score de risque cumulé le plus élevé, et pour chacune la liste des contrôles déclenchés.
  ### Recommandations
  Actions concrètes pour le contrôleur (demander un justificatif, vérifier la note de frais X contre la facture Airplus Y, etc.).
Si aucune anomalie n'est détectée, dis-le explicitement et indique tout de même les transactions ignorées.
Reste factuel : cite uniquement des éléments présents dans le résultat de l'outil.
""",
    tools=[audit_transactions],
    output_key=state_keys.AUDIT_REPORT,
)