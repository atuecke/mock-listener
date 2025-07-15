import argparse, asyncio, struct, time, wave, random, sys
from pathlib import Path
from collections import deque
from http import HTTPStatus
import aiohttp
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, BarColumn, TaskProgressColumn, TimeElapsedColumn

PAGE = 32_768            # 32 KiB payload per frame
SEQ_META = 0xFFFFFF       # reserved for future metadata frames

# ─────────────────────────────────────────────────────────────────────────────
def fmt_status(code: int) -> Text:
    colour = ("green" if 200 <= code < 300 else
              "cyan"  if 300 <= code < 400 else
              "yellow" if 400 <= code < 500 else
              "red")
    phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else ""
    return Text(f"{code} {phrase}", style=colour)

def make_frame(seq: int, payload: bytes) -> bytes:
    """Return 5‑byte frame header + payload."""
    return struct.pack("<I", seq)[:3] + struct.pack("<H", len(payload)) + payload
# ─────────────────────────────────────────────────────────────────────────────


def split_wav(wav_bytes: bytes) -> tuple[bytes, memoryview]:
    """
    Return (full_header, pcm_view).
    Header ≡ everything up through `"data"` + 4‑byte size field.
    """
    idx = wav_bytes.find(b"data")
    if idx < 0:
        raise ValueError("no 'data' tag found in WAV")
    header_end = idx + 8           # include 'data' + size field
    return wav_bytes[:header_end], memoryview(wav_bytes[header_end:])


async def streamer(listener_id: str,
                   full_header: bytes,
                   pcm: memoryview,
                   page_dur: float,
                   interval_s: float,
                   stagger_s: float,
                   endpoint: str,
                   progress: Progress,
                   task_id: int,
                   dashboard_hist: deque,
                   session: aiohttp.ClientSession):
    """Maintain one long‑lived HTTP POST; restart seq=0 after each interval."""
    if stagger_s > 0:
        await asyncio.sleep(random.uniform(0, stagger_s))

    total_pages = (len(pcm) + PAGE - 1) // PAGE

    async def body_gen():
        seq = 0
        while True:
            # header frame
            yield make_frame(0, full_header)
            seq = 1
            progress.reset(task_id, total=total_pages, completed=0)

            # PCM pages
            for ofs in range(0, len(pcm), PAGE):
                payload = pcm[ofs:ofs + PAGE]
                yield make_frame(seq, payload)
                seq = (seq + 1) & 0xFFFFFF
                progress.update(task_id, advance=1)
                await asyncio.sleep(page_dur)

            # mark DONE in UI
            dashboard_hist.appendleft((listener_id, Text("DONE", style="green"),
                                        time.strftime("%H:%M:%S", time.gmtime())))
            await asyncio.sleep(interval_s)

    start_ts = time.strftime("%H:%M:%S", time.gmtime())
    async with session.post(f"{endpoint}?listener_id={listener_id}",
                            data=body_gen()) as resp:
        dashboard_hist.appendleft((listener_id, fmt_status(resp.status), start_ts))
        await resp.text()
        await resp.wait_for_close()


async def dashboard_updater(history: deque, progress: Progress):
    with Live(refresh_per_second=4, vertical_overflow="visible") as live:
        while True:
            tbl = Table(show_header=True, header_style="bold magenta",
                        row_styles=["none", "dim"])
            tbl.add_column("Listener",  width=12)
            tbl.add_column("Status",    width=14)
            tbl.add_column("Time UTC",  width=10)
            for lid, status_text, ts in history:
                tbl.add_row(lid, status_text, ts)
            live.update(Group(progress, tbl))
            await asyncio.sleep(0.25)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default="sample.wav",
                    help="WAV file to loop‑stream")
    ap.add_argument("-n", "--num-sources", type=int, default=1,
                    help="Number of concurrent listeners")
    ap.add_argument("-i", "--interval", type=float, default=20.0,
                    help="Seconds to wait after each full file before restarting seq=0")
    ap.add_argument("-s", "--stagger", type=float, default=5.0,
                    help="Random start offset per listener")
    ap.add_argument("-u", "--url", default="http://localhost:8000/stream")
    args = ap.parse_args()

    wav_path = Path(args.file)
    if not wav_path.exists():
        sys.exit(f"WAV not found: {wav_path}")

    wav_bytes = wav_path.read_bytes()
    full_header, pcm = split_wav(wav_bytes)

    # pacing derived from the fmt chunk (via wave module)
    with wave.open(str(wav_path), "rb") as wf:
        rate, chans, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    bytes_per_sec = rate * chans * width
    page_dur = PAGE / bytes_per_sec

    progress = Progress(
        "[progress.description]{task.fields[lid]}",
        BarColumn(None), TaskProgressColumn(), TimeElapsedColumn(),
    )
    hist = deque(maxlen=12)
    task_ids = {
        lid: progress.add_task("", lid=lid, total=1)
        for lid in (f"listener{idx:02d}" for idx in range(1, args.num_sources + 1))
    }

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=None)) as session:
        stream_tasks = [
            asyncio.create_task(
                streamer(lid, full_header, pcm, page_dur, args.interval,
                         args.stagger, args.url, progress, task_ids[lid], hist, session)
            )
            for lid in task_ids
        ]
        ui_task = asyncio.create_task(dashboard_updater(hist, progress))

        try:
            await asyncio.gather(*stream_tasks, ui_task)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user")