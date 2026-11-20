# Conversational Travel Expenses Agent

A conversational, multi-agent assistant for **auditing employee travel and expense reports** (*notes de frais*), built with the [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/).

You describe what to analyse in plain language, for example *"Analyse the expenses of Marie Dupont for 2024"* or *"Audit the SMARTOSS team for 2023"*. The agent finds the right employees in HR data, pulls their trips and expenses from BigQuery, cross-checks the two expense systems (ONEO and AirPlus) and returns an audit report that flags anomalies.

## How it works

The root agent (`google-adk/note_de_frais_agent/agent.py`) is an ADK `Workflow` that chains specialised sub-agents with deterministic routing nodes:

```
START → intake_agent → scope_router
          ├─ "ask" → ask_missing_info      (asks the user for the missing employee/team/year)
          └─ "run" → hr_agent → perimetre_router
                       ├─ "stop" → report_hr_issue   (not found / ambiguous / team too large)
                       └─ "run"  → oneo_agent → airplus_agent ─┐
                                        └──────────────────────┴→ extraction_join → auditor_agent
```

| Agent | Role |
|---|---|
| `intake_agent` | Extracts the analysis scope (an employee or a team, plus a year) from the conversation as structured JSON. |
| `hr_agent` | Resolves the scope into a list of employee IDs (matricules) using the HR snapshot for that year. |
| `oneo_agent` | Fetches trips and expense lines from ONEO for those employees. |
| `airplus_agent` | Fetches the matching AirPlus transactions (flights, hotels, trains, cars) for the same trips. |
| `auditor_agent` | Normalises both sources and runs anomaly controls, then writes the audit report. |

Routing (`routing.py`) and the anomaly controls (`sub_agents/auditor_agent/controls.py`) are plain Python and make no LLM calls, so their results can be reproduced and tested. The auditor runs five controls:

- amounts above a per-category threshold (meals, hotel per night, taxi, train, flight, car rental)
- expenses on weekends
- duplicate expenses
- split transactions
- statistical outliers

## Tech stack

- Google ADK (`Workflow`, `Agent`, `JoinNode`, BigQuery toolset in read-only mode)
- Google BigQuery for the HR, ONEO and AirPlus data
- LiteLLM proxy as the LLM backend

## Configuration

Settings are read from environment variables, which can be put in a `.env` file (git-ignored):

| Variable | Purpose |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | GCP project for BigQuery |
| `BIGQUERY_PROJECT_HR`, `BIGQUERY_DATASET_HR`, `BIGQUERY_TABLE_HR` | HR table |
| `BIGQUERY_DATASET_ONEO`, `BIGQUERY_TABLE_ONEO` | ONEO expenses table |
| `BIGQUERY_DATASET_AIRPLUS`, `BIGQUERY_TABLE_AIRPLUS` | AirPlus transactions table |
| `LITE_LLM_MODEL`, `LITE_LLM_API_KEY`, `LITE_LLM_BASE_URL` | LLM settings, sent through the LiteLLM proxy |
| `NDF_ANNEE_MIN` | Earliest year that can be analysed (default 2021) |
| `NDF_MAX_TEAM_SIZE` | Largest team analysed in one run (default 200) |
| `NDF_MAX_ROWS` | Most rows extracted per source (default 300) |
| `AIRPLUS_MARGE_AVANT_MOIS`, `AIRPLUS_MARGE_APRES_MOIS` | Months of AirPlus data searched before and after the year (defaults 3 and 1) |
| `AUDIT_SEUIL_*` | Per-category thresholds for the audit, in EUR |

## Running

With Google Cloud credentials set up (`gcloud auth application-default login`), run this from the `google-adk/` directory:

```bash
adk web
```

Then choose `note_de_frais_agent` in the ADK dev UI. You can also use `adk run note_de_frais_agent` to chat in the terminal.
