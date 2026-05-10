"""Allow running as `python -m mas_agent`."""
import asyncio
from mas_agent.cli import main

raise SystemExit(asyncio.run(main()))
