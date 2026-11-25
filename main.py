#! /usr/bin/python3
# -*- coding: utf-8 -*-

from bot import bot
from pyrogram import idle

# 面板
from bot.modules.panel import *
# 命令
from bot.modules.commands import *
# 其他
from bot.modules.extra import *
from bot.modules.callback import *
from bot.web import *
from bot.scheduler.bot_commands import BotCommands


async def main():
    async with bot:
        await BotCommands.set_commands(bot)
        await idle()


bot.run(main())
