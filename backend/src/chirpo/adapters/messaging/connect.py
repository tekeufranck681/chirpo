"""Connecting the configured messaging adapters, with retries while a platform is unreachable."""

import asyncio

from chirpo.config import Settings
from chirpo.core.errors import MessagingError
from chirpo.core.ports import MessagingAdapter
from chirpo.logging import get_logger

CONNECT_ATTEMPTS = 5

log = get_logger(__name__)


async def connect_messaging(settings: Settings) -> dict[str, MessagingAdapter]:
    """Adapters by platform. Platforms without credentials are skipped, not errors."""
    if settings.telegram_bot_token is None:
        log.warning("telegram_not_configured", hint="set CHIRPO_TELEGRAM_BOT_TOKEN")
        return {}
    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter

    token = settings.telegram_bot_token.get_secret_value()
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            telegram = await TelegramAdapter.connect(token)
        except MessagingError as error:
            if attempt == CONNECT_ATTEMPTS:
                raise
            log.warning("telegram_connect_failed", error=str(error), attempt=attempt)
            await asyncio.sleep(2**attempt)
        else:
            return {telegram.platform: telegram}
    raise AssertionError("unreachable")
