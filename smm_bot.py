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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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
#  MONGODB — Single persistent connection
# ═══════════════════════════════════════════════════════════════════════════════
_db = None

def get_db():
    global _db
    if _db is not None:
        return _db
    if not MONGO_URI:
        return None
    try:
        import certifi
        client = MongoClient(
            MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=10
        )
    except Exception:
        client = MongoClient(
            MONGO_URI,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=10
        )
    _db = client["smmbot"]
    return _db

def get_col(name):
    db = get_db()
    if db is None:
        return None
    return db[name]

def get_user(uid):
    col = get_col("users")
    uid = str(uid)
    if col is None:
        return {"_id": uid, "balance": 0.0, "orders": [], "joined": False, "signup_bonus_given": False, "captcha_solved": False}
    doc = col.find_one_and_update(
        {"_id": uid},
        {"$setOnInsert": {"_id": uid, "balance": 0.0, "orders": [], "joined": False, "signup_bonus_given": False, "captcha_solved": False}},
        upsert=True,
        return_document=True
    )
    return doc

def update_user(uid, data):
    col = get_col("users")
    if col is not None:
        col.update_one({"_id": str(uid)}, {"$set": data}, upsert=True)

def add_balance(uid, amount):
    col = get_col("users")
    if col is not None:
        result = col.find_one_and_update(
            {"_id": str(uid)},
            {"$inc": {"balance": round(amount, 2)}},
            upsert=True,
            return_document=True
        )
        return round(result.get("balance", 0), 2) if result else 0
    return 0

def deduct_balance(uid, amount):
    col = get_col("users")
    if col is not None:
        result = col.find_one_and_update(
            {"_id": str(uid)},
            {"$inc": {"balance": -round(amount, 2)}},
            upsert=True,
            return_document=True
        )
        return round(result.get("balance", 0), 2) if result else 0
    return 0

def save_order(uid, order_data):
    col = get_col("orders")
    if col is not None:
        order_data["user_id"] = str(uid)
        order_data["created_at"] = datetime.now()
        col.insert_one(order_data)

def get_pending_referral(referred_uid):
    """Check karo kisi ka referral pending hai"""
    col = get_col("referrals")
    if col is not None:
        return col.find_one({"referred_id": str(referred_uid), "bonus_paid": False})
    return None

def mark_referral_paid(referred_uid):
    col = get_col("referrals")
    if col is not None:
        col.update_one({"referred_id": str(referred_uid)}, {"$set": {"bonus_paid": True}})

