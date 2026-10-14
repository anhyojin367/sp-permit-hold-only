from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.clova_transport import ClovaServiceError
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.permit_conditions import parse_permit_conditions
from sp_pdf_judger.permit_group_judgement import (
    classify_group,
    execute_permit_conditions,
    validate_group_response,
)
from sp_pdf_judger.permit_grouping import (
    BatchCandidate,
    BatchEvidence,
    PermitGroup,
    SPFact,
)
from sp_pdf_judger.permit_llm_protocol import (
    GroundedConditionDecision,
    GroundedGroupResponse,
)
from sp_pdf_judger.permit_models import PermitChunk, PermitCondition


@pytest.mark.parametrize("source,returned,accepted", [
    ("생존율은 80% 이상이어\n야 한다.", "생존율은 80% 이상이어야 한다.", True),
    ("삼투압은 6.00 \nmOsm/kg이다.", "삼투압은 6.00 mOsm/kg이다.", True),
    ("삼투압은 6.00 \nmOsm/kg이다.", "삼투압은 6.00 nmOsm/kg이다.", False),
    ("삼투압은 6.00 \nmOsm/kg이다.", "삼투압은 6.0 mOsm/kg이다.", False),
    ("기록 1 \n0", "기록 10", False),
    ("결과 no \npositive", "결과 nopositive", False),
])
def test_grounding_allows_pdf_wrapping_only_without_changing_numeric_or_word_tokens(source, returned, accepted):
    condition = _condition(quote=source)
    fact = _fact()
    response = GroundedGroupResponse(selected_evidence_id="evidence-1",
        decisions=[GroundedConditionDecision(condition_id="condition-1", status="pass",
            fact_ids=["fact-1"], quote=returned)], status=PASS_LABEL, reason="조건 충족")
    verdict = validate_group_response(response, _group(facts=(fact,)), (_chunk(),),
        (condition,), execute_permit_conditions((condition,), (fact,)))
    assert verdict.status == (PASS_LABEL if accepted else HOLD_LABEL)
    if not accepted:
        assert "quote_invalid" in verdict.reason


def _condition(
    condition_id: str = "condition-1",
    *,
    kind: str = "compare",
    operator: str | None = ">=",
    value: Decimal | None = Decimal("80"),
    unit: str | None = "%",
    lower: Decimal | None = None,
    upper: Decimal | None = None,
    lower_inclusive: bool = True,
    upper_inclusive: bool = True,
    metric_key: str = "survival_rate",
    method_key: str | None = None,
    evidence_id: str = "evidence-1",
    quote: str = "생존율은 80% 이상이어야 한다.",
    children: tuple[str, ...] = (),
) -> PermitCondition:
    return PermitCondition(
        condition_id=condition_id,
        evidence_id=evidence_id,
        clause_index=0,
        kind=kind,
        operator=operator,
        value=value,
        unit=unit,
        lower=lower,
        upper=upper,
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
        children=children,
        quote=quote,
        stage_key="Component A 중간체원액",
        test_key="확인시험",
        metric_key=metric_key,
        mandatory=True,
        method_key=method_key,
        scope_text=quote,
    )


def _fact(
    fact_id: str = "fact-1",
    *,
    metric_key: str = "survival_rate",
    numeric_value: Decimal | None = Decimal("80"),
    unit: str | None = "%",
    qualifier: str | None = None,
    method_key: str | None = None,
    field: str = "result",
    value: str = "80%",
    order: int | float = 1,
) -> SPFact:
    return SPFact(
        fact_id=fact_id,
        record_order_idx=order,
        row_ref=f"record:{order}",
        field=field,
        value=value,
        quote=value,
        page_start=1,
        page_end=1,
        source_kind="test",
        metric_key=metric_key,
        numeric_value=numeric_value,
        unit=unit,
        method_key=method_key,
        qualifier=qualifier,
    )


def _batch() -> BatchEvidence:
    candidate = BatchCandidate(
        value="LOT-A",
        stage_key="componenta중간체원액",
        record_order_idx=0,
        source_kind="exact_content_label",
        quote="제조번호 LOT-A",
        page_start=1,
        page_end=1,
    )
    return BatchEvidence(
        state="explicit",
        batch="LOT-A",
        stage_key="componenta중간체원액",
        record_order_idxs=(0,),
        candidates=(candidate,),
    )


