import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bot import LOGGER, _open, emby_line, config, schedall
from bot.func_helper.concurrency import get_user_lock
from bot.func_helper.emby import emby
from bot.func_helper.fix_bottons import re_create_ikb, registration_notice_ikb
from bot.func_helper.msg_utils import editMessage, sendMessage
from bot.func_helper.utils import tem_adduser
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby


REGISTRATION_NOTICE = (
    "📢 **用户须知**\n\n"
    "1. **私人影视服务器，线路严禁外传。**禁止下载影片、使用爆米花、媒体库模式或创建播放列表；开启这些功能可能导致各种问题，发现违规将直接 Ban。\n\n"
    "2. 私人服务器成本较高，请大家珍惜使用。访问白名单优化线路时请关闭代理，或将白名单优化线路加入直连规则。\n\n"
    "3. 如遇“继续播放”无速度，请移除“继续观看”记录或点击“已观看”，重新播放后再拖到之前看到的时间节点；这是本地缓存机制导致的。\n\n"
    "4. 发送口令领取注册码后，请联系 `@emby_dusheng_bot`。电脑端和安卓手机端推荐免费的 [Hills](https://t.me/Hills_app)；iOS 推荐免费的 [Lenna](https://t.me/Lenna_App)，需使用外区 Apple ID 下载（国区没有）。Infuse、SenPlayer 为付费客户端。\n\n"
    "5. 注册完成后，下载并注册豆瓣 App，复制豆瓣 ID，然后在 Bot「用户功能 → 豆瓣想看」中绑定 ID。搜索想看的内容并点击“想看”；服务器每 30 分钟同步一次大家的想看动态并自动下载。若自动下载的清晰度不理想，可联系服主洗版。\n\n"
    "阅读完毕后，点击下方按钮加入 Bot 管理的群组。"
)


@dataclass
class RegisterJob:
    user_id: int
    username: str
    pwd2: str
    stats: bool
    days: int
    status_message: object
    payment_code_id: Optional[str] = None
    payment_tier: str = "normal"
    reservation_key: Optional[str] = None


