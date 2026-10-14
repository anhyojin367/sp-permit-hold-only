"""Prepare historical replay fixtures for tests; no network or CLOVA requests."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "fixtures" / "historical"


def rebase(value):
    if isinstance(value, list):
        return [rebase(item) for item in value]
    if isinstance(value, dict):
        return {key: rebase(item) for key, item in value.items()}
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        marker = "/sp_semi_dev/"
        if normalized.startswith(("C:/", "c:/")) and marker in normalized and "\n" not in value:
            return str(ROOT / normalized.split(marker, 1)[1])
    return value


def main():
    files = sorted(SOURCE.rglob("*.json"))
    if not files:
        raise SystemExit("Historical fixtures missing from tests/fixtures/historical")
    count = 0
    for source in files:
        target = ROOT / ".local_validation" / source.relative_to(SOURCE)
        if target.exists():
            continue  # Never overwrite fresh validation or a user's judgement.
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            json.dump(rebase(json.loads(source.read_text(encoding="utf-8"))),
                      handle, ensure_ascii=False, indent=2)
        count += 1
    print(f"Prepared {count} historical fixtures; existing files preserved; API calls: 0.")
    print("Saved replay inputs are not a new CLOVA evaluation. Old preview paths may be unavailable.")


if __name__ == "__main__":
    main()