def _group(
    *,
    facts: tuple[SPFact, ...] = (),
    candidate_ids: tuple[str, ...] = ("evidence-1",),
    blocker: str | None = None,
    record_orders: tuple[int | float, ...] = (1,),
) -> PermitGroup:
    return PermitGroup(
        group_id="group-1",
        stage_key="componenta중간체원액",
        test_key="확인시험",
        batch="LOT-A" if blocker is None else None,
        method_key=None,
        record_order_idxs=record_orders,
        facts=facts,
        candidate_evidence_ids=candidate_ids,
        blocker_code=blocker,
        batch_evidence=_batch() if blocker is None else BatchEvidence(
            state="missing",
            batch=None,
            stage_key="componenta중간체원액",
            record_order_idxs=(),
            candidates=(),
        ),
    )


def _chunk(evidence_id: str = "evidence-1") -> PermitChunk:
    return PermitChunk(
        source_file="permit.pdf",
        page_number=4,
        section_number="2.1.1",
        title="확인시험",
        text="생존율은 80% 이상이어야 한다.",
        page_start=4,
        page_end=4,
        section_path_titles=("Component A 중간체원액", "확인시험"),
        normalized_test_name="확인시험",
        normalized_stage_path="Component A 중간체원액",
        evidence_id=evidence_id,
    )


@pytest.mark.parametrize(
    "condition,fact,expected",
    [
        (
            _condition(value=Decimal(21 * 86400), unit="s", metric_key="incubation_duration"),
            _fact(metric_key="incubation_duration", numeric_value=Decimal("21"), unit="일"),
            "pass",
        ),
        (
            _condition(value=Decimal(21 * 86400), unit="s", metric_key="incubation_duration"),
            _fact(metric_key="incubation_duration", numeric_value=Decimal("20"), unit="일"),
            "fail",
        ),
        (
            _condition(
                kind="range",
                operator="between",
                value=None,
                unit="℃",
                lower=Decimal("34"),
                upper=Decimal("38"),
                metric_key="incubation_temperature",
            ),
            _fact(metric_key="incubation_temperature", numeric_value=Decimal("34"), unit="℃"),
            "pass",
        ),
        (
            _condition(value=Decimal("10"), unit="count", metric_key="sample_count"),
            _fact(metric_key="sample_count", numeric_value=Decimal("10"), unit="개"),
            "pass",
        ),
        (
            _condition(value=Decimal("80"), unit="%", metric_key="survival_rate"),
            _fact(metric_key="survival_rate", numeric_value=Decimal("79.9"), unit="%"),
            "fail",
        ),
        (
            _condition(value=Decimal("80"), unit="%", metric_key="survival_rate"),
            _fact(metric_key="survival_rate", numeric_value=Decimal("80"), unit="℃"),
            "hold",
        ),
    ],
)
def test_deterministic_numeric_boundaries_units_and_duration(condition, fact, expected):
    execution, = execute_permit_conditions((condition,), (fact,))
    assert execution.status == expected
    assert execution.fact_ids == ((fact.fact_id,) if expected != "hold" else ())


def test_exclusive_range_boundaries_are_not_silently_inclusive():
    condition = _condition(
        kind="range",
        operator="between",
        value=None,
        unit="℃",
        lower=Decimal("34"),
        upper=Decimal("38"),
        lower_inclusive=False,
        upper_inclusive=False,
        metric_key="incubation_temperature",
    )
    lower = _fact(metric_key="incubation_temperature", numeric_value=Decimal("34"), unit="℃")
    upper = replace(lower, fact_id="upper", numeric_value=Decimal("38"))
    middle = replace(lower, fact_id="middle", numeric_value=Decimal("36"))
    assert execute_permit_conditions((condition,), (lower,))[0].status == "fail"
    assert execute_permit_conditions((condition,), (upper,))[0].status == "fail"
    assert execute_permit_conditions((condition,), (middle,))[0].status == "pass"


@pytest.mark.parametrize(
    "operator,qualifier,expected",
    [
        ("absent", "absent", "pass"),
        ("absent", "exists", "fail"),
        ("exists", "exists", "pass"),
        ("exists", "absent", "fail"),
        ("negative", "negative", "pass"),
        ("negative", "positive", "fail"),
    ],
)
def test_qualitative_conditions_require_explicit_observations(operator, qualifier, expected):
    metric = "passage" if operator == "exists" else "haemagglutination"
    condition = _condition(
        kind="qualitative",
        operator=operator,
        value=None,
        unit=None,
        metric_key=metric,
    )
    fact = _fact(
        metric_key=metric,
        numeric_value=None,
        unit=None,
        qualifier=qualifier,
        value=qualifier,
    )
    assert execute_permit_conditions((condition,), (fact,))[0].status == expected


