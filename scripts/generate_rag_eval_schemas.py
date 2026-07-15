from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.evaluation.rag import (  # noqa: E402
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservationBundle,
    RagEvalReport,
)


SCHEMAS = {
    PROJECT_ROOT / "evals/rag/schema/dataset_v1.schema.json": RagEvalDataset,
    PROJECT_ROOT / "evals/rag/schema/gates_v1.schema.json": RagEvalGatePolicy,
    PROJECT_ROOT
    / "evals/rag/schema/observations_v1.schema.json": RagEvalObservationBundle,
    PROJECT_ROOT / "evals/rag/schema/report_v1.schema.json": RagEvalReport,
}


def render_schema(model) -> str:
    return (
        json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate RAG evaluation schemas.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    stale: list[str] = []
    for path, model in SCHEMAS.items():
        expected = render_schema(model)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                stale.append(str(path.relative_to(PROJECT_ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")

    if stale:
        print("stale RAG evaluation schemas: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
