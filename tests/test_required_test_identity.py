"""A required test identity must exist in one actual test, not concatenated names."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.policy_engine import Rule, required_tests


@pytest.mark.parametrize("rows,expected", [
    ([{"test_name": "외래성인자"}, {"test_name": "부정시험(in vitro)"}], "FAIL"),
    ([{"test_name": "외래성인자", "parent_test_group": "부정시험(in vitro)"}], "FAIL"),
    ([{"record_type": "heading", "test_name": "외래성인자부정시험(in vitro)"}], "FAIL"),
    ([{"test_name": "외래성인자 부정시험 (in vitro)"}], "PASS"),
    ([{"test_name": "세포접종시험", "parent_test_group": "외래성인자부정시험(in vitro)"}], "PASS"),
])
def test_required_names_do_not_cross_record_or_field_boundaries(rows, expected):
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    ctx = SimpleNamespace(pdf_path=Path("synthetic.pdf"), select=lambda selector: records,
                          evidence=lambda r: {"page": 1, "quote": r.test_name})
    rule = Rule(id="T", title="필수시험", instruction="누락 확인", operation="required_tests",
                params={"tests": ["외래성인자부정시험(in vitro)"]})
    result = required_tests(rule, ctx)
    assert result.status == expected
