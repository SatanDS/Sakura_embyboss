"""
定时检测账户有无过期
"""
from datetime import timedelta, datetime

from pyrogram.errors import FloodWait
from sqlalchemy import and_
from sqlalchemy.exc import OperationalError
from asyncio import sleep
from bot import bot, group, LOGGER, _open, config
from bot.func_helper.emby import emby
from bot.func_helper.utils import tem_deluser
from bot.sql_helper.sql_emby import Emby, get_all_emby, sql_update_emby, sql_managed_entitlement
from bot.sql_helper.sql_emby2 import get_all_emby2, Emby2, sql_update_emby2


def _managed_or_none(tg):
    """Treat pre-payment installations without ledger tables as legacy."""
    try:
        return sql_managed_entitlement(tg)
    except OperationalError as exc:
        if "no such table" in str(exc).lower() or "doesn't exist" in str(exc).lower():
            return None
        raise


async def reconcile_managed_accounts():
    """Keep remote policy and local projection aligned without replacing paid queues."""
    from bot.sql_helper import Session
    from bot.payments.entitlements import (AccountEntitlement, append_days, china_now,
                                           mark_block, resolve_entitlement, sync_projection)
    from bot.func_helper.concurrency import get_user_lock
    with Session() as session:
        accounts = session.query(AccountEntitlement.tg).all()
    for (tg,) in accounts:
        async with get_user_lock(tg):
            try:
                with Session.begin() as session:
                    row = session.query(Emby).filter(Emby.tg == tg).with_for_update().one_or_none()
                    if row is None or not row.embyid:
                        continue
                    now = china_now()
                    result = resolve_entitlement(session, row, now)
                    if result is None:
                        continue
                    if result.blocked_reason not in {None, "expiry"}:
                        continue
                    if not result.allowed:
                        # The account and its balances share the same lock and
                        # transaction as the new period. Paid time wins a race.
                        field, cost = None, 0
                        if row.us >= 30:
                            field, cost = "us", 30
                        elif _open.exchange and _open.exchange_cost > 0 and row.iv >= _open.exchange_cost:
                            field, cost = "iv", _open.exchange_cost
                        if field:
                            state = session.query(AccountEntitlement).filter_by(tg=tg).one()
                            state.blocked_reason = "expiry"
                            source = f"auto-renew:{tg}:{result.final_expiry.isoformat() if result.final_expiry else 'empty'}"
                            append_days(session, row, 30, source, now)
                            setattr(row, field, getattr(row, field) - cost)
                            result = resolve_entitlement(session, row, now)
                    if result.allowed:
                        # A previous failed remote enable remains retryable.
                        if not await emby.emby_change_policy(emby_id=row.embyid, disable=False):
                            raise RuntimeError("Emby account activation failed")
                        sync_projection(session, row, now)
                    elif row.lv != 'c':
                        if not await emby.emby_change_policy(emby_id=row.embyid, disable=True):
                            raise RuntimeError("Emby account disable failed")
                        mark_block(session, row, "expiry", now)
            except Exception as exc:
                LOGGER.error(f"Managed account reconciliation failed for {tg}: {type(exc).__name__}")


