import os
from datetime import date, datetime
from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
from google.adk.models.lite_llm import LiteLlm
from google.cloud import bigquery
import google.auth

from dotenv import load_dotenv
import litellm
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ... import state_keys  # noqa: E402

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")

litellm.use_litellm_proxy = True
llm = LiteLlm(model=os.getenv("LITE_LLM_MODEL"),
              api_key=os.getenv("LITE_LLM_API_KEY"),
              base_url=os.getenv("LITE_LLM_BASE_URL"))

credentials, _ = google.auth.default()
credentials_config = BigQueryCredentialsConfig(credentials=credentials)
bq_toolset = BigQueryToolset(
    credentials_config=credentials_config,
    bigquery_tool_config=BigQueryToolConfig(write_mode=WriteMode.BLOCKED),
)



DATASET_ID_AIRPLUS = os.getenv("BIGQUERY_DATASET_AIRPLUS")
TABLE_ID_AIRPLUS = os.getenv("BIGQUERY_TABLE_AIRPLUS")
FULL_TABLE_AIRPLUS = f"`{PROJECT_ID}.{DATASET_ID_AIRPLUS}.{TABLE_ID_AIRPLUS}`"

TRANSACTION_COLUMNS = (
    "document_number", "invoice_number", "trip_counter", "matricule", "spender", "sale_date",
    "invoice_date", "service_type", "services_provider", "sale_country", "sale_city",
    "departure_date", "arrival_date", "return_date", "nights", "rental_days",
    "transaction_amount", "billing_currency", "pnr", "fee_reasons", "code_projet", "cdr",
)
MAX_ROWS = int(os.getenv("NDF_MAX_ROWS", 300))
# Fenêtre de partitions autour de l'année : réservations anticipées (N-1) et facturation tardive (N+1)
MARGE_AVANT_MOIS = int(os.getenv("AIRPLUS_MARGE_AVANT_MOIS", 3))
MARGE_APRES_MOIS = int(os.getenv("AIRPLUS_MARGE_APRES_MOIS", 1))


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _shift_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def _period(tool_context: ToolContext) -> tuple[int, date, date]:
    annee = int(tool_context.state.get(state_keys.ANNEE) or date.today().year)
    debut = _shift_months(date(annee, 1, 1), -MARGE_AVANT_MOIS)
    fin = _shift_months(date(annee, 12, 31), MARGE_APRES_MOIS + 1)  # premier jour du mois suivant la marge
    return annee, debut, fin


def get_airplus_transactions(tool_context: ToolContext) -> dict:
    """Récupère en une seule requête les transactions Airplus des voyages identifiés par l'agent ONEO.

    Lit dans l'état de session les numero_voyage (== trip_counter Airplus) déposés par oneo_agent,
    les matricules déposés par hr_agent et l'année d'analyse. Filtre sur trip_counter IN
    (numero_voyages) ; si la liste est vide, se replie sur matricule IN (matricules). La requête est
    bornée sur les partitions date_part de l'année (avec une marge avant/après pour les réservations
    anticipées et la facturation tardive). Ne nécessite aucun argument.

    Returns:
        dict avec le filtre appliqué, les transactions (max NDF_MAX_ROWS lignes) et leur nombre,
        ou un statut ERROR si ni voyages ni matricules ne sont disponibles.
    """
    voyages = [v for v in (tool_context.state.get(state_keys.NUMERO_VOYAGES) or []) if v and v != "0"]
    matricules = [m for m in (tool_context.state.get(state_keys.MATRICULES) or []) if m]
    annee, debut, fin = _period(tool_context)
    params = [
        bigquery.ScalarQueryParameter("debut", "DATE", debut),
        bigquery.ScalarQueryParameter("fin", "DATE", fin),
    ]

    if voyages:
        where = "trip_counter IN UNNEST(@voyages)"
        params.append(bigquery.ArrayQueryParameter("voyages", "STRING", voyages))
        filtre = {"type": "trip_counter", "annee": annee, "numero_voyages": voyages, "matricules": matricules}
    elif matricules:
        where = "matricule IN UNNEST(@matricules)"
        params.append(bigquery.ArrayQueryParameter("matricules", "STRING", matricules))
        filtre = {"type": "matricule", "annee": annee, "numero_voyages": [], "matricules": matricules}
    else:
        return {
            "status": "ERROR",
            "detail": "Ni numero_voyages ni matricules dans l'état de session : les agents RH et ONEO "
                      "doivent d'abord résoudre le périmètre.",
        }

    client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    query = f"""
        SELECT {", ".join(TRANSACTION_COLUMNS)}
        FROM {FULL_TABLE_AIRPLUS}
        WHERE date_part >= @debut AND date_part < @fin
          AND {where}
        ORDER BY matricule, sale_date, trip_counter
        LIMIT {MAX_ROWS + 1}
    """
    rows = [
        {k: _jsonable(v) for k, v in dict(row).items()}
        for row in client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    ]
    transactions = rows[:MAX_ROWS]
    tool_context.state[state_keys.AIRPLUS_TRANSACTIONS] = transactions
    return {
        "status": "SUCCESS",
        "filtre": filtre,
        "count": len(transactions),
        "truncated": len(rows) > MAX_ROWS,
        "transactions": transactions,
    }