def test_explicit_method_scope_cannot_borrow_another_route_fact():
    condition = _condition(
        method_key="요막강내접종",
        metric_key="incubation_duration",
        value=Decimal(3 * 86400),
        unit="s",
    )
    wrong_route = _fact(
        metric_key="incubation_duration",
        numeric_value=Decimal(3),
        unit="일",
        method_key="난황낭접종",
    )
    execution, = execute_permit_conditions((condition,), (wrong_route,))
    assert (execution.status, execution.reason_code) == ("hold", "method_incompatible")
    assert execution.fact_ids == ()


EGG = (
    "요막강 내 접종의 경우, 유정란(10 ~ 11일) 최소 10개에 각각 검체를 요막강 내 주사하고 "
    "36 ± 2 ℃에서 3일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 한다. "
    "생존한 유정란의 요막액을 혼합하여 다른 유정란의 요막액에 계대하여 36 ± 2 ℃에서 "
    "3일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 하고, 배양 후 유정란의 "
    "요막액에 대해 기니픽, 닭 및 사람 O형 적혈구를 이용하여 혈구응집반응을 시험했을 때 "
    "혈구응집반응이 없어야 한다. "
    "난황낭 접종의 경우, 유정란(6 ~ 7 일) 최소 10개에 각각 검체를 난황낭에 주사하고 "
    "36 ± 2 ℃에서 9일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 한다. "
    "생존한 유정란의 난황낭을 혼합하여 다른 유정란에 계대하고 36 ± 2 ℃에서 9일 간 "
    "배양한 뒤 생존율을 검사했을 때 80% 이상이 생존해야 한다."
)


def _egg_conditions() -> tuple[PermitCondition, ...]:
    return parse_permit_conditions(
        PermitChunk(
            "permit.pdf",
            5,
            "2.1.2.1.10.3",
            "유정란접종시험",
            EGG,
            normalized_stage_path="CHO 마스터 세포주",
            normalized_test_name="유정란접종시험",
            evidence_id="egg-evidence",
        )
    )


def _passing_fact_for(condition: PermitCondition, index: int) -> SPFact:
    if condition.kind == "qualitative":
        return _fact(
            f"egg-fact-{index}",
            metric_key=condition.metric_key,
            numeric_value=None,
            unit=None,
            qualifier=condition.operator,
            method_key=condition.method_key,
            value=condition.operator or "",
            order=34,
        )
    if condition.kind == "range":
        assert condition.lower is not None and condition.upper is not None
        value = (condition.lower + condition.upper) / Decimal(2)
    else:
        assert condition.value is not None
        value = condition.value
    return _fact(
        f"egg-fact-{index}",
        metric_key=condition.metric_key,
        numeric_value=value,
        unit=condition.unit,
        method_key=condition.method_key,
        value=f"{value} {condition.unit or ''}".strip(),
        order=34,
    )


def test_complete_compound_egg_conditions_require_every_occurrence():
    conditions = _egg_conditions()
    atoms = tuple(condition for condition in conditions if condition.kind != "and")
    facts = tuple(_passing_fact_for(condition, index) for index, condition in enumerate(atoms))

    complete = execute_permit_conditions(conditions, facts)
    assert all(execution.status == "pass" for execution in complete)

    missing = execute_permit_conditions(conditions, facts[:-1])
    assert any(execution.status == "hold" for execution in missing)
    assert not any(execution.status == "fail" for execution in missing)

    survival_index = next(
        index for index, condition in enumerate(atoms)
        if condition.metric_key == "survival_rate"
    )
    failed_facts = list(facts)
    failed_facts[survival_index] = replace(
        failed_facts[survival_index],
        numeric_value=Decimal("79"),
        value="79%",
        quote="79%",
    )
    failed = execute_permit_conditions(conditions, tuple(failed_facts))
    assert any(execution.status == "fail" for execution in failed)
    assert next(
        execution for execution in failed
        if execution.condition_id == atoms[survival_index].condition_id
    ).fact_ids == (failed_facts[survival_index].fact_id,)


@pytest.mark.parametrize("children", [("leaf", "leaf"), ("missing",), ()])
def test_invalid_compound_graph_never_passes(children):
    leaf = _condition("leaf")
    compound = _condition(
        "compound",
        kind="and",
        operator="and",
        value=None,
        unit=None,
        metric_key="compound",
        children=children,
    )

    executions = execute_permit_conditions((leaf, compound), (_fact(),))

    assert executions[0].status == "pass"
    assert executions[1].status == "hold"
    assert executions[1].reason_code == "compound_invalid"


