"""Stream the microphone to WS /v1/dictations/live and print each final as it arrives.

    uv run python scripts/live_mic.py --book "My Book" --chapter 1 --page 1
    uv run python scripts/live_mic.py --book "My Book" --type manuscript --language hi

Press Ctrl+C to stop: the server transcribes whatever is still buffered, then says done.
"""

import argparse
import asyncio
import json
import signal

import sounddevice as sd
import websockets

SAMPLE_RATE = 16_000
BLOCK = 1_600  # 100 ms of audio per WebSocket frame


async def main(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    audio: asyncio.Queue[bytes] = asyncio.Queue()

    def on_audio(data: bytes, frames: int, time: object, status: object) -> None:
        loop.call_soon_threadsafe(audio.put_nowait, bytes(data))

    start = {
        "type": "start",
        "language": args.language,
        "book_name": args.book,
        "dictation_type": args.type,
        "chapter": args.chapter,
        "page_number": args.page,
        "partials": args.partials,
    }
    url = f"{args.url}/v1/dictations/live"
    async with websockets.connect(url, additional_headers={"author-id": args.author}) as ws:
        await ws.send(json.dumps(start))
        print(json.loads(await ws.recv()))

        async def send_audio() -> None:
            while True:
                await ws.send(await audio.get())

        async def print_messages() -> None:
            async for raw in ws:
                message = json.loads(raw)
                if message["type"] == "partial":  # redraw: stable words normal, tentative dimmed
                    stable, tentative = message.get("stable", ""), message.get("tentative", "")
                    gap = " " if stable and tentative else ""
                    line = f"{stable}{gap}\033[2m{tentative}\033[0m"
                    print(f"\r\033[K{line}", end="", flush=True)
                elif message["type"] == "final":
                    print(f"\r\033[K{message['text']}")
                else:
                    print(message)
                if message["type"] in ("done", "error"):
                    return

        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, stop.set)
        printer = asyncio.create_task(print_messages())
        sender = asyncio.create_task(send_audio())
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1, dtype="int16", callback=on_audio
        ):
            print("listening… Ctrl+C to stop")
            stopped = asyncio.create_task(stop.wait())
            await asyncio.wait({printer, stopped}, return_when=asyncio.FIRST_COMPLETED)
        sender.cancel()
        if not printer.done():  # Ctrl+C: ask the server to finish the buffered tail
            await ws.send(json.dumps({"type": "stop"}))
            await printer


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://127.0.0.1:8000")
    parser.add_argument("--author", default="jatin_01")
    parser.add_argument("--book", required=True)
    parser.add_argument("--type", default="chapter", choices=["chapter", "laps", "manuscript"])
    parser.add_argument("--chapter", type=int)
    parser.add_argument("--page", type=int)
    parser.add_argument("--language", default="en", choices=["en", "hi", "hinglish"])
    parser.add_argument("--partials", action="store_true", help="show words while speaking")
    asyncio.run(main(parser.parse_args()))