def save_referral(referrer_uid, referred_uid):
    col = get_col("referrals")
    if col is not None:
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
    if col is not None:
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
    ENTER_QUANTITY, CONFIRM_ORDER, CALC_QUANTITY,
) = range(7)

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
        [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")],
        [InlineKeyboardButton("🆓 How Is This Free?", callback_data="why_free")],
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
#  QUANTITY CALCULATOR — Live price keyboard
# ═══════════════════════════════════════════════════════════════════════════════
async def qty_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick quantity button dabaya"""
    query = update.callback_query
    await query.answer()

    data = query.data  # qty_100, qty_500, qty_custom

    s = context.user_data.get("selected_service", {})
    min_qty = int(s.get("min", 10))
    max_qty = int(s.get("max", 10000))
    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    if data == "qty_custom":
        s2 = context.user_data.get("selected_service", {})
        min_qty2 = int(s2.get("min", 10))
        max_qty2 = int(s2.get("max", 10000))

        # Quick suggestion buttons as Reply Keyboard
        suggestions = [min_qty2, 500, 1000, 2000, 5000, 10000]
        suggestions = sorted(list(set([q for q in suggestions if min_qty2 <= q <= max_qty2])))[:6]

        reply_kb = ReplyKeyboardMarkup(
            [[KeyboardButton(str(q)) for q in suggestions[i:i+3]] for i in range(0, len(suggestions), 3)],
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder=f"Min {min_qty2} - Max {max_qty2}"
        )

        await query.message.reply_text(
            f"✏️ *Quantity Enter Karo*\n\n"
            f"💳 *Balance:* ₹{balance:.2f}\n"
            f"📉 *Min:* {min_qty2} | 📈 *Max:* {max_qty2}\n\n"
            f"_Neeche se select karo ya khud type karo:_",
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        context.user_data["awaiting_quantity"] = True
        return ENTER_QUANTITY

    qty = int(data.replace("qty_", ""))
    total_price = calculate_price(s["rate"], qty)

    # Live price update keyboard
    quick_qtys = [min_qty, 500, 1000, 2000, 5000, 10000]
    quick_qtys = sorted(list(set([q for q in quick_qtys if min_qty <= q <= max_qty])))[:6]

    buttons = []
    row = []
    for q in quick_qtys:
        price = calculate_price(s.get("rate", 0), q)
        label = f"✅ {q} — ₹{price:.1f}" if q == qty else f"{q} — ₹{price:.1f}"
        row.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Status line
    if total_price < MIN_ORDER_AMOUNT:
        status = f"⚠️ Min order ₹{MIN_ORDER_AMOUNT:.0f} — quantity badhao!"
        can_order = False
    elif balance < total_price:
        needed = round(total_price - balance, 2)
        status = f"❌ ₹{needed} aur chahiye"
        can_order = False
    else:
        status = f"✅ Balance sufficient!"
        can_order = True

    if can_order:
        buttons.append([InlineKeyboardButton(f"✅ Confirm — ₹{total_price:.2f}", callback_data=f"confirm_qty_{qty}")])
    else:
        buttons.append([InlineKeyboardButton("👥 Refer & Earn — Balance Badhao", callback_data="refer_earn")])

    buttons.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data="qty_custom")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse")])

    await query.edit_message_text(
        f"📊 *Live Price Calculator*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name'][:35]}\n"
        f"🔢 *Selected Qty:* {qty}\n"
        f"💰 *Price:* ₹{total_price:.2f}\n"
        f"💳 *Your Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CALC_QUANTITY

async def confirm_qty_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm button directly from calculator"""
    query = update.callback_query
    await query.answer()

    qty = int(query.data.replace("confirm_qty_", ""))
    s = context.user_data.get("selected_service", {})
    total_price = calculate_price(s["rate"], qty)

    context.user_data["order_qty"] = qty
    context.user_data["order_price"] = total_price

    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    if balance < total_price:
        needed = round(total_price - balance, 2)
        await query.answer(f"❌ Balance kam hai! ₹{needed} aur chahiye.", show_alert=True)
        return CALC_QUANTITY

    await query.edit_message_text(
        f"📋 *Order Summary*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name']}\n"
        f"🔗 *Link:* `{context.user_data.get('order_link', '')}`\n"
        f"🔢 *Quantity:* {qty}\n"
        f"💰 *Total Price:* ₹{total_price:.2f}\n"
        f"💳 *Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Balance sufficient!* Confirm karein?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")],
            [InlineKeyboardButton("🔙 Back", callback_data="browse")]
        ])
    )
    return CONFIRM_ORDER

