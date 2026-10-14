# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path


@dataclass
class TestResult:
    name: str
    method: str
    criteria: str
    period: str
    result: str
    verdict: str


@dataclass
class StageNode:
    id: str
    label: str
    tests: list[TestResult] = field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    hold_count: int = 0
    total_count: int = 0
    layer: int = 0
    lane: int = 0
    kind: str = "process"


@dataclass
class FlowEdge:
    source: str
    target: str


@dataclass
class ManufacturingGraph:
    product_name: str
    nodes: list[StageNode] = field(default_factory=list)
    edges: list[FlowEdge] = field(default_factory=list)
    layers: list[list[str]] = field(default_factory=list)
    structure_provenance: dict[str, str] = field(default_factory=lambda: {
        "source": "csv_inferred", "reason": "no_pdf",
    })


PHASE_KEYWORDS: list[tuple[str, int]] = [
    ("마스터 세포주", 0),
    ("마스터세포주", 0),
    ("master", 0),
    ("제조용 세포주", 1),
    ("제조용세포주", 1),
    ("working cell", 1),
    ("중간체", 2),
    ("중간체원액", 2),
    ("intermediate", 2),
    ("component", 2),
    ("나노파티클", 3),
    ("원액", 3),
    ("bulk", 3),
    ("최종원액", 4),
    ("최종 원액", 4),
    ("final bulk", 4),
    ("완제", 5),
    ("완제의약품", 5),
    ("final product", 5),
    ("drug product", 5),
]


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[.\s/()]+", "_", s)
    s = re.sub(r"[^a-z0-9가-힣_]", "", s)
    return s.strip("_") or "stage"


def _normalize(name: str) -> str:
    return re.sub(r"[\s._\-/()]+", "", name.strip().lower())


def _node_kind(name: str) -> str:
    # Only explicit exporter buckets are non-process nodes; never guess by substring.
    norm = _normalize(name)
    if norm in {_normalize("문서 공통·기타 검증"), _normalize("문서공통")}:
        return "document_check"
    if norm == _normalize("단계 미분류 시험"):
        return "unassigned_check"
    return "process"


def _unique_stage_lookup(items):
    """A normalized spelling may identify only one stage, not merge its counts."""
    result = {}
    for name, value in items:
        key = _normalize(name)
        if key in result:
            raise ValueError("판정 집계 단계명이 중복되어 공정도와 일대일 대응할 수 없습니다.")
        result[key] = value
    return result


def _detect_phase(label: str) -> int | None:
    low = label.lower()
    best_phase = None
    best_len = 0
    for kw, phase in PHASE_KEYWORDS:
        if kw in low and len(kw) > best_len:
            best_phase = phase
            best_len = len(kw)
    return best_phase


def _extract_prefix(label: str) -> str | None:
    low = label.lower()
    for pat in [
        r"^(cho)\b",
        r"^(e[\.\s]*coli)\b",
        r"^(component\s*[a-z])\b",
        r"^([a-z]+)\s+(마스터|제조용|중간체)",
    ]:
        m = re.match(pat, low)
        if m:
            return re.sub(r"[\s.]+", "", m.group(1))
    return None


