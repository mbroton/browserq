import asyncio
import logging
import time
import random
import string

from playwright.async_api import (
    async_playwright,
    PlaywrightContextManager,
    Browser,
)
import playwright._impl._errors
import playwright.async_api

from app import jobs, database

_JOB_POLL_INTERVAL = 5
_HEARTBEAT_LOG_INTERVAL = 600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("master")


async def worker_loop(
    pw_ctx: PlaywrightContextManager,
    db: database.AsyncConnection,
    name: str,
    jobs_defs: dict[str, type[jobs.BaseJob]],
) -> None:
    log = logging.getLogger(name)
    log.info("Ready to accept jobs.")
    shutdown = False
    last_heartbeat = time.monotonic()
    browser: Browser | None = None
    current_job_task: asyncio.Task | None = None

    try:
        browser = await pw_ctx.chromium.launch(headless=True)

        while not shutdown:
            timeref = time.monotonic()

            try:
                job = await database.get_next_job(db, worker=name)
                if not job:
                    if timeref - last_heartbeat >= _HEARTBEAT_LOG_INTERVAL:
                        log.info("Worker is alive and polling for jobs")
                        last_heartbeat = timeref

                    await asyncio.sleep(_JOB_POLL_INTERVAL)
                    continue

                last_heartbeat = timeref
                log.info(f"Starting job {job.name!r} (ID: {job.id})")

                try:
                    async with await browser.new_context() as ctx:
                        async with await ctx.new_page() as page:
                            current_job_task = asyncio.create_task(
                                jobs_defs[job.name](**job.input).execute(page)
                            )
                            output = await current_job_task

                except (asyncio.CancelledError, playwright.async_api.Error):
                    log.error(f"Job interrupted, marking {job.id} as failed.")
                    await _cancel_task(current_job_task)
                    job.status = jobs.JobStatus.FAILED
                    await database.update_job_status(
                        db, job.id, job.status, None
                    )
                    shutdown = True
                    break

                except Exception:
                    log.exception("Job execution failed.")
                    job.status = jobs.JobStatus.FAILED

                else:
                    log.info(f"Job {job.id} is done.")
                    job.status = jobs.JobStatus.DONE

                output = output if job.status == jobs.JobStatus.DONE else None
                await database.update_job_status(db, job.id, job.status, output)

            except asyncio.CancelledError:
                log.info("Shutting down worker (no jobs interrupted).")
                shutdown = True
                break

    finally:
        await _cancel_task(current_job_task, log=log)

        if browser:
            await _shutdown_browser(browser, log=log)


async def _cancel_task(
    task: asyncio.Task | None, log: logging.Logger | None = None
) -> None:
    log = log or logger
    if task and not task.done():
        task.cancel()
        try:
            await task
        except Exception as e:
            log.debug(f"Canceled task clean up error: {e!r}")


async def _shutdown_browser(
    browser: Browser, timeout: float = 5.0, log: logging.Logger | None = None
) -> None:
    log = log or logger
    try:
        await asyncio.wait_for(browser.close(), timeout=timeout)
    except (asyncio.TimeoutError, playwright.async_api.Error) as e:
        log.warning(f"Failed to close browser gracefully: {e!r}")


def _get_random_chars(length: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


async def start_worker(
    name: str, jobs_defs: dict[str, type[jobs.BaseJob]]
) -> None:
    conn = await database.create_connection()
    try:
        async with async_playwright() as p:
            await worker_loop(pw_ctx=p, db=conn, name=name, jobs_defs=jobs_defs)
    finally:
        await conn.close()


async def main() -> None:
    jobs_defs = jobs.collect_jobs_defs()

    await start_worker(
        name=f"worker_{_get_random_chars(8)}", jobs_defs=jobs_defs
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
