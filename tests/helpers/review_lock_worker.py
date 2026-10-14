"""Isolated subprocess fixture: never parse PDFs or call a provider."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def wait_for(path):
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(.02)


def main(folder, name, key, mode):
    import sp_judgement_bridge as bridge
    (folder / ("pid_" + name)).write_text(str(os.getpid()))
    # Every filesystem write and external side effect is scoped to this fixture.
    bridge.tempfile.gettempdir = lambda: str(folder)
    bridge._artifact_key = lambda *a: key
    bridge.resolve_domain_detail_profile = lambda *a: SimpleNamespace(fingerprint="test", sources=[])
    bridge.resolve_permits = lambda *a, **kw: SimpleNamespace(paths=[], fingerprint="test", policy=None, errors=[])
    bridge._attach_rule_regression_log = lambda *a: None
    bridge._remember_judgement_artifacts = lambda *a: None
    bridge._write_summary_csv_from_result = lambda r, p: p.write_text("synthetic summary", encoding="utf-8")
    bridge.make_stage_csv_zip = lambda **k: None
    calls = []

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append("synthetic pipeline; actual API0")

        def run(self, *a, **k):
            (folder / ("entered_" + name)).touch()
            wait_for(folder / ("release_" + name))
            if mode == "raise":
                raise RuntimeError("synthetic interruption before publication")
            failed = mode == "fail"
            return SimpleNamespace(metadata={"llm_last_error": "synthetic failure" if failed else "",
                "llm_call_count": 1, "llm_success_count": 0 if failed else 1}, extracted_records=[])

    bridge.DocumentJudgePipeline = Pipeline

    def progress(message):
        if "다른 프로세스" in message:
            (folder / ("waiting_" + name)).touch()

    (folder / ("ready_" + name)).touch()
    try:
        value = bridge.ensure_judgement_artifacts(pdf_path=folder / "fixture.pdf", permit_paths=[],
            original_csv_dir=None, _background=True, _progress=progress, _retry_failed=mode != "view")
        report = {"pid": os.getpid(), "calls": len(calls), "failed": bridge._has_runtime_llm_failure(value),
                  "key": value.get("artifact_key"), "api_calls": 0}
    except Exception as exc:
        report = {"pid": os.getpid(), "calls": len(calls), "error": type(exc).__name__,
                  "message": str(exc), "api_calls": 0}
    (folder / ("result_" + name + ".json")).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), *sys.argv[2:])
