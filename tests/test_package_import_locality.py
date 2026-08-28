from __future__ import annotations

import subprocess
import sys
import textwrap


def test_milvus_adapter_import_does_not_load_embedding_or_upload_stacks() -> None:
    probe = textwrap.dedent(
        """
        import sys

        import backend.indexing.milvus_client  # noqa: F401

        forbidden = {
            "backend.indexing.document_loader",
            "backend.indexing.embedding",
            "backend.security.uploads",
            "langchain_community",
            "sentence_transformers",
        }
        loaded = sorted(name for name in forbidden if name in sys.modules)
        if loaded:
            raise SystemExit(f"unexpected eager imports: {loaded}")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
