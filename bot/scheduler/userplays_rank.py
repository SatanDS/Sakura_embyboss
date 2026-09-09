import math
import re
import cn2an
from datetime import datetime, timezone, timedelta

from bot import bot, bot_photo, group, sakura_b, LOGGER, ranks, _open 
from bot.func_helper.emby import emby
from bot.func_helper.utils import convert_s, cache, get_users, tem_deluser
from bot.func_helper.concurrency import get_user_lock
from bot.sql_helper import Session
from bot.sql_helper.sql_emby import sql_get_emby_by_embyid, sql_update_embys, Emby, sql_update_emby
from bot.func_helper.fix_bottons import plays_list_button


class Uplaysinfo:
    client = emby

    @classmethod
    @cache.memoize(ttl=120)
    async def users_playback_list(cls, days):
        try:
            play_list = await emby.emby_cust_commit(emby_id=None, days=days, method='sp')
        except Exception as e:
            print(f"Error fetching playback list: {e}")
            return None, 1, 1

        if play_list is None:
            return None, 1, 1

        with Session() as session:
            # 更高效地查询 Emby 表的数据
            result = session.query(Emby).filter(Emby.name.isnot(None)).all()

            if not result:
                return None, 1, []

            total_pages = math.ceil(len(play_list) / 10)
            members = await get_users()
            members_dict = {}

            for record in result:
                members_dict[record.name] = {
                    "name": members.get(record.tg, record.name),
                    "tg": record.tg,
                    "lv": record.lv,
                    "iv": record.iv
                }

            rank_medals = ["🥇", "🥈", "🥉", "🏅"]
            rank_points = [1000, 900, 800, 700, 600, 500, 400, 300, 200, 100]

            pages_data = []
            leaderboard_data = []

            for page_number in range(1, total_pages + 1):
                start_index = (page_number - 1) * 10
                end_index = start_index + 10
                page_data = f'**▎🏆{ranks.logo} {days} 天观影榜**\n\n'

                for rank, play_record in enumerate(play_list[start_index:end_index], start=start_index + 1):
                    medal = rank_medals[rank - 1] if rank < 4 else rank_medals[3]
                    member_info = members_dict.get(play_record[0], None)

                    if not member_info or not member_info["tg"]:
                        emby_name = play_record[0] + ' (未绑定Bot)'
                        tg = 'None'
                    else:
                        emby_name = member_info["name"]
                        tg = member_info["tg"]

                        # 计算积分
                        points = rank_points[rank - 1] + (int(play_record[1]) // 60) if rank <= 10 else (
                                    int(play_record[1]) // 60)
                        new_iv = member_info["iv"] + points
                        leaderboard_data.append([member_info["tg"], new_iv, f'{medal}{emby_name}', points])

                    formatted_time = await convert_s(int(play_record[1]))
                    page_data += f'{medal}**第{cn2an.an2cn(rank)}名** | [{emby_name}](https://www.google.com/search?q={tg})\n' \
                                 f'  观影时长 | {formatted_time}\n'

                page_data += f'\n#UPlaysRank {datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")}'
                pages_data.append(page_data)

            return pages_data, total_pages, leaderboard_data

    @staticmethod
    async def user_plays_rank(days=7, uplays=True):
        a, n, ls = await Uplaysinfo.users_playback_list(days)
        if not a:
            return await bot.send_photo(chat_id=group[0], photo=bot_photo,
                                        caption=f'🍥 获取过去{days}天UserPlays失败了嘤嘤嘤 ~ 手动重试 ')
        play_button = await plays_list_button(n, 1, days)
        send = await bot.send_photo(chat_id=group[0], photo=bot_photo, caption=a[0], reply_markup=play_button)
        if uplays and _open.uplays:
            if sql_update_embys(some_list=ls, method='iv'):
                text = f'**自动将观看时长转换为{sakura_b}**\n\n'
                for i in ls:
                    text += f'[{i[2]}](tg://user?id={i[0]}) 获得了 {i[3]} {sakura_b}奖励\n'
                n = 4096
                chunks = [text[i:i + n] for i in range(0, len(text), n)]
                for c in chunks:
                    await bot.send_message(chat_id=group[0],
                                           text=c + f'\n⏱️ 当前时间 - {datetime.now().strftime("%Y-%m-%d")}')
                LOGGER.info(f'【userplayrank】： ->成功 数据库执行批量操作{ls}')
            else:
                await send.reply(f'**🎂！！！为用户增加{sakura_b}出错啦** @工程师看看吧~ ')
                LOGGER.error(f'【userplayrank】：-？失败 数据库执行批量操作{ls}')

    @staticmethod
    async def check_low_activity():
        success, users = await emby.users()
        if not success:
            return await bot.send_message(chat_id=group[0], text='调用 Emby API 失败')
        from bot import config
        activity_check_days = config.activity_check_days
        notices = [f'正在执行 {activity_check_days} 天活跃检测...\n']
        for user in users:
            e = sql_get_emby_by_embyid(user["Id"])
            if not e:
                continue
            async with get_user_lock(e.tg):
                e = sql_get_emby_by_embyid(user["Id"])
                if not e:
                    continue
                now = datetime.now()
                if e.lv == 'c':
                    if e.disabled_at is None:
                        LOGGER.warning('跳过自动删除账户 %s：没有可确认的禁用时间', e.tg)
                        continue
                    if now < e.disabled_at + timedelta(days=config.freeze_days):
                        continue
                    if await emby.emby_del(emby_id=e.embyid):
                        if sql_update_emby(Emby.embyid == e.embyid, embyid=None, name=None, pwd=None, pwd2=None,
                                           lv='d', cr=None, ex=None):
                            tem_deluser()
                            notices.append(f'账户 {e.name} #id{e.tg} 冻结期结束，已删除。\n')
                        else:
                            LOGGER.error('账户 %s 已从 Emby 删除，但数据库更新失败', e.tg)
                    else:
                        notices.append(f'账户 {e.name} #id{e.tg} 删除失败，请检查 Emby 连接。\n')
                elif e.lv == 'b':
                    last_activity = user.get('LastActivityDate')
                    try:
                        if last_activity:
                            # Emby uses seven fractional digits; Python 3.10 accepts six.
                            timestamp = re.sub(r'\.(\d+)', lambda match: '.' + match[1][:6].ljust(6, '0'),
                                               last_activity, count=1)
                            ac_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            if ac_date.tzinfo is None:
                                ac_date = ac_date.replace(tzinfo=timezone.utc)
                        else:
                            ac_date = e.cr
                        if ac_date is None:
                            LOGGER.warning('跳过活跃检测账户 %s：缺少活动和注册时间', e.tg)
                            continue
                        if ac_date.tzinfo is not None:
                            ac_date = ac_date.astimezone().replace(tzinfo=None)
                    except (AttributeError, TypeError, ValueError):
                        LOGGER.warning('跳过活跃检测账户 %s：活动时间无效', e.tg)
                        continue
                    if now < ac_date + timedelta(days=activity_check_days):
                        continue
                    if await emby.emby_change_policy(emby_id=e.embyid, disable=True):
                        if sql_update_emby(Emby.embyid == e.embyid, lv='c', disabled_at=datetime.now()):
                            notices.append(f'账户 {e.name} #id{e.tg} 已连续 {activity_check_days} 天未活跃，已禁用。\n')
                        else:
                            LOGGER.error('账户 %s 已在 Emby 禁用，但数据库更新失败', e.tg)
                    else:
                        notices.append(f'账户 {e.name} #id{e.tg} 禁用失败，请检查 Emby 连接。\n')
        msg = ''.join(notices) + '活跃检测结束\n'
        for start in range(0, len(msg), 1000):
            await bot.send_message(chat_id=group[0], text=msg[start:start + 1000])
