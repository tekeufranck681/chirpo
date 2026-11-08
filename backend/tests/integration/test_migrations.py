"""The migrations must match the models and be reversible."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration


async def test_models_and_migrations_are_in_sync(alembic_cfg: Config) -> None:
    # Fails if a model was changed without generating a migration (`alembic check`).
    await asyncio.to_thread(command.check, alembic_cfg)


async def test_downgrade_to_empty_and_upgrade_again(alembic_cfg: Config) -> None:
    await asyncio.to_thread(command.downgrade, alembic_cfg, "base")
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
