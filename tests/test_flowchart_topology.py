"""Unsupported diagrams must not silently become a plausible partial process."""
from itertools import permutations
from types import SimpleNamespace

import pytest

import sp_flowchart_data as flow
from test_flowchart_provenance import diagram, fake_extractor


@pytest.mark.parametrize('second', [
    diagram(),
    diagram(nodes=[{'node_id': 'c', 'name': 'C'}, {'node_id': 'd', 'name': 'D'}],
            edges=[{'from': 'c', 'to': 'd'}]),
    diagram(nodes=[], edges=[]),
    diagram(edges=[{'from': 'missing', 'to': 'b'}]),
])
def test_later_flowchart_is_not_silently_discarded(monkeypatch, tmp_path, second):
    fake_extractor(monkeypatch, [diagram(), second])
    with pytest.raises(flow.FlowchartExtractionError, match='PDF 제조요약도'):
        flow.extract_pdf_flowchart(tmp_path / 'sp.pdf')


@pytest.mark.parametrize('data', [None, {}, {'nodes': [], 'edges': None},
    {'nodes': [], 'edges': {}}, {'nodes': {}, 'edges': []}, {'nodes': [], 'edges': ''},
    {'nodes': False, 'edges': []}, {'nodes': [], 'edges': 0}, {'nodes': []}])
def test_missing_or_wrong_typed_diagram_is_not_an_absent_diagram(monkeypatch, tmp_path, data):
    fake_extractor(monkeypatch, [SimpleNamespace(record_type='flowchart', diagram_data=data)])
    with pytest.raises(flow.FlowchartExtractionError, match='PDF 제조요약도'):
        flow.extract_pdf_flowchart(tmp_path / 'sp.pdf')


@pytest.mark.parametrize('pairs', [
    [('A', 'A')], [('A', 'B'), ('B', 'A')],
    [('A', 'B'), ('C', 'D'), ('D', 'C')],  # a cycle in a different component
    [('A', 'B'), ('B', 'C'), ('C', 'B')],
    [('A', 'B'), ('A', 'B')], [('A', 'missing')],
])
def test_builder_rejects_cycles_duplicate_or_dangling_edges(pairs):
    with pytest.raises(flow.FlowchartExtractionError, match='PDF 제조요약도'):
        flow.build_graph_from_flowchart('fixture', ['A', 'B', 'C', 'D'], pairs, {})


@pytest.mark.parametrize('pairs', [
    [('a', 'a')], [('a', 'b'), ('b', 'a')], [('a', 'b'), ('a', 'b')],
])
def test_extractor_rejects_invalid_topology_before_it_can_fallback(monkeypatch, tmp_path, pairs):
    fake_extractor(monkeypatch, [diagram(edges=[{'from': a, 'to': b} for a, b in pairs])])
    with pytest.raises(flow.FlowchartExtractionError, match='PDF 제조요약도'):
        flow.extract_pdf_flowchart(tmp_path / 'sp.pdf')


def test_normalized_names_are_ambiguous_at_extraction_boundary(monkeypatch, tmp_path):
    fake_extractor(monkeypatch, [diagram(nodes=[{'node_id': 'a', 'name': 'Stage A'},
        {'node_id': 'b', 'name': 'stage_a'}])])
    with pytest.raises(flow.FlowchartExtractionError, match='PDF 제조요약도'):
        flow.extract_pdf_flowchart(tmp_path / 'sp.pdf')


@pytest.mark.parametrize('endpoint', [True, 1.0])
def test_edge_ids_cannot_alias_integer_nodes_with_boolean_or_float(monkeypatch, tmp_path, endpoint):
    fake_extractor(monkeypatch, [diagram(nodes=[{'node_id': 1, 'name': 'A'}, {'node_id': 2, 'name': 'B'}],
        edges=[{'from': endpoint, 'to': 2}])])
    with pytest.raises(flow.FlowchartExtractionError):
        flow.extract_pdf_flowchart(tmp_path / 'sp.pdf')


