"""Slow graph preparation is separate from UI and paid judgement workers."""
from threading import Event
from types import SimpleNamespace

import pytest

import sp_simulation_jobs as simulation
from sp_review_jobs import ReviewJobs


@pytest.fixture
def setup_graph(monkeypatch, tmp_path):
    pdf, summary = tmp_path / 'sp.pdf', tmp_path / 'summary_after.csv'
    pdf.write_bytes(b'original synthetic PDF')
    summary.write_text('original summary', encoding='utf-8')
    (tmp_path / 'stage.csv').write_text('stage', encoding='utf-8')
    jobs, calls = ReviewJobs(), []
    monkeypatch.setattr(simulation, 'simulation_jobs', jobs)
    monkeypatch.setattr(simulation, 'load_simulation_graph', lambda *a, **k:
        (calls.append('load') or SimpleNamespace(product_name='fixture'), {}))
    monkeypatch.setattr(simulation, 'simulation_graph_to_json', lambda *a: '{"nodes":[{"id":"one"}]}')
    def get():
        return simulation.get_simulation_job(tmp_path, pdf, summary, version='test')
    try:
        yield SimpleNamespace(pdf=pdf, summary=summary, folder=tmp_path, calls=calls, jobs=jobs, get=get)
    finally:
        jobs.close()


def test_slow_graph_returns_immediately_and_reruns_share_the_job(monkeypatch, setup_graph):
    fixture = setup_graph
    started, release = Event(), Event()
    def load(*a, **k):
        fixture.calls.append('load')
        started.set()
        assert release.wait(5)
        return SimpleNamespace(product_name='fixture'), {}
    monkeypatch.setattr(simulation, 'load_simulation_graph', load)
    try:
        job = fixture.get()
        assert started.wait(2) and not job.future.done()
        assert fixture.get() is job
        simulation.forget_failed_simulation(fixture.pdf)
        assert fixture.get() is job
        release.set()
        assert job.future.result(timeout=2)[1] == 'fixture'
        assert fixture.get() is job and fixture.calls == ['load']
    finally:
        release.set()


@pytest.mark.parametrize('change', ['pdf', 'summary', 'stage', 'add_stage', 'remove_stage', 'version', 'path'])
def test_input_changes_get_new_graph_jobs(setup_graph, change):
    fixture = setup_graph
    original = fixture.get()
    original.future.result(timeout=2)
    if change in {'pdf', 'summary'}:
        getattr(fixture, change).write_bytes(b'changed and larger contents')
    elif change == 'stage':
        (fixture.folder / 'stage.csv').write_text('changed stage contents', encoding='utf-8')
    elif change == 'add_stage':
        (fixture.folder / 'another.csv').write_text('another stage', encoding='utf-8')
    elif change == 'remove_stage':
        (fixture.folder / 'stage.csv').unlink()
    if change == 'version':
        updated = simulation.get_simulation_job(fixture.folder, fixture.pdf, fixture.summary, version='new')
    elif change == 'path':
        other = fixture.folder / 'other.pdf'
        other.write_bytes(fixture.pdf.read_bytes())
        updated = simulation.get_simulation_job(fixture.folder, other, fixture.summary, version='test')
    else:
        updated = fixture.get()
    assert updated is not original
    updated.future.result(timeout=2)
    assert fixture.calls == ['load', 'load']


@pytest.mark.parametrize('invalid', ['empty', 'malformed', 'exception'])
def test_failure_stays_visible_until_explicit_retry(monkeypatch, setup_graph, invalid):
    fixture = setup_graph
    if invalid == 'exception':
        def fail(*a, **k):
            fixture.calls.append('load')
            raise RuntimeError('synthetic failure')
        monkeypatch.setattr(simulation, 'load_simulation_graph', fail)
    else:
        monkeypatch.setattr(simulation, 'simulation_graph_to_json', lambda *a:
            '{"nodes":[]}' if invalid == 'empty' else 'broken JSON')
    job = fixture.get()
    with pytest.raises(Exception):
        job.future.result(timeout=2)
    assert fixture.get() is job
    simulation.forget_failed_simulation(fixture.folder / 'different.pdf')
    assert fixture.get() is job
    simulation.forget_failed_simulation(fixture.pdf)
    retry = fixture.get()
    with pytest.raises(Exception):
        retry.future.result(timeout=2)
    assert retry is not job and fixture.calls == ['load', 'load']


@pytest.mark.parametrize('change', ['pdf', 'summary', 'stage'])
def test_changed_during_extraction_is_not_published(monkeypatch, setup_graph, change):
    fixture = setup_graph
    def load(*a, **k):
        path = getattr(fixture, change) if change != 'stage' else fixture.folder / 'stage.csv'
        path.write_text('changed during extraction with new length', encoding='utf-8')
        return SimpleNamespace(product_name='fixture'), {}
    monkeypatch.setattr(simulation, 'load_simulation_graph', load)
    with pytest.raises(simulation.SimulationInputsChanged):
        fixture.get().future.result(timeout=2)


def test_changed_while_queued_is_rejected_before_reading(monkeypatch, setup_graph):
    fixture = setup_graph
    release = Event()
    fixture.jobs.start('blocking', 'other', lambda _: release.wait(5))
    try:
        job = fixture.get()
        fixture.pdf.write_bytes(b'changed while waiting for graph worker')
        release.set()
        with pytest.raises(simulation.SimulationInputsChanged):
            job.future.result(timeout=2)
        assert fixture.calls == []
    finally:
        release.set()


@pytest.mark.parametrize('missing', ['pdf', 'summary'])
def test_missing_required_inputs_are_not_replaced_with_defaults(setup_graph, missing):
    fixture = setup_graph
    getattr(fixture, missing).unlink()
    with pytest.raises(FileNotFoundError):
        fixture.get()
    assert fixture.calls == []
