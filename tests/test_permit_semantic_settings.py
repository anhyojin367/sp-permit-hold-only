from dataclasses import FrozenInstanceError, asdict
import json

import pytest

from sp_pdf_judger.permit_semantic_settings import (
    SemanticSettings, read_semantic_settings, semantic_fingerprint, model_budget,
)


def test_default_is_legacy():
    assert read_semantic_settings({}).mode == 'legacy'


def test_shared_evidence_is_explicit():
    settings = read_semantic_settings({'SP_PERMIT_SEMANTIC_MODE': 'shared_evidence'})
    assert settings == SemanticSettings('shared_evidence', 4, 60000)
    with pytest.raises(FrozenInstanceError):
        settings.mode = 'legacy'


def test_unknown_mode_fails():
    with pytest.raises(ValueError, match='SP_PERMIT_SEMANTIC_MODE'):
        read_semantic_settings({'SP_PERMIT_SEMANTIC_MODE': 'typo'})


def test_settings_have_no_credentials():
    settings = read_semantic_settings({'CLOVA_API_KEY': 'private-key', 'SP_PERMIT_SEMANTIC_MODE': 'legacy'})
    assert 'private-key' not in json.dumps(asdict(settings)) + semantic_fingerprint(settings)
    assert semantic_fingerprint(settings) == semantic_fingerprint(read_semantic_settings({}))
    assert semantic_fingerprint(settings) != semantic_fingerprint(SemanticSettings('shared_evidence'))


def test_unknown_model_has_no_batch_budget():
    assert model_budget('unknown-model', 4096) is None
    assert model_budget('HCX-007', 4096).output_tokens == 4096
    assert model_budget('HCX-007', 40000).output_tokens == 32768
    assert model_budget('HCX-007', 4096).context_tokens == 128000
    assert model_budget('HCX-007', 4096).estimate_kind == 'utf8-byte-estimate'


@pytest.mark.parametrize('tokens', [0, -1, True, '4096'])
def test_invalid_output_budget_is_rejected(tokens):
    with pytest.raises(ValueError):
        model_budget('HCX-007', tokens)


@pytest.mark.parametrize('args', [('typo', 4, 60000), ('legacy', 0, 60000), ('legacy', 5, 60000), ('legacy', 4, 0)])
def test_invalid_settings_are_rejected(args):
    with pytest.raises(ValueError):
        SemanticSettings(*args)
