from .sub_agents.intake_agent.intake_agent import intake_agent
from .sub_agents.hr_agent.hr_agent import hr_agent
from .sub_agents.oneo_agent.oneo_agent import oneo_agent
from .sub_agents.airplus_agent.airplus_agent import airplus_agent
from .sub_agents.auditor_agent.auditor_agent import auditor_agent
from .routing import (
    ROUTE_ASK, ROUTE_RUN, ROUTE_STOP,
    ask_missing_info, perimetre_router, report_hr_issue, scope_router,
)
from google.adk import Workflow
from google.adk.workflow import JoinNode

# Attend la fin des deux extracteurs et transmet {"oneo_agent": ..., "airplus_agent": ...}
extraction_join = JoinNode(name="extraction_join")

# --- Root agent
#   START -> intake_agent (périmètre : employé ou équipe + année) -> scope_router
#     "ask"  -> ask_missing_info : informations manquantes, on redemande à l'utilisateur (fin de tour)
#     "run"  -> hr_agent (résolution des matricules) -> perimetre_router
#        "stop" -> report_hr_issue : introuvable / ambigu / équipe trop large (fin de tour)
#        "run"  -> oneo_agent (voyages + notes de frais) -> airplus_agent (même voyages) -> join -> auditor
# L'arête oneo_agent -> extraction_join garantit que l'auditor reçoit aussi la sortie ONEO.
root_agent = Workflow(
    name="root_agent",
    edges=[
        ("START", intake_agent, scope_router, {ROUTE_ASK: ask_missing_info, ROUTE_RUN: hr_agent}),
        (hr_agent, perimetre_router, {ROUTE_STOP: report_hr_issue, ROUTE_RUN: oneo_agent}),
        (oneo_agent, airplus_agent, extraction_join, auditor_agent),
        (oneo_agent, extraction_join),
    ]
)
