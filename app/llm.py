"""Fournisseurs LLM interchangeables : Anthropic (Claude), Mistral, Google (Gemini).

Chaque fournisseur reçoit le même prompt système, le même message et le même schéma JSON,
et renvoie le JSON décodé. On change de fournisseur avec LLM_PROVIDER (+ LLM_MODEL).
"""

from __future__ import annotations

import json
import os
from typing import Protocol

from app import config


class Refus(Exception):
    """Le modèle a refusé de répondre (filtre de sécurité) : on n'insiste pas."""


class ErreurConfiguration(Exception):
    """Clé absente/invalide ou modèle inconnu : inutile de continuer le lot."""


class Fournisseur(Protocol):
    nom: str
    modele: str
    variable_cle: str

    def classer(self, system: str, message: str, schema: dict) -> tuple[dict, str]:
        """Renvoie (JSON décodé, identifiant du modèle ayant répondu)."""


def _json(texte: str | None) -> dict:
    if not texte:
        raise RuntimeError("Réponse vide")
    return json.loads(texte)


# ---------------------------------------------------------------- Anthropic


class Anthropic:
    nom = "anthropic"
    variable_cle = "ANTHROPIC_API_KEY"

    def __init__(self, modele: str):
        import anthropic

        self.modele = modele
        self._sdk = anthropic
        self._client = anthropic.Anthropic(max_retries=4)

    def classer(self, system: str, message: str, schema: dict) -> tuple[dict, str]:
        a = self._sdk
        try:
            response = self._client.beta.messages.create(
                model=self.modele,
                max_tokens=16000,
                # En cas de refus, l'API relance la requête sur un modèle de repli.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                # Prompt système identique pour tous les scrutins : mis en cache.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": message}],
                output_config={
                    "effort": config.LLM_EFFORT,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as e:
            raise ErreurConfiguration(f"Anthropic : {e}") from e
        if response.stop_reason == "refusal":
            raise Refus(str(getattr(response, "stop_details", None)))
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Réponse tronquée (max_tokens)")
        texte = next((b.text for b in response.content if b.type == "text"), None)
        return _json(texte), response.model


# ------------------------------------------------------------------ Mistral


class Mistral:
    nom = "mistral"
    variable_cle = "MISTRAL_API_KEY"

    def __init__(self, modele: str):
        from mistralai.client import Mistral as Client

        self.modele = modele
        self._client = Client(api_key=os.environ.get(self.variable_cle, ""))

    def classer(self, system: str, message: str, schema: dict) -> tuple[dict, str]:
        from mistralai.client import errors

        try:
            response = self._client.chat.complete(
                model=self.modele,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "classement", "schema_definition": schema,
                                    "strict": True},
                },
                max_tokens=4000,
            )
        except errors.SDKError as e:
            if e.status_code in (401, 403, 404):
                raise ErreurConfiguration(f"Mistral : {e}") from e
            raise
        choix = response.choices[0]
        if choix.finish_reason in ("length", "model_length"):
            raise RuntimeError("Réponse tronquée (max_tokens)")
        if choix.finish_reason == "error":
            raise RuntimeError("Erreur côté Mistral")
        contenu = choix.message.content
        if isinstance(contenu, list):  # contenu découpé en blocs
            contenu = "".join(getattr(c, "text", "") or "" for c in contenu)
        return _json(contenu), response.model or self.modele


# ------------------------------------------------------------------- Gemini

_GEMINI_REFUS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class Gemini:
    nom = "gemini"
    variable_cle = "GEMINI_API_KEY"

    def __init__(self, modele: str):
        from google import genai

        self.modele = modele
        self._client = genai.Client(api_key=os.environ.get(self.variable_cle) or None)

    def classer(self, system: str, message: str, schema: dict) -> tuple[dict, str]:
        from google.genai import errors, types

        try:
            response = self._client.models.generate_content(
                model=self.modele,
                contents=message,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    max_output_tokens=16000,
                ),
            )
        except errors.ClientError as e:
            if e.code in (401, 403, 404):
                raise ErreurConfiguration(f"Gemini : {e}") from e
            raise
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise Refus(f"Gemini : requête bloquée ({feedback.block_reason})")
        if not response.candidates:
            raise RuntimeError("Gemini : aucune réponse")
        raison = response.candidates[0].finish_reason
        nom_raison = getattr(raison, "name", str(raison))
        if nom_raison in _GEMINI_REFUS:
            raise Refus(f"Gemini : réponse bloquée ({nom_raison})")
        if nom_raison == "MAX_TOKENS":
            raise RuntimeError("Réponse tronquée (max_tokens)")
        return _json(response.text), response.model_version or self.modele


# ----------------------------------------------------------------- sélection

FOURNISSEURS = {"anthropic": Anthropic, "mistral": Mistral, "gemini": Gemini}
MODELES_PAR_DEFAUT = {
    "anthropic": "claude-opus-5-5",
    "mistral": "mistral-large-latest",
    "gemini": "gemini-3.8-flash",
}


def nom_fournisseur() -> str:
    nom = config.LLM_PROVIDER
    if nom not in FOURNISSEURS:
        raise ErreurConfiguration(
            f"LLM_PROVIDER={nom!r} inconnu (choix : {', '.join(FOURNISSEURS)})"
        )
    return nom


def modele() -> str:
    return config.LLM_MODEL or MODELES_PAR_DEFAUT[nom_fournisseur()]


def identifiant() -> str:
    """Ex. « mistral:mistral-large-latest » : sert à l'empreinte de reclassement."""
    return f"{nom_fournisseur()}:{modele()}"


def cle_disponible() -> bool:
    nom = nom_fournisseur()
    variable = FOURNISSEURS[nom].variable_cle
    if nom == "anthropic":
        return bool(os.environ.get(variable) or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if nom == "gemini":
        return bool(os.environ.get(variable) or os.environ.get("GOOGLE_API_KEY"))
    return bool(os.environ.get(variable))


_instance: Fournisseur | None = None


def fournisseur() -> Fournisseur:
    global _instance
    if _instance is None or _instance.nom != nom_fournisseur() or _instance.modele != modele():
        _instance = FOURNISSEURS[nom_fournisseur()](modele())
    return _instance
