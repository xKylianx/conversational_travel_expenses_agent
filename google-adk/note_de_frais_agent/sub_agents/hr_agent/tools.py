"""Outils BigQuery de l'agent RH : résolution d'un employé ou d'une équipe en liste de matricules."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from google.adk.tools import ToolContext
from google.cloud import bigquery
import google.auth

from ... import state_keys

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
PROJECT_ID_HR = os.getenv("BIGQUERY_PROJECT_HR", PROJECT_ID)
DATASET_ID_HR = os.getenv("BIGQUERY_DATASET_HR")
TABLE_ID_HR = os.getenv("BIGQUERY_TABLE_HR")
TABLE_FQN_HR = f"`{PROJECT_ID_HR}.{DATASET_ID_HR}.{TABLE_ID_HR}`"

# Taille maximale d'une équipe analysable en une passe (volume de transactions / contexte LLM)
MAX_TEAM_SIZE = int(os.getenv("NDF_MAX_TEAM_SIZE", 200))
MAX_CANDIDATES = 10
LEVELS = tuple(f"niv{i}" for i in range(1, 9))

credentials, _ = google.auth.default()

# Snapshot RH de référence : le dernier mois disponible de l'année analysée (ou le plus récent si
# l'année est en cours). La table contient un snapshot complet par mois.
_SNAPSHOT = f"""
  SELECT MAX(date_part) FROM {TABLE_FQN_HR}
  WHERE date_part <= @fin_annee
