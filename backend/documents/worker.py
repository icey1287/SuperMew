from __future__ import annotations

import logging
import os
import random
import socket
from dataclasses import dataclass
from threading import Event, Thread
from uuid import uuid4

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import WorkerSettings, get_settings
from backend.documents.catalog import (
    CleanupBuild,
    CleanupJobExecution,
    DocumentCatalog,
    IndexJobExecution,
    VersionBuild,
    WorkerStatus,
)
from backend.documents.publication import DocumentPublication


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexingWorkerConfig:
    poll_seconds: float
    lease_seconds: int
    heartbeat_seconds: int
    retry_base_seconds: float
    retry_max_seconds: float
    retry_jitter_ratio: float

    @classmethod
    def from_settings(
        cls,
        settings: WorkerSettings | None = None,
    ) -> IndexingWorkerConfig:
        worker = settings or get_settings().worker
        return cls(
            poll_seconds=worker.indexing_poll_seconds,
            lease_seconds=worker.indexing_lease_seconds,
            heartbeat_seconds=worker.indexing_heartbeat_seconds,
            retry_base_seconds=worker.indexing_retry_base_seconds,
            retry_max_seconds=worker.indexing_retry_max_seconds,
            retry_jitter_ratio=worker.indexing_retry_jitter_ratio,
        )


def default_indexing_worker_id() -> str:
    worker_settings = get_settings().worker
    configured = worker_settings.indexing_worker_id.strip()
    shared = worker_settings.worker_id.strip()
    host = socket.gethostname().split(".", 1)[0] or "host"
    prefix = configured or (f"indexing-{shared}" if shared else "indexing")
    suffix = f"{host[:32]}-{os.getpid()}-{uuid4().hex[:10]}"
    prefix = prefix[: max(1, 127 - len(suffix))]
    return f"{prefix}-{suffix}"


def _public_error(exc: BaseException) -> tuple[str, bool, str | None]:
    public = getattr(exc, "public_error", None)
    if public is None:
        return ErrorCode.STORAGE_UNAVAILABLE.value, True, None
    code = str(getattr(public, "code", None) or ErrorCode.STORAGE_UNAVAILABLE.value)
    retryable = bool(getattr(public, "retryable", True))
    stage = getattr(public, "stage", None)
    return code[:64], retryable, str(stage)[:64] if stage else None