class RegisterQueueManager:
    def __init__(self):
        self._queue: asyncio.Queue[RegisterJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._busy_users: set[int] = set()
        self._reserved_slots = 0
        self._lock = asyncio.Lock()
        self._active_jobs = 0

    def _configured_worker_count(self) -> int:
        return max(1, int(getattr(_open, "register_worker_count", 5) or 5))

    def _configured_queue_limit(self) -> int:
        return max(1, int(getattr(_open, "register_queue_limit", 100) or 100))

    def _remaining_slot_count_locked(self) -> int:
        return max(0, int(_open.all_user) - int(_open.tem or 0))

    def _max_waiting_queue_size_locked(self) -> int:
        remaining_after_active = self._remaining_slot_count_locked() - self._active_jobs
        return max(0, min(self._configured_queue_limit(), remaining_after_active))

    async def ensure_started(self):
        async with self._lock:
            self._workers = [task for task in self._workers if not task.done()]
            missing = self._configured_worker_count() - len(self._workers)
            for index in range(missing):
                task = asyncio.create_task(self._worker_loop(index), name=f"register-worker-{index}")
                self._workers.append(task)

    async def is_user_busy(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self._busy_users

    async def enqueue(self, job: RegisterJob) -> tuple[bool, str, Optional[int]]:
        await self.ensure_started()
        async with self._lock:
            if job.user_id in self._busy_users:
                return False, "duplicate", None
            current_tem = int(_open.tem or 0)
            # A paid registration already owns a durable reservation created
            # with the order; do not reject it because another account filled
            # the live counter after payment.
            if not job.payment_code_id and current_tem + self._reserved_slots >= _open.all_user:
                return False, "slot_full", None
            if not job.payment_code_id and self._queue.qsize() >= self._max_waiting_queue_size_locked():
                return False, "queue_full", None

            if getattr(getattr(config, "payments", None), "enabled", False) and not job.payment_code_id:
                from bot.payments.service import reserve_registration, PaymentError
                from bot.sql_helper import Session
                try:
                    with Session.begin() as session:
                        job.reservation_key = f"free:{job.user_id}"
                        reserve_registration(session, job.reservation_key, _open.all_user)
                except PaymentError:
                    return False, "slot_full", None

            ahead = self._active_jobs + self._queue.qsize()
            self._busy_users.add(job.user_id)
            self._reserved_slots += 1
            await self._queue.put(job)
            return True, "queued", ahead + 1

    async def _worker_loop(self, worker_index: int):
        while True:
            job = await self._queue.get()
            async with self._lock:
                self._active_jobs += 1

            try:
                await self._process_job(job)
            except Exception as e:
                LOGGER.exception(f"注册队列worker异常[{worker_index}]: {e}")
                await self._safe_edit(job.status_message, "❌ 注册任务执行异常，请稍后重试。", re_create_ikb)
            finally:
                if job.reservation_key:
                    from bot.payments.service import release_registration
                    from bot.sql_helper import Session
                    with Session.begin() as session:
                        release_registration(session, job.reservation_key)
                async with self._lock:
                    self._active_jobs = max(0, self._active_jobs - 1)
                    self._busy_users.discard(job.user_id)
                    self._reserved_slots = max(0, self._reserved_slots - 1)
                self._queue.task_done()

    async def _process_job(self, job: RegisterJob):
        async with get_user_lock(job.user_id):
            current = sql_get_emby(tg=job.user_id)
            if not current:
                return await self._safe_edit(job.status_message, "⚠️ 数据库没有你，请重新 /start录入")
            if current.embyid:
                return await self._safe_edit(job.status_message, "💦 你已经有账户啦！请勿重复注册。")
            if not job.stats and int(current.us or 0) <= 0:
                return await self._safe_edit(job.status_message, "🤖 当前没有可用注册资格，请重新领取注册码后再试。")
            if _open.tem >= _open.all_user and not job.payment_code_id:
                return await self._safe_edit(
                    job.status_message,
                    f'**🚫 很抱歉，剩余可注册总数({_open.tem})，已达总注册限制({_open.all_user})。**',
                )

            await self._safe_edit(
                job.status_message,
                f'🆗 已进入处理\n\n用户名：**{job.username}**  安全码：**{job.pwd2}** \n\n__正在为您初始化账户，更新用户策略__......',
            )

            data = await emby.emby_create(name=job.username, days=job.days)
            if not data:
                return await self._safe_edit(
                    job.status_message,
                    '**- ❎ 已有此账户名，请重新输入注册\n- ❎ 或检查有无特殊字符\n- ❎ 或emby服务器连接不通，会话已结束！**',
                    re_create_ikb,
                )

            pwd = data[1]
            eid = data[0]
            ex = data[2]

            refreshed = sql_get_emby(tg=job.user_id)
            if not refreshed or refreshed.embyid:
                await self._rollback_created_account(job.user_id, eid, "创建后检测到账户状态已变化")
                return await self._safe_edit(job.status_message, '⚠️ 账户状态已变化，请重新打开面板确认。')

            if job.payment_code_id:
                from bot.payments.service import PaymentService, PaymentError
                from bot.payments.settings import PaymentSettings
                from bot import config
                try:
                    from bot.sql_helper import Session
                    PaymentService(Session, PaymentSettings.from_config(config), None).finalize_registration(
                        job.payment_code_id, job.user_id, eid, job.username, pwd, job.pwd2, datetime.now(), ex,
                    )
                    updated = True
                except PaymentError as exc:
                    LOGGER.error(f"付费注册码入账失败: tg={job.user_id}, error={exc.code}")
                    await self._rollback_created_account(job.user_id, eid, "付费注册码入账失败")
                    return await self._safe_edit(job.status_message, "❌ 付费注册入账失败，请联系管理员。")
            elif job.stats:
                updated = sql_update_emby(
                    Emby.tg == job.user_id,
                    embyid=eid,
                    name=job.username,
                    pwd=pwd,
                    pwd2=job.pwd2,
                    lv='b',
                    cr=datetime.now(),
                    ex=ex,
                )
            else:
                updated = sql_update_emby(
                    Emby.tg == job.user_id,
                    embyid=eid,
                    name=job.username,
                    pwd=pwd,
                    pwd2=job.pwd2,
                    lv='b',
                    cr=datetime.now(),
                    ex=ex,
                    us=0,
                )

            if not updated:
                await self._rollback_created_account(job.user_id, eid, "创建后写入数据库失败")
                return await self._safe_edit(job.status_message, "❌ 账户初始化失败，请稍后重试。")

            tem_adduser()

            if schedall.check_ex:
                ex_text = ex.strftime("%Y-%m-%d %H:%M:%S")
            elif schedall.low_activity:
                ex_text = f'__若{config.activity_check_days}天无观看将封禁__'
            else:
                ex_text = '__无需保号，放心食用__'

            await self._safe_edit(
                job.status_message,
                f'**▎创建用户成功🎉**\n\n'
                f'· 用户名称 | `{job.username}`\n'
                f'· 用户密码 | `{pwd}`\n'
                f'· 安全密码 | `{job.pwd2}`（仅发送一次）\n'
                f'· 到期时间 | `{ex_text}`\n'
                f'· 当前线路：\n'
                f'{emby_line}\n\n'
                f'**·【服务器】 - 查看线路和密码**',
            )

            try:
                notice_result = await sendMessage(
                    job.status_message,
                    REGISTRATION_NOTICE,
                    buttons=registration_notice_ikb,
                )
                if notice_result is not True:
                    LOGGER.warning(
                        f'注册须知发送失败: tg={job.user_id}, result={notice_result}'
                    )
            except Exception as e:
                # The account is already committed; a notice delivery failure must
                # never roll back a valid Emby registration.
                LOGGER.warning(f'注册须知发送失败: tg={job.user_id}, error={e}')

    async def _safe_edit(self, message, text: str, buttons=None):
        result = await editMessage(message, text, buttons)
        if result is True:
            return True
        return await sendMessage(message, text, buttons=buttons)

    async def _rollback_created_account(self, user_id: int, emby_id: str, reason: str):
        LOGGER.warning(f"注册队列回滚远端账户: tg={user_id}, emby_id={emby_id}, reason={reason}")
        try:
            deleted = await emby.emby_del(emby_id=emby_id)
            if not deleted:
                LOGGER.error(f"注册队列回滚失败: tg={user_id}, emby_id={emby_id}")
        except Exception as e:
            LOGGER.exception(f"注册队列回滚异常: tg={user_id}, emby_id={emby_id}, error={e}")


_register_queue_manager: Optional[RegisterQueueManager] = None


def get_register_queue_manager() -> RegisterQueueManager:
    global _register_queue_manager
    if _register_queue_manager is None:
        _register_queue_manager = RegisterQueueManager()
    return _register_queue_manager
