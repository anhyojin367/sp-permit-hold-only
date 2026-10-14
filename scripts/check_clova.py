"""Minimal connectivity check; never prints credentials or raw API errors."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sp_pdf_judger.llm import ClovaJudgeClient, JudgeResponse
from sp_pdf_judger.clova_client import request_structured_response

if __name__ == "__main__":
    c = ClovaJudgeClient()
    if not c.enabled:
        print("CLOVA not configured or client unavailable")
        raise SystemExit(1)
    try:
        result = request_structured_response(client=c.client.with_options(timeout=40, max_retries=0), model=c.model,
            prompt='Return JSON with status="검수합격" and reason="연결 확인".',
            response_model=JudgeResponse, max_completion_tokens=128, use_schema=False)
        print("CLOVA connection OK; model=" + c.model + "; parsed=" + result.status)
    except Exception as error:
        print("CLOVA check failed: " + type(error).__name__ + "; HTTP=" + str(getattr(error, "status_code", None)))
        raise SystemExit(1)
