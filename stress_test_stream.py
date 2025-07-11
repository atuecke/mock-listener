#!/usr/bin/env python3
"""
Persistent-connection mock sender for the streaming HTTP receiver.
One POST per listener, infinite chunked body.

usage:
    python mock_stream_listener.py -f sample.wav -n 4 -i 20 \
           -u http://localhost:8000/stream
"""
import argparse, asyncio, struct, time, wave, random
from pathlib import Path
from collections import deque
from http import HTTPStatus

import aiohttp
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, BarColumn, TaskProgressColumn, TimeElapsedColumn

PAGE     = 32_768         # 32 KiB
HDR_LEN  = 44             # standard RIFF/WAVE header size
SEQ_META = 0xFFFFFF       # reserved sequence for metadata frames

# ─────────────────────────────────────────────────────────────────────────────
# Rich helpers
# ─────────────────────────────────────────────────────────────────────────────
def fmt_status(code: int) -> Text:
    colour = ("green" if 200 <= code < 300 else
              "cyan"  if 300 <= code < 400 else
              "yellow" if 400 <= code < 500 else
              "red")
    phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else ""
    return Text(f"{code} {phrase}", style=colour)

# ─────────────────────────────────────────────────────────────────────────────
# Frame helpers
# ─────────────────────────────────────────────────────────────────────────────
def make_frame(seq: int, payload: bytes) -> bytes:
    """Return 5-B header + payload."""
    return struct.pack("<I", seq)[:3] + struct.pack("<H", len(payload)) + payload

# ─────────────────────────────────────────────────────────────────────────────
# Streaming coroutine – one per listener
# ─────────────────────────────────────────────────────────────────────────────
async def streamer(listener_id: str,
                   wav_bytes: bytes,
                   page_dur: float,
                   interval_s: float,
                   stagger_s: float,
                   endpoint: str,
                   progress: Progress,
                   task_id: int,
                   dashboard_hist: deque,
                   session: aiohttp.ClientSession):
    """
    Maintain a single HTTP POST with chunked body; send the WAV header +
    32 KiB pages; sleep `interval_s`; then restart seq=0 and send header again.
    """
    header = wav_bytes[:HDR_LEN]
    pcm    = memoryview(wav_bytes[HDR_LEN:])

    if stagger_s > 0:
       await asyncio.sleep(random.uniform(0, stagger_s))

    total_pages = (len(pcm) + PAGE - 1) // PAGE

    # Shared state between outer scope and inner generator
    seq        = 0
    last_reset = time.time()

    async def body_gen():
        nonlocal seq, last_reset
        while True:
            # 1) WAV header frame
            yield make_frame(0, header)
            seq = 1
            progress.reset(task_id, total=total_pages, completed=0)

            # 2) PCM pages
            for ofs in range(0, len(pcm), PAGE):
                payload = pcm[ofs:ofs + PAGE]
                yield make_frame(seq, payload)
                seq = (seq + 1) & 0xFFFFFF
                progress.update(task_id, advance=1)
                await asyncio.sleep(page_dur)

            ts_done = time.strftime("%H:%M:%S", time.gmtime())
            dashboard_hist.appendleft((listener_id,
                                        Text("DONE", style="green"),
                                        ts_done))

            # 3) Wait gap interval, then loop writes header again
            await asyncio.sleep(interval_s)
            last_reset = time.time()

    # Establish once – keep connection open
    start_ts = time.strftime("%H:%M:%S", time.gmtime())
    async with session.post(f"{endpoint}?listener_id={listener_id}",
                            data=body_gen()) as resp:
        dashboard_hist.appendleft((listener_id, fmt_status(resp.status), start_ts))
        await resp.text()            # consume 200 OK headers
        await resp.wait_for_close()  # should never return unless server drops

# ─────────────────────────────────────────────────────────────────────────────
# Dashboard updater
# ─────────────────────────────────────────────────────────────────────────────
async def dashboard_updater(history: deque,
                             progress: Progress):
    with Live(refresh_per_second=4, vertical_overflow="visible") as live:
        while True:
            tbl = Table(show_header=True, header_style="bold magenta", row_styles=["none", "dim"])
            tbl.add_column("Listener",  width=12)
            tbl.add_column("Status",    width=14)
            tbl.add_column("Start UTC", width=10)
            for lid, status_text, ts in history:
                tbl.add_row(lid, status_text, ts)

            live.update(Group(progress, tbl))   # pushes new state every 0.25 s
            await asyncio.sleep(0.25)

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default="sample.wav",
                    help="WAV file to loop-stream")
    ap.add_argument("-n", "--num-sources", type=int, default=1,
                    help="Number of concurrent listeners")
    ap.add_argument("-i", "--interval", type=float, default=5.0,
                    help="Seconds to wait after each full file before resetting seq")
    ap.add_argument("-s", "--stagger", type=float, default=5.0,
                    help="Random start-up offset per listener (seconds)")
    ap.add_argument("-u", "--url", default="http://localhost:8000/stream")
    args = ap.parse_args()

    wav_path = Path(args.file)
    if not wav_path.exists():
        raise SystemExit(f"WAV not found: {wav_path}")

    # derive pacing from WAV format
    with wave.open(str(wav_path), "rb") as wf:
        rate, chans, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    bytes_per_sec = rate * chans * width
    page_duration = PAGE / bytes_per_sec

    wav_bytes = wav_path.read_bytes()

    progress = Progress(
        "[progress.description]{task.fields[lid]}",
        BarColumn(bar_width=None),
        TaskProgressColumn(), TimeElapsedColumn(),
    )
    hist = deque(maxlen=12)

    # register tasks in Progress
    task_ids = {}
    for lid in (f"listener{idx:02d}" for idx in range(1, args.num_sources + 1)):
        task_ids[lid] = progress.add_task("", lid=lid, total=1)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=None)) as session:
        tasks = [
            asyncio.create_task(
                streamer(lid, wav_bytes, page_duration, args.interval, args.stagger,
                         args.url, progress, task_ids[lid], hist, session)
            )
            for lid in task_ids
        ]
        dash = asyncio.create_task(dashboard_updater(hist, progress))

        try:
            await asyncio.gather(*tasks, dash)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