# ═══════════════════════════════════════════════════════════════════════════════
#  GLOBAL MESSAGE HANDLER — Captcha ke liye (session-safe)
# ═══════════════════════════════════════════════════════════════════════════════
async def global_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har text message yahan aata hai — captcha pending ho to check karo"""
    user = update.effective_user
    user_doc = get_user(user.id)

    # Agar already verified hai to ignore karo
    if user_doc.get("joined") and user_doc.get("signup_bonus_given"):
        return

    # Quantity awaiting hai? (qty_custom ke baad)
    if context.user_data.get("awaiting_quantity"):
        text = update.message.text.strip()
        try:
            qty = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Sirf number type karo!",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        s = context.user_data.get("selected_service", {})
        if not s:
            context.user_data["awaiting_quantity"] = False
            return

        min_qty = int(s.get("min", 10))
        max_qty = int(s.get("max", 10000))
        user2 = get_user(user.id)
        balance = user2.get("balance", 0)
        total_price = calculate_price(s["rate"], qty)
        context.user_data["awaiting_quantity"] = False

        if qty < min_qty or qty > max_qty:
            suggestions = sorted(list(set([q for q in [min_qty, 500, 1000, 2000, 5000, 10000] if min_qty <= q <= max_qty])))[:6]
            reply_kb = ReplyKeyboardMarkup(
                [[KeyboardButton(str(q)) for q in suggestions[i:i+3]] for i in range(0, len(suggestions), 3)],
                resize_keyboard=True, one_time_keyboard=True
            )
            await update.message.reply_text(
                f"❌ *Invalid!* {min_qty} aur {max_qty} ke beech enter karo:",
                parse_mode="Markdown",
                reply_markup=reply_kb
            )
            context.user_data["awaiting_quantity"] = True
            return

        context.user_data["order_qty"] = qty
        context.user_data["order_price"] = total_price

        # Status
        if total_price < MIN_ORDER_AMOUNT:
            status_line = f"⚠️ Min order ₹{MIN_ORDER_AMOUNT:.0f} — quantity badhao!"
            can_order = False
        elif balance < total_price:
            needed = round(total_price - balance, 2)
            status_line = f"❌ ₹{needed} aur chahiye"
            can_order = False
        else:
            status_line = "✅ Balance sufficient!"
            can_order = True

        quick_qtys = sorted(list(set([q for q in [min_qty, 500, 1000, 2000, 5000, 10000] if min_qty <= q <= max_qty])))[:6]
        buttons = []
        row = []
        for q in quick_qtys:
            price = calculate_price(s.get("rate", 0), q)
            label = f"✅ {q} — ₹{price:.1f}" if q == qty else f"{q} — ₹{price:.1f}"
            row.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        if can_order:
            buttons.append([InlineKeyboardButton(f"✅ Confirm — ₹{total_price:.2f}", callback_data=f"confirm_qty_{qty}")])
        else:
            if total_price < MIN_ORDER_AMOUNT:
                buttons.append([InlineKeyboardButton("🔄 Order Again", callback_data=f"svc_{s.get('service', '')}")])
            else:
                buttons.append([InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")])

        buttons.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data="qty_custom")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse")])

        await update.message.reply_text(
            f"📊 *Live Price Calculator*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔷 *Service:* {s['name'][:35]}\n"
            f"🔢 *Qty:* {qty}\n"
            f"💰 *Price:* ₹{total_price:.2f}\n"
            f"💳 *Balance:* ₹{balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_line}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Captcha pending hai?
    if not user_doc.get("captcha_solved") and user_doc.get("captcha_answer") is not None:
        text = update.message.text.strip()
        try:
            user_answer = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Sirf *number* type karein!\n\nDobara try karein:",
                parse_mode="Markdown"
            )
            return

        correct = user_doc.get("captcha_answer")

        if user_answer != correct:
            question, answer = generate_captcha()
            update_user(user.id, {"captcha_answer": answer})
            await update.message.reply_text(
                f"❌ *Galat answer!*\n\n"
                f"Naya question try karein:\n\n"
                f"📝 *{question} = ?*",
                parse_mode="Markdown"
            )
            return

        # Sahi answer!
        update_user(user.id, {"captcha_solved": True, "captcha_answer": None})
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

# ═══════════════════════════════════════════════════════════════════════════════
#  WHY FREE
# ═══════════════════════════════════════════════════════════════════════════════
async def why_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🆓 *How Is ProGrow SMM Panel Free?*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💰 *How Do We Earn?*\n"
        "We buy SMM services in bulk at wholesale rates from trusted providers like SMMKings. "
        "When you place an order, a small markup is added on top of the wholesale price. "
        "This markup covers our costs and keeps the panel running.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 *Why Do We Give Free Balance?*\n"
        "The ₹10 signup bonus and ₹7 referral bonus come from our marketing budget. "
        "Instead of spending money on ads, we reward YOU directly for joining and referring friends. "
        "Every new user = more orders = more revenue = we keep giving bonuses.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *Are The Services Real?*\n"
        "100% real. Your orders go directly to SMMKings API — one of the most trusted SMM providers worldwide. "
        "We are simply a Telegram-based interface. "
        "You can verify any order ID on SMMKings directly.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔒 *Why No Deposit System?*\n"
        "We intentionally removed deposits to build trust. "
        "New users can try our services completely risk-free with the signup bonus. "
        "If you're satisfied, refer friends and earn more balance. Simple & transparent.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 _No hidden charges. No fake promises. Just real growth._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")],
            [InlineKeyboardButton("🛍️ Order Karo", callback_data="browse")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
async def show_main_menu(update, context, edit=False):
    user = update.effective_user
    user_data = get_user(user.id)
    balance = user_data.get("balance", 0)

    col = get_col("orders")
    total_orders = col.count_documents({"user_id": str(user.id)}) if col is not None else 0

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
    # Answer DB mein save karo — session-safe
    update_user(user.id, {"captcha_answer": answer, "captcha_solved": False})

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



async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne 'Maine Join Kar Liya' dabaya — verify karo"""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    user_doc = get_user(user.id)

    # Captcha solve nahi kiya to wapas bhejo
    if not user_doc.get("captcha_solved") and not user_doc.get("signup_bonus_given"):
        await query.edit_message_text(
            "⚠️ *Pehle Captcha Solve Karein!*\n\n/start dabayein aur captcha complete karein.",
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
        f"📸 Instagram | ▶️ YouTube | 👥 Facebook | ✈️ Telegram\n"
        f"✅ *All Services Available 24/7*",
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

    import time
    cache_data = context.bot_data.get("services_cache")
    cache_time = context.bot_data.get("services_cache_time", 0)
    if cache_data and (time.time() - cache_time) < 1800:  # 30 min cache
        services = cache_data
    else:
        services = await get_services()
        if services and isinstance(services, list):
            context.bot_data["services_cache"] = services
            context.bot_data["services_cache_time"] = time.time()

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

    # Basic link validation
    if not (link.startswith("http://") or link.startswith("https://")):
        await update.message.reply_text(
            "❌ *Invalid Link!*\n\n"
            "Please send a valid URL starting with `http://` or `https://`\n\n"
            "Example: `https://www.instagram.com/yourprofile`",
            parse_mode="Markdown"
        )
        return ENTER_LINK

    context.user_data["order_link"] = link

    s = context.user_data.get("selected_service", {})
    min_qty = s.get("min", 10)
    max_qty = s.get("max", 10000)

    s = context.user_data.get("selected_service", {})
    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    # Quick quantity buttons banao
    def qty_keyboard(selected_qty=None, rate=None):
        quick_qtys = [min_qty, 500, 1000, 2000, 5000, 10000]
        # Filter valid quantities
        quick_qtys = [q for q in quick_qtys if min_qty <= q <= max_qty]
        # Remove duplicates & sort
        quick_qtys = sorted(list(set(quick_qtys)))[:6]

        buttons = []
        row = []
        for q in quick_qtys:
            price = calculate_price(s.get("rate", 0), q)
            label = f"{q} — ₹{price:.1f}"
            if selected_qty == q:
                label = f"✅ {label}"
            row.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        buttons.append([InlineKeyboardButton("✏️ Custom Quantity Type Karo", callback_data="qty_custom")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"cat_{context.user_data.get('current_category', '')[:30]}")])
        return InlineKeyboardMarkup(buttons)

    context.user_data["qty_keyboard_func"] = True  # flag
    price_preview = calculate_price(s.get("rate", 0), min_qty)

    await update.message.reply_text(
        f"✅ *Link Accepted!*\n\n"
        f"⚠️ *DISCLAIMER:* _Once order is placed, balance cannot be refunded._\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Live Price Calculator*\n\n"
        f"💳 *Aapka Balance:* ₹{balance:.2f}\n"
        f"📉 *Min:* {min_qty} | 📈 *Max:* {max_qty}\n\n"
        f"_Quantity select karo ya custom type karo 👇_",
        parse_mode="Markdown",
        reply_markup=qty_keyboard()
    )

    return CALC_QUANTITY