def load_from_csv_directory(
    csv_dir: Path,
    summary_name: str = "summary.csv",
    summary_path: Path | None = None,
) -> ManufacturingGraph:
    csv_dir = Path(csv_dir)
    product_name = csv_dir.name.replace("_제조요약도_csv", "").replace("_csv", "")

    # summary 는 명시 경로(상위 폴더 등) 우선, 없으면 csv_dir 내부에서 읽는다.
    summary_path = Path(summary_path) if summary_path else (csv_dir / summary_name)
    stages: list[tuple[str, int, int, int, int]] = []

    if summary_path.exists():
        stages = [(name, c["pass"], c["fail"], c["hold"], c["total"])
                  for name, c in _read_summary_counts(summary_path).items()]

    nodes: list[StageNode] = []
    used_ids: set[str] = set()
    for name, pc, fc, hc, tc in stages:
        slug = _slugify(name)
        if slug in used_ids:
            slug = f"{slug}_{len(used_ids)}"
        used_ids.add(slug)
        tests = _load_stage_tests(csv_dir, name)
        nodes.append(StageNode(
            id=slug,
            label=name,
            tests=tests,
            pass_count=pc,
            fail_count=fc,
            hold_count=hc,
            total_count=tc,
            kind=_node_kind(name),
        ))

    if not nodes:
        for csv_file in sorted(csv_dir.glob("*.csv")):
            if csv_file.name in {"summary.csv", "summary_after.csv"}:
                continue
            name = csv_file.stem.replace("_", " ")
            tests = _load_stage_tests(csv_dir, name)
            slug = _slugify(name)
            if slug in used_ids:
                slug = f"{slug}_{len(used_ids)}"
            used_ids.add(slug)
            pc = sum(1 for t in tests if t.verdict == "검수합격")
            fc = sum(1 for t in tests if t.verdict == "검수불합격")
            hc = sum(1 for t in tests if t.verdict == "검수보류")
            nodes.append(StageNode(
                id=slug, label=name, tests=tests,
                pass_count=pc, fail_count=fc, hold_count=hc,
                total_count=len(tests),
                kind=_node_kind(name),
            ))

    _unique_stage_lookup((n.label, n) for n in nodes)
    edges = _infer_edges(nodes)
    _layout_with_supplemental_checks(nodes, edges)

    graph = ManufacturingGraph(product_name=product_name, nodes=nodes, edges=edges)
    graph.layers = _build_layer_list(nodes)
    return graph


def _load_stage_tests(csv_dir: Path, stage_name: str) -> list[TestResult]:
    norm = _normalize(stage_name)
    for csv_file in csv_dir.glob("*.csv"):
        if csv_file.name == "summary.csv":
            continue
        if _normalize(csv_file.stem) == norm:
            return _parse_test_csv(csv_file)
    for csv_file in csv_dir.glob("*.csv"):
        if csv_file.name == "summary.csv":
            continue
        if norm in _normalize(csv_file.stem) or _normalize(csv_file.stem) in norm:
            return _parse_test_csv(csv_file)
    return []


def _parse_test_csv(path: Path) -> list[TestResult]:
    tests: list[TestResult] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tests.append(TestResult(
                name=row.get("시험명", "").strip(),
                method=row.get("시험 방법", "").strip(),
                criteria=row.get("시험 기준", "").strip(),
                period=row.get("시험 기간", "").strip(),
                result=row.get("시험 결과", "").strip(),
                verdict=row.get("검수 결과", "").strip(),
            ))
    return tests


def _infer_edges(nodes: list[StageNode]) -> list[FlowEdge]:
    nodes = [n for n in nodes if n.kind == "process"]
    if len(nodes) <= 1:
        return []

    phases: dict[int, list[StageNode]] = defaultdict(list)
    unphased: list[StageNode] = []
    for n in nodes:
        p = _detect_phase(n.label)
        if p is not None:
            phases[p] = phases.get(p, [])
            phases[p].append(n)
        else:
            unphased.append(n)

    if not phases:
        return [FlowEdge(nodes[i].id, nodes[i + 1].id) for i in range(len(nodes) - 1)]

    sorted_phases = sorted(phases.keys())

    for n in unphased:
        best_phase = sorted_phases[-1]
        phases[best_phase].append(n)

    sorted_phases = sorted(phases.keys())
    edges: list[FlowEdge] = []

    for idx in range(len(sorted_phases) - 1):
        cur_phase = sorted_phases[idx]
        nxt_phase = sorted_phases[idx + 1]
        cur_nodes = phases[cur_phase]
        nxt_nodes = phases[nxt_phase]

        matched_targets: set[str] = set()
        for cn in cur_nodes:
            cp = _extract_prefix(cn.label)
            if cp:
                for nn in nxt_nodes:
                    np_ = _extract_prefix(nn.label)
                    if np_ and cp == np_:
                        edges.append(FlowEdge(cn.id, nn.id))
                        matched_targets.add(nn.id)

        unmatched_src = [cn for cn in cur_nodes if not any(e.source == cn.id for e in edges if e.target in {nn.id for nn in nxt_nodes})]
        unmatched_tgt = [nn for nn in nxt_nodes if nn.id not in matched_targets]

        if unmatched_src and unmatched_tgt:
            if len(unmatched_src) == len(unmatched_tgt):
                for s, t in zip(unmatched_src, unmatched_tgt):
                    edges.append(FlowEdge(s.id, t.id))
            else:
                for s in unmatched_src:
                    for t in unmatched_tgt:
                        edges.append(FlowEdge(s.id, t.id))
        elif unmatched_src and not unmatched_tgt:
            for s in unmatched_src:
                for t in nxt_nodes:
                    edges.append(FlowEdge(s.id, t.id))

    return edges


