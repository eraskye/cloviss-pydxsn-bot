import os
import json
import time
import asyncio
import logging
import secrets
import string
import threading
from datetime import datetime, timezone, timedelta

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from flask import Flask

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
JITLER_API_KEY = os.environ.get("JITLER_API_KEY", "")
JITLER_BASE_URL = os.environ.get("JITLER_BASE_URL", "https://api.jitler.top")

VALID_SEARCH_TYPES = ("number", "vks", "sherlock")
VALID_DURATIONS = (1, 3, 7, 30, 90, 180, 365)


# ---------- STORAGE ----------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"keys": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"keys": {}}


def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("save_data error: %s", e)


def now_utc():
    return datetime.now(timezone.utc)


# ---------- LICENSE ----------
def generate_key():
    def block():
        return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))
    return f"CLV-{block()}-{block()}-{block()}-{block()}"


def create_license(days):
    data = load_data()
    key = generate_key()
    while key in data["keys"]:
        key = generate_key()
    created = now_utc()
    expires = created + timedelta(days=days)
    data["keys"][key] = {
        "key": key, "duration_days": days,
        "created_at": created.isoformat(), "expires_at": expires.isoformat(),
        "status": "UNUSED", "bound_to": None, "activated_at": None,
    }
    save_data(data)
    return data["keys"][key]


def check_and_activate(key, user_id):
    data = load_data()
    lic = data["keys"].get(key)
    if not lic:
        return {"ok": False, "status": "INVALID", "error": "Ключ не найден"}
    if lic["status"] == "REVOKED":
        return {"ok": False, "status": "REVOKED", "error": "Ключ отозван"}
    exp = datetime.fromisoformat(lic["expires_at"])
    if now_utc() > exp:
        lic["status"] = "EXPIRED"
        save_data(data)
        return {"ok": False, "status": "EXPIRED", "error": "Срок действия истёк"}
    if lic["status"] == "UNUSED":
        lic["status"] = "ACTIVE"
        lic["bound_to"] = user_id
        lic["activated_at"] = now_utc().isoformat()
        save_data(data)
        return {"ok": True, "status": "ACTIVE", "expires_at": lic["expires_at"],
                "remaining_seconds": int((exp - now_utc()).total_seconds())}
    if lic["bound_to"] != user_id:
        return {"ok": False, "status": "DEVICE_MISMATCH", "error": "Ключ привязан к другому пользователю"}
    return {"ok": True, "status": "ACTIVE", "expires_at": lic["expires_at"],
            "remaining_seconds": int((exp - now_utc()).total_seconds())}


def has_active(uid):
    data = load_data()
    return any(v.get("bound_to") == uid and v.get("status") == "ACTIVE" for v in data["keys"].values())


# ---------- API ----------
def search_request(search_type, query, page=1):
    if search_type not in VALID_SEARCH_TYPES:
        return {"error": "INVALID_SEARCH_TYPE"}
    if not query:
        return {"error": "EMPTY_QUERY"}
    if not JITLER_API_KEY:
        return {"error": "API_KEY_MISSING"}

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {JITLER_API_KEY}"}
    try:
        r = requests.post(f"{JITLER_BASE_URL}/search",
                          headers=headers,
                          json={"type": search_type, "query": query, "page": page},
                          timeout=15)
    except requests.Timeout:
        return {"error": "REQUEST_TIMEOUT"}
    except Exception as e:
        return {"error": "NETWORK_ERROR", "detail": str(e)}

    if r.status_code == 401:
        return {"error": "INVALID_API_KEY"}
    if r.status_code == 403:
        return {"error": "API_FORBIDDEN"}
    if r.status_code != 200:
        return {"error": "API_ERROR", "status_code": r.status_code,
                "body": r.text[:200]}

    try:
        result = r.json()
    except Exception:
        return {"error": "INVALID_API_RESPONSE", "body": r.text[:200]}

    if result.get("result") is not True:
        return {"error": "API_ERROR", "detail": str(result)[:300]}

    if "id" in result:
        tid = result["id"]
        start = time.time()
        while time.time() - start < 45:
            try:
                r2 = requests.get(f"{JITLER_BASE_URL}/search/{tid}",
                                  headers={"Authorization": f"Bearer {JITLER_API_KEY}"},
                                  timeout=10)
            except Exception as e:
                return {"error": "NETWORK_ERROR", "detail": str(e)}
            if r2.status_code == 501:
                time.sleep(2); continue
            if r2.status_code != 200:
                return {"error": "API_ERROR", "status_code": r2.status_code}
            try:
                d2 = r2.json()
            except Exception:
                return {"error": "INVALID_API_RESPONSE"}
            if d2.get("result") is True and d2.get("response") is not None:
                return {"data": d2["response"]}
            time.sleep(2)
        return {"error": "API_TIMEOUT"}
    return {"data": result.get("response", [])}


