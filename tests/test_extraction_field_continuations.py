"""A numeric sublabel inside a result must not disappear at a colon."""
import importlib


def test_colon_labelled_result_keeps_every_measurement_and_next_test_separate():
    m = importlib.import_module("json추출")
    items = [m.Item(0, "line", 1, "주사제의 불용성미립자시험"),
             m.Item(1, "kv", 1, meta={"key": "시험기준", "value": "용기당 10 μm 이상: 6,000개 이하"}),
             m.Item(2, "kv", 1, meta={"key": "용기당 25 μm 이상", "value": "600개 이하"}),
             m.Item(3, "kv", 1, meta={"key": "시험결과", "value": "용기당 10 μm 이상: 5,000개,"}),
             m.Item(4, "kv", 1, meta={"key": "용기당 25 μm 이상", "value": "455개"}),
             m.Item(5, "line", 1, "엔도톡신시험"),
             m.Item(6, "kv", 1, meta={"key": "시험결과", "value": "95 EU/dose"})]
    rows = m.RecordExtractor()._extract_block(m.Block("5.2", "항원바이알 시험", 1, 1, items))
    tests = [row for row in rows if row.record_type == "test"]
    assert len(tests) == 2
    assert "455개" in tests[0].result
    assert "600개 이하" in tests[0].criteria
    assert "EU/dose" not in tests[0].result
    assert tests[1].result == "95 EU/dose"


def test_result_continuation_does_not_absorb_footnote_or_new_known_label():
    m = importlib.import_module("json추출")
    items = [m.Item(0, "line", 1, "확인시험"),
             m.Item(1, "kv", 1, meta={"key": "시험결과", "value": "A: 적합"}),
             m.Item(2, "kv", 1, meta={"key": "*참고", "value": "참고자료"}),
             m.Item(3, "kv", 1, meta={"key": "시험기간", "value": "2026.01.01"})]
    rows = m.RecordExtractor()._extract_block(m.Block("3.1", "시험", 1, 1, items))
    test = next(row for row in rows if row.record_type == "test")
    assert test.result == "A: 적합"
    assert test.test_period == "2026.01.01"
