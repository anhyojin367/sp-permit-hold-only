"""Review scope is separate from evidence-sharing/transport settings."""
import os

HOLD_ONLY_VERSION = 'hold-only-v1-20261014'


def validate_review_scope(value):
    if value not in {'all', 'hold_only'}:
        raise ValueError('Unknown permit review scope: expected all or hold_only')
    return value


def current_permit_review_scope():
    # The standalone app bootstrap always selects hold_only. Library callers
    # retain the original all-items contract for explicit regression comparisons.
    return validate_review_scope(os.environ.get('SP_PERMIT_REVIEW_SCOPE', 'all'))