"""
_PERSON_COLS = "xuid, prenom, nom, mail, cdr, " + ", ".join(LEVELS)


def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID, credentials=credentials)


def _run(sql: str, params: list) -> list[dict[str, Any]]:
    job = _client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(row) for row in job.result()]


def _fin_annee(annee: int) -> date:
    return min(date(annee, 12, 31), date.today())


def _norm_sql(column: str) -> str:
    """Expression SQL : minuscules, sans accents, espaces normalisés."""
    return f"TRIM(REGEXP_REPLACE(NORMALIZE(LOWER({column}), NFD), r'\\pM', ''))"


def _norm_py(value: str) -> str:
    import unicodedata
    return " ".join(
        "".join(c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)).split()
    )


def _hierarchy(row: dict[str, Any]) -> str:
    return " > ".join(str(row[l]) for l in LEVELS if row.get(l))


def _set_perimetre(tool_context: ToolContext, matricules: list[str], perimetre: dict[str, Any]) -> None:
    tool_context.state[state_keys.MATRICULES] = matricules
    tool_context.state[state_keys.PERIMETRE] = perimetre


def find_employee(nom: str, prenom: str, tool_context: ToolContext) -> dict:
    """Recherche un employé dans les données RH et enregistre son matricule pour les agents suivants.

    Recherche insensible à la casse et aux accents sur le snapshot RH de l'année d'analyse
    (état de session 'annee'). Si exactement une personne correspond, son xuid est enregistré
    dans l'état de session (clé 'matricules'). Sinon, retourne les candidats sans rien enregistrer.

    Args:
        nom: nom de famille de l'employé.
        prenom: prénom de l'employé.

    Returns:
        dict avec status ('OK' | 'AMBIGU' | 'INTROUVABLE'), le snapshot utilisé et les candidats
        (xuid, prenom, nom, mail, cdr, hiérarchie niv1>...>niv8).
    """
    annee = int(tool_context.state.get(state_keys.ANNEE) or date.today().year)
    params = [
        bigquery.ScalarQueryParameter("fin_annee", "DATE", _fin_annee(annee)),
        bigquery.ScalarQueryParameter("nom", "STRING", _norm_py(nom)),
        bigquery.ScalarQueryParameter("prenom", "STRING", _norm_py(prenom)),
    ]
    base = f"FROM {TABLE_FQN_HR} WHERE date_part = ({_SNAPSHOT}) AND xuid IS NOT NULL"
    exact = f"SELECT {_PERSON_COLS}, date_part {base} AND {_norm_sql('nom')} = @nom AND {_norm_sql('prenom')} = @prenom LIMIT {MAX_CANDIDATES + 1}"
    rows = _run(exact, params)
    match_type = "exact"
    if not rows:
        # Repli : le nom et le prénom contiennent les termes (noms composés, deuxième prénom...)
        fuzzy = (f"SELECT {_PERSON_COLS}, date_part {base} AND {_norm_sql('nom')} LIKE CONCAT('%', @nom, '%') "
                 f"AND {_norm_sql('prenom')} LIKE CONCAT('%', @prenom, '%') LIMIT {MAX_CANDIDATES + 1}")
        rows = _run(fuzzy, params)
        match_type = "partiel"

    candidats = [
        {"xuid": r["xuid"], "prenom": r["prenom"], "nom": r["nom"], "mail": r["mail"],
         "cdr": r["cdr"], "hierarchie": _hierarchy(r)}
        for r in rows[:MAX_CANDIDATES]
    ]
    snapshot = rows[0]["date_part"].isoformat() if rows else None

    if len(rows) == 1:
        c = candidats[0]
        _set_perimetre(tool_context, [c["xuid"]], {
            "type": "employe", "libelle": f"{c['prenom']} {c['nom']}", "annee": annee,
            "cdr": c["cdr"], "hierarchie": c["hierarchie"], "snapshot_rh": snapshot,
        })
        return {"status": "OK", "match": match_type, "snapshot_rh": snapshot, "annee": annee, "employe": c}
    if not rows:
        return {"status": "INTROUVABLE", "annee": annee, "detail": f"Aucun employé '{prenom} {nom}' dans le snapshot RH ≤ {_fin_annee(annee)}."}
    return {
        "status": "AMBIGU", "match": match_type, "snapshot_rh": snapshot, "annee": annee,
        "nb_candidats": len(rows) if len(rows) <= MAX_CANDIDATES else f">{MAX_CANDIDATES}",
        "candidats": candidats,
        "detail": "Plusieurs personnes correspondent : demander une précision (entité, mail, CDR).",
    }


def find_team(equipe: str, tool_context: ToolContext) -> dict:
    """Recherche une équipe dans les données RH et enregistre la liste de ses matricules.

    L'équipe est cherchée (insensible à la casse/accents) dans le CDR puis dans les niveaux
    hiérarchiques niv1 (le plus haut) à niv8 (le plus bas), d'abord en correspondance exacte,
    puis en correspondance partielle. Utilise le snapshot RH de l'année d'analyse (état 'annee').
    Si l'équipe compte au plus NDF_MAX_TEAM_SIZE membres, leurs xuid sont enregistrés dans l'état
    de session (clé 'matricules').

    Args:
        equipe: nom ou code de l'équipe, de l'entité, de la direction ou du CDR.

    Returns:
        dict avec status ('OK' | 'INTROUVABLE' | 'TROP_LARGE' | 'AMBIGU'), la colonne RH qui a
        matché, les CDR associés, le nombre de membres et la liste des membres (xuid, prenom, nom).
    """
    annee = int(tool_context.state.get(state_keys.ANNEE) or date.today().year)
    needle = _norm_py(equipe)
    params = [
        bigquery.ScalarQueryParameter("fin_annee", "DATE", _fin_annee(annee)),
        bigquery.ScalarQueryParameter("equipe", "STRING", needle),
    ]
    base = f"FROM {TABLE_FQN_HR} WHERE date_part = ({_SNAPSHOT}) AND xuid IS NOT NULL"
    columns = ("cdr",) + LEVELS

    # 1. Quelle colonne correspond ? (exacte d'abord, partielle ensuite)
    def _matches(op: str) -> list[dict[str, Any]]:
        union = " UNION ALL ".join(
            f"SELECT '{col}' AS colonne, {col} AS valeur, COUNT(DISTINCT xuid) AS nb {base} AND {_norm_sql(col)} {op} GROUP BY valeur"
            for col in columns
        )
        return _run(f"{union} ORDER BY nb DESC LIMIT 20", params)

    hits = _matches("= @equipe")
    match_type = "exact"
    if not hits:
        hits = _matches("LIKE CONCAT('%', @equipe, '%')")
        match_type = "partiel"
    if not hits:
        return {"status": "INTROUVABLE", "annee": annee,
                "detail": f"Aucune équipe/entité/CDR '{equipe}' dans le snapshot RH ≤ {_fin_annee(annee)}."}

    distinct_values = {(h["colonne"], h["valeur"]) for h in hits}
    if match_type == "partiel" and len(distinct_values) > 1:
        return {"status": "AMBIGU", "annee": annee, "match": match_type,
                "correspondances": [{"colonne": h["colonne"], "valeur": h["valeur"], "nb_membres": h["nb"]} for h in hits],
                "detail": "Plusieurs équipes correspondent partiellement : demander à l'utilisateur de choisir une valeur exacte."}

    # En correspondance exacte sur plusieurs colonnes (ex. même code en niv4 et niv5), on prend le niveau le plus peuplé
    colonne, valeur, nb = hits[0]["colonne"], hits[0]["valeur"], hits[0]["nb"]
    where_team = f"{base} AND {_norm_sql(colonne)} = @valeur"
    params_team = [params[0], bigquery.ScalarQueryParameter("valeur", "STRING", _norm_py(str(valeur)))]

    if nb > MAX_TEAM_SIZE:
        # Proposer une ventilation pour affiner : niveaux inférieurs successifs, puis CDR
        candidates = []
        if colonne in LEVELS:
            candidates = list(LEVELS[LEVELS.index(colonne) + 1:])
        candidates.append("cdr")
        sub_level, breakdown = None, []
        for col in candidates:
            breakdown = _run(
                f"SELECT {col} AS valeur, COUNT(DISTINCT xuid) AS nb_membres {where_team} AND {col} IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 20", params_team)
            if breakdown:
                sub_level = col
                break
        return {
            "status": "TROP_LARGE", "annee": annee, "colonne": colonne, "valeur": valeur, "nb_membres": nb,
            "max_membres": MAX_TEAM_SIZE, "sous_niveau": sub_level,
            "sous_equipes": [{"valeur": b["valeur"], "nb_membres": b["nb_membres"]} for b in breakdown],
            "detail": f"L'équipe '{valeur}' ({colonne}) compte {nb} membres, au-delà de la limite de {MAX_TEAM_SIZE}. "
                      "Proposer à l'utilisateur de choisir une sous-équipe.",
        }

    rows = _run(f"SELECT xuid, prenom, nom, cdr, {', '.join(LEVELS)}, date_part {where_team} ORDER BY nom, prenom", params_team)
    membres = [{"xuid": r["xuid"], "prenom": r["prenom"], "nom": r["nom"], "cdr": r["cdr"]} for r in rows]
    cdrs = sorted({r["cdr"] for r in rows if r["cdr"]})
    snapshot = rows[0]["date_part"].isoformat() if rows else None
    matricules = [m["xuid"] for m in membres]
    _set_perimetre(tool_context, matricules, {
        "type": "equipe", "libelle": str(valeur), "colonne": colonne, "annee": annee,
        "cdrs": cdrs, "nb_membres": len(matricules), "snapshot_rh": snapshot,
        "hierarchie": _hierarchy(rows[0]) if rows else None,
    })
    return {
        "status": "OK", "match": match_type, "annee": annee, "snapshot_rh": snapshot,
        "colonne": colonne, "valeur": valeur, "cdrs": cdrs, "nb_membres": len(matricules), "membres": membres,
    }
