from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.db.models import User  # noqa: E402
from backend.infra.database import SessionLocal  # noqa: E402


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _cleanup_user(username: str) -> None:
    db = SessionLocal()
    try:
        with db.begin():
            user = db.query(User).filter(User.username == username).first()
            if user is not None:
                db.delete(user)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a live model + Tavily Keyless Web Research smoke test."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    suffix = uuid4().hex[:12]
    username = f"web_smoke_{suffix}"
    password = f"Smoke-{uuid4().hex}"
    thread_id: str | None = None
    run_id: str | None = None
    headers: dict[str, str] = {}

    try:
        with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
            ready = client.get("/health/ready")
            ready.raise_for_status()
            web = ready.json().get("web_research") or {}
            if not all(web.get(key) is True for key in ("enabled", "ready", "search_ready")):
                raise RuntimeError("Web Research readiness is not healthy")

            registered = client.post(
                "/auth/register",
                json={"username": username, "password": password, "role": "user"},
            )
            registered.raise_for_status()
            headers = {
                "Authorization": f"Bearer {registered.json()['access_token']}"
            }

            created_thread = client.post(
                "/v1/threads",
                headers=headers,
                json={"title": "Web Research E2E Smoke"},
            )
            created_thread.raise_for_status()
            thread_id = created_thread.json()["thread_id"]

            created_run = client.post(
                f"/v1/threads/{thread_id}/runs",
                headers=headers,
                json={
                    "message": (
                        "/web-research 搜索 OpenAI 官方网站，"
                        "用一句中文回答，并引用搜索结果。"
                    ),
                    "idempotency_key": f"web-smoke-{suffix}",
                    "expected_thread_version": 0,
                    "multitask_strategy": "reject",
                    "on_disconnect": "continue",
                    "approved_tools": [],
                },
            )
            created_run.raise_for_status()
            run_id = created_run.json()["run"]["id"]

            deadline = time.monotonic() + args.timeout
            run: dict = {}
            while time.monotonic() < deadline:
                response = client.get(f"/v1/runs/{run_id}", headers=headers)
                response.raise_for_status()
                run = response.json()
                if run["status"] in TERMINAL_STATUSES:
                    break
                time.sleep(0.5)
            else:
                client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
                raise RuntimeError("Run did not reach a terminal state before timeout")

            events_response = client.get(
                f"/v1/runs/{run_id}/events",
                headers=headers,
                params={"after": 0, "limit": 500},
            )
            events_response.raise_for_status()
            events = events_response.json()["events"]
            projection = [
                {
                    "sequence": event["sequence"],
                    "type": event["type"],
                    "tool_name": (event.get("data") or {}).get("tool_name"),
                    "error_code": (event.get("data") or {}).get("error_code"),
                }
                for event in events
            ]
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": run["status"],
                        "error_code": run.get("error_code"),
                        "skill_name": run.get("skill_name"),
                        "events": projection,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

            web_search_completed = any(
                event["type"] == "tool.completed"
                and (event.get("data") or {}).get("tool_name") == "web_search"
                for event in events
            )
            if run["status"] != "succeeded":
                raise SystemExit(1)
            if not web_search_completed:
                raise RuntimeError("Run completed without a successful web_search")

            client.delete(f"/v1/threads/{thread_id}", headers=headers)
            thread_id = None
            client.post("/auth/logout")
    finally:
        _cleanup_user(username)


if __name__ == "__main__":
    main()