async def enter_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ *Sirf number enter karein!*\n\nExample: `1000`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return ENTER_QUANTITY

    s = context.user_data.get("selected_service", {})
    min_qty = int(s.get("min", 10))
    max_qty = int(s.get("max", 10000))
    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)
    total_price = calculate_price(s["rate"], qty)

    # Reply keyboard remove karo
    context.user_data["awaiting_quantity"] = False

    if qty < min_qty or qty > max_qty:
        suggestions = sorted(list(set([q for q in [min_qty, 500, 1000, 2000, 5000, 10000] if min_qty <= q <= max_qty])))[:6]
        reply_kb = ReplyKeyboardMarkup(
            [[KeyboardButton(str(q)) for q in suggestions[i:i+3]] for i in range(0, len(suggestions), 3)],
            resize_keyboard=True, one_time_keyboard=True
        )
        await update.message.reply_text(
            f"❌ *Invalid!* {min_qty} aur {max_qty} ke beech enter karo:",
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        return ENTER_QUANTITY

    context.user_data["order_qty"] = qty
    context.user_data["order_price"] = total_price

    # Status calculate karo
    if total_price < MIN_ORDER_AMOUNT:
        status_line = f"⚠️ Min order ₹{MIN_ORDER_AMOUNT:.0f} — quantity badhao!"
        can_order = False
    elif balance < total_price:
        needed = round(total_price - balance, 2)
        status_line = f"❌ ₹{needed} aur chahiye"
        can_order = False
    else:
        status_line = "✅ Balance sufficient!"
        can_order = True

    # Quick quantity buttons bhi dikhao wapas
    quick_qtys = [min_qty, 500, 1000, 2000, 5000, 10000]
    quick_qtys = sorted(list(set([q for q in quick_qtys if min_qty <= q <= max_qty])))[:6]

    buttons = []
    row = []
    for q in quick_qtys:
        price = calculate_price(s.get("rate", 0), q)
        label = f"✅ {q} — ₹{price:.1f}" if q == qty else f"{q} — ₹{price:.1f}"
        row.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if can_order:
        buttons.append([InlineKeyboardButton(f"✅ Confirm — ₹{total_price:.2f}", callback_data=f"confirm_qty_{qty}")])
    else:
        if total_price < MIN_ORDER_AMOUNT:
            buttons.append([InlineKeyboardButton("🔄 Order Again", callback_data=f"svc_{s.get('service', '')}")])
        else:
            buttons.append([InlineKeyboardButton("👥 Refer & Earn — Balance Badhao", callback_data="refer_earn")])

    buttons.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data="qty_custom")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse")])

    await update.message.reply_text(
        f"📊 *Live Price Calculator*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name'][:35]}\n"
        f"🔢 *Entered Qty:* {qty}\n"
        f"💰 *Price:* ₹{total_price:.2f}\n"
        f"💳 *Your Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_line}\n\n"
        f"_Quantity change karo ya confirm karo 👇_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CALC_QUANTITY

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
        needed = round(price - user_data.get("balance", 0), 2)
        await query.answer(
            f"❌ Balance kam hai! ₹{needed} aur chahiye.",
            show_alert=True
        )
        await query.edit_message_text(
            f"❌ *Balance Insufficient!*\n\n"
            f"💰 Aapka Balance: ₹{user_data.get('balance', 0):.2f}\n"
            f"💳 Order Price: ₹{price:.2f}\n"
            f"⚠️ ₹{needed} aur chahiye!\n\n"
            f"👥 Refer karo aur balance earn karo!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")
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
            f"⏰ Delivery shuru ho jaayegi jaldi!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Order Again", callback_data=f"svc_{s['service']}")],
                [InlineKeyboardButton("🔔 Track Order", callback_data="track_order")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]
            ])
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
    if col is not None:
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

    uid = update.effective_user.id
    user_data = get_user(uid)
    balance = user_data.get("balance", 0)
    signup_bonus = SIGNUP_BONUS if user_data.get("signup_bonus_given") else 0

    ref_count = get_referral_count(uid)
    referral_earnings = round(ref_count * REFERRAL_BONUS, 2)
    total_earned = round(signup_bonus + referral_earnings, 2)

    col = get_col("orders")
    total_orders = col.count_documents({"user_id": str(uid)}) if col is not None else 0

    # Total spent calculate karo
    total_spent = 0
    if col is not None:
        pipeline = [
            {"$match": {"user_id": str(uid)}},
            {"$group": {"_id": None, "total": {"$sum": "$price"}}}
        ]
        result = list(col.aggregate(pipeline))
        if result:
            total_spent = round(result[0]["total"], 2)

    # Pending referrals count
    ref_col = get_col("referrals")
    pending_refs = 0
    if ref_col is not None:
        pending_refs = ref_col.count_documents({"referrer_id": str(uid), "bonus_paid": False})

    await query.edit_message_text(
        f"👤 *My Account*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *WALLET*\n"
        f"💵 Current Balance: *₹{balance:.2f}*\n"
        f"📈 Total Earned: *₹{total_earned:.2f}*\n"
        f"📉 Total Spent: *₹{total_spent:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 *BONUSES*\n"
        f"✅ Signup Bonus: *₹{signup_bonus:.2f}*\n"
        f"👥 Referral Earnings: *₹{referral_earnings:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 *REFERRALS*\n"
        f"✅ Successful Referrals: *{ref_count}*\n"
        f"⏳ Pending Referrals: *{pending_refs}*\n"
        f"💸 Per Referral Bonus: *₹{REFERRAL_BONUS:.0f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *ORDERS*\n"
        f"🛍️ Total Orders Placed: *{total_orders}*\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")],
            [InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
        ])
    )

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    col = get_col("orders")
    orders = []
    if col is not None:
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
            [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer_earn")],
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
        f"━━━━━━━━━━━━━━━━━━━━",
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
#  QUANTITY CALCULATOR — Live price keyboard
# ═══════════════════════════════════════════════════════════════════════════════
async def qty_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick quantity button dabaya"""
    query = update.callback_query
    await query.answer()

    data = query.data  # qty_100, qty_500, qty_custom

    s = context.user_data.get("selected_service", {})
    min_qty = int(s.get("min", 10))
    max_qty = int(s.get("max", 10000))
    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    if data == "qty_custom":
        s2 = context.user_data.get("selected_service", {})
        min_qty2 = int(s2.get("min", 10))
        max_qty2 = int(s2.get("max", 10000))

        # Quick suggestion buttons as Reply Keyboard
        suggestions = [min_qty2, 500, 1000, 2000, 5000, 10000]
        suggestions = sorted(list(set([q for q in suggestions if min_qty2 <= q <= max_qty2])))[:6]

        reply_kb = ReplyKeyboardMarkup(
            [[KeyboardButton(str(q)) for q in suggestions[i:i+3]] for i in range(0, len(suggestions), 3)],
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder=f"Min {min_qty2} - Max {max_qty2}"
        )

        await query.message.reply_text(
            f"✏️ *Quantity Enter Karo*\n\n"
            f"💳 *Balance:* ₹{balance:.2f}\n"
            f"📉 *Min:* {min_qty2} | 📈 *Max:* {max_qty2}\n\n"
            f"_Neeche se select karo ya khud type karo:_",
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        context.user_data["awaiting_quantity"] = True
        return ENTER_QUANTITY

    qty = int(data.replace("qty_", ""))
    total_price = calculate_price(s["rate"], qty)

    # Live price update keyboard
    quick_qtys = [min_qty, 500, 1000, 2000, 5000, 10000]
    quick_qtys = sorted(list(set([q for q in quick_qtys if min_qty <= q <= max_qty])))[:6]

    buttons = []
    row = []
    for q in quick_qtys:
        price = calculate_price(s.get("rate", 0), q)
        label = f"✅ {q} — ₹{price:.1f}" if q == qty else f"{q} — ₹{price:.1f}"
        row.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Status line
    if total_price < MIN_ORDER_AMOUNT:
        status = f"⚠️ Min order ₹{MIN_ORDER_AMOUNT:.0f} — quantity badhao!"
        can_order = False
    elif balance < total_price:
        needed = round(total_price - balance, 2)
        status = f"❌ ₹{needed} aur chahiye"
        can_order = False
    else:
        status = f"✅ Balance sufficient!"
        can_order = True

    if can_order:
        buttons.append([InlineKeyboardButton(f"✅ Confirm — ₹{total_price:.2f}", callback_data=f"confirm_qty_{qty}")])
    else:
        buttons.append([InlineKeyboardButton("👥 Refer & Earn — Balance Badhao", callback_data="refer_earn")])

    buttons.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data="qty_custom")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse")])

    await query.edit_message_text(
        f"📊 *Live Price Calculator*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name'][:35]}\n"
        f"🔢 *Selected Qty:* {qty}\n"
        f"💰 *Price:* ₹{total_price:.2f}\n"
        f"💳 *Your Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CALC_QUANTITY

async def confirm_qty_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm button directly from calculator"""
    query = update.callback_query
    await query.answer()

    qty = int(query.data.replace("confirm_qty_", ""))
    s = context.user_data.get("selected_service", {})
    total_price = calculate_price(s["rate"], qty)

    context.user_data["order_qty"] = qty
    context.user_data["order_price"] = total_price

    user = get_user(update.effective_user.id)
    balance = user.get("balance", 0)

    if balance < total_price:
        needed = round(total_price - balance, 2)
        await query.answer(f"❌ Balance kam hai! ₹{needed} aur chahiye.", show_alert=True)
        return CALC_QUANTITY

    await query.edit_message_text(
        f"📋 *Order Summary*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 *Service:* {s['name']}\n"
        f"🔗 *Link:* `{context.user_data.get('order_link', '')}`\n"
        f"🔢 *Quantity:* {qty}\n"
        f"💰 *Total Price:* ₹{total_price:.2f}\n"
        f"💳 *Balance:* ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Balance sufficient!* Confirm karein?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")],
            [InlineKeyboardButton("🔙 Back", callback_data="browse")]
        ])
    )
    return CONFIRM_ORDER

# ═══════════════════════════════════════════════════════════════════════════════
#  GLOBAL MESSAGE HANDLER — Captcha ke liye (session-safe)
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Start + Captcha — ConversationHandler nahi, simple handlers use karo
    # Kyunki ConversationHandler state lose karta hai bot restart pe

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
            CALC_QUANTITY: [
                CallbackQueryHandler(qty_button_handler, pattern="^qty_"),
                CallbackQueryHandler(confirm_qty_handler, pattern="^confirm_qty_"),
                CallbackQueryHandler(refer_earn, pattern="^refer_earn$"),
                CallbackQueryHandler(back_handler, pattern="^back_"),
                CallbackQueryHandler(browse_services, pattern="^browse$"),
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

    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CallbackQueryHandler(why_free, pattern="^why_free$"))
    app.add_handler(CallbackQueryHandler(qty_button_handler, pattern="^qty_"))
    app.add_handler(CallbackQueryHandler(confirm_qty_handler, pattern="^confirm_qty_"))
    app.add_handler(CallbackQueryHandler(back_handler, pattern="^back_"))

    # Global message handler LAST mein — taaki ConversationHandler pehle handle kare
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_message_handler))

    logger.info("SMM Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
