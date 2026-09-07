"""Compatibility helpers for Telegram peer IDs used by newer supergroups."""

from pyrogram import utils as pyrogram_utils


# Telegram channel IDs are represented as 64-bit values.  Older Pyrogram
# releases only accepted the 32-bit channel-ID range in ``get_peer_type``.
# Keep the original handling for users and legacy chats, while allowing the
# larger channel IDs emitted by current Telegram supergroups.
_MAX_TELEGRAM_CHANNEL_ID = (1 << 52) - 1


def patch_large_channel_ids() -> None:
    """Allow modern large supergroup IDs in Pyrogram peer resolution."""

    if getattr(pyrogram_utils, "_sakura_large_channel_ids", False):
        return

    original_get_peer_type = pyrogram_utils.get_peer_type

    def get_peer_type(peer_id: int) -> str:
        if peer_id < pyrogram_utils.MAX_CHANNEL_ID:
            channel_id = pyrogram_utils.MAX_CHANNEL_ID - peer_id
            if 0 < channel_id <= _MAX_TELEGRAM_CHANNEL_ID:
                return "channel"
        return original_get_peer_type(peer_id)

    pyrogram_utils.get_peer_type = get_peer_type
    pyrogram_utils._sakura_large_channel_ids = True
