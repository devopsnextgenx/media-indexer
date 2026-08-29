"""Background job framework for long-running, resumable work: LLM metadata
backfill and duplicate detection. Both job types:

  - only make progress when the resource gate says the machine is free
    (load average / CPU threshold from config `jobs.resource_gate`),
  - can be paused and resumed without losing progress (checkpointed in the
    `background_jobs` MySQL row),
  - are triggered independently per mount (or "all"), decoupled from the
    indexing/scan pipeline.
"""
import logging
import os
import threading
import time
from typing import Optional

from media_indexer.config import settings
from media_indexer.database import mysql_db_instance
from media_indexer.llm_parser import parse_and_cache_title, ParserPaused
from media_indexer.llms import OllamaLLMClient

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE_STATUSES"  # not persisted; just documents intent below
_RUNNING_STATES = {"PENDING", "RUNNING"}
_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}


class ResourceGate:
    """Dependency-free 'only run when we have free resources' check. Uses
    1-minute load average (POSIX) and, if `psutil` happens to be installed,
    instantaneous CPU percent as a second signal."""

    def __init__(self, cfg=None):
        self.cfg = cfg or settings.jobs.resource_gate

    def is_free(self) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return True, "resource gate disabled"
        try:
            load1, _, _ = os.getloadavg()
        except (OSError, AttributeError):
            load1 = 0.0
        if load1 > self.cfg.max_load_average_1m:
            return False, f"load average {load1:.2f} > {self.cfg.max_load_average_1m}"

        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.2)
            if cpu > self.cfg.max_cpu_percent:
                return False, f"CPU {cpu:.0f}% > {self.cfg.max_cpu_percent}%"
        except ImportError:
            pass

        return True, "ok"

    def wait_until_free(self, should_stop=None) -> bool:
        """Blocks (sleeping check_interval_seconds) until resources are
        free. Returns False if `should_stop()` fired first."""
        while True:
            free, reason = self.is_free()
            if free:
                return True
            if should_stop and should_stop():
                return False
            logger.info(f"Resource gate busy ({reason}); waiting to resume job work...")
            time.sleep(self.cfg.check_interval_seconds)


resource_gate = ResourceGate()