def _topological_layers(node_ids, edge_pairs) -> dict:
    """Longest-path layers for a DAG; never turn a cycle into arbitrary layers."""
    degree = dict.fromkeys(node_ids, 0)
    if len(degree) != len(node_ids):
        raise ValueError("공정 노드 식별자가 중복되었습니다.")
    children = defaultdict(list)
    seen = set()
    for source, target in edge_pairs:
        if source not in degree or target not in degree:
            raise ValueError("연결선의 공정 노드를 찾을 수 없습니다.")
        if (source, target) in seen:
            raise ValueError("동일한 공정 연결선이 중복되었습니다.")
        seen.add((source, target))
        children[source].append(target)
        degree[target] += 1
    queue = deque(n for n in node_ids if degree[n] == 0)
    layers = dict.fromkeys(node_ids, 0)
    processed = 0
    while queue:
        nid = queue.popleft()
        processed += 1
        for child in children.get(nid, []):
            layers[child] = max(layers[child], layers[nid] + 1)
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    if processed != len(node_ids):
        raise ValueError("순환·재작업 연결은 현재 시뮬레이션에서 지원하지 않습니다.")
    return layers


def _assign_layers(nodes: list[StageNode], edges: list[FlowEdge]) -> None:
    layers = _topological_layers([n.id for n in nodes], [(e.source, e.target) for e in edges])
    id_to_node = {n.id: n for n in nodes}
    layer_groups: dict[int, list[str]] = defaultdict(list)
    for n in nodes:
        n.layer = layers[n.id]
        layer_groups[n.layer].append(n.id)

    for layer_idx in sorted(layer_groups.keys()):
        group = layer_groups[layer_idx]
        for lane, nid in enumerate(group):
            id_to_node[nid].lane = lane


def _build_layer_list(nodes: list[StageNode]) -> list[list[str]]:
    layer_map: dict[int, list[str]] = defaultdict(list)
    for n in nodes:
        layer_map[n.layer].append(n.id)
    max_layer = max(layer_map.keys(), default=0)
    return [layer_map.get(i, []) for i in range(max_layer + 1)]


def _layout_with_supplemental_checks(nodes: list[StageNode], edges: list[FlowEdge]) -> None:
    process = [n for n in nodes if n.kind == "process"]
    _assign_layers(process, edges)
    # Keep the existing last-column presentation, but add no manufacturing arrows.
    last_layer = max((n.layer for n in process), default=0)
    lane = sum(n.layer == last_layer for n in process)
    for n in nodes:
        if n.kind != "process":
            n.layer, n.lane = last_layer, lane
            lane += 1


def graph_to_json(graph: ManufacturingGraph) -> str:
    data = {
        "product_name": graph.product_name,
        "nodes": [asdict(n) for n in graph.nodes],
        "edges": [asdict(e) for e in graph.edges],
        "layers": graph.layers,
        "structure_provenance": dict(graph.structure_provenance),
    }
    return json.dumps(data, ensure_ascii=False)