def test_duplicate_condition_ids_fail_closed_instead_of_reusing_one_fact():
    first = _condition("duplicate")
    second = _condition("duplicate")

    executions = execute_permit_conditions((first, second), (_fact(),))

    assert len(executions) == 2
    assert all(
        (execution.status, execution.reason_code) == ("hold", "condition_duplicate")
        for execution in executions
    )


def test_group_members_are_executed_independently_before_fail_hold_pass_aggregation():
    condition = _condition()
    passing = _fact("member-pass", numeric_value=Decimal("85"), order=1)
    failing = _fact("member-fail", numeric_value=Decimal("79"), order=2)

    execution, = execute_permit_conditions((condition,), (passing, failing))

    assert execution.status == "fail"
    assert execution.fact_ids == (passing.fact_id, failing.fact_id)


def test_one_member_cannot_supply_a_second_members_missing_result():
    condition = _condition()
    passing = _fact("member-pass", numeric_value=Decimal("85"), order=1)
    unrelated = _fact(
        "member-unrelated",
        metric_key="sample_count",
        numeric_value=Decimal("10"),
        unit="개",
        order=2,
    )

    execution, = execute_permit_conditions((condition,), (passing, unrelated))

    assert execution.status == "hold"
    assert execution.fact_ids == (passing.fact_id,)


@pytest.mark.parametrize(
    "value",
    [
        "생존율은 80% 이상이어야 한다.",
        "혈구응집반응이 없어야 한다.",
    ],
)
def test_normative_text_copied_into_result_is_not_an_observation(value):
    condition = (
        _condition()
        if "80%" in value
        else _condition(
            kind="qualitative",
            operator="absent",
            value=None,
            unit=None,
            metric_key="haemagglutination",
        )
    )
    copied_requirement = _fact(
        "copied-requirement",
        metric_key="",
        numeric_value=None,
        unit=None,
        value=value,
    )

    execution, = execute_permit_conditions((condition,), (copied_requirement,))

    assert (execution.status, execution.fact_ids) == ("hold", ())


@pytest.mark.parametrize("value", ["<90%", "90% 미만"])
def test_raw_qualified_numeric_result_cannot_be_reinterpreted_as_exact_boundary(value):
    condition = _condition(value=Decimal("90"), unit="%")
    qualified_result = _fact(
        "qualified-result",
        metric_key="survival_rate",
        numeric_value=None,
        unit=None,
        value=value,
    )

    execution, = execute_permit_conditions((condition,), (qualified_result,))

    assert (execution.status, execution.reason_code) == (
        "fail",
        "condition_violated",
    )
    assert execution.fact_ids == (qualified_result.fact_id,)


def test_typed_numeric_value_cannot_erase_the_raw_result_comparator():
    condition = _condition(value=Decimal("90"), unit="%")
    qualified_result = _fact(
        "typed-qualified-result",
        metric_key="survival_rate",
        numeric_value=Decimal("90"),
        unit="%",
        value="<90%",
    )

    execution, = execute_permit_conditions((condition,), (qualified_result,))

    assert (execution.status, execution.reason_code) == (
        "fail",
        "condition_violated",
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("95% (repeat: 80%)", "fail"),
        ("95% (repeat: 92%)", "pass"),
        ("90% 이하", "hold"),
    ],
)
def test_every_repeated_numeric_observation_is_checked_without_optimistic_first_value(value, expected):
    condition = _condition(value=Decimal("90"), unit="%")
    repeated_result = _fact(
        "repeated-result",
        metric_key="survival_rate",
        numeric_value=None,
        unit=None,
        value=value,
    )

    execution, = execute_permit_conditions((condition,), (repeated_result,))

    assert execution.status == expected
    assert execution.fact_ids == (repeated_result.fact_id,)


def test_partial_structure_never_becomes_deterministic_pass_but_fail_closes():
    structured = _condition()
    unparsed = _condition(
        "unparsed-1",
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
        quote="별도 검증절차를 충족하여야 한다.",
    )
    fact = _fact()
    group = _group(facts=(fact,))
    candidates = (_chunk(),)

    passed = execute_permit_conditions((structured, unparsed), (fact,))
    assert classify_group(group, candidates, (structured, unparsed), passed).path == "ambiguous_llm"

    failed_fact = replace(fact, numeric_value=Decimal("79"), value="79%", quote="79%")
    failed_group = _group(facts=(failed_fact,))
    failed = execute_permit_conditions((structured, unparsed), (failed_fact,))
    assert classify_group(failed_group, candidates, (structured, unparsed), failed).path == "deterministic"


