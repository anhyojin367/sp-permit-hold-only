"""Synthetic, isolated status publication. Never runs judgement or the API."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def wait(path):
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(.02)


def main(folder, name, phase):
    import sp_judgement_bridge as bridge
    bridge.JUDGEMENT_STATUS_DIR = folder / "status"
    bridge.JUDGEMENT_STATUS_INDEX = folder / "status/status_index.json"
    (folder / ("pid_" + name)).write_text(str(os.getpid()))
    pdf = folder / (name + ".pdf")
    pdf.write_bytes(b"synthetic; never parsed")
    summary = folder / (name + ".csv")
    summary.write_text("제조명,검수합격,검수불합격,검수보류,총 계\nfixture,3,1,2,6\n", encoding="utf-8-sig")
    original_read, original_lock = bridge._read_status_index, bridge.artifact_process_lock
    original_replace = Path.replace

    def read():
        data = original_read()
        (folder / ("read_" + name)).touch()
        if phase == "read":
            wait(folder / ("release_" + name))
        return data

    @contextmanager
    def lock(path, **kwargs):
        kwargs["on_wait"] = lambda: (folder / ("waiting_" + name)).touch()
        with original_lock(path, **kwargs) as waited:
            yield waited

    def replace(path, target):
        if phase == "publish" and Path(target) == bridge.JUDGEMENT_STATUS_INDEX:
            (folder / ("pending_" + name)).touch()
            wait(folder / ("release_" + name))
        return original_replace(path, target)

    bridge._read_status_index, bridge.artifact_process_lock = read, lock
    Path.replace = replace
    try:
        bridge._remember_judgement_artifacts({"pdf_path": pdf, "artifact_key": name,
            "summary_before_path": summary, "summary_after_path": summary,
            "after_result": SimpleNamespace(metadata={})})
        report = {"pid": os.getpid(), "ok": True, "api_calls": 0}
    except Exception as exc:
        report = {"pid": os.getpid(), "error": type(exc).__name__, "message": str(exc), "api_calls": 0}
    (folder / ("result_" + name + ".json")).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), *sys.argv[2:])
