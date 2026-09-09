from datetime import datetime


from bot import LOGGER, partition_libs
from bot.func_helper.emby import emby
from bot.func_helper.concurrency import get_user_lock
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_partition import (
    sql_get_partition_code,
    sql_get_redeemed_partition_grant,
    sql_redeem_partition_code_atomic,
)


async def _redeem_partition_code(code: str, tg_id: int):
    async with get_user_lock(tg_id):
        return await _redeem_partition_code_locked(code, tg_id)


async def _redeem_partition_code_locked(code: str, tg_id: int):
    now = datetime.now()

    record = sql_get_partition_code(code)
    previous = None if record else sql_get_redeemed_partition_grant(code, tg_id, now)
    if not record and not previous:
        return False, "❌ 分区码无效。"

    partition = record.partition if record else previous.partition
    libs = partition_libs.get(partition, []) if partition_libs else []
    if not libs:
        LOGGER.warning("分区码对应分区未配置库: %s", partition)
        return False, "⚠️ 分区未配置库，请联系管理员。"

    emby_row = sql_get_emby(tg=tg_id)
    if not emby_row or not emby_row.embyid:
        return False, "⚠️ 未找到您的 Emby 账户，请先完成注册绑定。"

    if record:
        ok, partition, expires_at = sql_redeem_partition_code_atomic(
            code=code,
            tg=tg_id,
            embyid=emby_row.embyid,
            embyname=emby_row.name,
            now=now,
        )
        if not ok or not partition or not expires_at:
            return False, "❌ 分区码无效或已被使用。"
    else:
        # Retry a persisted grant without spending the code or extending it again.
        expires_at = previous.expires_at

    if not await emby.show_folders_by_names(emby_row.embyid, libs):
        return False, "⚠️ 分区授权已保存，但 Emby 权限更新失败，请使用同一分区码重试。"
    libs_text = "、".join(libs)
    return (
        True,
        f"✅ 已激活分区 {partition}\n"
        f"已激活媒体库：{libs_text}\n"
        f"可访问至：{expires_at:%Y-%m-%d %H:%M:%S}",
    )
