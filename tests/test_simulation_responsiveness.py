from pathlib import Path

from sp_sim2_viewer import build_simulation_html


def test_animation_is_bounded_and_cdn_failure_can_show_real_counts():
    html = build_simulation_html('{"nodes":[],"edges":[]}')
    assert '<script async src=' in html
    assert "const MAX_PRESENTATION_SECONDS=12" in html
    assert "tl.timeScale(Math.max(1,tl.duration()/MAX_PRESENTATION_SECONDS))" in html
    assert "typeof gsap==='undefined'" in html
    assert "setTimeout(finishPresentation" in html
    assert "counts[k]" in html and "revealFinalJudgementLink();" in html


def test_final_link_is_ready_with_actual_artifact_not_an_animation_timer():
    app = (Path(__file__).resolve().parents[1] / 'sp_app.py').read_text(encoding='utf-8')
    assert 'class="final-judge-after-sim is-visible"' in app
    assert 'if _sim_pdf and finish_delay > 0:' in app
    assert 'if not job.future.done():' in app
    assert '_render_pending_review(job)' in app
    assert '@st.fragment(run_every=1.0)' in app
