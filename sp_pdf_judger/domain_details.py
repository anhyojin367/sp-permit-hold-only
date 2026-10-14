from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import PACKAGE_DIR
from .utils import clean_text


DOMAIN_DETAIL_DIR = PACKAGE_DIR / "domain_details"
_MAX_DOCUMENT_CHARS = 7000
_MAX_CONTEXT_CHARS = 18000


def _normalize_match_key(value: str | None) -> str:
    text = clean_text(value).casefold()
    text = re.sub(
        r"(주식회사|유한회사|재단법인|사단법인|\(주\)|㈜|co\.?|ltd\.?|inc\.?|corporation)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text.strip()

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()

    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML front matter in {path}: {exc}") from exc

    if not isinstance(metadata, dict):
        raise ValueError(f"Front matter must be an object in {path}")
    return metadata, parts[2].strip()


@dataclass(frozen=True)
class DomainDetailDocument:
    scope: str
    key: str
    title: str
    aliases: tuple[str, ...]
    body: str
    source_path: Path
    source_root: Path = DOMAIN_DETAIL_DIR
    status: str = "active"
    review_status: str = "draft"
    priority: int = 0

    @property
    def source_name(self) -> str:
        try:
            return self.source_path.relative_to(self.source_root).as_posix()
        except ValueError:
            return self.source_path.as_posix()

    @property
    def content_hash(self) -> str:
        payload = (
            f"{self.scope}\n{self.key}\n{self.title}\n"
            f"{'|'.join(self.aliases)}\n{self.status}\n{self.review_status}\n"
            f"{self.priority}\n{self.source_name}\n{self.body}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DomainDetailProfile:
    requested_company: str
    requested_product: str
    matched_company: str
    matched_product: str
    documents: tuple[DomainDetailDocument, ...]

    @property
    def sources(self) -> list[str]:
        return [document.source_name for document in self.documents]

    @property
    def fingerprint(self) -> str:
        payload = "\n".join(document.content_hash for document in self.documents)
        payload += f"\n{self.matched_company}\n{self.matched_product}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def render_for_llm(self, max_chars: int = _MAX_CONTEXT_CHARS) -> str:
        if not self.documents:
            return ""

        labels = {
            "overall": "전체 디테일",
            "company": "회사 디테일",
            "product": "제품 디테일",
        }
        blocks: list[str] = []
        per_document_chars = max(
            1200,
            min(
                _MAX_DOCUMENT_CHARS,
                (max_chars // max(1, len(self.documents))) - 300,
            ),
        )
        for document in self.documents:
            body = document.body
            if len(body) > per_document_chars:
                body = body[:per_document_chars].rstrip() + "\n..."
            label = labels.get(document.scope, document.scope)
            blocks.append(
                f"[{label}: {document.title}]\n"
                f"출처: {document.source_name}\n"
                f"검토상태: {document.review_status}\n"
                f"{body}"
            )

        context = "\n\n".join(blocks).strip()
        if len(context) > max_chars:
            context = context[:max_chars].rstrip() + "\n..."
        return context


class DomainDetailStore:
    """Load and resolve overall, company, and product review details."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DOMAIN_DETAIL_DIR)
        self.documents = self._load_documents()

    def _load_documents(self) -> tuple[DomainDetailDocument, ...]:
        if not self.root.exists():
            return ()

        documents: list[DomainDetailDocument] = []
        for path in sorted(self.root.rglob("*.md")):
            metadata, body = _parse_markdown(path)
            scope = clean_text(metadata.get("scope")).lower()
            if scope not in {"overall", "company", "product"}:
                continue
            if clean_text(metadata.get("status")).lower() == "disabled":
                continue
            if not body:
                continue

            aliases = _string_list(metadata.get("aliases"))
            key = clean_text(metadata.get("id")) or path.stem
            title = clean_text(metadata.get("title")) or key
            if scope != "overall" and not aliases:
                aliases = (title, key)

            try:
                priority = int(metadata.get("priority", 0))
            except (TypeError, ValueError):
                priority = 0

            documents.append(
                DomainDetailDocument(
                    scope=scope,
                    key=key,
                    title=title,
                    aliases=aliases,
                    body=body,
                    source_path=path,
                    source_root=self.root,
                    status=clean_text(metadata.get("status")) or "active",
                    review_status=clean_text(metadata.get("review_status")) or "draft",
                    priority=priority,
                )
            )
        return tuple(documents)

    def _match_score(self, query: str, document: DomainDetailDocument) -> int:
        query_key = _normalize_match_key(query)
        if not query_key:
            return -1

        best = -1
        for alias in (*document.aliases, document.title, document.key):
            alias_key = _normalize_match_key(alias)
            if not alias_key:
                continue
            if query_key == alias_key:
                score = 100000 + len(alias_key)
            elif len(alias_key) >= 2 and alias_key in query_key:
                score = 50000 + len(alias_key)
            elif len(query_key) >= 3 and query_key in alias_key:
                score = 10000 + len(query_key)
            else:
                continue
            best = max(best, score + document.priority)
        return best

    def _best_match(self, scope: str, query: str) -> DomainDetailDocument | None:
        candidates = [
            (self._match_score(query, document), document)
            for document in self.documents
            if document.scope == scope
        ]
        candidates = [item for item in candidates if item[0] >= 0]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1].priority, item[1].key), reverse=True)
        return candidates[0][1]

    def resolve(self, company: str | None, product: str | None) -> DomainDetailProfile:
        company_text = clean_text(company)
        product_text = clean_text(product)
        overall = sorted(
            (document for document in self.documents if document.scope == "overall"),
            key=lambda document: (document.priority, document.key),
            reverse=True,
        )
        company_doc = self._best_match("company", company_text)
        product_doc = self._best_match("product", product_text)

        selected = [*overall]
        if company_doc is not None:
            selected.append(company_doc)
        if product_doc is not None:
            selected.append(product_doc)

        return DomainDetailProfile(
            requested_company=company_text,
            requested_product=product_text,
            matched_company=company_doc.title if company_doc else "",
            matched_product=product_doc.title if product_doc else "",
            documents=tuple(selected),
        )

    def infer_scope(self, text: str) -> tuple[str, str]:
        company_doc = self._best_match("company", text)
        product_doc = self._best_match("product", text)
        return (
            company_doc.title if company_doc else "",
            product_doc.title if product_doc else "",
        )


def resolve_domain_detail_profile(
    company: str | None,
    product: str | None,
    *,
    root: Path | None = None,
) -> DomainDetailProfile:
    return DomainDetailStore(root).resolve(company, product)


def detail_context_from_source(source: Any) -> str:
    metadata = getattr(source, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return ""
    return clean_text(metadata.get("domain_detail_context"))


def _print_profile(profile: DomainDetailProfile) -> None:
    print(f"requested_company={profile.requested_company}")
    print(f"requested_product={profile.requested_product}")
    print(f"matched_company={profile.matched_company or '-'}")
    print(f"matched_product={profile.matched_product or '-'}")
    print(f"fingerprint={profile.fingerprint}")
    print("sources=")
    for source in profile.sources:
        print(f"  - {source}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect resolved pharmaceutical review details.")
    parser.add_argument("--company", default="", help="Company name detected from the SP document")
    parser.add_argument("--product", default="", help="Product name detected from the SP document")
    parser.add_argument("--show-context", action="store_true", help="Print the merged LLM context")
    args = parser.parse_args(list(argv) if argv is not None else None)

    profile = resolve_domain_detail_profile(args.company, args.product)
    _print_profile(profile)
    if args.show_context:
        print("\n--- merged context ---\n")
        print(profile.render_for_llm())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
