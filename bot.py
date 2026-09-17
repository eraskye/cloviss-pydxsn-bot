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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
        "key": key,
        "duration_days": days,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "UNUSED",
        "bound_to": None,
        "activated_at": None,
    }
    save_data(data)
    return data["keys"][key]


def check_and_activate(key, user_id):
    data = load_data()
    lic = data["keys"].get(key)
    if not lic:
        return {"ok": False, "status": "INVALID", "error": "Кілт табылмады"}
    if lic["status"] == "REVOKED":
        return {"ok": False, "status": "REVOKED", "error": "Кілт өшірілген"}
    exp = datetime.fromisoformat(lic["expires_at"])
    if now_utc() > exp:
        lic["status"] = "EXPIRED"
        save_data(data)
        return {"ok": False, "status": "EXPIRED", "error": "Мерзімі өткен"}
    if lic["status"] == "UNUSED":
        lic["status"] = "ACTIVE"
        lic["bound_to"] = user_id
        lic["activated_at"] = now_utc().isoformat()
        save_data(data)
        return {"ok": True, "status": "ACTIVE", "expires_at": lic["expires_at"],
                "remaining_seconds": int((exp - now_utc()).total_seconds())}
    if lic["bound_to"] != user_id:
        return {"ok": False, "status": "DEVICE_MISMATCH", "error": "Кілт басқа пайдаланушыға байланған"}
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
                          headers=headers, json={"type": search_type, "query": query, "page": page},
                          timeout=30)
    except Exception as e:
        return {"error": "NETWORK_ERROR", "detail": str(e)}

    if r.status_code != 200:
        return {"error": "API_ERROR", "status_code": r.status_code}
    try:
        result = r.json()
    except Exception:
        return {"error": "INVALID_API_RESPONSE"}
    if result.get("result") is not True:
        return {"error": "API_ERROR", "detail": result}

    if "id" in result:
        tid = result["id"]
        start = time.time()
        while time.time() - start < 60:
            try:
                r2 = requests.get(f"{JITLER_BASE_URL}/search/{tid}",
                                  headers={"Authorization": f"Bearer {JITLER_API_KEY}"}, timeout=10)
            except Exception as e:
                return {"error": "NETWORK_ERROR", "detail": str(e)}
            if r2.status_code == 501:
                time.sleep(3)
                continue
            if r2.status_code != 200:
                return {"error": "API_ERROR"}
            try:
                d2 = r2.json()
            except Exception:
                return {"error": "INVALID_API_RESPONSE"}
            if d2.get("result") is True and d2.get("response") is not None:
                return {"data": d2["response"]}
            time.sleep(3)
        return {"error": "API_TIMEOUT"}
    return {"data": result.get("response", [])}


def get_me():
    if not JITLER_API_KEY:
        return {"error": "API_KEY_MISSING"}
    try:
        r = requests.get(f"{JITLER_BASE_URL}/me",
                         headers={"Authorization": f"Bearer {JITLER_API_KEY}"}, timeout=10)
    except Exception as e:
        return {"error": "NETWORK_ERROR", "detail": str(e)}
    if r.status_code != 200:
        return {"error": "API_ERROR", "status_code": r.status_code}
    try:
        return {"data": r.json()}
    except Exception:
        return {"error": "INVALID_API_RESPONSE"}


# ---------- HELPERS ----------
def is_admin(uid):
    return uid in ADMIN_IDS


# ---------- HANDLERS ----------
async def cmd_start(update, context):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "user"
    text = (
        f"👋 Сәлем, {name}!\n\n"
        "🔐 <b>CLOVISS SYSTEM PYDXSN</b>\n\n"
        "📋 <b>Командалар:</b>\n"
        "/activate &lt;кілт&gt; — лицензияны белсендіру\n"
        "/search — іздеу\n"
        "/me — лицензия ақпараты\n"
        "/help — көмек"
    )
    if is_admin(uid):
        text += (
            "\n\n👑 <b>Админ командалар:</b>\n"
            "/genkey &lt;күн&gt; — кілт жасау\n"
            "/listkeys — барлық кілттер\n"
            "/delkey &lt;кілт&gt; — кілтті өшіру\n"
            "/stats — статистика"
        )
    await update.message.reply_html(text)


