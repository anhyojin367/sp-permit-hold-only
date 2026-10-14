"""Keep library regressions independent of app imports during collection."""
import pytest


@pytest.fixture(autouse=True)
def isolated_review_scope(monkeypatch, tmp_path):
    # Some legacy UI tests import sp_app at collection time, which correctly
    # boots the standalone HOLD-only app. Library tests retain their original
    # all-items contract; HOLD-only tests select their scope explicitly, and
    # relocated subprocess/runtime tests exercise the real forced bootstrap.
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'all')
    # App collection also selects its persistent runtime roots. A protocol test
    # must exercise its fake provider, not replay an earlier suite's response.
    # Individual cache tests may still override these paths and test warm reuse.
    monkeypatch.setenv('CLOVA_RESPONSE_CACHE_DIR', str(tmp_path / 'responses'))
    monkeypatch.setenv('SP_PERMIT_INDEX_DIR', str(tmp_path / 'permit-index'))