def test_candidate_and_condition_identity_use_group_stage_normalization():
    """Indexed paths retain ancestors/suffixes that grouping deliberately strips."""
    fact = _fact()
    group = _group(facts=(fact,))
    candidate = replace(
        _chunk(),
        normalized_stage_path=(
            "원료의약품 > Component A 중간체원액에 대한 시험"
        ),
        normalized_test_name="2.1.1 확인시험",
    )
    condition = replace(
        _condition(),
        stage_key="원료의약품 > Component A 중간체원액에 대한 시험",
        test_key="2.1.1 확인시험",
    )
    executions = execute_permit_conditions((condition,), (fact,))

    gate = classify_group(group, (candidate,), (condition,), executions)

    assert (gate.path, gate.reason_code) == ("deterministic", "all_conditions_pass")


def test_explicit_search_alias_is_preserved_in_candidate_compatibility():
    fact = _fact()
    group = replace(
        _group(facts=(fact,)),
        test_key="소유래플라스마부정시험",
        compatible_test_keys=(
            "소유래플라스마부정시험",
            "소유래바이러스부정시험",
        ),
    )
    candidate = replace(
        _chunk(),
        title="소유래바이러스부정시험",
        normalized_test_name="소유래바이러스부정시험",
    )
    condition = replace(
        _condition(),
        test_key="소유래바이러스부정시험",
    )
    executions = execute_permit_conditions((condition,), (fact,))

    gate = classify_group(group, (candidate,), (condition,), executions)

    assert (gate.path, gate.reason_code) == ("deterministic", "all_conditions_pass")


def test_fuzzy_substring_candidate_is_not_authoritative_compatibility():
    fact = _fact()
    group = _group(facts=(fact,))
    candidate = replace(
        _chunk(),
        title="최종확인시험",
        normalized_test_name="최종확인시험",
    )
    condition = replace(_condition(), test_key="최종확인시험")
    executions = execute_permit_conditions((condition,), (fact,))

    gate = classify_group(group, (candidate,), (condition,), executions)

    assert (gate.path, gate.reason_code) == (
        "hold_without_llm",
        "candidate_incompatible",
    )


@pytest.mark.parametrize(
    "group,conditions,expected_reason",
    [
        (_group(blocker="batch_missing"), (_condition(),), "batch_missing"),
        (_group(facts=()), (_condition(),), "fact_missing"),
        (_group(facts=(_fact(unit="℃"),)), (_condition(),), "unit_incompatible"),
        (_group(facts=(_fact(),), candidate_ids=()), (), "candidate_missing"),
    ],
)
def test_incomplete_or_blocked_groups_hold_without_llm(group, conditions, expected_reason):
    executions = execute_permit_conditions(conditions, group.facts)
    gate = classify_group(group, (_chunk(),) if group.candidate_evidence_ids else (), conditions, executions)
    assert (gate.path, gate.reason_code) == ("hold_without_llm", expected_reason)


def test_missing_result_for_one_group_member_is_not_filled_by_another_member():
    condition = _condition()
    fact = _fact(numeric_value=Decimal("85"), order=1)
    group = _group(facts=(fact,), record_orders=(1, 2))
    executions = execute_permit_conditions((condition,), group.facts)

    gate = classify_group(group, (_chunk(),), (condition,), executions)

    assert (gate.path, gate.reason_code) == ("hold_without_llm", "fact_missing")


def test_alternative_candidate_failure_does_not_close_group_before_selection():
    first = _condition("first", evidence_id="evidence-1", value=Decimal("80"))
    second = _condition("second", evidence_id="evidence-2", value=Decimal("70"))
    fact = _fact(numeric_value=Decimal("75"))
    group = _group(
        facts=(fact,),
        candidate_ids=("evidence-1", "evidence-2"),
    )
    candidates = (_chunk("evidence-1"), _chunk("evidence-2"))
    executions = execute_permit_conditions((first, second), group.facts)

    gate = classify_group(group, candidates, (first, second), executions)

    assert (gate.path, gate.reason_code) == ("ambiguous_llm", "multiple_candidates")


def _response(
    conditions: tuple[PermitCondition, ...],
    facts: tuple[SPFact, ...],
    *,
    evidence_id: str = "evidence-1",
    status: str = PASS_LABEL,
) -> GroundedGroupResponse:
    return GroundedGroupResponse(
        selected_evidence_id=evidence_id,
        decisions=[
            GroundedConditionDecision(
                condition_id=condition.condition_id,
                status="pass",
                fact_ids=[facts[0].fact_id] if facts else [],
                quote=condition.quote,
            )
            for condition in conditions
        ],
        status=status,
        reason="모든 조건을 확인했습니다.",
    )


