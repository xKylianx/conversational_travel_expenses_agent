import os
from datetime import date, datetime
from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
from google.adk.models.lite_llm import LiteLlm
from google.cloud import bigquery
import google.auth

from ... import state_keys

from dotenv import load_dotenv
import litellm
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

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

DATASET_ID_ONEO = os.getenv("BIGQUERY_DATASET_ONEO")
TABLE_ID_ONEO = os.getenv("BIGQUERY_TABLE_ONEO")
FULL_TABLE_ONEO = f"`{PROJECT_ID}.{DATASET_ID_ONEO}.{TABLE_ID_ONEO}`"

MAX_ROWS = int(os.getenv("NDF_MAX_ROWS", 300))
EXPENSE_COLUMNS = (
    "numero_ligne_de_frais", "numero_ndf", "numero_voyage", "matricule", "spender", "superviseur_n1",
    "date_depense", "date_approbation", "categorie_frais", "nature_frais", "motif_de_frais",
    "lieu_depense", "pays", "montant_soumis", "montant_paye", "conformite_du_montant_soumis",
    "statut", "code_projet", "cdr", "societe",
)


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _period(tool_context: ToolContext) -> tuple[int, date, date]:
    annee = int(tool_context.state.get(state_keys.ANNEE) or date.today().year)
    return annee, date(annee, 1, 1), date(annee, 12, 31)


def _run(query: str, params: list) -> list[dict]:
    client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    job = client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [{k: _jsonable(v) for k, v in dict(row).items()} for row in job.result()]


def get_list_travel(tool_context: ToolContext) -> dict:
    """Liste les numéros de voyage ONEO du périmètre (matricules résolus par l'agent RH) sur l'année d'analyse.

    Lit dans l'état de session les matricules (déposés par hr_agent) et l'année (déposée par
    scope_router), exécute une seule requête bornée sur la partition date_part de l'année
    (valeur bouche-trou '0' ignorée) et enregistre la liste des numero_voyage dans l'état de
    session pour que airplus_agent filtre sa table sur trip_counter. Ne nécessite aucun argument.

    Returns:
        dict avec l'année, les matricules, les voyages par matricule, la liste globale des
        numero_voyage et leur nombre ; status ERROR si aucun matricule n'est disponible.
    """
    matricules = [m for m in (tool_context.state.get(state_keys.MATRICULES) or []) if m]
    if not matricules:
        return {"status": "ERROR", "detail": "Aucun matricule dans l'état de session : l'agent RH doit d'abord résoudre le périmètre."}
    annee, debut, fin = _period(tool_context)
    rows = _run(
        f"""
        SELECT matricule, numero_voyage, MIN(date_depense) AS premiere_depense, MAX(date_depense) AS derniere_depense,
               COUNT(*) AS nb_lignes, ROUND(SUM(montant_soumis), 2) AS total_soumis
        FROM {FULL_TABLE_ONEO}
        WHERE date_part BETWEEN @debut AND @fin
          AND matricule IN UNNEST(@matricules)
          AND numero_voyage IS NOT NULL AND numero_voyage NOT IN ('', '0')
        GROUP BY 1, 2
        ORDER BY 1, 3
        """,
        [
            bigquery.ScalarQueryParameter("debut", "DATE", debut),
            bigquery.ScalarQueryParameter("fin", "DATE", fin),
            bigquery.ArrayQueryParameter("matricules", "STRING", matricules),
        ],
    )
    voyages = sorted({r["numero_voyage"] for r in rows})
    tool_context.state[state_keys.NUMERO_VOYAGES] = voyages
    return {
        "status": "SUCCESS",
        "annee": annee,
        "matricules": matricules,
        "count": len(voyages),
        "numero_voyages": voyages,
        "voyages_detail": rows,
    }


