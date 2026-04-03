#!/usr/bin/env python3
"""Worker node entry point for distributed LM Arena Automation.

Connects to a coordinator server and executes browser automation tasks
on this machine. Each node manages its own Playwright browsers and
ArenaWorker instances.

Usage:
    python worker_node.py \\
        --coordinator ws://192.168.1.10:8001/node-ws \\
        --node-id worker-1 \\
        --max-workers 8 \\
        --token "shared-secret"

Environment variables:
    LM_ARENA_NODE_TOKEN   Auth token (alternative to --token)
    LM_ARENA_COORDINATOR  Coordinator URL (alternative to --coordinator)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.distributed.node_client import NodeClient
from src.distributed.protocol import NodeDisplayInfo

logger = logging.getLogger("worker_node")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LM Arena Automation — Worker Node",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--coordinator",
        default=os.environ.get("LM_ARENA_COORDINATOR", "ws://localhost:8001/node-ws"),
        help="Coordinator WebSocket URL (default: ws://localhost:8001/node-ws)",
    )
    parser.add_argument(
        "--node-id",
        default=os.environ.get("HOSTNAME", f"node-{os.getpid()}"),
        help="Unique identifier for this node (default: hostname or PID)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=12,
        help="Maximum concurrent browser windows (default: 12)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LM_ARENA_NODE_TOKEN", ""),
        help="Authentication token for coordinator connection",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run browsers in headless mode",
    )
    # Display settings
    parser.add_argument(
        "--display-monitors",
        type=int,
        default=1,
        help="Number of monitors (default: 1)",
    )
    parser.add_argument(
        "--display-width",
        type=int,
        default=1920,
        help="Monitor width in pixels (default: 1920)",
    )
    parser.add_argument(
        "--display-height",
        type=int,
        default=1080,
        help="Monitor height in pixels (default: 1080)",
    )
    parser.add_argument(
        "--taskbar-height",
        type=int,
        default=40,
        help="Taskbar height in pixels (default: 40)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    display = NodeDisplayInfo(
        monitor_count=args.display_monitors,
        monitor_width=args.display_width,
        monitor_height=args.display_height,
        taskbar_height=args.taskbar_height,
    )

    client = NodeClient(
        coordinator_url=args.coordinator,
        node_id=args.node_id,
        max_workers=args.max_workers,
        auth_token=args.token,
        display=display,
        headless=args.headless,
    )

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    # add_signal_handler is not supported on Windows
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

    # Run client in background, wait for shutdown signal
    client_task = asyncio.ensure_future(client.start())

    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        pass

    await client.stop()

    if not client_task.done():
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    asyncio.run(main(args))
