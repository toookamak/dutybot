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


class BotApp:
    def __init__(self, cfg: Config, notifier: Notifier, expected: ExpectedRestarts) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.expected = expected
        self._pending_token: Optional[str] = None
        self._pending_pids: list[int] = []

    def watch(self) -> WatchList:
        return self.cfg.reload_watch()

    def allowed(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return False
        try:
            allowed_id = int(self.cfg.allowed_chat_id)
        except (TypeError, ValueError):
            return False
        if user.id != allowed_id or chat.id != allowed_id:
            return False
        if getattr(chat, "type", "private") != "private":
            return False
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.allowed(update):
            return
        await update.message.reply_text(
            "值班 Bot 已就绪。请使用下方菜单。",
            reply_markup=MAIN_KEYBOARD,
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.allowed(update) or not update.message:
            return
        text = (update.message.text or "").strip()
        handlers = {
            BTN_STATUS: self.cmd_status,
            BTN_CPU: self.cmd_cpu,
            BTN_IO: self.cmd_io,
            BTN_SVC: self.cmd_services,
            BTN_CLEAN: self.cmd_cleanup,
            BTN_REBOOT: self.cmd_reboot_ask,
            BTN_ADD: None,  # conversation
            BTN_DEL: self.cmd_delete_list,
        }
        if text == BTN_ADD:
            return
        fn = handlers.get(text)
        if fn:
            await fn(update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        watch = self.watch()
        rows = []
        snap = services.snapshot(watch)
        for svc in watch.services:
            st = snap.units.get(svc.unit) or services.systemctl_show(svc.unit)
            hung = bool(svc.probe and st.is_active and snap.probes.get(svc.unit) is False)
            rows.append(services.format_unit_line(svc, st, hung=hung))
        data = status.collect(watch, unit_rows=rows)
        ssh_ok, reason = services.ssh_journal_available()
        card = status.format_status_card(data, title="设备状态")
        if not ssh_ok:
            card += f"\nSSH 通知：不可用（{escape(reason or 'journal 没有 ssh/sshd')}）"
        elif not watch.services:
            card += "\n服务：看守名单为空"
        await update.message.reply_html(card, reply_markup=MAIN_KEYBOARD)

    async def cmd_cpu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(procs.format_cpu_top(procs.cpu_top()), reply_markup=MAIN_KEYBOARD)

    async def cmd_io(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(procs.format_io_top(procs.io_top()), reply_markup=MAIN_KEYBOARD)

    def _svc_keyboard(self) -> InlineKeyboardMarkup:
        watch = self.watch()
        rows = []
        for svc in watch.services:
            rows.append(
                [
                    InlineKeyboardButton(svc.name, callback_data=f"s:{svc.id}"),
                ]
            )
        if not rows:
            rows = [[InlineKeyboardButton("（名单为空）", callback_data="noop")]]
        return InlineKeyboardMarkup(rows)

    def _svc_actions(self, sid: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("状态", callback_data=f"st:{sid}"),
                    InlineKeyboardButton("最近日志", callback_data=f"lg:{sid}"),
                    InlineKeyboardButton("重启", callback_data=f"rs:{sid}"),
                ],
                [InlineKeyboardButton("返回服务列表", callback_data="svclist")],
            ]
        )

    async def cmd_services(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        watch = self.watch()
        if not watch.services:
            await update.message.reply_text("看守名单为空。可用「添加服务」加入。", reply_markup=MAIN_KEYBOARD)
            return
        await update.message.reply_text("选择服务：", reply_markup=self._svc_keyboard())

    async def cmd_cleanup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        watch = self.watch()
        zombies = procs.list_zombies()
        orphans = procs.list_orphans(watch)
        text = procs.format_preview(zombies, orphans)
        if not orphans:
            self._pending_token = None
            self._pending_pids = []
            await update.message.reply_html(text, reply_markup=MAIN_KEYBOARD)
            return
        token = procs.save_preview(orphans)
        self._pending_token = token
        self._pending_pids = [o.pid for o in orphans]
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("确认终止孤儿", callback_data="kill:ok"),
                    InlineKeyboardButton("取消", callback_data="kill:no"),
                ]
            ]
        )
        await update.message.reply_html(text + "\n\n确认后将终止上述孤儿进程。", reply_markup=kb)

    async def cmd_reboot_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("确认", callback_data="rb:1"),
                    InlineKeyboardButton("取消", callback_data="rb:no"),
                ]
            ]
        )
        await update.message.reply_text("将在约 1 分钟后重启本机。确认？", reply_markup=kb)

    async def cmd_delete_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        watch = self.watch()
        if not watch.services:
            await update.message.reply_text("看守名单为空。", reply_markup=MAIN_KEYBOARD)
            return
        rows = [
            [InlineKeyboardButton(f"删除 {svc.name}", callback_data=f"d1:{svc.id}")]
            for svc in watch.services
        ]
        await update.message.reply_text(
            "删除仅从看守名单移除，不会停止对应 unit。",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def add_begin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not self.allowed(update):
            return ConversationHandler.END
        await update.message.reply_text("请输入 unit 名（须为 *.service）。发送 /cancel 取消。")
        return ADD_UNIT

    async def add_unit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not self.allowed(update):
            return ConversationHandler.END
        unit = (update.message.text or "").strip()
        if not validate_unit_name(unit):
            await update.message.reply_text("unit 名无效，须匹配 *.service。请重试或 /cancel。")
            return ADD_UNIT
        context.user_data["add_unit"] = unit
        await update.message.reply_text("请输入显示名：")
        return ADD_NAME

    async def add_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not self.allowed(update):
            return ConversationHandler.END
        name = (update.message.text or "").strip() or context.user_data.get("add_unit", "svc")
        context.user_data["add_name"] = name
        await update.message.reply_text("可选探测地址 host:port，无则发送 - ：")
        return ADD_PROBE

    async def add_probe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not self.allowed(update):
            return ConversationHandler.END
        raw = (update.message.text or "").strip()
        try:
            probe = parse_probe(raw)
        except ValueError as exc:
            await update.message.reply_text(f"{exc}。请重试或发送 - 。")
            return ADD_PROBE
        unit = context.user_data.get("add_unit")
        name = context.user_data.get("add_name") or unit
        sid = slug_id(unit)
        watch = self.watch()
        # unique id
        base = sid
        i = 2
        while watch.by_id(sid) or not validate_service_id(sid):
            sid = f"{base}-{i}"
            i += 1
        try:
            watch.add(WatchService(id=sid, name=name, unit=unit, probe=probe))
            watch.save()
        except ValueError as exc:
            await update.message.reply_text(f"未能添加：{exc}", reply_markup=MAIN_KEYBOARD)
            return ConversationHandler.END
        await update.message.reply_text(
            f"已添加 {name}（{unit}）。删除不会停止该 unit。",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def add_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        if update.message:
            await update.message.reply_text("已取消。", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not self.allowed(update):
            await query.answer()
            return
        data = query.data or ""
        await query.answer()
        if data == "noop":
            return
        if data == "svclist":
            await query.edit_message_text("选择服务：", reply_markup=self._svc_keyboard())
            return
        if data.startswith("s:"):
            sid = data[2:]
            svc = self.watch().by_id(sid)
            if not svc:
                await query.edit_message_text("该条目已不在名单中。")
                return
            await query.edit_message_text(
                f"{svc.name}（{svc.unit}）",
                reply_markup=self._svc_actions(sid),
            )
            return
        if data.startswith("st:"):
            await self._cb_status(query, data[3:])
            return
        if data.startswith("lg:"):
            await self._cb_logs(query, data[3:])
            return
        if data.startswith("rs:"):
            sid = data[3:]
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("确认重启", callback_data=f"r2:{sid}"),
                        InlineKeyboardButton("取消", callback_data=f"s:{sid}"),
                    ]
                ]
            )
            svc = self.watch().by_id(sid)
            name = svc.name if svc else sid
            await query.edit_message_text(f"确认重启 {name}？", reply_markup=kb)
            return
        if data.startswith("r2:"):
            await self._cb_restart(query, data[3:])
            return
        if data.startswith("d1:"):
            sid = data[3:]
            svc = self.watch().by_id(sid)
            if not svc:
                await query.edit_message_text("该条目已不在名单中。")
                return
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("确认删除（不停 unit）", callback_data=f"d2:{sid}"),
                        InlineKeyboardButton("取消", callback_data="noop"),
                    ]
                ]
            )
            await query.edit_message_text(
                f"从看守名单删除 {svc.name}（{svc.unit}）？不会 stop/disable 该 unit。",
                reply_markup=kb,
            )
            return
        if data.startswith("d2:"):
            sid = data[3:]
            watch = self.watch()
            removed = watch.remove_id(sid)
            watch.save()
            if removed:
                await query.edit_message_text(
                    f"已从名单删除 {removed.name}（{removed.unit}），未停止该 unit。"
                )
            else:
                await query.edit_message_text("该条目已不在名单中。")
            return
        if data == "kill:no":
            procs.clear_preview()
            self._pending_token = None
            await query.edit_message_text("已取消清理。")
            return
        if data == "kill:ok":
            await self._cb_kill(query)
            return
        if data == "rb:no":
            await query.edit_message_text("已取消重启。")
            return
        if data == "rb:1":
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("再次确认重启", callback_data="rb:2"),
                        InlineKeyboardButton("取消", callback_data="rb:no"),
                    ]
                ]
            )
            await query.edit_message_text("再次确认：立即安排约 1 分钟后重启本 Guest？", reply_markup=kb)
            return
        if data == "rb:2":
            code, out, err = await services.dutyctl_async("reboot")
            if code == 0:
                await query.edit_message_text("已安排约 1 分钟后重启，恢复后会推送通知。")
            else:
                await query.edit_message_text(f"重启失败：{(err or out or str(code)).strip()}")
            return

    async def _cb_status(self, query, sid: str) -> None:
        svc = self.watch().by_id(sid)
        if not svc:
            await query.edit_message_text("该条目已不在名单中。")
            return
        info = services.show(svc.unit)
        body = services.format_service_status(svc, info)
        await query.edit_message_text(body, parse_mode="HTML", reply_markup=self._svc_actions(sid))

    async def _cb_logs(self, query, sid: str) -> None:
        svc = self.watch().by_id(sid)
        if not svc:
            await query.edit_message_text("该条目已不在名单中。")
            return
        raw = services.logs(svc.unit, n=40)
        body = f"<b>{escape(svc.name)} 最近日志</b>\n<pre>{escape(raw)}</pre>"
        if len(body) > 4000:
            body = body[:4000] + "…"
        await query.edit_message_text(body, parse_mode="HTML", reply_markup=self._svc_actions(sid))

    async def _cb_restart(self, query, sid: str) -> None:
        svc = self.watch().by_id(sid)
        if not svc:
            await query.edit_message_text("该条目已不在名单中。")
            return
        ExpectedRestarts.mark(svc.unit)
        code, out, err = await services.dutyctl_async("restart-unit", svc.unit)
        if code == 0:
            await query.edit_message_text(
                f"已重启 {svc.name}（{svc.unit}）。菜单发起的重启不会再广播为意外重启。",
                reply_markup=self._svc_actions(sid),
            )
        else:
            msg = (err or out or f"exit {code}").strip()
            await query.edit_message_text(f"重启失败：{msg}", reply_markup=self._svc_actions(sid))

    async def _cb_kill(self, query) -> None:
        token = self._pending_token
        pids = list(self._pending_pids)
        if not token or not pids:
            await query.edit_message_text("预览已过期，请重新打开「清理进程」。")
            return
        args = ["kill-pids", token, *[str(int(p)) for p in pids]]
        code, out, err = await services.dutyctl_async(*args)
        self._pending_token = None
        self._pending_pids = []
        if code == 0:
            await query.edit_message_text(f"已发出终止。\n{(out or '').strip()}")
        else:
            await query.edit_message_text(f"终止失败：{(err or out or str(code)).strip()}")


def build_application(cfg: Config, notifier: Notifier, expected: ExpectedRestarts | None = None) -> Application:
    bot_logic = BotApp(cfg, notifier, expected or ExpectedRestarts())
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .build()
    )

    async def drop_strangers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not bot_logic.allowed(update):
            raise ApplicationHandlerStop()

    app.add_handler(TypeHandler(Update, drop_strangers), group=-1)
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_ADD}$"), bot_logic.add_begin)],
        states={
            ADD_UNIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_logic.add_unit)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_logic.add_name)],
            ADD_PROBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_logic.add_probe)],
        },
        fallbacks=[CommandHandler("cancel", bot_logic.add_cancel)],
        name="add_service",
        persistent=False,
    )
    app.add_handler(CommandHandler("start", bot_logic.start))
    app.add_handler(CommandHandler("menu", bot_logic.start))
    app.add_handler(CommandHandler("cancel", bot_logic.add_cancel))
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(bot_logic.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_logic.on_text))
    return app
