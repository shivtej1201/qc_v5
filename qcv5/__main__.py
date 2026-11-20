"""``python -m qcv5`` starts the web app (dashboard + API) on http://127.0.0.1:8002."""
import argparse
import asyncio
import sys


def main() -> None:
    p = argparse.ArgumentParser(prog="qcv5", description="QC V5 web app (dashboard + API)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8002)
    a = p.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn
    uvicorn.run("qcv5.api.server:app", host=a.host, port=a.port, loop="asyncio")


if __name__ == "__main__":
    main()
