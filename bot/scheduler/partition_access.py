from datetime import datetime

from bot import partition_libs, bot, LOGGER
from bot.func_helper.concurrency import get_user_lock
from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_partition import (
    sql_get_active_grants_by_user,
    sql_get_expired_grants,
    sql_mark_grants_expired,
)


async def check_partition_access():
    """Revoke expired libraries, retaining failed work for the next check."""
    if not partition_libs:
        return
    expired = sql_get_expired_grants(datetime.now())
    user_ids = sorted({grant.tg for grant in expired})
    for tg_id in user_ids:
        async with get_user_lock(tg_id):
            now = datetime.now()
            grants = sql_get_expired_grants(now, tg=tg_id)
            if not grants:
                continue
            emby_row = sql_get_emby(tg=tg_id)
            if not emby_row or not emby_row.embyid:
                continue
            active = sql_get_active_grants_by_user(tg_id, now)
            keep_libs = {lib for grant in active for lib in partition_libs.get(grant.partition, [])}

            # A missing mapping cannot prove that its old libraries were revoked.
            known_grants = [grant for grant in grants if partition_libs.get(grant.partition)]
            if len(known_grants) != len(grants):
                LOGGER.warning('分区撤权保留待处理记录 tg=%s：分区媒体库配置缺失', tg_id)
            if not known_grants:
                continue
            revoke_libs = {lib for grant in known_grants for lib in partition_libs[grant.partition]}
            hide_targets = sorted(revoke_libs - keep_libs)
            if hide_targets and not await emby.hide_folders_by_names(emby_row.embyid, hide_targets):
                LOGGER.warning('分区撤权失败 tg=%s，将在下次检查重试', tg_id)
                continue
            if not sql_mark_grants_expired([grant.id for grant in known_grants], now=now):
                LOGGER.warning('分区撤权记录更新失败 tg=%s，将在下次检查重试', tg_id)
                continue
            parts_text = '、'.join(sorted({grant.partition for grant in known_grants}))
            notice = f'分区授权到期提醒\n到期分区：{parts_text}\n'
            if hide_targets:
                notice += '已禁用媒体库：' + '、'.join(hide_targets)
            else:
                notice += '媒体库仍被其他有效分区覆盖。'
        try:
            await bot.send_message(tg_id, notice)
        except Exception as exc:
            LOGGER.warning('分区到期通知发送失败 tg=%s: %s', tg_id, exc)
