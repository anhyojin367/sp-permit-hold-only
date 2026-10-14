"""Select an MD profile from explicit permit identity without correcting SP text."""
from .policy_engine import norm
import re


def resolve_rule_product(product, permit_store, book):
    known = {norm(p): p for rule in book.rules for p in rule.products}
    audit = {"submitted_product": product, "rule_product": product, "source": "submitted_product"}
    if norm(product) in known or not permit_store.enabled:
        return product, audit
    patterns = {r.params["permit_pattern"] for r in book.rules
                if r.operation == "permit_field" and r.params.get("sp_field") == "제품명"}
    matches = {}
    for chunk in permit_store.chunks:
        for pattern in patterns:
            for match in re.finditer(pattern, chunk.text, re.S):
                identity = norm(match[1])
                if identity:
                    matches.setdefault(identity, []).append({"file": chunk.source_file,
                        "page": chunk.page_number, "quote": match.group(0)})
    # Conflicting/missing/unrecognized permits never enable a guessed profile.
    if len(matches) == 1 and next(iter(matches)) in known:
        identity = next(iter(matches))
        product = known[identity]
        audit.update(rule_product=product, source="linked_permit_identity", evidence=matches[identity])
    return product, audit
