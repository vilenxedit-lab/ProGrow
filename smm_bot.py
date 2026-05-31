"""
SMM Panel Telegram Bot - Updated Version
Changes:
- Captcha on /start (math-based)
- Channel join verification → ₹10 signup bonus
- Referral system → ₹7 bonus (after referred user joins channel + starts bot)
- Refer & Earn button in main menu
- Payment/UPI system removed
- Admin user ID removed from public views
- Minimum order ₹50
- Track order always shows Pending
"""

import os
import random
import logging
import asyncio
import aiohttp
from datetime import datetime
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
MONGO_URI        = os.environ.get("MONGO_URI", "")
SMM_API_KEY      = os.environ.get("SMM_API_KEY", "")
SMM_API_URL      = "https://smmkings.com/api/v2"
ADMIN_IDS        = list(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))
MARKUP           = 1.5
USD_TO_INR       = 96.0
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@nexuspredictionss")
SIGNUP_BONUS     = 10.0   # ₹10 signup bonus
REFERRAL_BONUS   = 7.0    # ₹7 referral bonus
MIN_ORDER_AMOUNT = 50.0   # Minimum ₹50 order

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  MONGODB
# ═══════════════════════════════════════════════════════════════════════════════
def get_col(name):
    if not MONGO_URI:
        return None
    try:
        import certifi
        client = MongoClient(
            MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000
        )
    except Exception:
        client = MongoClient(
            MONGO_URI,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000
        )
    return client["smmbot"][name]

def get_user(uid):
    col = get_col("users")
    uid = str(uid)
    if col is None:
        return {"_id": uid, "balance": 0.0, "orders": [], "joined": False, "signup_bonus_given": False}
    doc = col.find_one({"_id": uid})
    if not doc:
        doc = {"_id": uid, "balance": 0.0, "orders": [], "joined": False, "signup_bonus_given": False}
        col.insert_one(doc)
    return doc

def update_user(uid, data):
    col = get_col("users")
    if col:
        col.update_one({"_id": str(uid)}, {"$set": data}, upsert=True)

def add_balance(uid, amount):
    user = get_user(uid)
    new_bal = round(user.get("balance", 0) + amount, 2)
    update_user(uid, {"balance": new_bal})
    return new_bal

def deduct_balance(uid, amount):
    user = get_user(uid)
    new_bal = round(user.get("balance", 0) - amount, 2)
    update_user(uid, {"balance": new_bal})
    return new_bal

def save_order(uid, order_data):
    col = get_col("orders")
    if col:
        order_data["user_id"] = str(uid)
        order_data["created_at"] = datetime.now()
        col.insert_one(order_data)

def get_pending_referral(referred_uid):
    """Check karo kisi ka referral pending hai"""
    col = get_col("referrals")
    if col:
        return col.find_one({"referred_id": str(referred_uid), "bonus_paid": False})
    return None

def mark_referral_paid(referred_uid):
    col = get_col("referrals")
    if col:
        col.update_one({"referred_id": str(referred_uid)}, {"$set": {"bonus_paid": True}})

def save_referral(referrer_uid, referred_uid):
    col = get_col("referrals")
    if col:
        existing = col.find_one({"referred_id": str(referred_uid)})
        if not existing:
            col.insert_one({
                "referrer_id": str(referrer_uid),
                "referred_id": str(referred_uid),
                "bonus_paid": False,
                "created_at": datetime.now()
            })

def get_referral_count(uid):
    col = get_col("referrals")
    if col:
        return col.count_documents({"referrer_id": str(uid), "bonus_paid": True})
    return 0

# ═══════════════════════════════════════════════════════════════════════════════
#  SMMKINGS API
# ═══════════════════════════════════════════════════════════════════════════════
async def smm_api(action, **kwargs):
    params = {"key": SMM_API_KEY, "action": action}
    params.update(kwargs)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SMM_API_URL, data=params, timeout=30) as r:
                return await r.json()
    except Exception as e:
        logger.error(f"SMM API error: {e}")
        return None

async def get_services():
    return await smm_api("services")

