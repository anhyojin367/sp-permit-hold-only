"""Keep document checks out of manufacturing arrows without losing verdicts."""
import csv
import json

import pytest

import sp_flowchart_data as flow


def inputs(tmp_path, rows):
    summary = tmp_path / 'summary_after.csv'
    with summary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['제조명', '검수합격', '검수불합격', '검수보류', '총 계'])
        writer.writerows(rows)
        writer.writerow(['전체', *[sum(row[i] for row in rows) for i in range(1, 5)]])
    return summary


def load(monkeypatch, tmp_path, rows, diagram):
    summary = inputs(tmp_path, rows)
    monkeypatch.setattr(flow, 'extract_pdf_flowchart', lambda _: diagram)
    return flow.load_simulation_graph(tmp_path, tmp_path / 'sp.pdf', summary)


def assert_conserved(graph, counts):
    payload = json.loads(flow.simulation_graph_to_json(graph, counts))
    for field in ('pass', 'fail', 'hold', 'total'):
        expected = sum(row[field] for row in counts.values())
        assert sum(n[field + '_count'] for n in payload['nodes']) == expected
        assert sum(n['after_' + field] for n in payload['nodes']) == expected
    flattened = [node for layer in graph.layers for node in layer]
    assert sorted(flattened) == sorted(n.id for n in graph.nodes)
    return payload


@pytest.mark.parametrize('name', ['문서 공통·기타 검증', '문서공통', '단계 미분류 시험'])
@pytest.mark.parametrize('pdf', [True, False])
def test_supplemental_checks_never_create_manufacturing_arrows(monkeypatch, tmp_path, name, pdf):
    rows = [['A', 2, 0, 0, 2], ['B', 0, 1, 1, 2], [name, 3, 2, 1, 6]]
    graph, counts = load(monkeypatch, tmp_path, rows, (['A', 'B'], [('A', 'B')]) if pdf else None)
    supplement = next(n for n in graph.nodes if n.label == name)
    assert supplement.kind == ('unassigned_check' if name == '단계 미분류 시험' else 'document_check')
    assert all(supplement.id not in (e.source, e.target) for e in graph.edges)
    assert graph.structure_provenance['source'] == ('pdf_extracted' if pdf else 'csv_inferred')
    assert len(graph.edges) == 1
    assert supplement.id in graph.layers[-1]
    assert_conserved(graph, counts)


def test_pdf_arrows_win_even_when_inferred_csv_has_more_edges(monkeypatch, tmp_path):
    names = ['CHO 마스터 세포주', 'E.coli 마스터 세포주', '제조용 세포주', '완제의약품']
    pairs = [(names[0], names[2]), (names[1], names[3])]
    graph, counts = load(monkeypatch, tmp_path, [[n, 1, 0, 0, 1] for n in names], (names, pairs))
    labels = {n.id: n.label for n in graph.nodes}
    assert [(labels[e.source], labels[e.target]) for e in graph.edges] == pairs
    assert graph.structure_provenance['source'] == 'pdf_extracted'
    assert_conserved(graph, counts)


def test_partial_pdf_with_branches_cannot_discard_a_csv_stage(monkeypatch, tmp_path):
    rows = [[n, 1, 0, 0, 1] for n in ['A', 'B', 'C', 'D']]
    graph, counts = load(monkeypatch, tmp_path, rows, (['A', 'B', 'C'], [('A', 'B'), ('A', 'C')]))
    assert graph.structure_provenance == {'source': 'csv_inferred', 'reason': 'pdf_not_selected'}
    assert {n.label for n in graph.nodes} == {'A', 'B', 'C', 'D'}
    assert_conserved(graph, counts)


def test_unrecognized_common_name_remains_a_process_not_a_silent_exception(monkeypatch, tmp_path):
    graph, counts = load(monkeypatch, tmp_path, [['A', 1, 0, 0, 1], ['문서검증공정', 1, 0, 0, 1]],
                         (['A', 'B'], [('A', 'B')]))
    assert graph.structure_provenance['source'] == 'csv_inferred'
    assert all(n.kind == 'process' for n in graph.nodes)
    assert_conserved(graph, counts)