class IndexingWorker:
    """Durable dispatcher for version publication and exact-version cleanup."""

    worker_kind = "indexing"

    def __init__(
        self,
        *,
        catalog: DocumentCatalog | None = None,
        publication: DocumentPublication | None = None,
        worker_id: str | None = None,
        config: IndexingWorkerConfig | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.catalog = catalog or DocumentCatalog()
        self.publication = publication or DocumentPublication(catalog=self.catalog)
        self.worker_id = (worker_id or default_indexing_worker_id()).strip()[:128]
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        self.config = config or IndexingWorkerConfig.from_settings()
        self._random = random_source or random.Random()
        self._prefer_cleanup = False
        publication_config = getattr(self.publication, "config", None)
        build_profile = getattr(publication_config, "build_profile", None)
        self.build_fingerprint = getattr(build_profile, "fingerprint", None)

    def _record_heartbeat(
        self,
        status: WorkerStatus,
        *,
        current_job_id: str | None = None,
        current_job_kind: str | None = None,
    ) -> None:
        metadata = {}
        if current_job_id:
            metadata["current_job_id"] = current_job_id
        if current_job_kind:
            metadata["current_job_kind"] = current_job_kind
        if self.build_fingerprint:
            metadata["build_fingerprint"] = self.build_fingerprint
        self.catalog.record_worker_heartbeat(
            worker_id=self.worker_id,
            worker_kind=self.worker_kind,
            status=status,
            metadata=metadata,
        )

    def _retry_delay_seconds(self, attempts: int) -> float:
        exponent = max(int(attempts) - 1, 0)
        raw = min(
            self.config.retry_base_seconds * (2**exponent),
            self.config.retry_max_seconds,
        )
        jitter = raw * self.config.retry_jitter_ratio
        return max(raw + self._random.uniform(-jitter, jitter), 0.0)

    def _heartbeat_thread(
        self,
        *,
        job_id: str,
        execution: IndexJobExecution | CleanupJobExecution,
        cleanup: bool,
        stop_event: Event,
        ownership_lost: Event,
    ) -> Thread:
        def loop() -> None:
            while not stop_event.wait(self.config.heartbeat_seconds):
                try:
                    if cleanup:
                        self.catalog.heartbeat_cleanup_job(
                            job_id=job_id,
                            execution=execution,
                            lease_seconds=self.config.lease_seconds,
                        )
                    else:
                        self.catalog.heartbeat_index_job(
                            job_id=job_id,
                            execution=execution,
                            lease_seconds=self.config.lease_seconds,
                        )
                    self._record_heartbeat(
                        WorkerStatus.RUNNING,
                        current_job_id=job_id,
                        current_job_kind="cleanup" if cleanup else "index",
                    )
                except AppError as exc:
                    if str(exc.public_error.code) == ErrorCode.CONFLICT.value:
                        ownership_lost.set()
                        return
                    logger.warning(
                        "indexing worker heartbeat rejected",
                        extra={"job_id": job_id, "error_code": exc.public_error.code},
                    )
                except Exception:
                    logger.exception(
                        "indexing worker heartbeat failed",
                        extra={"job_id": job_id},
                    )

        thread = Thread(
            target=loop,
            name=f"index-heartbeat:{job_id}",
            daemon=True,
        )
        thread.start()
        return thread

    @staticmethod
    def _stop_heartbeat(stop_event: Event, thread: Thread) -> None:
        stop_event.set()
        thread.join(timeout=5)

    def _still_owns_index(
        self,
        build: VersionBuild,
        execution: IndexJobExecution,
    ) -> bool:
        try:
            self.catalog.assert_index_lease(
                job_id=build.job.id,
                execution=execution,
            )
            return True
        except AppError as exc:
            if str(exc.public_error.code) == ErrorCode.CONFLICT.value:
                return False
            raise

    def _run_index(self, build: VersionBuild) -> None:
        execution = IndexJobExecution(
            worker_id=self.worker_id,
            execution_fence=build.job.execution_fence,
        )
        heartbeat_stop = Event()
        ownership_lost = Event()
        heartbeat = self._heartbeat_thread(
            job_id=build.job.id,
            execution=execution,
            cleanup=False,
            stop_event=heartbeat_stop,
            ownership_lost=ownership_lost,
        )
        try:
            self.publication.run(build.job.id, execution=execution)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            if ownership_lost.is_set() or not self._still_owns_index(build, execution):
                logger.warning(
                    "indexing execution lost ownership",
                    extra={"job_id": build.job.id},
                )
                return
            code, retryable, stage = _public_error(exc)
            detail = f"stage={stage}" if stage else None
            if retryable:
                self.catalog.schedule_index_retry(
                    job_id=build.job.id,
                    execution=execution,
                    retry_delay_seconds=self._retry_delay_seconds(build.job.attempts),
                    error_code=code,
                    error_detail_redacted=detail,
                )
            else:
                self.catalog.fail(
                    job_id=build.job.id,
                    publication_fence=build.job.publication_fence,
                    error_code=code,
                    error_detail_redacted=detail,
                    execution=execution,
                )
        finally:
            self._stop_heartbeat(heartbeat_stop, heartbeat)

    def _run_cleanup(self, build: CleanupBuild) -> None:
        execution = CleanupJobExecution(
            worker_id=self.worker_id,
            execution_fence=build.job.execution_fence,
        )
        heartbeat_stop = Event()
        ownership_lost = Event()
        heartbeat = self._heartbeat_thread(
            job_id=build.job.id,
            execution=execution,
            cleanup=True,
            stop_event=heartbeat_stop,
            ownership_lost=ownership_lost,
        )
        try:
            self.catalog.update_cleanup_job(
                job_id=build.job.id,
                execution=execution,
                current_step="physical_cleanup",
            )
            self.publication.cleanup_version(
                document=build.document,
                version=build.version,
                finalize=False,
                step_callback=lambda step: self.catalog.update_cleanup_job(
                    job_id=build.job.id,
                    execution=execution,
                    current_step=step,
                ),
            )
            if ownership_lost.is_set():
                return
            self.catalog.complete_cleanup_job(
                job_id=build.job.id,
                execution=execution,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            if ownership_lost.is_set():
                return
            code, retryable, stage = _public_error(exc)
            detail = f"stage={stage}" if stage else None
            try:
                if retryable:
                    self.catalog.schedule_cleanup_retry(
                        job_id=build.job.id,
                        execution=execution,
                        retry_delay_seconds=self._retry_delay_seconds(
                            build.job.attempts
                        ),
                        error_code=code,
                        error_detail_redacted=detail,
                    )
                else:
                    self.catalog.dead_letter_cleanup_job(
                        job_id=build.job.id,
                        execution=execution,
                        error_code=code,
                        error_detail_redacted=detail,
                    )
            except AppError as transition_error:
                if str(transition_error.public_error.code) != ErrorCode.CONFLICT.value:
                    raise
        finally:
            self._stop_heartbeat(heartbeat_stop, heartbeat)

    def run_once(self, stop_event: Event | None = None) -> bool:
        self._record_heartbeat(WorkerStatus.RUNNING)
        claimers = (
            ("cleanup", self.catalog.claim_cleanup_job, self._run_cleanup),
            ("index", self.catalog.claim_index_job, self._run_index),
        )
        if not self._prefer_cleanup:
            claimers = tuple(reversed(claimers))
        for kind, claim, runner in claimers:
            if stop_event is not None and stop_event.is_set():
                return False
            claim_kwargs = {
                "worker_id": self.worker_id,
                "lease_seconds": self.config.lease_seconds,
            }
            if kind == "index" and self.build_fingerprint:
                claim_kwargs["build_fingerprint"] = self.build_fingerprint
            build = claim(
                **claim_kwargs,
            )
            if build is None:
                continue
            self._prefer_cleanup = not self._prefer_cleanup
            self._record_heartbeat(
                WorkerStatus.RUNNING,
                current_job_id=build.job.id,
                current_job_kind=(
                    "cleanup" if isinstance(build, CleanupBuild) else "index"
                ),
            )
            runner(build)
            self._record_heartbeat(WorkerStatus.RUNNING)
            return True
        return False

    def run_forever(self, stop_event: Event) -> None:
        self._record_heartbeat(WorkerStatus.STARTING)
        self._record_heartbeat(WorkerStatus.RUNNING)
        try:
            while not stop_event.is_set():
                worked = self.run_once(stop_event)
                if not worked:
                    stop_event.wait(self.config.poll_seconds)
        finally:
            self._record_heartbeat(WorkerStatus.DRAINING)
            self._record_heartbeat(WorkerStatus.STOPPED)


__all__ = [
    "IndexingWorker",
    "IndexingWorkerConfig",
    "default_indexing_worker_id",
]
