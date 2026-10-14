from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sp_pdf_judger.domain_details import DomainDetailStore
from sp_pdf_judger.llm import ClovaJudgeClient


def _write_detail(
    root: Path,
    relative_path: str,
    *,
    scope: str,
    title: str,
    aliases: list[str],
    body: str,
) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    alias_yaml = "\n".join(f"  - {alias}" for alias in aliases)
    path.write_text(
        (
            "---\n"
            f"scope: {scope}\n"
            f"id: {path.stem}\n"
            f"title: {title}\n"
            "aliases:\n"
            f"{alias_yaml}\n"
            "status: active\n"
            "---\n\n"
            f"{body}\n"
        ),
        encoding="utf-8",
    )


def test_resolve_merges_overall_company_and_product(tmp_path: Path) -> None:
    _write_detail(
        tmp_path,
        "overall.md",
        scope="overall",
        title="공통",
        aliases=["전체"],
        body="공통 시험기준을 확인한다.",
    )
    _write_detail(
        tmp_path,
        "companies/gc.md",
        scope="company",
        title="GC녹십자",
        aliases=["GC녹십자", "녹십자"],
        body="회사 제조번호 체계를 확인한다.",
    )
    _write_detail(
        tmp_path,
        "products/albumin.md",
        scope="product",
        title="사람혈청알부민",
        aliases=["알부민", "녹십자-알부민주20%"],
        body="원료혈장부터 완제까지 추적한다.",
    )

    profile = DomainDetailStore(tmp_path).resolve(
        "주식회사 GC녹십자",
        "녹십자-알부민주20%",
    )

    assert profile.matched_company == "GC녹십자"
    assert profile.matched_product == "사람혈청알부민"
    assert profile.sources == [
        "overall.md",
        "companies/gc.md",
        "products/albumin.md",
    ]
    context = profile.render_for_llm()
    assert "[전체 디테일: 공통]" in context
    assert "[회사 디테일: GC녹십자]" in context
    assert "[제품 디테일: 사람혈청알부민]" in context


def test_unknown_company_and_product_still_use_overall(tmp_path: Path) -> None:
    _write_detail(
        tmp_path,
        "overall.md",
        scope="overall",
        title="공통",
        aliases=["전체"],
        body="항상 확인한다.",
    )

    profile = DomainDetailStore(tmp_path).resolve("새 회사", "새 제품")

    assert profile.matched_company == ""
    assert profile.matched_product == ""
    assert profile.sources == ["overall.md"]


def test_detail_content_change_updates_fingerprint(tmp_path: Path) -> None:
    _write_detail(
        tmp_path,
        "overall.md",
        scope="overall",
        title="공통",
        aliases=["전체"],
        body="첫 번째 내용",
    )
    first = DomainDetailStore(tmp_path).resolve("", "").fingerprint

    _write_detail(
        tmp_path,
        "overall.md",
        scope="overall",
        title="공통",
        aliases=["전체"],
        body="두 번째 내용",
    )
    second = DomainDetailStore(tmp_path).resolve("", "").fingerprint

    assert first != second


def test_llm_prompt_contains_merged_detail_context() -> None:
    captured: dict[str, object] = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                parsed={
                    "status": "검수합격",
                    "reason": "시험결과가 시험기준과 같아 검수합격으로 판단했습니다.",
                    "normalized_criteria": "음성",
                    "normalized_result": "음성",
                }
            )

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            legacy_response = FakeModels().generate_content(**kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(legacy_response.parsed, ensure_ascii=False)
                        )
                    )
                ]
            )

    client = ClovaJudgeClient(
        api_key="",
        domain_detail_context=(
            "[전체 디테일: 공통]\n공통 확인\n\n"
            "[회사 디테일: GC녹십자]\n회사 확인\n\n"
            "[제품 디테일: 사람혈청알부민]\n제품 확인"
        ),
    )
    client.enabled = True
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    response = client.explain(
        test_name="확인시험",
        criteria="음성",
        result="음성",
        rag_contexts=["약전 근거"],
    )

    assert response is not None
    prompt = str(captured["messages"][0]["content"])
    assert "[전체 디테일: 공통]" in prompt
    assert "[회사 디테일: GC녹십자]" in prompt
    assert "[제품 디테일: 사람혈청알부민]" in prompt
    assert "현재 SP 문서의 명시적 시험기준·시험결과" in prompt
