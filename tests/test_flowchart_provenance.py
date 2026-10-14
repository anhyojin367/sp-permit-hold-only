"""Do not disguise failed extraction or inferred edges as source PDF arrows."""
import json
import sys
from types import SimpleNamespace

import pytest

import sp_flowchart_data as flow


def fake_extractor(monkeypatch, records=None, fail_at=None):
    def component(name, result):
        def run(*a):
            if fail_at == name:
                raise OSError('synthetic reader error')
            return result
        return lambda *a: SimpleNamespace(read=run, run=run)
    monkeypatch.setitem(sys.modules, 'extractor_codex_0517', SimpleNamespace(**{
        name: component(name, records if name == 'RecordExtractor' else [])
        for name in ('PDFReader','Normalizer','SectionBuilder','BlockBuilder','RecordExtractor')}))


def diagram(nodes=None, edges=None):
    return SimpleNamespace(record_type='flowchart', diagram_data={
        'nodes': nodes if nodes is not None else [
            {'node_id':'a','name':'A'}, {'node_id':'b','name':'B'}],
        'edges': edges if edges is not None else [{'from':'a','to':'b'}]})


@pytest.mark.parametrize('stage', ['PDFReader','Normalizer','SectionBuilder','BlockBuilder','RecordExtractor'])
def test_extraction_exception_is_not_converted_to_absence(monkeypatch, tmp_path, stage):
    fake_extractor(monkeypatch, fail_at=stage)
    with pytest.raises(RuntimeError, match='PDF 제조요약도') as error:
        flow.extract_pdf_flowchart(tmp_path/'sp.pdf')
    assert isinstance(error.value.__cause__, OSError)


@pytest.mark.parametrize('records,expected', [([],None), ([diagram(nodes=[],edges=[])],None),
    ([diagram()],(['A','B'],[('A','B')])),
    ([diagram(edges=[])],(['A','B'],[]))])
def test_absent_and_valid_diagrams_remain_distinct(monkeypatch,tmp_path,records,expected):
    fake_extractor(monkeypatch, records)
    assert flow.extract_pdf_flowchart(tmp_path/'sp.pdf') == expected


@pytest.mark.parametrize('value', [
    diagram(nodes=[{'node_id':'a','name':'A'},{'node_id':'a','name':'B'}]),
    diagram(nodes=[{'node_id':'a','name':'A'},{'node_id':'b','name':'A'}]),
    diagram(nodes=[{'name':'A'},{'node_id':'b','name':'B'}]),
    diagram(nodes=[{'node_id':'a','name':''},{'node_id':'b','name':'B'}]),
    diagram(edges=[{'from':'missing','to':'b'}]),
    diagram(nodes=['not a node']),
])
def test_ambiguous_nodes_and_dangling_arrows_are_not_silently_dropped(monkeypatch,tmp_path,value):
    fake_extractor(monkeypatch,[value])
    with pytest.raises(RuntimeError, match='PDF 제조요약도'):
        flow.extract_pdf_flowchart(tmp_path/'sp.pdf')


@pytest.fixture
def csv_inputs(tmp_path):
    path=tmp_path/'summary_after.csv'
    path.write_text('제조명,검수합격,검수불합격,검수보류,총 계\nA,1,0,0,1\nB,0,1,1,2\n전체,1,1,1,3\n',encoding='utf-8')
    return tmp_path,path