async def cmd_help(update, context):
    await cmd_start(update, context)


async def cmd_activate(update, context):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ Қолдану: /activate CLV-XXXXX-XXXXX-XXXXX-XXXXX")
        return
    key = context.args[0].strip().upper()
    result = check_and_activate(key, uid)
    if result["ok"]:
        exp = datetime.fromisoformat(result["expires_at"]).strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_html(
            f"✅ <b>Кілт қабылданды!</b>\n\n"
            f"🟢 Статус: ACTIVE\n📅 Аяқталуы: {exp}\n\n"
            f"Енді /search арқылы іздей аласыз."
        )
    else:
        await update.message.reply_html(
            f"❌ <b>Қате:</b> {result.get('error', 'Жарамсыз')}\n"
            f"Статус: {result.get('status')}"
        )


async def cmd_me(update, context):
    uid = update.effective_user.id
    data = load_data()
    my = [k for k, v in data["keys"].items() if v.get("bound_to") == uid and v.get("status") == "ACTIVE"]
    if not my:
        await update.message.reply_text("❌ Активті лицензия жоқ.\n/activate <кілт>")
        return
    lines = ["🔐 <b>Сіздің лицензияңыз:</b>\n"]
    for k in my:
        v = data["keys"][k]
        exp = datetime.fromisoformat(v["expires_at"])
        rem = int((exp - now_utc()).total_seconds())
        d, h = rem // 86400, (rem % 86400) // 3600
        lines.append(f"🔑 <code>{k}</code>\n⏳ Қалды: {d} күн {h} сағат\n")
    await update.message.reply_html("\n".join(lines))


async def cmd_search(update, context):
    uid = update.effective_user.id
    if not has_active(uid):
        await update.message.reply_text("❌ Алдымен /activate <кілт>")
        return
    kb = [
        [InlineKeyboardButton("📱 Нөмір", callback_data="st:number")],
        [InlineKeyboardButton("👤 VK", callback_data="st:vks")],
        [InlineKeyboardButton("🔍 Telegram/Username", callback_data="st:sherlock")],
    ]
    await update.message.reply_text("🔍 Іздеу түрін таңдаңыз:", reply_markup=InlineKeyboardMarkup(kb))


async def on_search_type(update, context):
    q = update.callback_query
    await q.answer()
    stype = q.data.split(":", 1)[1]
    context.user_data["search_type"] = stype
    await q.edit_message_text(
        f"Түрі: <b>{stype.upper()}</b>\n\nЕнді сұранысты жазыңыз:",
        parse_mode="HTML"
    )