# --- Airplus Agent Configuration
# Les doubles accolades {{...}} sont résolues à l'exécution par ADK avec l'état de session
# (matricules déposés par hr_agent, numero_voyages par oneo_agent, annee par scope_router).
airplus_agent = Agent(
    name="airplus_agent",
    model=llm,
    description="Agent en charge de l'extraction des transactions Airplus",
    instruction=f"""
Vous êtes un agent d'extraction spécialisé dans l'analyse des transactions Airplus.
Votre rôle est d'extraire les informations pertinentes des transactions Airplus soumises qui contiennent les données relatives aux vols et trajets en train.

## Informations transmises par les agents RH et ONEO
- Matricules du périmètre : {{matricules?}}
- Année d'analyse : {{annee?}}
- Numéros de voyage ONEO (numero_voyage) : {{numero_voyages?}}

Dans Airplus, la colonne trip_counter correspond exactement au numero_voyage ONEO.
Le message utilisateur que tu reçois est le résultat de l'agent ONEO.

## Procédure (une seule requête)
1. Appelle l'outil get_airplus_transactions (sans argument) : il filtre lui-même la table sur
   trip_counter IN (numero_voyages) sur l'année d'analyse et se replie sur les matricules si la liste est vide.
2. Ne refais PAS de recherche des voyages et n'enchaîne PAS de requêtes SQL de repli :
   si l'outil renvoie 0 transaction, dis-le simplement dans ta réponse.
3. N'utilise execute_sql que si une information précise, absente du résultat de l'outil, est explicitement demandée.
4. Si l'outil renvoie une erreur (ni voyages ni matricule), signale-le et n'interroge pas la table.

Dans ta réponse finale, rappelle l'année, les matricules et les numéros de voyage utilisés (filtre appliqué), puis présente
les transactions (document_number, matricule, spender, trip_counter, sale_date, service_type, services_provider, sale_country,
sale_city, departure_date, arrival_date, nights, transaction_amount, pnr) sous forme de tableau.
Si plusieurs matricules (équipe), regroupe par voyageur. Si le résultat est tronqué (truncated = true), précise-le.

Voici le dataset et la table que vous pouvez interroger :
- Project : {PROJECT_ID}
- Dataset : {DATASET_ID_AIRPLUS}
- Table : {TABLE_ID_AIRPLUS}

Pour information, voici les colonnes disponibles dans la table :

filename	STRING	Nom du fichier
horodate	STRING	Horodatage
date_part	DATE	date de partitionnement (mois)
insert_date	TIMESTAMP	date insertion
sale_date	DATE	Jour de la vente: Date à laquelle la vente a été effectuée
invoice_date	DATE	Jour de facturation: Date d'émission de la facture, date comptable
contract_name	STRING	Nom du contrat principal
card_type	STRING	Type de carte (Logée, Logée AirPlus, Company Account)
service_type	STRING	Type de service vendu (Hôtel, Location voiture, Frais agence, etc.)
original_service_type	STRING	Type de service ayant déclenché des frais d'agence
services_provider	STRING	Prestataire de services - Chaîne (nom)
sale_country	STRING	Pays où la vente a été réalisée
sale_city	STRING	Ville où la vente a été réalisée
trip_counter	STRING	" ce champ est utilisé pour mapper 
le trip counter (identifiant voyage)"
matricule	STRING	ce champ est utilisé pour mapper le CUID/XUID
cdr	STRING	CDR
fee_reasons	STRING	raison de la dépense
code_projet	STRING	code  projet
pnr	STRING	le numéro de réservation
spender	STRING	Nom du voyageur
arrival_date	DATE	Date d'arrivée
departure_date	DATE	Date de départ
nights	FLOAT	Nombre de nuitées
pickup_location	STRING	Lieu de prise en charge
return_location	STRING	Lieu de retour
vehicle_class	STRING	Classe de véhicule
car_brand	STRING	Marque du véhicule
rental_days	FLOAT	Nombre de jours de location
return_date	DATE	Date de retour
mileage	FLOAT	Kilométrage parcouru
fuel_consumed	STRING	Consommation de carburant
transaction_count	INTEGER	Nombre de transactions
transaction_amount	FLOAT	Montant de la transaction
international_consolidation	STRING	consolidation internationale 
consolidation_name	STRING	Nom de la consolidation
group_contract_name	STRING	Nom du contrat liant le groupe
card_name	STRING	Nom (Numéro) de la carte
contracted_company	STRING	Nom de la compagnie avec laquelle le contrat a été signé
cost_center	STRING	Centre de coûts
department	STRING	Département
destination	STRING	Destination
departure_date_bd	STRING	Date de départ DB
action_number_AK	STRING	Identifiant de l'action
customer_reference	STRING	Référence client
invoice_number	STRING	Numéro de facture
notes	STRING	Notes supplémentaires
vat_code	STRING	Code TVA
document_number	STRING	Identifiant du document
reservation_number	STRING	Numéro de réservation
transfer_date	STRING	Date de transfert
aida_id	STRING	Identifiant AIDA
EUR	STRING	Devise (EUR)
invoice_total	STRING	Montant total de la facture
billing_currency	STRING	Devise de facturation

Utilise la colonne trip_counter (== numero_voyage ONEO) pour identifier les voyages correspondants.
""",
    tools=[get_airplus_transactions, bq_toolset],
)