@pytest.mark.parametrize("mutation", ["wrong_evidence", "missing", "duplicate", "unknown_fact", "wrong_quote", "top_status"])
def test_grounded_group_response_rejects_unknown_or_incomplete_ids(mutation):
    condition = _condition(kind="unparsed", operator=None, value=None, unit=None, metric_key="unparsed")
    fact = _fact(numeric_value=None, unit=None, qualifier="fit", value="적합")
    group = _group(facts=(fact,))
    response = _response((condition,), (fact,))
    if mutation == "wrong_evidence":
        response = response.model_copy(update={"selected_evidence_id": "other"})
    elif mutation == "missing":
        response = response.model_copy(update={"decisions": []})
    elif mutation == "duplicate":
        response = response.model_copy(update={"decisions": response.decisions * 2})
    elif mutation == "unknown_fact":
        decision = response.decisions[0].model_copy(update={"fact_ids": ["unknown"]})
        response = response.model_copy(update={"decisions": [decision]})
    elif mutation == "wrong_quote":
        decision = response.decisions[0].model_copy(update={"quote": "모델이 만든 근거"})
        response = response.model_copy(update={"decisions": [decision]})
    else:
        response = response.model_copy(update={"status": FAIL_LABEL})

    verdict = validate_group_response(
        response,
        group,
        (_chunk(),),
        (condition,),
        execute_permit_conditions((condition,), (fact,)),
    )

    assert verdict.status == HOLD_LABEL
    assert verdict.gate_path == "hold_without_llm"


def test_semantic_pass_must_cite_result_evidence_for_every_group_member():
    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
    )
    facts = tuple(
        _fact(
            f"semantic-{order}",
            numeric_value=None,
            unit=None,
            qualifier="fit",
            value="예상 염기서열과 일치함",
            order=order,
        )
        for order in (1, 2)
    )
    group = _group(facts=facts, record_orders=(1, 2))
    response = _response((condition,), facts)

    verdict = validate_group_response(
        response,
        group,
        (_chunk(),),
        (condition,),
        execute_permit_conditions((condition,), facts),
    )

    assert (verdict.status, verdict.gate_path) == (HOLD_LABEL, "hold_without_llm")


def test_semantic_pass_covers_distinct_positions_when_order_idx_repeats():
    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
    )
    first = replace(
        _fact("first-row", numeric_value=None, unit=None, value="일치", order=1),
        row_ref="record:1:position:0",
    )
    second = replace(
        _fact("second-row", numeric_value=None, unit=None, value="일치", order=1),
        row_ref="record:1:position:1",
    )
    group = replace(
        _group(facts=(first, second), record_orders=(1, 1)),
        record_positions=(0, 1),
    )
    response = GroundedGroupResponse(
        selected_evidence_id="evidence-1",
        decisions=[
            GroundedConditionDecision(
                condition_id=condition.condition_id,
                status="pass",
                fact_ids=[first.fact_id],
                quote=condition.quote,
            )
        ],
        status=PASS_LABEL,
        reason="첫 번째 행만 인용",
    )

    verdict = validate_group_response(
        response,
        group,
        (_chunk(),),
        (condition,),
        execute_permit_conditions((condition,), (first, second)),
    )

    assert (verdict.status, verdict.gate_path) == (HOLD_LABEL, "hold_without_llm")


class _FakeGroupClient:
    enabled = True

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def explain_permit_group_once(self, **kwargs):
        self.calls += 1
        return self.response


def _engine(client, *, errors=()):
    store = SimpleNamespace(
        enabled=True,
        chunks=[_chunk()],
        extraction_errors=list(errors),
    )
    return JudgeEngine(
        SimpleNamespace(search=lambda *args, **kwargs: []),
        llm_client=client,
        permit_store=store,
        permit_catalog_valid=True,
    )


def test_judge_group_calls_llm_once_only_for_semantic_ambiguity():
    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
        quote="예상되는 염기서열과 일치하여야 한다.",
    )
    facts = tuple(
        _fact(
            f"fact-{order}",
            numeric_value=None,
            unit=None,
            qualifier="fit",
            value="예상 염기서열과 일치함",
            order=order,
        )
        for order in (1, 2, 3)
    )
    group = _group(facts=facts, record_orders=(1, 2, 3))
    response = _response((condition,), facts)
    response = response.model_copy(
        update={
            "decisions": [
                response.decisions[0].model_copy(
                    update={"fact_ids": [fact.fact_id for fact in facts]}
                )
            ]
        }
    )
    client = _FakeGroupClient(response)

    verdict = _engine(client).judge_permit_group(
        group,
        {"evidence-1": (condition,)},
    )

    assert (verdict.status, verdict.gate_path) == (PASS_LABEL, "ambiguous_llm")
    assert client.calls == 1