async def check_expired():
    # Older isolated runners load this function without helper definitions.
    if 'reconcile_managed_accounts' in globals():
        await reconcile_managed_accounts()
    # 询问 到期时间的用户，判断有无积分，有则续期，无就禁用
    # Both normal and whitelist accounts are subscription-bound.  A whitelist
    # account must therefore enter the same expiry/auto-renew flow as a normal
    # account instead of bypassing expiry forever.
    rst = get_all_emby(
        and_(
            Emby.ex.isnot(None),
            Emby.ex < datetime.now(),
            Emby.lv.in_(('a', 'b')),
        )
    )
    if rst is None:
        return LOGGER.info('【到期检测】- 等级 a/b 无到期用户，跳过')
    ext = (datetime.now() + timedelta(days=30))

    # Older installations could have whitelist rows without an expiry because
    # whitelist used to mean "permanent". Revoke only the VIP flag for those
    # rows; the normal account remains available for renewal.
    legacy_whitelist = get_all_emby(and_(Emby.lv == 'a', Emby.ex.is_(None)))
    for legacy in legacy_whitelist or []:
        if _managed_or_none(legacy.tg) is not None:
            continue
        if sql_update_emby(Emby.tg == legacy.tg, lv='b'):
            LOGGER.warning(
                f'【白名单期限迁移】账户 {legacy.tg} 没有订阅到期时间，已撤销永久白名单'
            )
    for r in rst:
        if _managed_or_none(r.tg) is not None:
            continue
        if r.us >= 30:
            b = r.us - 30
            if sql_update_emby(Emby.tg == r.tg, ex=ext, us=b):
                text = f'【到期检测】\n#id{r.tg} 续期账户 [{r.name}](tg://user?id={r.tg})\n' \
                       f'在当前时间自动续期30天\n' \
                       f'📅实时到期：{ext.strftime("%Y-%m-%d %H:%M:%S")}'
                LOGGER.info(text)
            else:
                text = f'【到期检测】\n#id{r.tg} 续期账户 [{r.name}](tg://user?id={r.tg})\n' \
                       f'自动续期失败，请联系闺蜜（管理）'
                LOGGER.error(text)
            try:
                await bot.send_message(r.tg, text)
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                await bot.send_message(r.tg, text)
            except Exception as e:
                LOGGER.error(e)

        elif _open.exchange and r.iv >= _open.exchange_cost:
            b = r.iv - _open.exchange_cost
            if sql_update_emby(Emby.tg == r.tg, ex=ext, iv=b):
                text = f'【到期检测】\n#id{r.tg} 续期账户 [{r.name}](tg://user?id={r.tg})\n' \
                       f'在当前时间自动续期30天\n' \
                       f'📅实时到期: {ext.strftime("%Y-%m-%d %H:%M:%S")}'
                LOGGER.info(text)
            else:
                text = f'【到期检测】\n#id{r.tg} 续期账户 [{r.name}](tg://user?id={r.tg})\n续期失败，请联系闺蜜（管理）'
                LOGGER.error(text)
            try:
                await bot.send_message(r.tg, text)
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                await bot.send_message(r.tg, text)
            except Exception as e:
                LOGGER.error(e)

        else:
            if await emby.emby_change_policy(emby_id=r.embyid, disable=True):
                disabled_at = datetime.now()
                dead_day = disabled_at + timedelta(days=config.freeze_days)
                if sql_update_emby(Emby.tg == r.tg, lv='c', disabled_at=disabled_at,
                                   entitlement_block_reason='expiry'):
                    text = f'【到期检测】\n#id{r.tg} 到期禁用 [{r.name}](tg://user?id={r.tg})\n将为您封存至 {dead_day.strftime("%Y-%m-%d")}，请及时续期'
                    LOGGER.info(text)
                else:
                    text = f'【到期检测】\n#id{r.tg} 到期禁用 [{r.name}](tg://user?id={r.tg}) 已禁用，数据库写入失败'
                    LOGGER.warning(text)
            else:
                text = f'【到期检测】\n#id{r.tg} 到期禁用 [{r.name}](tg://user?id={r.tg}) embyapi操作失败'
                LOGGER.error(text)
            try:
                send = await bot.send_message(r.tg, text)
                await send.forward(group[0])
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                send = await bot.send_message(r.tg, text)
                await send.forward(group[0])
            except Exception as e:
                LOGGER.error(e)

    rsc = get_all_emby(and_(Emby.ex < datetime.now(), Emby.lv == 'c'))
    if rsc is None:
        return LOGGER.info('【到期检测】- 等级 c 无到期用户，跳过')
    for c in rsc:
        if _managed_or_none(c.tg) is not None:
            continue
        if c.us >= 30:
            c_us = c.us - 30
            if await emby.emby_change_policy(emby_id=c.embyid, disable=False):
                if sql_update_emby(Emby.tg == c.tg, lv='b', ex=ext, us=c_us):
                    text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg})\n' \
                           f'在当前时间自动续期30天\n📅实时到期: {ext.strftime("%Y-%m-%d %H:%M:%S")}'
                    LOGGER.info(text)
                else:
                    text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg}) 数据库写入失败，请联系管理'
                    LOGGER.warning(text)
            else:
                text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg}) embyapi操作失败'
                LOGGER.error(text)
            try:
                await bot.send_message(c.tg, text)
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                await bot.send_message(c.tg, text)
            except Exception as e:
                LOGGER.error(e)

        elif _open.exchange and c.iv >= _open.exchange_cost:
            c_iv = c.iv - _open.exchange_cost
            if await emby.emby_change_policy(emby_id=c.embyid, disable=False):
                if sql_update_emby(Emby.tg == c.tg, lv='b', ex=ext, iv=c_iv):
                    text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg})\n在当前时间自动续期30天\n📅实时到期：{ext.strftime("%Y-%m-%d %H:%M:%S")}'
                    LOGGER.info(text)
                else:
                    text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg}) 已禁用，数据库写入失败，请联系管理'
                    LOGGER.warning(text)
            else:
                text = f'【到期检测】\n#id{c.tg} 解封账户 [{c.name}](tg://user?id={c.tg}) embyapi操作失败，请联系管理'
                LOGGER.error(text)
            try:
                await bot.send_message(c.tg, text)
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                await bot.send_message(c.tg, text)
            except Exception as e:
                LOGGER.error(e)

        else:
            if c.disabled_at is None:
                LOGGER.warning('跳过自动删除账户 %s：没有可确认的禁用时间', c.tg)
                continue
            delete_day = c.disabled_at + timedelta(days=config.freeze_days)
            if datetime.now() < delete_day:
                continue
            if await emby.emby_del(emby_id=c.embyid):
                if sql_update_emby(Emby.embyid == c.embyid, embyid=None, name=None, pwd=None, pwd2=None, lv='d', cr=None,
                                   ex=None):
                    tem_deluser()
                text = f'【到期检测】\n#id{c.tg} 删除账户 [{c.name}](tg://user?id={c.tg})\n已冻结 {config.freeze_days} 天，执行清除任务。期待下次与你相遇'
                LOGGER.info(text)
            else:
                text = f'【到期检测】\n#id{c.tg} #删除账户 [{c.name}](tg://user?id={c.tg})\n到期删除失败，请检查以免无法进行后续使用'
                LOGGER.warning(text)
            try:
                send = await bot.send_message(c.tg, text)
                await send.forward(group[0])
            except FloodWait as f:
                LOGGER.warning(str(f))
                await sleep(f.value * 1.2)
                send = await bot.send_message(c.tg, text)
                await send.forward(group[0])
            except Exception as e:
                LOGGER.error(e)

    rseired = get_all_emby2(
        and_(
            Emby2.lv.in_(('a', 'b')),
            Emby2.expired == 0,
            Emby2.ex.isnot(None),
            Emby2.ex < datetime.now(),
        )
    )
    if rseired is None:
        return LOGGER.info(f'【封禁检测】- emby2 无数据，跳过')
    for e in rseired:
        print(e.embyid)
        if await emby.emby_change_policy(emby_id=e.embyid, disable=True):
            if sql_update_emby2(Emby2.embyid == e.embyid, expired=1, lv='c'):
                text = f"【封禁检测】- 到期封印非TG账户 [{e.name}](google.com?q={e.embyid}) Done！"
                LOGGER.info(text)
            else:
                text = f'【封禁检测】- 到期封印非TG账户：`{e.name}` 数据库更改失败'
        else:
            text = f'【封禁检测】- 到期封印非TG账户：`{e.name}` embyapi操作失败，请手动处理'
        try:
            await bot.send_message(group[0], text)
        except FloodWait as f:
            LOGGER.warning(str(f))
            await sleep(f.value * 1.2)
            await bot.send_message(group[0], text)
        except Exception as e:
            LOGGER.error(e)

    # Non-Telegram Emby2 rows can also contain legacy permanent whitelist
    # flags. They have no Telegram user to notify, so just downgrade them.
    legacy_emby2_whitelist = get_all_emby2(
        and_(Emby2.lv == 'a', Emby2.ex.is_(None))
    )
    for legacy in legacy_emby2_whitelist or []:
        if sql_update_emby2(Emby2.embyid == legacy.embyid, lv='b'):
            LOGGER.warning(
                f'【白名单期限迁移】非TG账户 {legacy.name} 没有订阅到期时间，已撤销永久白名单'
            )