def test_pdf_extra_process_is_zero_and_csv_details_survive(monkeypatch, tmp_path):
    (tmp_path / 'A.csv').write_text('시험명,시험 방법,시험 기준,시험 기간,시험 결과,검수 결과\n시험a,,,,,검수합격\n', encoding='utf-8')
    graph, counts = load(monkeypatch, tmp_path, [['A', 1, 0, 0, 1], ['B', 0, 1, 0, 1]],
                         (['A', 'B', 'C'], [('A', 'B'), ('B', 'C')]))
    assert next(n for n in graph.nodes if n.label == 'A').tests[0].name == '시험a'
    assert next(n for n in graph.nodes if n.label == 'C').total_count == 0
    assert_conserved(graph, counts)


def test_normalized_stage_spelling_maps_once(monkeypatch, tmp_path):
    graph, counts = load(monkeypatch, tmp_path, [['Stage A', 2, 0, 0, 2], ['B', 0, 0, 1, 1]],
                         (['stage_a', 'B'], [('stage_a', 'B')]))
    assert graph.structure_provenance['source'] == 'pdf_extracted'
    assert_conserved(graph, counts)


@pytest.mark.parametrize('names', [['A', 'A'], ['Stage A', 'stage_a']])
def test_duplicate_summary_identity_is_rejected(monkeypatch, tmp_path, names):
    with pytest.raises(ValueError, match='중복'):
        load(monkeypatch, tmp_path, [[n, 1, 0, 0, 1] for n in names], None)


def test_normalized_pdf_collision_cannot_double_a_verdict_count(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match='중복'):
        load(monkeypatch, tmp_path, [['Stage A', 1, 0, 0, 1]],
             (['Stage A', 'stage_a'], [('Stage A', 'stage_a')]))


@pytest.mark.parametrize('nodes,summary', [
    ([flow.StageNode('a', 'A')], {'A': {'pass': 1, 'fail': 0, 'hold': 0, 'total': 1},
                               'B': {'pass': 0, 'fail': 1, 'hold': 0, 'total': 1}}),
    ([flow.StageNode('a', 'Stage A'), flow.StageNode('b', 'stage_a')],
     {'Stage A': {'pass': 1, 'fail': 0, 'hold': 0, 'total': 1}}),
])
def test_serialization_does_not_silently_drop_or_double_counts(nodes, summary):
    with pytest.raises(ValueError, match='판정 집계'):
        flow.simulation_graph_to_json(flow.ManufacturingGraph('fixture', nodes=nodes), summary)


def test_only_document_checks_have_no_arrows_but_keep_counts(monkeypatch, tmp_path):
    graph, counts = load(monkeypatch, tmp_path,
                         [['문서 공통·기타 검증', 2, 1, 0, 3], ['단계 미분류 시험', 0, 0, 1, 1]], None)
    assert not graph.edges
    assert len(graph.nodes) == 2
    assert_conserved(graph, counts)


def test_csv_only_without_summary_transfers_counts_to_pdf(monkeypatch, tmp_path):
    for label in ['A', 'B']:
        (tmp_path / f'{label}.csv').write_text('시험명,검수 결과\na,검수합격\n', encoding='utf-8')
    monkeypatch.setattr(flow, 'extract_pdf_flowchart', lambda _: (['A', 'B'], [('A', 'B')]))
    graph, counts = flow.load_simulation_graph(tmp_path, tmp_path / 'sp.pdf')
    assert counts == {}
    assert [n.pass_count for n in graph.nodes] == [1, 1]
    assert [len(n.tests) for n in graph.nodes] == [1, 1]


def test_supplemental_ids_cannot_collide_with_pdf_ids(monkeypatch, tmp_path):
    graph, counts = load(monkeypatch, tmp_path, [['문서공통', 1, 0, 0, 1]],
                         (['문서공통!', 'B'], [('문서공통!', 'B')]))
    assert len({n.id for n in graph.nodes}) == 3
    assert_conserved(graph, counts)


@pytest.mark.parametrize('values', ['bad,0,0,1', '-1,0,0,0', '1.5,0,0,1', ',0,0,1', '1,0,0', '1,1,0,1'])
def test_invalid_summary_numbers_cannot_be_silently_zeroed(tmp_path, values):
    summary = tmp_path / 'summary_after.csv'
    summary.write_text('제조명,검수합격,검수불합격,검수보류,총 계\nA,' + values + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='판정 집계'):
        flow.load_simulation_graph(tmp_path, explicit_summary_path=summary)