async def place_order(service_id, link, quantity):
    return await smm_api("add", service=service_id, link=link, quantity=quantity)

async def check_order_status(order_id):
    return await smm_api("status", order=order_id)

def calculate_price(rate_usd, quantity):
    rate_inr_per_1000 = float(rate_usd) * USD_TO_INR
    price = (rate_inr_per_1000 * quantity / 1000) * MARKUP
    return round(price, 2)

# ═══════════════════════════════════════════════════════════════════════════════
#  CHANNEL JOIN CHECK
# ═══════════════════════════════════════════════════════════════════════════════
async def check_channel_membership(bot, user_id: int) -> bool:
    """Check karo user ne channel join kiya hai ya nahi"""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Channel check error: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
#  CAPTCHA
# ═══════════════════════════════════════════════════════════════════════════════
def generate_captcha():
    """Simple math captcha generate karo"""
    a = random.randint(1, 20)
    b = random.randint(1, 20)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
        question = f"{a} + {b}"
    elif op == "-":
        # Ensure positive result
        if a < b:
            a, b = b, a
        answer = a - b
        question = f"{a} - {b}"
    else:
        # Keep multiplication small
        a = random.randint(1, 10)
        b = random.randint(1, 10)
        answer = a * b
        question = f"{a} × {b}"
    return question, answer

# ═══════════════════════════════════════════════════════════════════════════════
#  STATES
# ═══════════════════════════════════════════════════════════════════════════════
(
    CAPTCHA_STATE,
    BROWSE_CATEGORY, SELECT_SERVICE, ENTER_LINK,
    ENTER_QUANTITY, CONFIRM_ORDER,
) = range(6)

# ═══════════════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Services Order Karo", callback_data="browse")],
        [InlineKeyboardButton("📊 Mera Balance", callback_data="my_balance"),
         InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("🔔 Track Order", callback_data="track_order"),
         InlineKeyboardButton("❓ Support", callback_data="help")],
        [InlineKeyboardButton("🤝 Refer & Earn", callback_data="refer_earn")],
        [InlineKeyboardButton("ℹ️ How To Use", callback_data="how_to_use")]
    ])

def back_keyboard(back_to="main"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"back_{back_to}")
    ]])

def channel_join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Channel Join Karo", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("✅ Maine Join Kar Liya", callback_data="check_join")]
    ])

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
async def show_main_menu(update, context, edit=False):
    user = update.effective_user
    user_data = get_user(user.id)
    balance = user_data.get("balance", 0)

    col = get_col("orders")
    total_orders = col.count_documents({"user_id": str(user.id)}) if col else 0

    text = (
        f"👋 *Welcome, {user.first_name}!*\n\n"
        f"🚀 *ProGrow SMM Panel*\n"
        f"_Professional Social Media Growth Services_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Balance:*  ₹{balance:.2f}\n"
        f"📦 *Total Orders:* {total_orders}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📸 Instagram  •  ▶️ YouTube\n"
        f"👥 Facebook  •  ✈️ Telegram\n\n"
        f"1000+ premium services available! 🎯\n\n"
        f"Select an option below 👇"
    )

    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )

# ═══════════════════════════════════════════════════════════════════════════════
#  /START — CAPTCHA → CHANNEL → BONUS
# ═══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args  # Referral check

    # Referral code save karo (process baad mein hoga)
    if args and args[0].startswith("ref_"):
        referrer_id = args[0].replace("ref_", "")
        if referrer_id != str(user.id):
            context.user_data["pending_referrer"] = referrer_id

    user_doc = get_user(user.id)

    # Agar user pehle se verified hai to seedha main menu
    if user_doc.get("joined") and user_doc.get("signup_bonus_given"):
        await show_main_menu(update, context)
        return ConversationHandler.END

    # Step 1: Captcha dikhao
    question, answer = generate_captcha()
    context.user_data["captcha_answer"] = answer
    context.user_data["captcha_solved"] = False

    await update.effective_message.reply_text(
        f"👋 *Welcome to ProGrow SMM Panel!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Captcha Verify Karein*\n\n"
        f"Neeche diya gaya calculation solve karein:\n\n"
        f"📝 *{question} = ?*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Sirf answer number mein type karein_",
        parse_mode="Markdown"
    )
    return CAPTCHA_STATE

