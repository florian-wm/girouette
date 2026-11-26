"""Fournisseurs LLM : sélection et interprétation des réponses (SDK simulés, aucun appel réseau)."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from app import classify, config, llm

SCHEMA = {"type": "object"}
OK = '{"resume": "r", "objectifs": []}'


def test_selection_et_modele_par_defaut(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "mistral")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    assert llm.identifiant() == "mistral:mistral-large-latest"
    monkeypatch.setattr(config, "LLM_MODEL", "mistral-small-latest")
    assert llm.identifiant() == "mistral:mistral-small-latest"
    monkeypatch.setattr(config, "LLM_PROVIDER", "chatgpt")
    with pytest.raises(llm.ErreurConfiguration):
        llm.identifiant()


def test_cle_disponible(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    assert not llm.cle_disponible()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm.cle_disponible()


def test_changer_de_fournisseur_declenche_un_reclassement(monkeypatch):
    s = {"numero": 1, "date": "2026-01-01", "nature": "ensemble", "titre": "t", "sort": "adopté"}
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    h1 = classify.input_hash(s)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    assert classify.input_hash(s) != h1


def _mistral(finish_reason="stop", content=OK):
    p = llm.Mistral.__new__(llm.Mistral)
    p.modele = "mistral-large-latest"
    p._client = MagicMock()
    p._client.chat.complete.return_value = NS(
        model="mistral-large-2512",
        choices=[NS(finish_reason=finish_reason, message=NS(content=content))],
    )
    return p


def test_mistral():
    p = _mistral()
    data, modele = p.classer("sys", "msg", SCHEMA)
    assert data["resume"] == "r" and modele == "mistral-large-2512"
    kwargs = p._client.chat.complete.call_args.kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["response_format"]["json_schema"]["schema_definition"] == SCHEMA
    assert _mistral(content=[NS(text='{"resume": "x",'), NS(text=' "objectifs": []}')]).classer("s", "m", SCHEMA)[0]["resume"] == "x"
    with pytest.raises(RuntimeError):
        _mistral(finish_reason="length").classer("s", "m", SCHEMA)


def _gemini(finish="STOP", block=None, text=OK):
    p = llm.Gemini.__new__(llm.Gemini)
    p.modele = "gemini-3.8-flash"
    p._client = MagicMock()
    p._client.models.generate_content.return_value = NS(
        prompt_feedback=NS(block_reason=block) if block else None,
        candidates=[NS(finish_reason=NS(name=finish))],
        text=text, model_version="gemini-3.8-flash-001",
    )
    return p


def test_gemini():
    p = _gemini()
    data, modele = p.classer("sys", "msg", SCHEMA)
    assert data["objectifs"] == [] and modele == "gemini-3.8-flash-001"
    cfg = p._client.models.generate_content.call_args.kwargs["config"]
    assert cfg.system_instruction == "sys" and cfg.response_json_schema == SCHEMA
    with pytest.raises(llm.Refus):
        _gemini(finish="SAFETY").classer("s", "m", SCHEMA)
    with pytest.raises(llm.Refus):
        _gemini(block="SAFETY").classer("s", "m", SCHEMA)
    with pytest.raises(RuntimeError):
        _gemini(finish="MAX_TOKENS").classer("s", "m", SCHEMA)


def test_anthropic_refus():
    p = llm.Anthropic.__new__(llm.Anthropic)
    import anthropic

    p.modele, p._sdk, p._client = "claude-opus-5-5", anthropic, MagicMock()
    p._client.beta.messages.create.return_value = NS(stop_reason="refusal", stop_details=None, content=[], model="m")
    with pytest.raises(llm.Refus):
        p.classer("s", "m", SCHEMA)
    p._client.beta.messages.create.return_value = NS(
        stop_reason="end_turn", content=[NS(type="thinking"), NS(type="text", text=OK)], model="claude-opus-5-5")
    assert p.classer("s", "m", SCHEMA) == ({"resume": "r", "objectifs": []}, "claude-opus-5-5")


def test_erreur_de_configuration_arrete_le_lot(monkeypatch):
    from tests.test_classify_scoring import _charger

    _charger()
    appels = []

    def faux(s):
        appels.append(s["uid"])
        raise llm.ErreurConfiguration("clé invalide")

    monkeypatch.setattr(classify, "appeler_llm", faux)
    monkeypatch.setattr(config, "LLM_CONCURRENCY", 1)
    with pytest.raises(llm.ErreurConfiguration):
        classify.classify(limit=10)
    assert len(appels) == 1
