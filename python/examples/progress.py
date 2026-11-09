"""Show live progress while a conversion runs: overall percent, time left,
queue position, and what each page is doing right now.

    export IMAGE2PPT_API_KEY=i2p_live_your_key
    uv run --with image2ppt python progress.py slide1.png slide2.png

Set IMAGE2PPT_LANGUAGE=zh-CN to get the step text in Chinese (English by default).
Building a progress bar in your own web page instead? Keep the API key on your
server: submit there, have the page ask your server every few seconds, and answer
it from client.get_job(job_id) — the same fields shown below.
"""

import os
import sys

from image2ppt import Image2PPTClient, Image2PPTError, Job


def show(job: Job, elapsed: float) -> None:
    """Called after every status poll with the fresh job and seconds waited."""
    line = f"[{elapsed:4.0f}s] {job.progress}%"
    if job.queue_ahead:
        line += f", {job.queue_ahead} job(s) ahead in the queue"
    if job.estimated_remaining_seconds is not None:
        line += f", about {job.estimated_remaining_seconds}s left"
    print(line)
    for page in job.page_progress or []:
        # step_label is display text: show it as-is, never branch on it.
        print(f"    page {page.page_number}: {page.progress}%  {page.step_label or ''}")


def main() -> int:
    api_key = os.environ.get("IMAGE2PPT_API_KEY")
    paths = sys.argv[1:]
    if not api_key or not paths:
        print("set IMAGE2PPT_API_KEY and pass file paths", file=sys.stderr)
        return 2

    client = Image2PPTClient(
        api_key=api_key, accept_language=os.environ.get("IMAGE2PPT_LANGUAGE")
    )
    try:
        job = client.convert(paths, dest_path="out.pptx", on_progress=show)
    except Image2PPTError as err:
        print(f"failed: {err}", file=sys.stderr)
        return 1
    print(f"wrote out.pptx — credits used: {job.credits_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