def _read_summary_counts(path: Path) -> dict[str, dict[str, int]]:
    """Read summary CSV as {stage_name: {pass, fail, hold, total}}.

    The generated summary files use a stable positional layout:
    stage, pass, fail, hold, total. Reading by position is more robust than
    trusting localized headers, which may be mojibaked in some Windows paths.
    """
    out: dict[str, dict[str, int]] = {}
    if not path.exists():
        return out

    def to_int(value):
        text = str(value).strip()
        if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", text):
            raise ValueError("판정 집계 숫자가 올바른 음이 아닌 정수가 아닙니다.")
        return int(text.replace(",", ""))

    total_markers = {"전체", "총계", "합계", "total", "overall"}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    for row in rows[1:]:
        if not row or not any(str(cell).strip() for cell in row):
            continue
        name = str(row[0]).strip()
        if not name or name.casefold() in total_markers:
            continue
        if len(row) < 5:
            raise ValueError("판정 집계 숫자 열이 누락되었습니다.")
        if name in out:
            raise ValueError("판정 집계 단계명이 중복되었습니다.")
        out[name] = {
            "pass": to_int(row[1]),
            "fail": to_int(row[2]),
            "hold": to_int(row[3]),
            "total": to_int(row[4]),
        }
        c = out[name]
        if c["total"] != c["pass"] + c["fail"] + c["hold"]:
            raise ValueError("판정 집계의 합격·불합격·보류 합과 총계가 일치하지 않습니다.")
    _unique_stage_lookup(out.items())
    return out


def _locate_summary(csv_dir: Path, name: str) -> Path | None:
    """summary 파일을 상위 폴더(프로젝트 루트) → csv_dir 순으로 찾는다."""
    for base in (csv_dir.parent, csv_dir):
        cand = base / name
        if cand.exists():
            return cand
    return None


class FlowchartExtractionError(RuntimeError):
    """An unreadable or ambiguous diagram must not look like absent evidence."""


