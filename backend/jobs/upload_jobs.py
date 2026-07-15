"""上传任务进度管理。

轻量版先使用进程内存保存任务状态，适合当前单进程开发部署。
如果后续要支持多进程或服务重启恢复，可以把同样的数据结构迁移到 Redis/PostgreSQL。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Literal
from uuid import uuid4


StepStatus = Literal["pending", "running", "completed", "failed"]
JobStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cleanup_pending",
]


DEFAULT_STEPS = [
    ("upload", "文档上传"),
    ("reserve", "候选版本准备"),
    ("parse", "解析与版本化分块"),
    ("parent_store", "候选父级分块写入"),
    ("vector_store", "候选向量写入"),
    ("verify", "索引一致性核验"),
    ("publish", "原子发布新版本"),
]

DELETE_STEPS = [
    ("prepare", "原子撤销检索范围"),
    ("milvus", "清理向量索引"),
    ("parent_store", "清理父级分块与缓存"),
    ("object_store", "清理版本对象"),
    ("finalize", "确认清理状态"),
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class UploadJobManager:
    """线程安全的上传任务状态容器。"""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = Lock()

    def create_job(
        self,
        filename: str,
        *,
        steps: list[tuple[str, str]] | None = None,
        current_step: str = "upload",
        message: str = "等待上传",
        completion_step: str = "vector_store",
    ) -> dict:
        steps = steps or DEFAULT_STEPS
        job_id = uuid4().hex
        now = _now_iso()
        job = {
            "job_id": job_id,
            "filename": filename,
            "status": "pending",
            "current_step": current_step,
            "message": message,
            # 完成节点用于区分上传和删除，避免 complete_job 写死最后一步。
            "completion_step": completion_step,
            "total_chunks": 0,
            "processed_chunks": 0,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "steps": [
                {
                    "key": key,
                    "label": label,
                    "percent": 0,
                    "status": "pending",
                    "message": "",
                }
                for key, label in steps
            ],
        }
        with self._lock:
            self._jobs[job_id] = job
            return deepcopy(job)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def update_step(
        self,
        job_id: str,
        step_key: str,
        percent: int,
        status: StepStatus = "running",
        message: str = "",
        *,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
    ) -> dict | None:
        percent = max(0, min(100, int(percent)))
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None

            step = self._find_step(job, step_key)
            if not step:
                return None

            step["percent"] = percent
            step["status"] = status
            step["message"] = message
            job["status"] = "failed" if status == "failed" else "running"
            job["current_step"] = step_key
            job["message"] = message
            job["updated_at"] = _now_iso()

            if total_chunks is not None:
                job["total_chunks"] = int(total_chunks)
            if processed_chunks is not None:
                job["processed_chunks"] = int(processed_chunks)

            return deepcopy(job)

    def complete_step(
        self, job_id: str, step_key: str, message: str = ""
    ) -> dict | None:
        return self.update_step(job_id, step_key, 100, "completed", message)

    def complete_job(self, job_id: str, message: str = "文档入库完成") -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for step in job["steps"]:
                if step["status"] != "failed":
                    step["percent"] = 100
                    step["status"] = "completed"
            job["status"] = "completed"
            job["current_step"] = job.get("completion_step") or job["current_step"]
            job["message"] = message
            job["error"] = None
            job["updated_at"] = _now_iso()
            return deepcopy(job)

    def fail_job(self, job_id: str, step_key: str, error: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            step = self._find_step(job, step_key)
            if step:
                step["status"] = "failed"
                step["message"] = error
            job["status"] = "failed"
            job["current_step"] = step_key
            job["message"] = error
            job["error"] = error
            job["updated_at"] = _now_iso()
            return deepcopy(job)

    def mark_cleanup_pending(
        self,
        job_id: str,
        step_key: str,
        message: str,
    ) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            step = self._find_step(job, step_key)
            step_index = job["steps"].index(step) if step else len(job["steps"])
            for previous in job["steps"][:step_index]:
                if previous["status"] != "failed":
                    previous["percent"] = 100
                    previous["status"] = "completed"
            if step:
                step["status"] = "failed"
                step["message"] = message
            job["status"] = "cleanup_pending"
            job["current_step"] = step_key
            job["message"] = message
            job["error"] = "CLEANUP_PENDING"
            job["updated_at"] = _now_iso()
            return deepcopy(job)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [deepcopy(job) for job in self._jobs.values()]

    @staticmethod
    def _find_step(job: dict, step_key: str) -> dict | None:
        for step in job["steps"]:
            if step["key"] == step_key:
                return step
        return None


upload_job_manager = UploadJobManager()
delete_job_manager = UploadJobManager()
