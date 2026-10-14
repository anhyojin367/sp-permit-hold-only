"""Read-only inventory for MD review. Never updates/approves a policy or calls CLOVA."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.policy_required_tests import permit_scope_inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permit", type=Path, action="append", required=True)
    parser.add_argument("--stage-title", action="append", required=True,
                        help="Repeat each exact heading in order from the top-level chapter")
    args = parser.parse_args(argv)
    if any(not p.is_file() or p.suffix.lower() != ".pdf" for p in args.permit):
        parser.error("Every --permit must name an existing PDF")
    store = PermitPdfStore(args.permit, policy=PermitPolicy("linked", True, True, True, "auto"))
    inventory = permit_scope_inventory(store, args.stage_title)
    print(json.dumps(inventory, ensure_ascii=False, indent=2))
    return 2 if inventory.get("error") else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
