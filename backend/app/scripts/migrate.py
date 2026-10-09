"""One-off release job: `python -m app.scripts.migrate`.

Applies pending Alembic migrations, then creates the LangGraph checkpoint tables - the two schema changes
that must happen exactly once per release, BEFORE the API and worker containers start (they `depends_on` this
job completing). Running them in every container's CMD (the old behaviour) races when several replicas boot
together. Exits non-zero on failure so the orchestrator blocks the rollout instead of starting against an
unmigrated database.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parents[2]
logger = logging.getLogger("app.scripts.migrate")


async def _warm_checkpointer() -> None:
    from app.ai.scorecard_builder import get_graph_manager

    manager = get_graph_manager()
    await manager.get_compiled_graph()
    await manager.aclose()


def main() -> int:
    from alembic import command
    from alembic.config import Config
    from app.logging_config import configure_logging

    configure_logging()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    logger.info("Applying Alembic migrations.")
    command.upgrade(cfg, "head")
    logger.info("Creating LangGraph checkpoint tables.")
    asyncio.run(_warm_checkpointer())
    logger.info("Migrations complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
