"""Nœuds de routage déterministes du workflow notes de frais (pas d'appel LLM)."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.genai import types

from . import state_keys

ROUTE_ASK = "ask"
ROUTE_RUN = "run"
ROUTE_STOP = "stop"

MIN_ANNEE = int(os.getenv("NDF_ANNEE_MIN", 2021))


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _text_event(text: str, route: str | None = None, state: dict | None = None) -> Event:
    return Event(
        message=types.Content(role="model", parts=[types.Part(text=text)]),
        output=text,
        route=route,
        state=state,
    )


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_scope(scope: dict) -> tuple[dict, list[str]]:
    """Normalise le périmètre et renvoie (scope_normalisé, liste des informations manquantes)."""
    mode = _clean(scope.get("mode"))
    nom, prenom, equipe = _clean(scope.get("nom")), _clean(scope.get("prenom")), _clean(scope.get("equipe"))
    annee_raw = scope.get("annee")
    missing: list[str] = []

    if mode not in ("employe", "equipe"):
        mode = "employe" if (nom or prenom) else "equipe" if equipe else None
    if mode is None:
        missing.append("le périmètre : le **nom et prénom** d'un employé, ou le nom d'une **équipe / entité / CDR**")
    elif mode == "employe":
        if not nom:
            missing.append("le **nom** de l'employé")
        if not prenom:
            missing.append("le **prénom** de l'employé")
    elif not equipe:
        missing.append("le nom de l'**équipe** (ou entité / CDR)")

    annee = None
    try:
        annee = int(str(annee_raw).strip()) if annee_raw not in (None, "") else None
    except ValueError:
        annee = None
    if annee is None or not (MIN_ANNEE <= annee <= date.today().year):
        missing.append(f"l'**année d'analyse** (entre {MIN_ANNEE} et {date.today().year})")
        annee = None

    normalized = {"mode": mode, "nom": nom, "prenom": prenom, "equipe": equipe, "annee": annee}
    return normalized, missing


def scope_router(ctx: Context) -> Event:
    """Vérifie que le périmètre est complet : route 'run' vers l'agent RH, sinon 'ask' (question à l'utilisateur)."""
    scope, missing = validate_scope(_as_dict(ctx.state.get(state_keys.ANALYSIS_SCOPE)))
    state: dict[str, Any] = {state_keys.ANALYSIS_SCOPE: scope}

    if missing:
        known = []
        if scope["mode"] == "employe" and (scope["nom"] or scope["prenom"]):
            known.append(f"employé : {scope['prenom'] or '?'} {scope['nom'] or '?'}")
        if scope["mode"] == "equipe" and scope["equipe"]:
            known.append(f"équipe : {scope['equipe']}")
        if scope["annee"]:
            known.append(f"année : {scope['annee']}")
        text = "Pour lancer l'analyse des notes de frais, il me manque :\n" + "\n".join(f"- {m}" for m in missing)
        if known:
            text += "\n\nDéjà noté : " + ", ".join(known) + "."
        text += "\n\nExemple : « Analyse les dépenses de Marie Dupont sur 2024 » ou « Audite l'équipe SMARTOSS pour 2023 »."
        return _text_event(text, route=ROUTE_ASK, state=state)

    # Nouvelle analyse : on repart d'un état propre pour les étapes suivantes
    state[state_keys.ANNEE] = scope["annee"]
    for key in state_keys.RUN_KEYS:
        state[key] = None
    if scope["mode"] == "employe":
        text = f"Périmètre validé : employé {scope['prenom']} {scope['nom']}, année {scope['annee']}."
    else:
        text = f"Périmètre validé : équipe « {scope['equipe']} », année {scope['annee']}."
    return _text_event(text, route=ROUTE_RUN, state=state)


def ask_missing_info(ctx: Context, node_input: Any = None) -> types.Content:
    """Nœud terminal : restitue la question posée par scope_router à l'utilisateur."""
    text = node_input if isinstance(node_input, str) else str(node_input or "Merci de préciser votre demande.")
    return types.Content(role="model", parts=[types.Part(text=text)])


def perimetre_router(ctx: Context, node_input: Any = None) -> Event:
    """Après l'agent RH : route 'run' si des matricules ont été résolus, sinon 'stop'."""
    matricules = ctx.state.get(state_keys.MATRICULES) or []
    hr_text = node_input if isinstance(node_input, str) else str(node_input or "")
    if matricules:
        perimetre = ctx.state.get(state_keys.PERIMETRE) or {}
        text = f"{len(matricules)} matricule(s) résolu(s) ({perimetre.get('type', 'périmètre')}) — extraction en cours."
        return _text_event(text, route=ROUTE_RUN)
    text = (
        "L'analyse ne peut pas démarrer : aucun matricule n'a pu être résolu dans les données RH.\n\n"
        f"{hr_text}\n\nMerci de préciser votre demande (orthographe du nom, prénom, nom exact de l'équipe ou niveau plus fin)."
    )
    return _text_event(text, route=ROUTE_STOP)


def report_hr_issue(ctx: Context, node_input: Any = None) -> types.Content:
    """Nœud terminal : explique pourquoi le périmètre RH n'a pas pu être résolu."""
    text = node_input if isinstance(node_input, str) else str(node_input or "Périmètre RH non résolu.")
    return types.Content(role="model", parts=[types.Part(text=text)])