class BackgroundJobManager:
    def __init__(self):
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _llm_job_id(mount: Optional[str]) -> str:
        return f"llm_parse:{mount or 'all'}"

    @staticmethod
    def _dup_job_id(mount: Optional[str]) -> str:
        return f"duplicate_detect:{mount or 'all'}"

    def _is_alive(self, job_id: str) -> bool:
        with self._lock:
            t = self._threads.get(job_id)
            return bool(t and t.is_alive())

    def _should_stop_factory(self, job_id: str):
        def _should_stop() -> bool:
            job = mysql_db_instance.get_job(job_id)
            return bool(job and job.get("requested_status") in ("PAUSED", "CANCELLED"))
        return _should_stop

    # -- LLM parse job ---------------------------------------------------
    def start_llm_parse_job(self, mount: Optional[str] = None) -> str:
        job_id = self._llm_job_id(mount)
        if self._is_alive(job_id):
            return job_id

        job = mysql_db_instance.get_job(job_id)
        if not job:
            total = mysql_db_instance.count_files_needing_llm_parse(mount)
            mysql_db_instance.create_job(job_id, "llm_parse", mount, total_items=total)
        mysql_db_instance.update_job(job_id, status="RUNNING", requested_status=None, last_error=None)

        t = threading.Thread(target=self._run_llm_parse, args=(job_id, mount), daemon=True)
        with self._lock:
            self._threads[job_id] = t
        t.start()
        return job_id

    def _run_llm_parse(self, job_id: str, mount: Optional[str]):
        should_stop = self._should_stop_factory(job_id)
        client = OllamaLLMClient(model_name=settings.jobs.llm_parsing.model_name)
        batch_size = settings.jobs.llm_parsing.batch_size

        def on_waiting():
            mysql_db_instance.update_job(job_id, status="RUNNING", last_error="waiting for an Ollama endpoint...")

        job = mysql_db_instance.get_job(job_id) or {}
        after = job.get("checkpoint")
        processed = job.get("processed_items", 0) or 0
        failed = job.get("failed_items", 0) or 0

        try:
            while True:
                if should_stop():
                    mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=after)
                    logger.info(f"Job {job_id} paused at checkpoint '{after}'")
                    return

                if not resource_gate.wait_until_free(should_stop=should_stop):
                    mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=after)
                    return

                rows = mysql_db_instance.get_files_needing_llm_parse(
                    mount=mount, limit=batch_size, after_file_name=after
                )
                if not rows:
                    break

                for row in rows:
                    if should_stop():
                        mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=after,
                                                      processed_items=processed, failed_items=failed)
                        logger.info(f"Job {job_id} paused mid-batch at checkpoint '{after}'")
                        return
                    try:
                        parse_and_cache_title(
                            row["file_name"], row.get("file_path", ""),
                            client=client, on_waiting_for_ollama=on_waiting, should_stop=should_stop,
                        )
                        processed += 1
                    except ParserPaused:
                        mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=after,
                                                      processed_items=processed, failed_items=failed)
                        return
                    except Exception as e:
                        failed += 1
                        logger.error(f"LLM parse job {job_id} failed on {row['file_name']}: {e}")
                    after = row["file_name"]

                mysql_db_instance.update_job(
                    job_id, status="RUNNING", checkpoint=after,
                    processed_items=processed, failed_items=failed, last_error=None,
                )

            mysql_db_instance.update_job(
                job_id, status="COMPLETED", processed_items=processed, failed_items=failed,
                requested_status=None,
            )
            logger.info(f"LLM parse job {job_id} completed: {processed} processed, {failed} failed")
        except Exception as e:
            logger.error(f"LLM parse job {job_id} crashed: {e}", exc_info=True)
            mysql_db_instance.update_job(job_id, status="FAILED", last_error=str(e))
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    # -- duplicate detection job -----------------------------------------
    def start_duplicate_detect_job(self, mount_registry: dict, mount: Optional[str] = None) -> str:
        """`mount_registry` is {name: MountConfig}. Runs detection for one
        mount, or every registered mount if `mount` is None — independently
        of any indexing/scan run."""
        job_id = self._dup_job_id(mount)
        if self._is_alive(job_id):
            return job_id

        targets = [mount] if mount else list(mount_registry.keys())
        job = mysql_db_instance.get_job(job_id)
        if not job:
            mysql_db_instance.create_job(job_id, "duplicate_detect", mount, total_items=len(targets))
        mysql_db_instance.update_job(job_id, status="RUNNING", requested_status=None, last_error=None)

        t = threading.Thread(target=self._run_duplicate_detect, args=(job_id, mount_registry, targets), daemon=True)
        with self._lock:
            self._threads[job_id] = t
        t.start()
        return job_id

    def _run_duplicate_detect(self, job_id: str, mount_registry: dict, targets: list[str]):
        from media_indexer.duplicates import DuplicateDetector
        from media_indexer.database import db_instance

        should_stop = self._should_stop_factory(job_id)
        job = mysql_db_instance.get_job(job_id) or {}
        processed = job.get("processed_items", 0) or 0
        checkpoint = job.get("checkpoint")

        # Resume past mounts already completed in a prior run of this job.
        start_index = targets.index(checkpoint) + 1 if checkpoint in targets else 0

        try:
            for name in targets[start_index:]:
                if should_stop():
                    mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=checkpoint, processed_items=processed)
                    return
                if not resource_gate.wait_until_free(should_stop=should_stop):
                    mysql_db_instance.update_job(job_id, status="PAUSED", checkpoint=checkpoint, processed_items=processed)
                    return

                mnt = mount_registry[name]
                detector = DuplicateDetector(
                    qdrant_client=db_instance.client,
                    collection_name=db_instance.collection_name,
                    similarity_threshold=settings.duplicates.similarity_threshold,
                    media_type=mnt.media_type,
                )
                detector.detect_for_mount(name, mnt.path)
                processed += 1
                checkpoint = name
                mysql_db_instance.update_job(job_id, status="RUNNING", checkpoint=checkpoint, processed_items=processed)

            mysql_db_instance.update_job(job_id, status="COMPLETED", processed_items=processed, requested_status=None)
        except Exception as e:
            logger.error(f"Duplicate detect job {job_id} crashed: {e}", exc_info=True)
            mysql_db_instance.update_job(job_id, status="FAILED", last_error=str(e))
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    # -- control -----------------------------------------------------------
    def pause(self, job_id: str) -> bool:
        return mysql_db_instance.request_job_status(job_id, "PAUSED")

    def resume(self, job_id: str, mount_registry: Optional[dict] = None) -> str:
        job = mysql_db_instance.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown job {job_id}")
        if self._is_alive(job_id):
            mysql_db_instance.request_job_status(job_id, None)
            return job_id
        if job["job_type"] == "llm_parse":
            return self.start_llm_parse_job(job.get("mount_name"))
        return self.start_duplicate_detect_job(mount_registry or {}, job.get("mount_name"))

    def cancel(self, job_id: str) -> bool:
        return mysql_db_instance.request_job_status(job_id, "CANCELLED")

    def status(self, job_id: str) -> Optional[dict]:
        return mysql_db_instance.get_job(job_id)

    def list_jobs(self) -> list[dict]:
        return mysql_db_instance.list_jobs()


job_manager = BackgroundJobManager()