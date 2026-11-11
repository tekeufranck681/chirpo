"""Tidies the pending actions on a schedule (M5.4).

- A card nobody pressed in time expires, and its message says so (no buttons left to press).
- A run that was confirmed but never finished (the worker was killed mid-run) is failed after
  STUCK_AFTER, and its card says to check GitHub. The worker's job timeout is 5 minutes, so no
  run that is still alive is ever that old.
- The cut-offs compare the app's clock with the database's `updated_at`; a skew of a minute or
  two is nothing against 15 minutes and 7 days.
- Finished rows are deleted after KEEP_FINISHED: they hold the full text of issue bodies and
  comments, and the audit log keeps who did what.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from chirpo.core import messages
from chirpo.core.domain import SentMessage
from chirpo.core.pending import PendingAction
from chirpo.core.ports import MessagingAdapter, PendingActions

STUCK_AFTER = timedelta(minutes=15)
KEEP_FINISHED = timedelta(days=7)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Swept:
    expired: int
    failed: int
    purged: int


async def sweep(
    pending: PendingActions, messengers: Mapping[str, MessagingAdapter], now: datetime
) -> Swept:
    expired = await pending.expire_due(now)
    failed = await pending.fail_stuck(now - STUCK_AFTER)
    purged = await pending.purge(now - KEEP_FINISHED)
    await _tell(expired, messages.EXPIRED, messengers)
    await _tell(failed, messages.CRASHED, messengers)
    log.info("pending_swept expired=%d failed=%d purged=%d", len(expired), len(failed), purged)
    return Swept(len(expired), len(failed), purged)


async def _tell(
    rows: Iterable[PendingAction], text: str, messengers: Mapping[str, MessagingAdapter]
) -> None:
    """Edits each card, best effort: the row is already updated, and one message that can't be
    edited (deleted, chat gone, any adapter error) must not stop the others."""
    for row in rows:
        messaging = messengers.get(row.chat.platform)
        if messaging is None or row.message_id is None:
            continue
        card = SentMessage(chat=row.chat, message_id=row.message_id)
        try:
            await messaging.edit_message(card, text)
        except Exception as error:  # an adapter error of any kind: log it, tell the others
            log.warning("pending_card_not_edited error=%s", type(error).__name__, exc_info=True)