async def captcha_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captcha answer verify karo"""
    text = update.message.text.strip()

    try:
        user_answer = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Sirf *number* type karein!\n\nDobara try karein:",
            parse_mode="Markdown"
        )
        return CAPTCHA_STATE

    correct = context.user_data.get("captcha_answer")

    if user_answer != correct:
        # Naya captcha do
        question, answer = generate_captcha()
        context.user_data["captcha_answer"] = answer
        await update.message.reply_text(
            f"❌ *Galat answer!*\n\n"
            f"Naya question try karein:\n\n"
            f"📝 *{question} = ?*",
            parse_mode="Markdown"
        )
        return CAPTCHA_STATE

    # Captcha solved!
    context.user_data["captcha_solved"] = True

    # Step 2: Channel join karne bolo
    await update.message.reply_text(
        f"✅ *Captcha Solved!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 *Hamara Channel Join Karein*\n\n"
        f"Bot use karne ke liye aur *₹10 signup bonus* paane ke liye\n"
        f"pehle hamara Telegram channel join karna zaroori hai!\n\n"
        f"👇 Neeche button dabao:",
        parse_mode="Markdown",
        reply_markup=channel_join_keyboard()
    )
    return ConversationHandler.END

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne 'Maine Join Kar Liya' dabaya — verify karo"""
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    # Captcha pehle solve hona chahiye
    if not context.user_data.get("captcha_solved"):
        await query.edit_message_text(
            "⚠️ Pehle captcha solve karein!\n\n/start dabayein.",
            parse_mode="Markdown"
        )
        return

    is_member = await check_channel_membership(context.bot, user.id)

    if not is_member:
        await query.edit_message_text(
            f"❌ *Aapne Channel Join Nahi Kiya!*\n\n"
            f"Pehle channel join karein, phir button dabayein.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _Channel join kiye bina bot use nahi hoga_",
            parse_mode="Markdown",
            reply_markup=channel_join_keyboard()
        )
        return

    # Channel joined! Signup bonus do
    user_doc = get_user(user.id)
    new_balance = user_doc.get("balance", 0)

    if not user_doc.get("signup_bonus_given"):
        new_balance = add_balance(user.id, SIGNUP_BONUS)
        update_user(user.id, {"joined": True, "signup_bonus_given": True})
    else:
        update_user(user.id, {"joined": True})

    # Referral bonus check karo
    referral_msg = ""
    pending_referrer = context.user_data.get("pending_referrer")
    if pending_referrer and not user_doc.get("signup_bonus_given"):
        # Referral save karo
        save_referral(pending_referrer, user.id)

        # Referral bonus referrer ko do
        referrer_new_bal = add_balance(pending_referrer, REFERRAL_BONUS)
        mark_referral_paid(user.id)

        referral_msg = f"\n🤝 *Referral Bonus:* Aapke referrer ko ₹{REFERRAL_BONUS} mil gaye!"

        # Referrer ko notify karo
        try:
            ref_count = get_referral_count(pending_referrer)
            await context.bot.send_message(
                int(pending_referrer),
                f"🎉 *Referral Bonus Mila!*\n\n"
                f"Aapke referral ne channel join kar liya!\n"
                f"💰 *+₹{REFERRAL_BONUS}* aapke wallet mein add ho gaya!\n"
                f"💳 *New Balance:* ₹{referrer_new_bal:.2f}\n"
                f"👥 *Total Successful Referrals:* {ref_count}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        context.user_data.pop("pending_referrer", None)

    await query.edit_message_text(
        f"🎉 *Welcome to ProGrow SMM Panel!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Channel Join Verified!\n"
        f"🎁 *Signup Bonus: +₹{SIGNUP_BONUS}* credited!\n"
        f"💰 *Current Balance:* ₹{new_balance:.2f}"
        f"{referral_msg}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ab bot ka mazaa lo! 🚀",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  REFER & EARN
# ═══════════════════════════════════════════════════════════════════════════════
async def refer_earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    ref_count = get_referral_count(user.id)

    user_doc = get_user(user.id)
    balance = user_doc.get("balance", 0)

    await query.edit_message_text(
        f"🤝 *Refer & Earn Program*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💸 *Har Referral pe:* ₹{REFERRAL_BONUS:.0f}\n"
        f"✅ *Successful Referrals:* {ref_count}\n"
        f"💰 *Aapka Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 *Apna Referral Link Share Karo:*\n"
        f"`{referral_link}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Kaise Kaam Karta Hai:*\n\n"
        f"1️⃣ Apna link apne dosto ko share karo\n"
        f"2️⃣ Woh link se bot open karein\n"
        f"3️⃣ Woh captcha solve karein\n"
        f"4️⃣ Channel join karein\n"
        f"5️⃣ Bot start karte hi aapko *₹{REFERRAL_BONUS:.0f}* mil jayenge!\n\n"
        f"⚠️ _Bonus tabhi milega jab dost channel join karke bot start kare_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  BROWSE SERVICES
# ═══════════════════════════════════════════════════════════════════════════════
async def browse_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Channel membership verify karo pehle
    is_member = await check_channel_membership(context.bot, update.effective_user.id)
    if not is_member:
        await query.edit_message_text(
            "⚠️ *Pehle Channel Join Karein!*\n\nBot use karne ke liye channel join karna zaroori hai.",
            parse_mode="Markdown",
            reply_markup=channel_join_keyboard()
        )
        return ConversationHandler.END

    await query.edit_message_text(
        "⏳ *Services load ho rahi hain...*\n\nThoda wait karein!",
        parse_mode="Markdown"
    )

    cached = context.bot_data.get("services_cache")
    if cached:
        services = cached
    else:
        services = await get_services()
        if services and isinstance(services, list):
            context.bot_data["services_cache"] = services

    if not services:
        await query.edit_message_text(
            "❌ *Services load nahi ho sakein!*\n\nThodi der baad try karein.",
            parse_mode="Markdown",
            reply_markup=back_keyboard()
        )
        return ConversationHandler.END

    PLATFORMS = {
        "instagram": {"emoji": "📸", "label": "Instagram"},
        "facebook":  {"emoji": "👥", "label": "Facebook"},
        "youtube":   {"emoji": "▶️", "label": "YouTube"},
        "telegram":  {"emoji": "✈️", "label": "Telegram"},
    }

    platform_services = {p: [] for p in PLATFORMS}
    all_categories = {}

    for s in services:
        cat = s.get("category", "Other")
        for p in PLATFORMS:
            if p in cat.lower():
                platform_services[p].append(s)
                if cat not in all_categories:
                    all_categories[cat] = []
                all_categories[cat].append(s)

    context.user_data["categories"] = all_categories
    context.user_data["platform_services"] = platform_services
    context.user_data["services"] = {str(s["service"]): s for s in services}

    buttons = [
        [
            InlineKeyboardButton(f"📸 Instagram", callback_data="platform_instagram"),
            InlineKeyboardButton(f"▶️ YouTube", callback_data="platform_youtube"),
        ],
        [
            InlineKeyboardButton(f"👥 Facebook", callback_data="platform_facebook"),
            InlineKeyboardButton(f"✈️ Telegram", callback_data="platform_telegram"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]

    total = sum(len(v) for v in platform_services.values())
    await query.edit_message_text(
        f"🛍️ *Services Browse Karein*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Total Services:* {total}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Platform select karein 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return BROWSE_CATEGORY

async def show_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    platform = query.data.replace("platform_", "")
    PLATFORM_EMOJIS = {
        "instagram": "📸", "facebook": "👥",
        "youtube": "▶️", "telegram": "✈️"
    }
    emoji = PLATFORM_EMOJIS.get(platform, "📂")

    all_categories = context.user_data.get("categories", {})
    platform_services = context.user_data.get("platform_services", {})

    sub_cats = {}
    for cat, svcs in all_categories.items():
        if platform in cat.lower():
            sub_cats[cat] = svcs

    if not sub_cats:
        await query.answer("❌ Koi service nahi mili!", show_alert=True)
        return BROWSE_CATEGORY

    context.user_data["current_platform"] = platform
    total = len(platform_services.get(platform, []))

    buttons = []
    for cat in sorted(sub_cats.keys()):
        count = len(sub_cats[cat])
        short = cat
        for p in ["Instagram", "Facebook", "YouTube", "Telegram"]:
            short = short.replace(p, "").strip(" -|")
        if not short:
            short = cat
        buttons.append([InlineKeyboardButton(
            f"{emoji} {short[:28]} ({count})",
            callback_data=f"cat_{cat[:30]}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse_services")])

    await query.edit_message_text(
        f"{emoji} *{platform.capitalize()} Services*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Total:* {total} services\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Category select karein 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return BROWSE_CATEGORY

async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat_name = query.data.replace("cat_", "")
    categories = context.user_data.get("categories", {})

    full_cat = None
    for c in categories:
        if c.startswith(cat_name) or c[:30] == cat_name:
            full_cat = c
            break

    if not full_cat:
        await query.answer("Category nahi mili!", show_alert=True)
        return BROWSE_CATEGORY

    services = categories[full_cat]
    context.user_data["current_category"] = full_cat

    buttons = []
    for s in services[:20]:
        sid = s["service"]
        name = s["name"][:40]
        rate = calculate_price(s["rate"], 1000)
        buttons.append([InlineKeyboardButton(
            f"#{sid} {name} — ₹{rate}/1K",
            callback_data=f"svc_{sid}"
        )])

    buttons.append([InlineKeyboardButton("🔙 Categories", callback_data="browse")])

    await query.edit_message_text(
        f"📂 *{full_cat}*\n\n"
        f"{len(services)} services available hain\n"
        f"_(Prices per 1000 units)_\n\n"
        f"Service select karein 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return SELECT_SERVICE

async def show_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sid = query.data.replace("svc_", "")
    services = context.user_data.get("services", {})
    s = services.get(sid)

    if not s:
        await query.answer("Service nahi mili!", show_alert=True)
        return SELECT_SERVICE

    context.user_data["selected_service"] = s

    min_qty = s.get("min", 10)
    max_qty = s.get("max", 10000)
    price_1k = calculate_price(s["rate"], 1000)

    text = (
        f"🔷 *Service Details*\n\n"
        f"*{s['name']}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Price:* ₹{price_1k} per 1000\n"
        f"📉 *Min Order:* {min_qty}\n"
        f"📈 *Max Order:* {max_qty}\n"
        f"🔄 *Refill:* {'✅' if s.get('refill') else '❌'}\n"
        f"❌ *Cancel:* {'✅' if s.get('cancel') else '❌'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Order karne ke liye *link* bhejein 👇"
    )

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=f"cat_{context.user_data.get('current_category', '')[:30]}")
        ]])
    )

    return ENTER_LINK

async def enter_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    context.user_data["order_link"] = link

    s = context.user_data.get("selected_service", {})
    min_qty = s.get("min", 10)
    max_qty = s.get("max", 10000)

    await update.message.reply_text(
        f"✅ *Link save ho gaya!*\n\n"
        f"Ab *quantity* enter karein:\n"
        f"_(Min: {min_qty} | Max: {max_qty})_",
        parse_mode="Markdown"
    )

    return ENTER_QUANTITY

async def enter_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Sirf number enter karein!")
        return ENTER_QUANTITY

    s = context.user_data.get("selected_service", {})
    min_qty = int(s.get("min", 10))
    max_qty = int(s.get("max", 10000))

    if qty < min_qty or qty > max_qty:
        await update.message.reply_text(
            f"❌ Quantity {min_qty} aur {max_qty} ke beech honi chahiye!"
        )
        return ENTER_QUANTITY

    context.user_data["order_qty"] = qty
    total_price = calculate_price(s["rate"], qty)
    context.user_data["order_price"] = total_price

    # Minimum ₹50 order check
    if total_price < MIN_ORDER_AMOUNT:
        await update.message.reply_text(
            f"❌ *Minimum Order ₹{MIN_ORDER_AMOUNT:.0f} ka hona chahiye!*\n\n"
            f"Is service ka total price ₹{total_price:.2f} hai jo minimum se kam hai.\n"
            f"Zyada quantity try karein ya koi aur service choose karein.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Services Dekho", callback_data="browse")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]
            ])
        )
        return ConversationHandler.END

    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    text = (
        f"📋 *Order Summary*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name']}\n"
        f"🔗 *Link:* `{context.user_data['order_link']}`\n"
        f"🔢 *Quantity:* {qty}\n"
        f"💰 *Total Price:* ₹{total_price}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Aapka Balance:* ₹{balance:.2f}\n"
    )

    if balance >= total_price:
        text += f"\n✅ *Balance sufficient hai!*\nOrder confirm karein?"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Order", callback_data="confirm_order")],
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
        ])
    else:
        needed = round(total_price - balance, 2)
        text += (
            f"\n❌ *Balance kam hai!* ₹{needed} aur chahiye.\n\n"
            f"💡 Refer karein aur balance earn karein!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Refer & Earn", callback_data="refer_earn")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    return CONFIRM_ORDER

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    s = context.user_data.get("selected_service", {})
    qty = context.user_data.get("order_qty")
    price = context.user_data.get("order_price")
    link = context.user_data.get("order_link")

    user_data = get_user(user.id)
    if user_data.get("balance", 0) < price:
        await query.edit_message_text(
            "❌ Balance kam hai! Refer karein aur balance earn karein.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤝 Refer & Earn", callback_data="refer_earn")
            ]])
        )
        return ConversationHandler.END

    # Minimum order check again
    if price < MIN_ORDER_AMOUNT:
        await query.edit_message_text(
            f"❌ *Minimum order ₹{MIN_ORDER_AMOUNT:.0f} ka hona chahiye!*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    await query.edit_message_text("⏳ *Order place ho raha hai...*", parse_mode="Markdown")

    # SMMKings pe order place karo
    result = await place_order(s["service"], link, qty)

    if result and "order" in result:
        smm_order_id = result["order"]
        deduct_balance(user.id, price)

        save_order(user.id, {
            "smm_order_id": smm_order_id,
            "service_id": s["service"],
            "service_name": s["name"],
            "link": link,
            "quantity": qty,
            "price": price,
            "status": "pending"
        })

        new_balance = get_user(user.id).get("balance", 0)

        # Admin ko order notify karo
        for admin_id in ADMIN_IDS:
            if admin_id:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"🛍️ *Naya Order!*\n\n"
                        f"🔷 Service: {s['name']}\n"
                        f"🔗 Link: {link}\n"
                        f"🔢 Qty: {qty}\n"
                        f"💰 Price: ₹{price}\n"
                        f"🆔 SMM Order ID: {smm_order_id}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

        await query.edit_message_text(
            f"✅ *Order Successfully Place Ho Gaya!*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Order ID:* `{smm_order_id}`\n"
            f"🔷 *Service:* {s['name']}\n"
            f"🔢 *Quantity:* {qty}\n"
            f"💰 *Charged:* ₹{price}\n"
            f"💳 *Remaining Balance:* ₹{new_balance:.2f}\n"
            f"📊 *Status:* ⏳ Pending\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏰ Delivery shuru ho jaayegi jaldi!\n"
            f"Status check ke liye 'Track Order' use karein.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    else:
        error = result.get("error", "Unknown error") if result else "API error"
        await query.edit_message_text(
            f"❌ *Order Failed!*\n\nError: `{error}`\n\nDobara try karein.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  TRACK ORDER — Always Pending
# ═══════════════════════════════════════════════════════════════════════════════
async def track_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    col = get_col("orders")
    orders = []
    if col:
        orders = list(col.find(
            {"user_id": str(update.effective_user.id)},
            sort=[("created_at", -1)],
            limit=5
        ))

    if not orders:
        await query.edit_message_text(
            "🔔 *Track Order*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "❌ Koi active order nahi mila!\n\n"
            "Order karne ke liye 'Services Order Karo' dabayein.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Services Order Karo", callback_data="browse")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ])
        )
        return

    text = "🔔 *Order Tracking*\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for o in orders:
        text += (
            f"⏳ *Order #{o.get('smm_order_id', 'N/A')}*\n"
            f"📦 {o.get('service_name', 'N/A')[:35]}\n"
            f"🔢 Qty: {o.get('quantity')} | 💰 ₹{o.get('price')}\n"
            f"📊 Status: *Pending*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
        )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="track_order")],
            [InlineKeyboardButton("🛍️ New Order", callback_data="browse")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  BALANCE & ORDERS
# ═══════════════════════════════════════════════════════════════════════════════
async def my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_data = get_user(update.effective_user.id)
    balance = user_data.get("balance", 0)
    ref_count = get_referral_count(update.effective_user.id)

    col = get_col("orders")
    total_orders = col.count_documents({"user_id": str(update.effective_user.id)}) if col else 0

    await query.edit_message_text(
        f"💰 *My Wallet*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 *Current Balance:* ₹{balance:.2f}\n"
        f"📦 *Total Orders:* {total_orders}\n"
        f"🤝 *Successful Referrals:* {ref_count}\n"
        f"💸 *Referral Earnings:* ₹{ref_count * REFERRAL_BONUS:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💡 _Refer karein aur aur balance earn karein!_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Refer & Earn", callback_data="refer_earn")],
            [InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    col = get_col("orders")
    orders = []
    if col:
        orders = list(col.find(
            {"user_id": str(update.effective_user.id)},
            sort=[("created_at", -1)],
            limit=5
        ))

    if not orders:
        await query.edit_message_text(
            "📋 *Koi order nahi mila!*\n\nPehla order karein! 🚀",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Services", callback_data="browse")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ])
        )
        return

    text = "📋 *Aapke Recent Orders:*\n\n"
    for o in orders:
        text += (
            f"⏳ *Order #{o.get('smm_order_id', 'N/A')}*\n"
            f"   {o.get('service_name', 'N/A')[:30]}\n"
            f"   Qty: {o.get('quantity')} | ₹{o.get('price')}\n"
            f"   Status: *Pending*\n\n"
        )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Track Order", callback_data="track_order")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  HOW TO USE
# ═══════════════════════════════════════════════════════════════════════════════
async def how_to_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "ℹ️ *How To Use — ProGrow SMM Panel*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 *Step 1 — Bot Start Karo*\n"
        "• Captcha solve karo\n"
        "• Channel join karo → *₹10 free* milenge!\n\n"
        "📌 *Step 2 — Balance Earn Karo*\n"
        "• Dosto ko refer karo\n"
        "• Har successful referral pe *₹7* milenge\n\n"
        "📌 *Step 3 — Service Select Karo*\n"
        "• 'Services Order Karo' dabayein\n"
        "• Platform select karo (Instagram/YouTube etc)\n"
        "• Category aur service chunein\n\n"
        "📌 *Step 4 — Order Karo*\n"
        "• Profile/Post ka link paste karo\n"
        "• Quantity enter karo\n"
        "• Order confirm karo (min ₹50)\n\n"
        "📌 *Step 5 — Track Karo*\n"
        "• 'Track Order' se status dekho\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 _Koi problem? Support se contact karein!_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Refer & Earn", callback_data="refer_earn")],
            [InlineKeyboardButton("🛍️ Order Karo", callback_data="browse")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  HELP
# ═══════════════════════════════════════════════════════════════════════════════
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        f"❓ *Support & Help*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Quick Guide:*\n\n"
        f"1️⃣ *Services Order Karo* — Platform select karo, service chunein\n"
        f"2️⃣ *Refer & Earn* — Dosto ko refer karo, ₹7/referral pao\n"
        f"3️⃣ *Order* — Link & quantity enter karo (min ₹50)\n"
        f"4️⃣ *Track Order* — Status check karo\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📞 *Contact Admin:* @NexusXedit\n\n"
        f"*Support ke liye admin se contact karein!*",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin stats: /stats"""
    if update.effective_user.id not in ADMIN_IDS:
        return

    users_col = get_col("users")
    orders_col = get_col("orders")
    referrals_col = get_col("referrals")

    total_users = users_col.count_documents({}) if users_col else 0
    total_orders = orders_col.count_documents({}) if orders_col else 0
    total_referrals = referrals_col.count_documents({"bonus_paid": True}) if referrals_col else 0
    referral_payout = total_referrals * REFERRAL_BONUS
    signup_payout = (users_col.count_documents({"signup_bonus_given": True}) if users_col else 0) * SIGNUP_BONUS

    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📦 Total Orders: {total_orders}\n"
        f"🤝 Successful Referrals: {total_referrals}\n"
        f"💸 Referral Payouts: ₹{referral_payout:.2f}\n"
        f"🎁 Signup Bonuses Given: ₹{signup_payout:.2f}",
        parse_mode="Markdown"
    )

async def admin_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin manually balance add kare: /addbal USER_ID AMOUNT"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        args = context.args
        user_id = int(args[0])
        amount = float(args[1])
        new_balance = add_balance(user_id, amount)
        await update.message.reply_text(
            f"✅ *Balance Added!*\nUser: `{user_id}`\nAmount: ₹{amount}\nNew Balance: ₹{new_balance}",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            user_id,
            f"✅ *₹{amount} aapke wallet mein add ho gaya!*\n💳 *Balance:* ₹{new_balance:.2f}",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\nFormat: /addbal USER_ID AMOUNT")

# ═══════════════════════════════════════════════════════════════════════════════
#  BACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ("back_main", "main_menu"):
        await show_main_menu(update, context, edit=True)
    elif query.data == "browse" or query.data == "browse_services":
        await browse_services(update, context)

    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Start + Captcha conversation
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CAPTCHA_STATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_handler)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )

    # Order conversation
    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(browse_services, pattern="^browse$")],
        states={
            BROWSE_CATEGORY: [
                CallbackQueryHandler(show_platform, pattern="^platform_"),
                CallbackQueryHandler(show_category, pattern="^cat_"),
                CallbackQueryHandler(browse_services, pattern="^browse_services$"),
                CallbackQueryHandler(back_handler, pattern="^back_")
            ],
            SELECT_SERVICE: [
                CallbackQueryHandler(show_service, pattern="^svc_"),
                CallbackQueryHandler(browse_services, pattern="^browse$"),
                CallbackQueryHandler(show_category, pattern="^cat_"),
                CallbackQueryHandler(my_balance, pattern="^my_balance$"),
                CallbackQueryHandler(my_orders, pattern="^my_orders$"),
                CallbackQueryHandler(track_order, pattern="^track_order$"),
                CallbackQueryHandler(how_to_use, pattern="^how_to_use$"),
            ],
            ENTER_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_link),
                CallbackQueryHandler(show_category, pattern="^cat_"),
            ],
            ENTER_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_quantity)
            ],
            CONFIRM_ORDER: [
                CallbackQueryHandler(confirm_order, pattern="^confirm_order$"),
                CallbackQueryHandler(back_handler, pattern="^back_"),
                CallbackQueryHandler(refer_earn, pattern="^refer_earn$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(back_handler, pattern="^back_main$")
        ],
        allow_reentry=True
    )

    app.add_handler(start_conv)
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("addbal", admin_add_balance))
    app.add_handler(order_conv)
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(my_balance, pattern="^my_balance$"))
    app.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(track_order, pattern="^track_order$"))
    app.add_handler(CallbackQueryHandler(how_to_use, pattern="^how_to_use$"))
    app.add_handler(CallbackQueryHandler(refer_earn, pattern="^refer_earn$"))
    app.add_handler(CallbackQueryHandler(back_handler, pattern="^back_"))

    logger.info("SMM Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
