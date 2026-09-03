"""Telegram menu (python-telegram-bot v21+). One ALLOWED_CHAT_ID."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from dutybot.config import (
    Config,
    WatchList,
    WatchService,
    parse_probe,
    slug_id,
    validate_service_id,
    validate_unit_name,
)
from dutybot.services import ExpectedRestarts
from dutybot.notify import Notifier, escape
from dutybot import procs, services, status

log = logging.getLogger("dutybot.bot")

BTN_STATUS = "设备状态"
BTN_CPU = "CPU 前五"
BTN_IO = "读写前五"
BTN_SVC = "服务"
BTN_CLEAN = "清理进程"
BTN_REBOOT = "重启系统"
BTN_ADD = "添加服务"
BTN_DEL = "删除服务"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_STATUS, BTN_CPU],
        [BTN_IO, BTN_SVC],
        [BTN_CLEAN, BTN_REBOOT],
        [BTN_ADD, BTN_DEL],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADD_UNIT, ADD_NAME, ADD_PROBE = range(3)