# ---------- KEYBOARDS ----------
def main_menu_kb(is_admin_user=False):
    kb = [
        [KeyboardButton("🔍 Поиск"), KeyboardButton("👤 Мой ключ")],
        [KeyboardButton("🔑 Активировать ключ"), KeyboardButton("ℹ️ Помощь")],
    ]
    if is_admin_user:
        kb.append([KeyboardButton("👑 Админ-панель")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def admin_menu_kb():
    kb = [
        [KeyboardButton("🆕 Создать ключ"), KeyboardButton("📋 Все ключи")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("🗑 Удалить ключ")],
        [KeyboardButton("⬅️ Главное меню")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def search_types_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Номер телефона", callback_data="st:number")],
        [InlineKeyboardButton("👤 Профиль VK", callback_data="st:vks")],
        [InlineKeyboardButton("🔍 Telegram / Username", callback_data="st:sherlock")],
        [InlineKeyboardButton("❌ Отмена", callback_data="st:cancel")],
    ])


def durations_kb():
    rows = [
        [InlineKeyboardButton("1 день", callback_data="dur:1"),
         InlineKeyboardButton("3 дня", callback_data="dur:3"),
         InlineKeyboardButton("7 дней", callback_data="dur:7")],
        [InlineKeyboardButton("30 дней", callback_data="dur:30"),
         InlineKeyboardButton("90 дней", callback_data="dur:90"),
         InlineKeyboardButton("365 дней", callback_data="dur:365")],
        [InlineKeyboardButton("❌ Отмена", callback_data="dur:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


# ---------- HANDLERS ----------
async def cmd_start(update, context):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "друг"
    text = (
        f"👋 <b>Привет, {name}!</b>\n\n"
        "🔐 <b>CLOVISS SYSTEM PYDXSN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Используйте кнопки ниже 👇"
    )
    await update.message.reply_html(text, reply_markup=main_menu_kb(is_admin(uid)))


async def cmd_help(update, context):
    await update.message.reply_html(
        "ℹ️ <b>Помощь</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔑 <b>Активация ключа</b>\n"
        "Введите ваш ключ: <code>CLV-XXXXX-XXXXX-XXXXX-XXXXX</code>\n\n"
        "🔍 <b>Поиск</b>\n"
        "Нажмите кнопку и выберите тип поиска\n\n"
        "👤 <b>Мой ключ</b>\n"
        "Показывает оставшееся время лицензии",
        reply_markup=main_menu_kb(is_admin(update.effective_user.id))
    )


async def cmd_search(update, context):
    uid = update.effective_user.id
    if not has_active(uid):
        await update.message.reply_text(
            "❌ Сначала активируйте ключ.",
            reply_markup=main_menu_kb(is_admin(uid))
        )
        return
    await update.message.reply_text(
        "🔍 <b>Выберите тип поиска:</b>",
        parse_mode="HTML",
        reply_markup=search_types_kb()
    )


async def cmd_me(update, context):
    uid = update.effective_user.id
    data = load_data()
    my = [k for k, v in data["keys"].items()
          if v.get("bound_to") == uid and v.get("status") == "ACTIVE"]
    if not my:
        await update.message.reply_html(
            "❌ Нет активной лицензии.\n🔑 Активируйте ключ.",
            reply_markup=main_menu_kb(is_admin(uid))
        )
        return
    lines = ["👤 <b>Ваша лицензия</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
    for k in my:
        v = data["keys"][k]
        exp = datetime.fromisoformat(v["expires_at"])
        rem = int((exp - now_utc()).total_seconds())
        d, h = rem // 86400, (rem % 86400) // 3600
        lines.append(f"🔑 <code>{k}</code>")
        lines.append(f"⏳ Осталось: <b>{d} д. {h} ч.</b>")
        lines.append(f"📅 Истекает: {exp.strftime('%Y-%m-%d %H:%M')} UTC\n")
    await update.message.reply_html("\n".join(lines), reply_markup=main_menu_kb(is_admin(uid)))


# ---------- CALLBACKS ----------
async def on_callback(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id

    if data.startswith("st:"):
        stype = data.split(":", 1)[1]
        if stype == "cancel":
            context.user_data.pop("search_type", None)
            await q.edit_message_text("❌ Отменено.")
            return
        context.user_data["search_type"] = stype
        names = {"number": "📱 Номер телефона", "vks": "👤 Профиль VK", "sherlock": "🔍 Telegram / Username"}
        await q.edit_message_text(
            f"✅ Тип: <b>{names.get(stype, stype)}</b>\n\n"
            f"Теперь введите запрос:",
            parse_mode="HTML"
        )
        return

    if data.startswith("dur:"):
        if not is_admin(uid):
            await q.edit_message_text("❌ Нет доступа.")
            return
        val = data.split(":", 1)[1]
        if val == "cancel":
            await q.edit_message_text("❌ Отменено.")
            return
        days = int(val)
        lic = create_license(days)
        await q.edit_message_text(
            f"✅ <b>Ключ создан!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔑 <code>{lic['key']}</code>\n"
            f"📅 Срок: <b>{days} д.</b>\n"
            f"⏰ Истекает: {datetime.fromisoformat(lic['expires_at']).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"<i>Скопируйте ключ</i>",
            parse_mode="HTML"
        )
        return


# ---------- TEXT ROUTER ----------
async def on_text(update, context):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Search query
    stype = context.user_data.get("search_type")
    if stype:
        context.user_data.pop("search_type", None)
        if not has_active(uid):
            await update.message.reply_text("❌ Нет лицензии.")
            return
        msg = await update.message.reply_text("⏳ Поиск... (до 30 сек)")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, search_request, stype, text, 1)
        if "error" in result:
            err = result["error"]
            mapping = {
                "API_KEY_MISSING": "🔑 API-ключ не настроен (Render → Environment)",
                "INVALID_API_KEY": "🔑 API-ключ недействителен",
                "REQUEST_TIMEOUT": "⏰ API не ответил (timeout)",
                "API_TIMEOUT": "⏰ Поиск занял слишком много времени",
                "NETWORK_ERROR": "🌐 Сетевая ошибка",
                "API_ERROR": "❌ Ошибка API",
            }
            human = mapping.get(err, f"❌ {err}")
            await msg.edit_text(f"{human}\n\n<code>{str(result)[:300]}</code>", parse_mode="HTML")
            return
        output = json.dumps(result.get("data"), ensure_ascii=False, indent=2)
        if len(output) > 3800:
            output = output[:3800] + "\n...(сокращено)"
        await msg.edit_text(f"✅ <b>Результат:</b>\n<pre>{output}</pre>", parse_mode="HTML")
        return

    # Menu buttons
    if text == "🔍 Поиск":
        await cmd_search(update, context); return
    if text == "👤 Мой ключ":
        await cmd_me(update, context); return
    if text == "🔑 Активировать ключ":
        await update.message.reply_text(
            "🔑 Отправьте ваш ключ:\n<code>CLV-XXXXX-XXXXX-XXXXX-XXXXX</code>",
            parse_mode="HTML"
        )
        context.user_data["awaiting_key"] = True
        return
    if text == "ℹ️ Помощь":
        await cmd_help(update, context); return

    # Admin buttons
    if text == "👑 Админ-панель" and is_admin(uid):
        await update.message.reply_text(
            "👑 <b>Админ-панель</b>", parse_mode="HTML",
            reply_markup=admin_menu_kb()
        )
        return
    if text == "⬅️ Главное меню":
        await update.message.reply_text(
            "🏠 Главное меню", reply_markup=main_menu_kb(is_admin(uid))
        )
        return
    if text == "🆕 Создать ключ" and is_admin(uid):
        await update.message.reply_text(
            "📅 <b>Выберите срок:</b>", parse_mode="HTML",
            reply_markup=durations_kb()
        )
        return
    if text == "📋 Все ключи" and is_admin(uid):
        data_ = load_data()
        keys = list(data_["keys"].values())
        if not keys:
            await update.message.reply_text("Ключей нет.", reply_markup=admin_menu_kb())
            return
        lines = [f"📋 <b>Всего: {len(keys)}</b>\n"]
        for k in keys[:25]:
            e = {"UNUSED": "🟡", "ACTIVE": "🟢", "EXPIRED": "🔴", "REVOKED": "⚫"}.get(k["status"], "⚪")
            lines.append(f"{e} <code>{k['key']}</code> | {k['duration_days']}д")
        if len(keys) > 25:
            lines.append(f"\n...ещё {len(keys)-25}")
        await update.message.reply_html("\n".join(lines), reply_markup=admin_menu_kb())
        return
    if text == "📊 Статистика" and is_admin(uid):
        keys = load_data()["keys"].values()
        c = {"UNUSED": 0, "ACTIVE": 0, "EXPIRED": 0, "REVOKED": 0}
        for k in keys:
            c[k["status"]] = c.get(k["status"], 0) + 1
        await update.message.reply_html(
            f"📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🟡 Не активировано: {c['UNUSED']}\n"
            f"🟢 Активных: {c['ACTIVE']}\n"
            f"🔴 Истекших: {c['EXPIRED']}\n"
            f"⚫ Удалённых: {c['REVOKED']}\n"
            f"📦 Всего: {sum(c.values())}",
            reply_markup=admin_menu_kb()
        )
        return
    if text == "🗑 Удалить ключ" and is_admin(uid):
        context.user_data["awaiting_delkey"] = True
        await update.message.reply_text("🗑 Отправьте ключ для удаления:", reply_markup=admin_menu_kb())
        return

    # Awaiting key activation
    if context.user_data.get("awaiting_key"):
        context.user_data.pop("awaiting_key", None)
        key = text.upper()
        result = check_and_activate(key, uid)
        if result["ok"]:
            exp = datetime.fromisoformat(result["expires_at"]).strftime("%Y-%m-%d %H:%M UTC")
            await update.message.reply_html(
                f"✅ <b>Ключ принят!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 Статус: ACTIVE\n"
                f"📅 Истекает: {exp}",
                reply_markup=main_menu_kb(is_admin(uid))
            )
        else:
            await update.message.reply_html(
                f"❌ <b>Ошибка:</b> {result.get('error', 'Недействителен')}",
                reply_markup=main_menu_kb(is_admin(uid))
            )
        return

    # Awaiting delkey
    if context.user_data.get("awaiting_delkey") and is_admin(uid):
        context.user_data.pop("awaiting_delkey", None)
        key = text.upper()
        data_ = load_data()
        if key in data_["keys"]:
            del data_["keys"][key]
            save_data(data_)
            await update.message.reply_text(f"✅ Удалено: {key}", reply_markup=admin_menu_kb())
        else:
            await update.message.reply_text("❌ Не найдено.", reply_markup=admin_menu_kb())
        return

    # Default
    await update.message.reply_html(
        "Используйте кнопки ниже 👇",
        reply_markup=main_menu_kb(is_admin(uid))
    )


# ---------- HELPERS ----------
def is_admin(uid):
    return uid in ADMIN_IDS


# ---------- BOT ----------
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logging.info("✅ Telegram bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)


# ---------- FLASK ----------
web = Flask(__name__)


@web.route("/")
def health():
    return "CLOVISS BOT is running", 200


@web.route("/health")
def health2():
    return {"ok": True, "service": "CLOVISS BOT"}, 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    logging.info("🌐 Web server on port %s", port)
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ---------- MAIN ----------
def main():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN отсутствует!"); return
    if not ADMIN_IDS:
        logging.error("❌ ADMIN_IDS отсутствует!"); return

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()


if __name__ == "__main__":
    main()
