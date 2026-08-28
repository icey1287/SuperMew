"""Verify production Compose rejects absent and empty required Secrets."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.prod.yml"
REQUIRED = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
)
PLACEHOLDERS = {
    "POSTGRES_DB": "supermew_config_check",
    "POSTGRES_USER": "supermew_config_check",
    "POSTGRES_PASSWORD": "config-check-password",
    "REDIS_PASSWORD": "config-check-redis-password",
    "MINIO_ROOT_USER": "config-check-minio",
    "MINIO_ROOT_PASSWORD": "config-check-minio-password",
}


def _config(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "-q",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    complete = {**os.environ, **PLACEHOLDERS}
    success = _config(complete)
    if success.returncode != 0:
        raise RuntimeError(
            f"complete production Compose config failed: {success.stderr}"
        )

    for required in REQUIRED:
        rejected_environments = {
            "unset": {key: value for key, value in complete.items() if key != required},
            "empty": {**complete, required: ""},
        }
        for case, environment in rejected_environments.items():
            result = _config(environment)
            if result.returncode == 0:
                raise AssertionError(
                    f"production Compose accepted {case} required variable {required}"
                )
            if required not in result.stderr:
                raise AssertionError(
                    f"{case} {required} failed without naming the required variable"
                )

    print(f"production Compose requires {len(REQUIRED)} explicit non-empty variables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