def test_longer_merge_path_moves_all_descendants_forward_independent_of_record_order():
    names = ['A', 'B', 'C', 'D', 'E']
    pairs = [('A', 'D'), ('A', 'B'), ('B', 'C'), ('C', 'D'), ('D', 'E')]
    for order in permutations(names):
        graph = flow.build_graph_from_flowchart('fixture', list(order), pairs, {})
        layers = {n.label: n.layer for n in graph.nodes}
        assert layers == {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
        assert all(layers[a] < layers[b] for a, b in pairs)


def test_disconnected_acyclic_processes_remain_valid_without_invented_arrows():
    graph = flow.build_graph_from_flowchart('fixture', ['A', 'B', 'C', 'D', 'E'],
                                           [('A', 'B'), ('C', 'D')], {})
    assert {n.label: n.layer for n in graph.nodes} == {'A': 0, 'B': 1, 'C': 0, 'D': 1, 'E': 0}
    assert len(graph.edges) == 2


@pytest.mark.parametrize('case', ['multiple', 'cycle'])
def test_loader_propagates_unsupported_diagram_even_with_usable_csv(monkeypatch, tmp_path, case):
    (tmp_path / 'summary_after.csv').write_text(
        '제조명,검수합격,검수불합격,검수보류,총 계\nA,1,0,0,1\nB,1,0,0,1\n', encoding='utf-8')
    records = [diagram(), diagram()] if case == 'multiple' else [diagram(edges=[{'from':'a','to':'a'}])]
    fake_extractor(monkeypatch, records)
    with pytest.raises(flow.FlowchartExtractionError):
        flow.load_simulation_graph(tmp_path, tmp_path / 'sp.pdf')


@pytest.mark.parametrize('case,reason', [('multiple', '여러 개'), ('cycle', '순환·재작업')])
def test_real_graph_job_and_streamlit_show_failure_without_simulation(monkeypatch, tmp_path, case, reason):
    from streamlit.testing.v1 import AppTest
    import sp_app as app
    import sp_simulation_jobs as simulation
    from sp_review_jobs import ReviewJobs

    pdf, summary = tmp_path / 'sp.pdf', tmp_path / 'summary_after.csv'
    pdf.write_bytes(b'synthetic input: extractor records are isolated below')
    summary.write_text('제조명,검수합격,검수불합격,검수보류,총 계\nA,1,0,0,1\nB,1,0,0,1\n', encoding='utf-8')
    fake_extractor(monkeypatch, [diagram(), diagram()] if case == 'multiple'
                   else [diagram(edges=[{'from': 'a', 'to': 'a'}])])
    jobs = ReviewJobs()
    monkeypatch.setattr(simulation, 'simulation_jobs', jobs)
    monkeypatch.setattr(app, '_find_simulation_csv_dir', lambda _: tmp_path)
    monkeypatch.setattr(app, '_document_from_pdf', lambda _: SimpleNamespace(permit_files=[]))
    monkeypatch.setattr(app, '_cached_simulation_artifacts_for_doc', lambda *a, **k: {'summary_after_path': summary})
    monkeypatch.setattr(app, '_load_judgement_bridge', lambda: {})
    renders = []
    monkeypatch.setattr(app.components, 'html', lambda *a, **k: renders.append(a))
    try:
        job = simulation.get_simulation_job(tmp_path, pdf, summary, version=app.APP_CACHE_VERSION)
        with pytest.raises(flow.FlowchartExtractionError):
            job.future.result(timeout=2)
        page = AppTest.from_string(f'from pathlib import Path\nimport sp_app as app\n'
            f'app._render_inline_simulation("fixture",Path({str(pdf)!r}))').run()
        for _ in range(2):
            assert not page.exception and len(page.error) == 1
            assert reason in page.error[0].value
            assert not renders and not page.warning
            assert simulation.get_simulation_job(tmp_path, pdf, summary, version=app.APP_CACHE_VERSION) is job
            page.run()
    finally:
        jobs.close()
