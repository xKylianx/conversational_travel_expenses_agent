import json
import os
from datetime import date
from typing import Literal, Optional

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.lite_llm import LiteLlm
import litellm
from pydantic import BaseModel, Field

from ... import state_keys

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

litellm.use_litellm_proxy = True
llm = LiteLlm(model=os.getenv("LITE_LLM_MODEL"),
              api_key=os.getenv("LITE_LLM_API_KEY"),
              base_url=os.getenv("LITE_LLM_BASE_URL"))


class AnalysisScope(BaseModel):
    """Périmètre d'analyse extrait de la demande de l'utilisateur."""

    mode: Optional[Literal["employe", "equipe"]] = Field(
        None, description="'employe' si une personne est visée, 'equipe' si une équipe/entité/CDR est visée")
    nom: Optional[str] = Field(None, description="Nom de famille de l'employé (mode employe)")
    prenom: Optional[str] = Field(None, description="Prénom de l'employé (mode employe)")
    equipe: Optional[str] = Field(None, description="Nom/code de l'équipe, de l'entité ou du CDR (mode equipe)")
    annee: Optional[int] = Field(None, description="Année d'analyse sur 4 chiffres")


INSTRUCTION_TEMPLATE = """
Tu es l'agent d'accueil d'un outil d'audit des notes de frais. Date du jour : {today}.

Ta seule tâche : extraire de la conversation le périmètre d'analyse et le renvoyer au format JSON strict
correspondant au schéma AnalysisScope (mode, nom, prenom, equipe, annee). Tu ne poses pas de question,
tu ne fais aucune analyse.

Périmètre déjà connu (issu des échanges précédents) : {previous}

Règles :
- Fusionne le périmètre déjà connu avec les nouvelles informations du message ; une information nouvelle remplace l'ancienne.
- mode = "employe" si une personne (nom et/ou prénom) est citée ; "equipe" si une équipe, une entité, une direction ou un CDR est cité.
  Si le message cite les deux, privilégie la personne. Si rien ne permet de trancher, laisse mode à null.
- annee : année sur 4 chiffres. Interprète "cette année" / "l'an dernier" par rapport à la date du jour.
  Si une période de type "2024-2025" est donnée, prends la première année. Si aucune année n'est donnée, laisse null.
- N'invente rien : toute information absente reste null.
- Réponds uniquement avec le JSON, sans texte autour.
"""


async def intake_instruction(ctx: ReadonlyContext) -> str:
    previous = ctx.state.get(state_keys.ANALYSIS_SCOPE) or {}
    return INSTRUCTION_TEMPLATE.format(
        today=date.today().isoformat(),
        previous=json.dumps(previous, ensure_ascii=False),
    )


intake_agent = Agent(
    name="intake_agent",
    model=llm,
    description="Extrait le périmètre d'analyse (employé ou équipe, année) de la demande utilisateur",
    instruction=intake_instruction,
    output_schema=AnalysisScope,
    output_key=state_keys.ANALYSIS_SCOPE,
)