async def on_text(update, context):
    uid = update.effective_user.id
    stype = context.user_data.get("search_type")
    if not stype:
        return
    qtext = update.message.text.strip()
    if not qtext:
        return
    if not has_active(uid):
        await update.message.reply_text("❌ Лицензия жоқ.")
        return
    context.user_data.pop("search_type", None)
    msg = await update.message.reply_text("⏳ Ізделуде...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, search_request, stype, qtext, 1)
    if "error" in result:
        await msg.edit_text(f"❌ Қате: {result['error']}")
        return
    text = json.dumps(result.get("data"), ensure_ascii=False, indent=2)
    if len(text) > 3800:
        text = text[:3800] + "\n...(қысқартылды)"
    await msg.edit_text(f"✅ <b>Нәтиже:</b>\n<pre>{text}</pre>", parse_mode="HTML")


# ---------- ADMIN ----------
async def cmd_genkey(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            f"❌ /genkey <күн>\nМысалы: /genkey 30\n\n"
            f"Қолжетімді: {', '.join(map(str, VALID_DURATIONS))}"
        )
        return
    days = int(context.args[0])
    if days not in VALID_DURATIONS:
        await update.message.reply_text(f"❌ Тек: {', '.join(map(str, VALID_DURATIONS))}")
        return
    lic = create_license(days)
    await update.message.reply_html(
        f"✅ <b>Кілт жасалды!</b>\n\n"
        f"🔑 <code>{lic['key']}</code>\n"
        f"📅 {days} күн\n"
        f"⏰ {datetime.fromisoformat(lic['expires_at']).strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def cmd_listkeys(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    data = load_data()
    keys = list(data["keys"].values())
    if not keys:
        await update.message.reply_text("Кілттер жоқ.")
        return
    changed = False
    for k in keys:
        if k["status"] == "ACTIVE" and now_utc() > datetime.fromisoformat(k["expires_at"]):
            k["status"] = "EXPIRED"
            changed = True
    if changed:
        save_data(data)
    lines = [f"📋 <b>Барлығы: {len(keys)}</b>\n"]
    for k in keys[:30]:
        e = {"UNUSED": "🟡", "ACTIVE": "🟢", "EXPIRED": "🔴", "REVOKED": "⚫"}.get(k["status"], "⚪")
        lines.append(f"{e} <code>{k['key']}</code> | {k['duration_days']}к | {k['status']}")
    if len(keys) > 30:
        lines.append(f"\n...тағы {len(keys)-30}")
    await update.message.reply_html("\n".join(lines))


async def cmd_delkey(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if not context.args:
        await update.message.reply_text("❌ /delkey CLV-...")
        return
    key = context.args[0].strip().upper()
    data = load_data()
    if key in data["keys"]:
        del data["keys"][key]
        save_data(data)
        await update.message.reply_text(f"✅ Өшірілді: {key}")
    else:
        await update.message.reply_text("❌ Табылмады.")


async def cmd_stats(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    keys = load_data()["keys"].values()
    c = {"UNUSED": 0, "ACTIVE": 0, "EXPIRED": 0, "REVOKED": 0}
    for k in keys:
        c[k["status"]] = c.get(k["status"], 0) + 1
    await update.message.reply_html(
        f"📊 <b>Статистика:</b>\n\n"
        f"🟡 Белсендірілмеген: {c['UNUSED']}\n"
        f"🟢 Активті: {c['ACTIVE']}\n"
        f"🔴 Мерзімі өткен: {c['EXPIRED']}\n"
        f"⚫ Өшірілген: {c['REVOKED']}\n"
        f"📦 Барлығы: {sum(c.values())}"
    )


# ---------- BOT (негізгі ағында) ----------
def run_bot():
    """Telegram bot — НЕГІЗГІ ағында іске қосылады (asyncio үшін)."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("listkeys", cmd_listkeys))
    app.add_handler(CommandHandler("delkey", cmd_delkey))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_search_type, pattern="^st:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logging.info("✅ Telegram bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)


# ---------- FLASK (жеке ағында) ----------
web = Flask(__name__)


@web.route("/")
def health():
    return "CLOVISS BOT is running", 200


@web.route("/health")
def health2():
    return {"ok": True, "service": "CLOVISS BOT"}, 200


def run_flask():
    """Flask — жеке ағында, Render порт талап етеді."""
    port = int(os.environ.get("PORT", 10000))
    logging.info("🌐 Web server on port %s", port)
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ---------- MAIN ----------
def main():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN жоқ!")
        return
    if not ADMIN_IDS:
        logging.error("❌ ADMIN_IDS жоқ!")
        return

    # Flask-ты жеке ағында қосамыз (Render үшін порт керек)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Ботты НЕГІЗГІ ағында қосамыз (asyncio осылай талап етеді)
    run_bot()


if __name__ == "__main__":
    main()