def get_oneo_expenses(tool_context: ToolContext) -> dict:
    """Récupère en une seule requête les lignes de notes de frais ONEO des voyages identifiés par get_list_travel.

    Filtre sur la partition date_part de l'année d'analyse, les matricules du périmètre et les
    numero_voyage déposés dans l'état de session (max NDF_MAX_ROWS lignes). Ne nécessite aucun argument.

    Returns:
        dict avec le filtre appliqué, les lignes de frais et leur nombre ; status ERROR si
        get_list_travel n'a pas été appelé avant.
    """
    matricules = [m for m in (tool_context.state.get(state_keys.MATRICULES) or []) if m]
    voyages = [v for v in (tool_context.state.get(state_keys.NUMERO_VOYAGES) or []) if v and v != "0"]
    if not matricules:
        return {"status": "ERROR", "detail": "Aucun matricule dans l'état de session."}
    if not voyages:
        return {"status": "SUCCESS", "count": 0, "truncated": False, "lignes": [],
                "detail": "Aucun numero_voyage : appeler get_list_travel d'abord, ou aucun voyage sur la période."}
    annee, debut, fin = _period(tool_context)
    rows = _run(
        f"""
        SELECT {", ".join(EXPENSE_COLUMNS)}
        FROM {FULL_TABLE_ONEO}
        WHERE date_part BETWEEN @debut AND @fin
          AND matricule IN UNNEST(@matricules)
          AND numero_voyage IN UNNEST(@voyages)
        ORDER BY matricule, numero_voyage, date_depense
        LIMIT {MAX_ROWS + 1}
        """,
        [
            bigquery.ScalarQueryParameter("debut", "DATE", debut),
            bigquery.ScalarQueryParameter("fin", "DATE", fin),
            bigquery.ArrayQueryParameter("matricules", "STRING", matricules),
            bigquery.ArrayQueryParameter("voyages", "STRING", voyages),
        ],
    )
    lignes = rows[:MAX_ROWS]
    tool_context.state[state_keys.ONEO_LIGNES] = lignes
    return {
        "status": "SUCCESS",
        "filtre": {"annee": annee, "matricules": matricules, "numero_voyages": voyages},
        "count": len(lignes),
        "truncated": len(rows) > MAX_ROWS,
        "lignes": lignes,
    }


# Les doubles accolades {{...}} sont résolues à l'exécution par ADK avec l'état de session.
oneo_agent = Agent(
    name="oneo_agent",
    model=llm,
    description="Agent d'extraction des notes de frais ONEO",
    instruction=f"""
Vous êtes un agent d'extraction spécialisé dans l'analyse des notes de frais ONEO.
Votre rôle est d'extraire les lignes de notes de frais du périmètre résolu par l'agent RH.

## Périmètre (déposé par l'agent RH)
- Matricules (== xuid RH) : {{matricules?}}
- Périmètre : {{perimetre?}}
- Année d'analyse : {{annee?}}

## Procédure (deux appels d'outil, aucune requête SQL à écrire)
1. Appelle get_list_travel (sans argument) : il liste les numero_voyage des matricules sur l'année
   et les transmet à l'agent Airplus (trip_counter == numero_voyage).
2. Appelle get_oneo_expenses (sans argument) : il récupère les lignes de frais de ces voyages.
3. Si get_list_travel renvoie 0 voyage, dis-le et arrête-toi : n'enchaîne pas de requêtes de repli.
4. N'utilise execute_sql que pour une information précise explicitement demandée et absente des outils.

## Réponse attendue
Rappelle l'année, les matricules et la liste des numero_voyage, puis présente les lignes de frais
(numero_ligne_de_frais, matricule, spender, numero_voyage, date_depense, categorie_frais, nature_frais,
lieu_depense, pays, montant_soumis, date_approbation, statut) sous forme de tableau.
Si plusieurs matricules (équipe), regroupe par voyageur. Si le résultat est tronqué (truncated = true), précise-le.

Voici le dataset et la table que vous pouvez interroger :
- Project : {PROJECT_ID}
- Dataset : {DATASET_ID_ONEO}
- Table : {TABLE_ID_ONEO}

Pour information, voici les colonnes disponibles dans la table :
filename	STRING	Nom du fichier
horodate	STRING	Horodatage
date_part	DATE	date de partitionnement (mois)
insert_date	TIMESTAMP	date insertion
cdr	STRING	CDR
matricule	STRING	Matricule
spender	STRING	Voyageur
superviseur_n1	STRING	superviseur_n1
numero_ndf	STRING	n°_NDF
numero_voyage	STRING	n°_VOYAGE
motif_de_frais	STRING	Motif_de_frais
pays	STRING	Pays
lieu_depense	STRING	Lieu_dépense
categorie_frais	STRING	Catégorie_frais
nature_frais	STRING	Nature_frais
date_depense	DATE	Date_dépense
date_approbation	DATE	Date_d'approbation
conformite_du_montant_soumis	STRING	Conformité_du_montant_soumis
montant_soumis	FLOAT	Montant soumis
montant_paye	FLOAT	Montant_payé
tva_deductible	FLOAT	TVA Déductible
montant_de_reference_pvg	FLOAT	Montant_de_référence_PVG
code_projet	STRING	Code_Projet
societe	STRING	Société
mois	STRING	mois
cdr_et_libelle	STRING	CDR_et_libellé
statut	STRING	statut
numero_ligne_de_frais	STRING	n°_ligne_de_frais
entite_comex	STRING	Entité_COMEX
entite_comex_et_libelle	STRING	Entité_COMEX_et_libellé
type_de_donnees_origine	STRING	Type de données origine
devise_de_consolidation	STRING	Devise de consolidation

Utilise la colonne numero_voyage pour identifier les voyages correspondants.
""",
    tools=[get_list_travel, get_oneo_expenses, bq_toolset],
)