def test_judge_group_propagates_provider_service_failure():
    class FailingClient(_FakeGroupClient):
        def explain_permit_group_once(self, **kwargs):
            self.calls += 1
            raise ClovaServiceError("rate_limit", 429, "42901")

    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
        quote="예상되는 염기서열과 일치하여야 한다.",
    )
    fact = _fact(
        numeric_value=None,
        unit=None,
        qualifier="fit",
        value="예상 염기서열과 일치함",
    )
    client = FailingClient(None)

    with pytest.raises(ClovaServiceError, match="42901"):
        _engine(client).judge_permit_group(
            _group(facts=(fact,)),
            {"evidence-1": (condition,)},
        )
    assert client.calls == 1


def test_deterministic_failure_cannot_be_reversed_and_spends_no_llm_call():
    condition = _condition()
    fact = _fact(numeric_value=Decimal("79"), value="79%")
    group = _group(facts=(fact,))
    client = _FakeGroupClient(_response((condition,), (fact,)))

    verdict = _engine(client).judge_permit_group(group, {"evidence-1": (condition,)})

    assert (verdict.status, verdict.gate_path) == (FAIL_LABEL, "deterministic")
    assert client.calls == 0
    assert condition.quote in verdict.reason


@pytest.mark.parametrize(
    "group,errors",
    [
        (_group(facts=()), ()),
        (_group(blocker="batch_missing"), ()),
        (_group(facts=(_fact(),)), ("Permit index hash_mismatch for permit.pdf",)),
        (_group(facts=(_fact(),)), ("Permit OCR page 4 is unreadable",)),
    ],
)
def test_missing_blocked_or_fatal_group_never_calls_llm(group, errors):
    condition = _condition(kind="unparsed", operator=None, value=None, unit=None, metric_key="unparsed")
    client = _FakeGroupClient(_response((condition,), group.facts))
    verdict = _engine(client, errors=errors).judge_permit_group(
        group,
        {"evidence-1": (condition,)},
    )
    assert verdict.status == HOLD_LABEL
    assert client.calls == 0


def test_unrelated_unreadable_permit_page_does_not_block_candidate_page():
    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
    )
    fact = _fact(numeric_value=None, unit=None, qualifier="fit", value="적합")
    group = _group(facts=(fact,))
    client = _FakeGroupClient(_response((condition,), (fact,)))

    verdict = _engine(client, errors=("Permit OCR page 2 is unreadable",)).judge_permit_group(
        group,
        {"evidence-1": (condition,)},
    )

    assert verdict.status == PASS_LABEL
    assert client.calls == 1


def test_malformed_group_response_fails_closed_without_a_second_call():
    class MalformedResponse:
        @property
        def selected_evidence_id(self):
            raise RuntimeError("malformed provider object")

    condition = _condition(
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
    )
    fact = _fact(numeric_value=None, unit=None, qualifier="fit", value="적합")
    client = _FakeGroupClient(MalformedResponse())

    verdict = _engine(client).judge_permit_group(
        _group(facts=(fact,)),
        {"evidence-1": (condition,)},
    )

    assert (verdict.status, verdict.gate_path) == (HOLD_LABEL, "hold_without_llm")
    assert client.calls == 1


def _semantic_group_inputs(
    *,
    group_id: str = "group-1",
    evidence_id: str = "evidence-1",
    condition_id: str = "condition-1",
    fact_id: str = "fact-1",
):
    condition = _condition(
        condition_id,
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        metric_key="unparsed",
        evidence_id=evidence_id,
        quote="예상되는 염기서열과 일치하여야 한다.",
    )
    fact = _fact(
        fact_id,
        numeric_value=None,
        unit=None,
        qualifier="fit",
        value="예상되는 염기서열과 일치함",
    )
    group = replace(
        _group(facts=(fact,), candidate_ids=(evidence_id,)),
        group_id=group_id,
    )
    return group, condition, fact


def test_prepare_group_performs_no_provider_call_and_returns_all_grounding_inputs():
    group, condition, _fact_value = _semantic_group_inputs()
    client = _FakeGroupClient(None)
    engine = _engine(client)

    prepared = engine.prepare_permit_group(
        group,
        {"evidence-1": (condition,)},
    )

    assert client.calls == 0
    assert prepared.group is group
    assert prepared.candidates == (_chunk(),)
    assert prepared.conditions == (condition,)
    assert tuple(item.condition_id for item in prepared.executions) == (
        condition.condition_id,
    )
    assert prepared.gate.path == "ambiguous_llm"
    assert prepared.immediate_verdict is None


