"""Explicit settings for the experimental authoritative semantic path."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from typing import Literal, Mapping
from .config import CLOVA_MAX_COMPLETION_TOKENS

RETRIEVAL_VERSION = 'permit-precise-v1'
BUNDLE_VERSION = 'permit-evidence-bundle-v1'
SEMANTIC_BATCH_VERSION = 'authoritative-semantic-batch-v2'


@dataclass(frozen=True)
class SemanticSettings:
    mode: Literal['legacy', 'shared_evidence'] = 'legacy'
    max_records: int = 4
    max_prompt_chars: int = 60000
    max_completion_tokens: int = CLOVA_MAX_COMPLETION_TOKENS

    def __post_init__(self):
        if self.mode not in {'legacy', 'shared_evidence'}:
            raise ValueError('SP_PERMIT_SEMANTIC_MODE must be legacy or shared_evidence')
        if type(self.max_records) is not int or not 1 <= self.max_records <= 4:
            raise ValueError('semantic max_records must be between 1 and 4')
        if type(self.max_prompt_chars) is not int or not 1 <= self.max_prompt_chars <= 60000:
            raise ValueError('semantic max_prompt_chars must be between 1 and 60000')
        if type(self.max_completion_tokens) is not int or self.max_completion_tokens <= 0:
            raise ValueError('semantic max_completion_tokens must be a positive integer')


@dataclass(frozen=True)
class ModelBudget:
    context_tokens: int
    output_tokens: int
    estimate_kind: str = 'utf8-byte-estimate'


def read_semantic_settings(environ: Mapping[str, str] | None = None) -> SemanticSettings:
    values = os.environ if environ is None else environ
    mode = values.get('SP_PERMIT_SEMANTIC_MODE', 'legacy').strip()
    if mode == 'legacy':
        return SemanticSettings()
    return SemanticSettings(mode, max_completion_tokens=int(values.get('CLOVA_MAX_COMPLETION_TOKENS', CLOVA_MAX_COMPLETION_TOKENS)))


def semantic_fingerprint(settings: SemanticSettings) -> str:
    payload = [asdict(settings), RETRIEVAL_VERSION, BUNDLE_VERSION, SEMANTIC_BATCH_VERSION]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def model_budget(model: str, max_completion_tokens: int) -> ModelBudget | None:
    if type(max_completion_tokens) is not int or max_completion_tokens <= 0:
        raise ValueError('max_completion_tokens must be a positive integer')
    # https://guide.ncloud-docs.com/docs/en/clovastudio-info (checked 2026-10-13).
    # UTF-8 byte counts used by the scheduler are not actual tokenizer usage.
    if model != 'HCX-007':
        return None
    return ModelBudget(128000, min(max_completion_tokens, 32768))


def semantic_cache_fingerprint(settings: SemanticSettings) -> str:
    """Keep existing legacy cache identities byte-for-byte compatible."""
    return '' if settings.mode == 'legacy' else semantic_fingerprint(settings)
