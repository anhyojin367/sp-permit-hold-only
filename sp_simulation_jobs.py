"""Process-local graph preparation, separate from Streamlit and CLOVA jobs.

Only execution placement changes: the existing PDF/CSV graph loader is retained.
The input signature uses size/mtime, as before; it is not a content-hash guarantee.
"""
import hashlib
import json
from pathlib import Path

from sp_flowchart_data import load_simulation_graph, simulation_graph_to_json
from sp_review_jobs import ReviewJobs

simulation_jobs = ReviewJobs()


class SimulationInputsChanged(RuntimeError):
    pass


def simulation_input_signature(csv_dir: Path, pdf_path: Path | None, explicit_summary_path: Path | None = None) -> tuple:
    """Invalidate when the PDF, summaries, or any stage CSV changes."""
    csv_dir = Path(csv_dir)
    parts = []
    for path in (pdf_path, explicit_summary_path, csv_dir.parent / 'summary_after.csv',
                 csv_dir.parent / 'summary.csv', csv_dir / 'summary_after.csv', csv_dir / 'summary.csv'):
        try:
            stat = Path(path).stat() if path else None
            parts.append((str(Path(path)) if path else '', int(stat.st_size) if stat else 0,
                          int(stat.st_mtime_ns) if stat else 0))
        except OSError:
            parts.append((str(path or ''), 0, 0))
    # Membership changes matter too, including addition/removal of a stage CSV.
    for path in sorted(csv_dir.glob('*.csv'), key=lambda p: p.name):
        stat = path.stat()
        parts.append((path.name, int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(parts)


def get_simulation_job(csv_dir: Path, pdf_path: Path, summary_after_path: Path, *, version: str):
    csv_dir, pdf_path, summary_after_path = (Path(p).resolve() for p in (csv_dir, pdf_path, summary_after_path))
    if not csv_dir.is_dir() or not pdf_path.is_file() or not summary_after_path.is_file():
        raise FileNotFoundError('현재 문서와 판정 요약 파일을 확인할 수 없습니다.')
    signature = simulation_input_signature(csv_dir, pdf_path, summary_after_path)
    encoded = json.dumps([version, str(csv_dir), str(pdf_path), str(summary_after_path), signature],
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    key = hashlib.sha256(encoded).hexdigest()

    def worker(progress):
        def verify_inputs():
            if signature != simulation_input_signature(csv_dir, pdf_path, summary_after_path):
                raise SimulationInputsChanged('제조요약도를 준비하는 중 입력 파일이 변경됐습니다. 목록에서 검수 진행을 다시 눌러 주세요.')
        verify_inputs()
        progress('PDF 제조요약도와 판정 요약을 읽고 있습니다')
        graph, summary = load_simulation_graph(csv_dir, pdf_path, explicit_summary_path=summary_after_path)
        graph_json = simulation_graph_to_json(graph, summary)
        payload = json.loads(graph_json)
        if not isinstance(payload, dict) or not isinstance(payload.get('nodes'), list) or not payload['nodes']:
            raise ValueError('제조요약도 노드가 비어 있거나 올바르지 않습니다.')
        verify_inputs()
        progress('제조요약도 준비 완료')
        return graph_json, graph.product_name

    return simulation_jobs.start(key, str(pdf_path), worker)


def forget_failed_simulation(pdf_path: Path) -> None:
    """Existing review button retries failed graphs; pending/healthy jobs survive."""
    simulation_jobs.forget_failed(str(Path(pdf_path).resolve()), lambda _: False)