def test_prepare_returns_immediate_verdict_for_deterministic_and_structural_hold():
    condition = _condition()
    client = _FakeGroupClient(None)
    engine = _engine(client)

    deterministic = engine.prepare_permit_group(
        _group(facts=(_fact(numeric_value=Decimal("79"), value="79%"),)),
        {"evidence-1": (condition,)},
    )
    structural = engine.prepare_permit_group(
        _group(blocker="batch_missing"),
        {"evidence-1": (condition,)},
    )

    assert deterministic.gate.path == "deterministic"
    assert deterministic.immediate_verdict is not None
    assert deterministic.immediate_verdict.status == FAIL_LABEL
    assert structural.gate.path == "hold_without_llm"
    assert structural.immediate_verdict is not None
    assert structural.immediate_verdict.status == HOLD_LABEL
    assert client.calls == 0


def test_resolve_valid_batch_item_matches_single_group_validator():
    group, condition, fact = _semantic_group_inputs()
    engine = _engine(_FakeGroupClient(None))
    prepared = engine.prepare_permit_group(
        group,
        {"evidence-1": (condition,)},
    )
    response = _response((condition,), (fact,))

    resolved = engine.resolve_prepared_permit_group(prepared, response)
    direct = validate_group_response(
        response,
        group,
        prepared.candidates,
        prepared.conditions,
        prepared.executions,
    )

    assert resolved == direct
    assert (resolved.status, resolved.gate_path) == (
        PASS_LABEL,
        "ambiguous_llm",
    )


@pytest.mark.parametrize(
    "foreign_field,expected_reason",
    [
        ("evidence", "candidate_invalid"),
        ("condition", "condition_coverage_invalid"),
        ("fact", "fact_id_invalid"),
    ],
)
def test_cross_group_evidence_condition_or_fact_ids_hold_only_that_group(
    foreign_field,
    expected_reason,
):
    first_group, first_condition, first_fact = _semantic_group_inputs()
    second_group, second_condition, second_fact = _semantic_group_inputs(
        group_id="group-2",
        evidence_id="evidence-2",
        condition_id="condition-2",
        fact_id="fact-2",
    )
    store = SimpleNamespace(
        enabled=True,
        chunks=[_chunk("evidence-1"), _chunk("evidence-2")],
        extraction_errors=[],
    )
    engine = JudgeEngine(
        SimpleNamespace(search=lambda *args, **kwargs: []),
        llm_client=_FakeGroupClient(None),
        permit_store=store,
        permit_catalog_valid=True,
    )
    first = engine.prepare_permit_group(
        first_group,
        {"evidence-1": (first_condition,)},
    )
    second = engine.prepare_permit_group(
        second_group,
        {"evidence-2": (second_condition,)},
    )
    invalid = _response((first_condition,), (first_fact,))
    if foreign_field == "evidence":
        invalid = invalid.model_copy(
            update={"selected_evidence_id": "evidence-2"}
        )
    elif foreign_field == "condition":
        invalid = invalid.model_copy(
            update={
                "decisions": [
                    invalid.decisions[0].model_copy(
                        update={
                            "condition_id": second_condition.condition_id,
                            "quote": second_condition.quote,
                        }
                    )
                ]
            }
        )
    else:
        invalid = invalid.model_copy(
            update={
                "decisions": [
                    invalid.decisions[0].model_copy(
                        update={"fact_ids": [second_fact.fact_id]}
                    )
                ]
            }
        )

    rejected = engine.resolve_prepared_permit_group(first, invalid)
    unaffected = engine.resolve_prepared_permit_group(
        second,
        _response(
            (second_condition,),
            (second_fact,),
            evidence_id="evidence-2",
        ),
    )

    assert (rejected.status, rejected.gate_path) == (
        HOLD_LABEL,
        "hold_without_llm",
    )
    assert expected_reason in rejected.reason
    assert (unaffected.status, unaffected.gate_path) == (
        PASS_LABEL,
        "ambiguous_llm",
    )


def test_existing_single_group_wrapper_remains_compatible():
    group, condition, fact = _semantic_group_inputs()
    response = _response((condition,), (fact,))
    client = _FakeGroupClient(response)
    engine = _engine(client)

    prepared = engine.prepare_permit_group(
        group,
        {"evidence-1": (condition,)},
    )
    expected = engine.resolve_prepared_permit_group(prepared, response)
    actual = engine.judge_permit_group(
        group,
        {"evidence-1": (condition,)},
    )

    assert actual == expected
    assert (actual.status, actual.gate_path) == (
        PASS_LABEL,
        "ambiguous_llm",
    )
    assert client.calls == 1
