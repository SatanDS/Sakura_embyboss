"""按当前权限更新 Telegram 命令菜单。"""

from bot import owner, admins, group, LOGGER, user_p, admin_p, owner_p, bot
from pyrogram.types import BotCommandScopeChatMember, BotCommandScopeChat, BotCommandScopeAllPrivateChats, \
    BotCommandScopeAllGroupChats


class BotCommands:
    user_p = user_p
    admin_p = admin_p
    owner_p = owner_p
    client = bot

    @staticmethod
    def _commands_for(uid):
        if uid == owner:
            return owner_p
        if uid in admins:
            return admin_p
        return user_p

    @staticmethod
    async def _set_commands(client, commands, scope):
        target = (f"{type(scope).__name__} chat_id={getattr(scope, 'chat_id', None)} "
                  f"user_id={getattr(scope, 'user_id', None)}")
        try:
            if await client.set_bot_commands(commands, scope=scope):
                return True
            LOGGER.error(f"命令菜单更新失败：{target}，Telegram 未确认成功")
        except Exception as exc:
            LOGGER.error(f"命令菜单更新失败：{target}，{type(exc).__name__}")
        return False

    @staticmethod
    async def set_commands(client):
        targets = [(user_p, BotCommandScopeAllPrivateChats()),
                   (user_p, BotCommandScopeAllGroupChats())]
        for uid in dict.fromkeys([*admins, owner]):
            commands = BotCommands._commands_for(uid)
            targets.append((commands, BotCommandScopeChat(chat_id=uid)))
            for chat_id in group:
                targets.append((commands, BotCommandScopeChatMember(chat_id=chat_id, user_id=uid)))

        failures = 0
        for commands, scope in targets:
            if not await BotCommands._set_commands(client, commands, scope):
                failures += 1
        if failures:
            LOGGER.warning(f"命令菜单初始化有 {failures} 项失败；私聊 /start 可重试当前用户菜单")
        else:
            LOGGER.info("————初始化 命令显示 done————")
        return failures == 0

    @staticmethod
    async def refresh_private_commands(client, uid):
        return await BotCommands._set_commands(
            client, BotCommands._commands_for(uid), BotCommandScopeChat(chat_id=uid))

    @staticmethod
    async def refresh_user_commands(client, uid):
        success = await BotCommands.refresh_private_commands(client, uid)
        for chat_id in group:
            if not await BotCommands._set_commands(
                    client, BotCommands._commands_for(uid),
                    BotCommandScopeChatMember(chat_id=chat_id, user_id=uid)):
                success = False
        return success

    @staticmethod
    async def pro_commands(client, uid):
        return await BotCommands.refresh_user_commands(client, uid)

    @staticmethod
    async def rev_commands(client, uid):
        return await BotCommands.refresh_user_commands(client, uid)
