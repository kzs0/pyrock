"""A background worker: one source, an operation per job, a thread pool.

Run it::

    python examples/worker.py                  # processes JOBS jobs (default 20), then exits
    BEDROCK_LOG_CANONICAL=true python examples/worker.py

The source ``background.worker`` prefixes every operation opened beneath it,
so ``process_job`` is recorded as ``background.worker.process_job`` with the
metrics ``background_worker_process_job_{count,successes,failures,duration_ms}``
labelled by ``worker_type``. Jobs run on a :class:`pyrock.ContextThreadPoolExecutor`,
which copies the caller's context (instance, source) into every submitted
call; a plain ``ThreadPoolExecutor`` would run them against the noop on most
interpreters. One job in seven fails, to show failure accounting. The final
metrics are written to stdout before shutdown.
"""

from __future__ import annotations

import os
import random
import sys
import time

import pyrock
from pyrock.metric import prometheus


class JobError(Exception):
    """A simulated job failure."""


@pyrock.operation("process_job", metric_labels=["job.kind"])
def process_job(job_id: int) -> None:
    kind = "resize" if job_id % 2 else "thumbnail"
    pyrock.register({"job.id": job_id, "job.kind": kind})  # job.id: traced/logged, not a label
    with pyrock.step("fetch_input"):
        time.sleep(random.uniform(0.001, 0.01))  # noqa: S311 - demo jitter, not crypto
    if job_id % 7 == 0:
        raise JobError(f"job {job_id} failed")  # escapes: failure metric + error span
    pyrock.debug("job done", {"job.id": job_id})


def run(jobs: int) -> None:
    with (
        pyrock.init(service="example-worker", server_enabled=False) as bedrock,
        pyrock.source(
            "background.worker",
            attrs={"worker.type": "pool"},
            metric_labels=["worker.type"],
        ) as src,
        pyrock.ContextThreadPoolExecutor(max_workers=4) as pool,
    ):
        futures = []
        for job_id in range(1, jobs + 1):
            src.sum("jobs_received")
            futures.append(pool.submit(process_job, job_id))
        failed = 0
        for future in futures:
            if future.exception() is not None:
                failed += 1
        src.gauge("last_batch_failed", failed)
        pyrock.info("batch finished", jobs=jobs, failed=failed)
        sys.stdout.write(prometheus.encode(bedrock.metrics).decode())


if __name__ == "__main__":
    run(int(os.environ.get("JOBS", "20")))