def _flowchart_record_data(dd: dict):
    if not isinstance(dd, dict):
        raise ValueError("공정도 자료 형식 오류")
    nodes, edges = dd.get("nodes"), dd.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("노드/연결선 목록 형식 오류")
    id2name, node_names = {}, []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("노드 형식 오류")
        node_id, name = node.get("node_id"), node.get("name")
        if (type(node_id) not in (str, int) or not str(node_id).strip()
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("노드 식별자/이름 누락")
        name = name.strip()
        if node_id in id2name or name in node_names:
            raise ValueError("노드 식별자/이름 중복")
        id2name[node_id] = name
        node_names.append(name)
    _unique_stage_lookup((name, name) for name in node_names)
    edge_pairs = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("연결선 형식 오류")
        source, target = edge.get("from"), edge.get("to")
        if type(source) not in (str, int) or type(target) not in (str, int):
            raise ValueError("연결선 노드 식별자 형식 오류")
        if source not in id2name or target not in id2name:
            raise ValueError("연결선의 노드를 찾을 수 없음")
        edge_pairs.append((id2name[source], id2name[target]))
    _topological_layers(node_names, edge_pairs)
    return (node_names, edge_pairs) if node_names else None


def extract_pdf_flowchart(pdf_path: Path):
    """extractor_codex_0517 로 PDF의 실제 제조 요약도(노드/연결)를 추출한다.

    반환: (node_names[순서], edge_pairs[(src_name,tgt_name)]) 또는 None(공정도 미발견).
    읽기/구조 오류는 별도 예외다. 추출된 연결선이며 원문과의 의미 일치를 보증하지 않는다.
    """
    try:
        import extractor_codex_0517 as ex
        pages = ex.PDFReader(str(pdf_path)).read()
        items = ex.Normalizer().run(pages)
        sections = ex.SectionBuilder().run(items)
        blocks = ex.BlockBuilder().run(items, sections)
        records = ex.RecordExtractor().run(blocks)
    except Exception as exc:
        raise FlowchartExtractionError("PDF 제조요약도 추출에 실패했습니다. CSV 추정 구조로 대체하지 않습니다.") from exc

    diagrams = [r for r in records if getattr(r, "record_type", None) == "flowchart"]
    if len(diagrams) > 1:
        raise FlowchartExtractionError(
            "PDF 제조요약도가 여러 개여서 연결 관계를 확정할 수 없습니다. "
            "현재 시뮬레이션은 다중 공정도를 지원하지 않으며, 일부만 선택하거나 임의로 합치지 않습니다.")
    if not diagrams:
        return None
    try:
        return _flowchart_record_data(getattr(diagrams[0], "diagram_data", None))
    except Exception as exc:
        raise FlowchartExtractionError(
            f"PDF 제조요약도 노드/연결선이 불완전하거나 지원되지 않습니다: {exc} "
            "CSV 추정 구조로 대체하지 않습니다.") from exc


def build_graph_from_flowchart(
    product_name: str,
    node_names: list[str],
    edge_pairs: list[tuple[str, str]],
    counts: dict[str, dict[str, int]],
) -> ManufacturingGraph:
    """PDF에서 추출한 실제 노드/연결 + summary 카운트로 그래프를 만든다."""
    count_by_norm = _unique_stage_lookup(counts.items())
    _unique_stage_lookup((name, name) for name in node_names)
    try:
        _topological_layers(node_names, edge_pairs)
    except ValueError as exc:
        raise FlowchartExtractionError(f"PDF 제조요약도 공정 구조를 표시할 수 없습니다: {exc}") from exc
    used_ids: set[str] = set()
    name_to_id: dict[str, str] = {}
    nodes: list[StageNode] = []
    for name in node_names:
        slug = _slugify(name)
        if slug in used_ids:
            slug = f"{slug}_{len(used_ids)}"
        used_ids.add(slug)
        name_to_id[name] = slug
        c = count_by_norm.get(_normalize(name), {})
        nodes.append(StageNode(
            id=slug, label=name,
            pass_count=int(c.get("pass", 0)),
            fail_count=int(c.get("fail", 0)),
            hold_count=int(c.get("hold", 0)),
            total_count=int(c.get("total", 0)),
        ))
    edges = [FlowEdge(name_to_id[s], name_to_id[t]) for s, t in edge_pairs]
    _assign_layers(nodes, edges)
    graph = ManufacturingGraph(product_name=product_name, nodes=nodes, edges=edges)
    graph.layers = _build_layer_list(nodes)
    graph.structure_provenance = {"source": "pdf_extracted", "reason": "selected_pdf"}
    return graph


def _should_use_pdf_flowchart(csv_graph: ManufacturingGraph, pdf_graph: ManufacturingGraph) -> bool:
    """Prefer extracted arrows only when every CSV process has one PDF counterpart."""
    if not pdf_graph.nodes or not pdf_graph.edges:
        return False
    csv_names = {_normalize(n.label) for n in csv_graph.nodes if n.kind == "process"}
    pdf_names = {_normalize(n.label) for n in pdf_graph.nodes}
    # Exporter buckets are not evidence of a manufacturing stage in the PDF.
    return csv_names <= pdf_names and all(_node_kind(n.label) == "process" for n in pdf_graph.nodes)


def _merge_csv_verdicts(pdf_graph: ManufacturingGraph, csv_graph: ManufacturingGraph) -> None:
    csv_nodes = _unique_stage_lookup((n.label, n) for n in csv_graph.nodes)
    for n in pdf_graph.nodes:
        source = csv_nodes.get(_normalize(n.label))
        if source is not None:
            n.tests = list(source.tests)
            n.pass_count, n.fail_count = source.pass_count, source.fail_count
            n.hold_count, n.total_count = source.hold_count, source.total_count
    used_ids = {n.id for n in pdf_graph.nodes}
    for n in csv_graph.nodes:
        if n.kind == "process":
            continue
        node_id = n.id
        while node_id in used_ids:
            node_id += "_check"
        used_ids.add(node_id)
        pdf_graph.nodes.append(replace(n, id=node_id, tests=list(n.tests)))
    _layout_with_supplemental_checks(pdf_graph.nodes, pdf_graph.edges)
    pdf_graph.layers = _build_layer_list(pdf_graph.nodes)


def load_simulation_graph(
    csv_dir: Path,
    pdf_path: Path | None = None,
    explicit_summary_path: Path | None = None,
) -> tuple[ManufacturingGraph, dict[str, dict[str, int]]]:
    """Load the simulation graph and final summary counts.

    Counts come from summary_after.csv/summary.csv. PDF arrows win when all CSV
    processes map uniquely. Document/unassigned checks remain disconnected display
    nodes. Incomplete matching keeps the explicitly warned CSV inference.
    """
    csv_dir = Path(csv_dir)
    if explicit_summary_path is not None and Path(explicit_summary_path).exists():
        summary_path: Path | None = Path(explicit_summary_path)
    else:
        summary_path = _locate_summary(csv_dir, "summary_after.csv") or _locate_summary(csv_dir, "summary.csv")
    summary_counts = _read_summary_counts(summary_path) if summary_path else {}

    product_name = csv_dir.name.replace("_제조요약도_csv", "").replace("_csv", "")
    graph = load_from_csv_directory(csv_dir, summary_path=summary_path)

    if pdf_path:
        fc = extract_pdf_flowchart(Path(pdf_path))
        graph.structure_provenance["reason"] = "pdf_not_found"
        if fc:
            node_names, edge_pairs = fc
            pdf_graph = build_graph_from_flowchart(product_name, node_names, edge_pairs, summary_counts)
            if _should_use_pdf_flowchart(graph, pdf_graph):
                _merge_csv_verdicts(pdf_graph, graph)
                graph = pdf_graph
            else:
                graph.structure_provenance["reason"] = "pdf_not_selected" if edge_pairs else "pdf_without_arrows"

    return graph, summary_counts
def _after_lookup(after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return _unique_stage_lookup(after.items())


def simulation_graph_to_json(graph: ManufacturingGraph, summary: dict[str, dict[str, int]]) -> str:
    """최종 summary 카운트를 화면 표시 필드와 after_* 목표 필드에 같이 직렬화한다."""
    summary_norm = _after_lookup(summary)
    node_norm = _unique_stage_lookup((n.label, n) for n in graph.nodes)
    if summary_norm.keys() - node_norm.keys():
        raise ValueError("공정도에서 일부 판정 집계 단계를 찾을 수 없습니다. 누락된 숫자로 표시하지 않습니다.")
    nodes = []
    for n in graph.nodes:
        d = asdict(n)
        counts = summary_norm.get(_normalize(n.label))
        if counts:
            d["pass_count"] = counts["pass"]
            d["fail_count"] = counts["fail"]
            d["hold_count"] = counts["hold"]
            d["total_count"] = counts["total"]
            d["after_pass"] = counts["pass"]
            d["after_fail"] = counts["fail"]
            d["after_hold"] = counts["hold"]
            d["after_total"] = counts["total"]
        else:
            d["after_pass"] = n.pass_count
            d["after_fail"] = n.fail_count
            d["after_hold"] = n.hold_count
            d["after_total"] = n.total_count
        nodes.append(d)
    data = {
        "product_name": graph.product_name,
        "nodes": nodes,
        "edges": [asdict(e) for e in graph.edges],
        "layers": graph.layers,
        "structure_provenance": dict(graph.structure_provenance),
    }
    return json.dumps(data, ensure_ascii=False)


def simulation_structure_notice(graph_json: str) -> str | None:
    """Trusted UI text for edge provenance, independent of judgement totals."""
    evidence = json.loads(graph_json).get("structure_provenance") or {}
    if evidence == {"source": "pdf_extracted", "reason": "selected_pdf"}:
        return None
    reasons = {
        "no_pdf": "PDF 공정도를 제공하지 않아",
        "pdf_not_found": "PDF에서 공정도 노드를 찾지 못해",
        "pdf_without_arrows": "PDF에서 공정도 연결선을 찾지 못해",
        "pdf_not_selected": "현재 구조 선택 기준에서 PDF 공정도가 선택되지 않아",
    }
    if evidence.get("source") == "csv_inferred" and evidence.get("reason") in reasons:
        return (reasons[evidence["reason"]] + " CSV 단계명으로 추정한 연결선을 표시합니다. "
                "실제 PDF의 공정 순서를 확인한 결과가 아닙니다. 판정 숫자는 현재 문서의 판정 요약을 사용합니다.")
    return "공정도 연결선의 출처를 확인할 수 없습니다. 실제 PDF 공정 순서로 해석하지 마세요."
