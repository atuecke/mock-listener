import time
import asyncio
import argparse
import random
from pathlib import Path
from collections import deque
from http import HTTPStatus

import aiohttp
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

def format_status(code: int) -> Text:
    """
    Map an HTTP status code to a descriptive phrase and style:
      • 2xx → green (Success)
      • 3xx → cyan (Redirect)
      • 4xx → yellow (Client Error)
      • 5xx → red (Server Error)
      • else → white
    """
    # get standard phrase or fallback
    phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else ""
    if 200 <= code < 300:
        style = "green"
    elif 300 <= code < 400:
        style = "cyan"
    elif 400 <= code < 500:
        style = "yellow"
    elif 500 <= code < 600:
        style = "red"
    else:
        style = "white"
    return Text(f"{code} {phrase}", style=style)

async def producer(listener_id: str, interval: float, job_queue: asyncio.Queue):
    """
    Every `interval` seconds (after a random head-start), enqueue a job to send.
    """
    await asyncio.sleep(random.uniform(0, interval))  # stagger start
    while True:
        await job_queue.put(listener_id)
        await asyncio.sleep(interval)

async def consumer(job_queue: asyncio.Queue,
                   wav_path: Path,
                   endpoint: str,
                   session: aiohttp.ClientSession,
                   history: deque,
                   timestamps: deque):
    """
    Pull jobs off the queue, do the HTTP POST, and record results.
    """
    while True:
        listener_id = await job_queue.get()
        now = time.time()
        ts_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        filename = f"{listener_id}_{ts_str}.wav"

        form = aiohttp.FormData()
        form.add_field("listener_id", listener_id)
        form.add_field(
            "file",
            wav_path.open("rb"),
            filename=filename,
            content_type="audio/wav"
        )

        try:
            async with session.post(endpoint, data=form) as resp:
                status_text = format_status(resp.status)
        except Exception as e:
            # on network or other exception, mark bold red
            status_text = Text(f"ERR: {e}", style="bold red")

        # record for UI: store a Text object for rich styling
        history.appendleft((listener_id, filename, status_text, ts_str))
        timestamps.append(now)

        job_queue.task_done()

async def ui_updater(history: deque,
                     timestamps: deque,
                     interval: float,
                     job_queue: asyncio.Queue):
    """
    Refresh a Rich Live display showing:
      • Throughput in last `interval` seconds  
      • Pending jobs in queue  
      • Last 20 sends
    """
    with Live(refresh_per_second=4, vertical_overflow="visible") as live:
        while True:
            now = time.time()
            # slide window for throughput
            while timestamps and timestamps[0] < now - interval:
                timestamps.popleft()
            sent_count = len(timestamps)
            pending_count = job_queue.qsize()

            throughput = Text.assemble(
                ("Throughput: ", "bold"),
                (f"{sent_count}", "cyan"),
                (f" files in last {interval:.0f}s\n", "")
            )
            pending = Text.assemble(
                ("Pending in queue: ", "bold"),
                (str(pending_count), "yellow"),
                (" jobs\n", "")
            )

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Listener", width=10)
            table.add_column("Filename", width=32)
            table.add_column("Status", justify="left", width=8)
            table.add_column("When (UTC)", width=16)
            for lid, fn, status_text, ts in history:
                # status_text is a Rich Text object with color/style
                table.add_row(lid, fn, status_text, ts)

            live.update(Group(throughput, pending, table))
            await asyncio.sleep(0.25)

async def main():
    parser = argparse.ArgumentParser(
        description="Stress-test an HTTP endpoint with staggered uploads."
    )
    parser.add_argument("-ip", "--ip",
                        help="IP of the HTTP endpoint (ex. localhost:8000)",
                        type=str,
                        default="localhost")
    parser.add_argument("-f", "--file",
                        dest="file",
                        help="Path to the recording to send",
                        type=str,
                        default="./wav_samples/sample.wav")
    parser.add_argument("-n", "--num-sources",
                        type=int,
                        default=1,
                        help="How many concurrent sources to simulate")
    parser.add_argument("-i", "--interval",
                        type=float,
                        default=20.0,
                        help="Interval in seconds between each send per source")
    parser.add_argument("-url", "--url",
                        help="A full url to post to. this overrides the ip",
                        type=str,
                        default=None)
    args = parser.parse_args()

    endpoint = f"http://{args.ip}:8000/recording_endpoint"
    if args.url:
        endpoint = args.url

    wav_path = Path(args.file)
    if not wav_path.is_file():
        print("ERROR: file not found:", wav_path)
        return

    # shared state
    history = deque(maxlen=10)   # last 10 sends
    timestamps = deque()         # for throughput calc
    job_queue = asyncio.Queue()  # backlog of pending sends

    listener_ids = [f"listener{id:02d}" for id in range(1, args.num_sources + 1)]

    async with aiohttp.ClientSession() as session:
        # start producers
        producers = [
            asyncio.create_task(producer(lid, args.interval, job_queue))
            for lid in listener_ids
        ]
        # start one consumer per source
        consumers = [
            asyncio.create_task(
                consumer(job_queue, wav_path, endpoint, session, history, timestamps)
            )
            for _ in listener_ids
        ]
        # start UI
        ui_task = asyncio.create_task(ui_updater(history, timestamps, args.interval, job_queue))

        try:
            await asyncio.gather(*producers, *consumers, ui_task)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")