@pytest.mark.parametrize('pdf_present,extracted,source,reason', [
    (False,None,'csv_inferred','no_pdf'),
    (True,None,'csv_inferred','pdf_not_found'),
    (True,(['A','B'],[]),'csv_inferred','pdf_without_arrows'),
    (True,(['A','C'],[('A','C')]),'csv_inferred','pdf_not_selected'),
    (True,(['A','B'],[('A','B')]),'pdf_extracted','selected_pdf'),
])
def test_provenance_survives_serialization_without_changing_counts(monkeypatch,csv_inputs,pdf_present,extracted,source,reason):
    folder,summary=csv_inputs
    monkeypatch.setattr(flow,'extract_pdf_flowchart',lambda _: extracted)
    graph,counts=flow.load_simulation_graph(folder,folder/'sp.pdf' if pdf_present else None,summary)
    expected={'source':source,'reason':reason}
    for encode in (lambda:flow.graph_to_json(graph),lambda:flow.simulation_graph_to_json(graph,counts)):
        assert json.loads(encode())['structure_provenance'] == expected
    payload=json.loads(flow.simulation_graph_to_json(graph,counts))
    assert [[n['pass_count'],n['fail_count'],n['hold_count']] for n in payload['nodes']] == [[1,0,0],[0,1,1]]


def test_loader_does_not_hide_reader_failure_behind_existing_csv(monkeypatch,csv_inputs):
    folder,summary=csv_inputs
    fake_extractor(monkeypatch,fail_at='PDFReader')
    with pytest.raises(RuntimeError,match='PDF 제조요약도'):
        flow.load_simulation_graph(folder,folder/'sp.pdf',summary)


@pytest.mark.parametrize('evidence,expected', [
    ({'source':'csv_inferred','reason':reason},'추정한 연결선')
    for reason in ('no_pdf','pdf_not_found','pdf_without_arrows','pdf_not_selected')
] + [({'source':'pdf_extracted','reason':'selected_pdf'},None),
     ({},'출처를 확인할 수 없습니다'),
     ({'source':'invented','reason':'selected_pdf'},'출처를 확인할 수 없습니다')])
def test_notice_distinguishes_inference_from_extraction(evidence,expected):
    notice=flow.simulation_structure_notice(json.dumps({'structure_provenance':evidence}))
    if expected is None:
        assert notice is None
    else:
        assert expected in notice


@pytest.mark.parametrize('state', ['inferred','extracted','unknown','failure'])
def test_actual_app_panel_shows_warning_or_error_without_changing_verdicts(monkeypatch,tmp_path,state):
    from concurrent.futures import Future
    from streamlit.testing.v1 import AppTest
    import sp_app as app
    pdf,summary=tmp_path/'sp.pdf',tmp_path/'summary_after.csv'
    pdf.write_bytes(b'not parsed')
    summary.write_text('not parsed',encoding='utf-8')
    future=Future()
    if state == 'failure':
        future.set_exception(flow.FlowchartExtractionError('PDF 제조요약도 추출 실패'))
    else:
        graph=flow.ManufacturingGraph('fixture',nodes=[flow.StageNode('a','A',pass_count=1,total_count=1)])
        if state == 'extracted':
            graph.structure_provenance={'source':'pdf_extracted','reason':'selected_pdf'}
        elif state == 'unknown':
            graph.structure_provenance={}
        future.set_result((flow.simulation_graph_to_json(graph,{}),'fixture'))
    monkeypatch.setattr(app,'_find_simulation_csv_dir',lambda _:tmp_path)
    monkeypatch.setattr(app,'_document_from_pdf',lambda _:SimpleNamespace(permit_files=[]))
    monkeypatch.setattr(app,'_load_judgement_bridge',lambda: {})
    monkeypatch.setattr(app,'_cached_simulation_artifacts_for_doc',lambda *a,**k:{'summary_after_path':summary})
    monkeypatch.setattr(app,'get_simulation_job',lambda *a,**k:SimpleNamespace(future=future))
    renders=[]
    monkeypatch.setattr(app.components,'html',lambda *a,**k:renders.append(a[0]))
    page=AppTest.from_string(f'from pathlib import Path\nimport sp_app as app\napp._render_inline_simulation("fixture",Path({str(pdf)!r}))').run()
    assert not page.exception
    assert len(page.warning) == (state in {'inferred','unknown'})
    assert len(page.error) == (state == 'failure')
    assert len(renders) == (state != 'failure')
    if state == 'inferred':
        assert '추정한 연결선' in page.warning[0].value
    if state == 'failure':
        assert 'PDF 제조요약도' in page.error[0].value
