"""
SMM Panel Telegram Bot
- SMMKings API se services fetch karta hai
- 1.5x markup automatically lagate hai
- User wallet system (MongoDB)
- Manual UTR payment verification
- Auto order placement via SMMKings API
"""

import os
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
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
MONGO_URI       = os.environ.get("MONGO_URI", "")
SMM_API_KEY     = os.environ.get("SMM_API_KEY", "")         # SMMKings API key
SMM_API_URL     = "https://smmkings.com/api/v2"             # SMMKings API URL
ADMIN_IDS       = list(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))
UPI_ID          = os.environ.get("UPI_ID", "yourname@upi")  # Aapka UPI ID
MARKUP          = 1.5                                        # 1.5x price
USD_TO_INR      = 83.0                                       # USD to INR rate

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
        return {"_id": uid, "balance": 0.0, "orders": []}
    doc = col.find_one({"_id": uid})
    if not doc:
        doc = {"_id": uid, "balance": 0.0, "orders": []}
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

def save_payment(uid, utr, amount, status="pending"):
    col = get_col("payments")
    if col:
        col.insert_one({
            "user_id": str(uid),
            "utr": utr,
            "amount": amount,
            "status": status,
            "created_at": datetime.now()
        })

def get_pending_payments():
    col = get_col("payments")
    if col:
        return list(col.find({"status": "pending"}))
    return []

def update_payment(utr, status):
    col = get_col("payments")
    if col:
        col.update_one({"utr": utr}, {"$set": {"status": status}})

# ═══════════════════════════════════════════════════════════════════════════════
#  SMMKINGS API
# ═══════════════════════════════════════════════════════════════════════════════
async def smm_api(action, **kwargs):
    """SMMKings API call"""
    params = {"key": SMM_API_KEY, "action": action}
    params.update(kwargs)
    try:
        import json
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SMM_API_URL, data=params) as r:
                text = await r.text()
                logger.info(f"SMM API ({action}): {text[:200]}")
                return json.loads(text)
    except Exception as e:
        logger.error(f"SMM API error ({action}): {e}")
        return None

async def get_services():
    """SMMKings se sari services fetch karo"""
    return await smm_api("services")

async def place_order(service_id, link, quantity):
    """SMMKings pe order place karo"""
    return await smm_api("add", service=service_id, link=link, quantity=quantity)

async def check_order_status(order_id):
    """Order status check karo"""
    return await smm_api("status", order=order_id)

def calculate_price(rate_usd, quantity):
    """USD rate se INR price calculate karo with markup"""
    rate_inr = float(rate_usd) * USD_TO_INR / 1000  # per 1000 units
    price = (rate_inr * quantity / 1000) * MARKUP
    return round(price, 2)

# ═══════════════════════════════════════════════════════════════════════════════
#  STATES
# ═══════════════════════════════════════════════════════════════════════════════
(
    BROWSE_CATEGORY, SELECT_SERVICE, ENTER_LINK,
    ENTER_QUANTITY, CONFIRM_ORDER,
    ENTER_AMOUNT, ENTER_UTR,
    ADMIN_VERIFY
) = range(8)

# ═══════════════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Services Order Karo", callback_data="browse")],
        [InlineKeyboardButton("💰 Wallet Recharge Karo", callback_data="add_funds")],
        [InlineKeyboardButton("📊 Mera Balance", callback_data="my_balance"),
         InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ])

def back_keyboard(back_to="main"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"back_{back_to}")
    ]])

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
async def show_main_menu(update, context, edit=False):
    user = update.effective_user
    user_data = get_user(user.id)
    balance = user_data.get("balance", 0)
    total_orders = len(user_data.get("orders", []))

    text = (
        f"👋 *Namaste, {user.first_name}!*\n\n"
        f"🚀 *SMM Panel — Social Media Services*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Aapka Balance:*  ₹{balance:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Instagram, YouTube, Telegram, TikTok\n"
        f"aur 1000+ services available hain! 🎯\n\n"
        f"Neeche se option chunein 👇"
    )

    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await show_main_menu(update, context)

# ═══════════════════════════════════════════════════════════════════════════════
#  BROWSE SERVICES
# ═══════════════════════════════════════════════════════════════════════════════
async def browse_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "⏳ *Services load ho rahi hain...*\n\nThoda wait karein!",
        parse_mode="Markdown"
    )

    services = await get_services()

    if not services:
        await query.edit_message_text(
            "❌ *Services load nahi ho sakein!*\n\nThodi der baad try karein.",
            parse_mode="Markdown",
            reply_markup=back_keyboard()
        )
        return ConversationHandler.END

    # Categories nikalo
    categories = {}
    for s in services:
        cat = s.get("category", "Other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(s)

    context.user_data["categories"] = categories
    context.user_data["services"] = {str(s["service"]): s for s in services}

    # Category buttons banao
    buttons = []
    for cat in sorted(categories.keys()):
        count = len(categories[cat])
        buttons.append([InlineKeyboardButton(
            f"📂 {cat} ({count})",
            callback_data=f"cat_{cat[:30]}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])

    await query.edit_message_text(
        f"🛍️ *Services Browse Karein*\n\n"
        f"Total {len(services)} services available hain!\n"
        f"Koi bhi category select karein 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return BROWSE_CATEGORY

async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat_name = query.data.replace("cat_", "")
    categories = context.user_data.get("categories", {})

    # Full category name dhundho
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
    for s in services[:20]:  # Max 20 show karo
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
        text += f"\n❌ *Balance kam hai!* ₹{needed} aur chahiye.\n\nPehle wallet recharge karein!"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Wallet Recharge Karo", callback_data="add_funds")],
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
            "❌ Balance kam hai! Pehle wallet recharge karein.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Recharge", callback_data="add_funds")
            ]])
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

        await query.edit_message_text(
            f"✅ *Order Successfully Place Ho Gaya!*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Order ID:* `{smm_order_id}`\n"
            f"🔷 *Service:* {s['name']}\n"
            f"🔢 *Quantity:* {qty}\n"
            f"💰 *Charged:* ₹{price}\n"
            f"💳 *Remaining Balance:* ₹{new_balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏰ Delivery shuru ho jaayegi jaldi!\n"
            f"Status check karne ke liye /orders use karein.",
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
#  WALLET / ADD FUNDS
# ═══════════════════════════════════════════════════════════════════════════════
async def add_funds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        f"💰 *Wallet Recharge Karein*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*UPI ID:* `{UPI_ID}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Steps:*\n"
        f"1️⃣ UPI se payment karein\n"
        f"2️⃣ Amount enter karein\n"
        f"3️⃣ UTR number bhejein\n"
        f"4️⃣ Admin verify karega → balance add hoga ✅\n\n"
        f"*Minimum recharge:* ₹50\n\n"
        f"⚠️ _Verification mein 5-15 min lag sakte hain_\n\n"
        f"Kitna recharge karna chahte hain?"
    )

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

    return ENTER_AMOUNT

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Sirf number enter karein! Jaise: 100")
        return ENTER_AMOUNT

    if amount < 50:
        await update.message.reply_text("❌ Minimum recharge ₹50 hai!")
        return ENTER_AMOUNT

    context.user_data["recharge_amount"] = amount

    await update.message.reply_text(
        f"✅ *Amount: ₹{amount}*\n\n"
        f"Ab *UPI ID:* `{UPI_ID}` pe ₹{amount} bhejein\n\n"
        f"Payment ke baad *UTR number* bhejein\n"
        f"_(12 digit number — transaction details mein milega)_",
        parse_mode="Markdown"
    )

    return ENTER_UTR

async def enter_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utr = update.message.text.strip()
    user = update.effective_user
    amount = context.user_data.get("recharge_amount", 0)

    if not utr.isdigit() or len(utr) < 10:
        await update.message.reply_text(
            "❌ *Galat UTR!*\n\nUTR 10-12 digit ka number hota hai.\nDobara enter karein:",
            parse_mode="Markdown"
        )
        return ENTER_UTR

    save_payment(user.id, utr, amount)

    # Admin ko notify karo
    for admin_id in ADMIN_IDS:
        if admin_id:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"💰 *Naya Payment Request!*\n\n"
                    f"👤 User: {user.first_name} (`{user.id}`)\n"
                    f"💵 Amount: ₹{amount}\n"
                    f"🔢 UTR: `{utr}`\n\n"
                    f"Verify karne ke liye:\n"
                    f"/approve {utr} {user.id} {amount}\n"
                    f"/reject {utr} {user.id}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await update.message.reply_text(
        f"✅ *Payment Request Submit Ho Gaya!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Amount: ₹{amount}\n"
        f"🔢 UTR: `{utr}`\n"
        f"⏳ Status: Pending\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Admin verify karega — 5-15 min mein balance add ho jaayega! 😊",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  BALANCE & ORDERS
# ═══════════════════════════════════════════════════════════════════════════════
async def my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_data = get_user(update.effective_user.id)
    balance = user_data.get("balance", 0)

    col = get_col("orders")
    total_orders = col.count_documents({"user_id": str(update.effective_user.id)}) if col else 0

    col2 = get_col("payments")
    total_spent = 0
    if col2:
        for p in col2.find({"user_id": str(update.effective_user.id), "status": "approved"}):
            total_spent += p.get("amount", 0)

    await query.edit_message_text(
        f"💰 *Aapka Wallet*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 *Current Balance:* ₹{balance:.2f}\n"
        f"📊 *Total Orders:* {total_orders}\n"
        f"💳 *Total Recharged:* ₹{total_spent:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Balance badhane ke liye 'Wallet Recharge' karein!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Recharge Karo", callback_data="add_funds")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
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
        status = o.get("status", "pending")
        emoji = "✅" if status == "Completed" else "⏳" if status == "pending" else "🔄"
        text += (
            f"{emoji} *Order #{o.get('smm_order_id', 'N/A')}*\n"
            f"   {o.get('service_name', 'N/A')[:30]}\n"
            f"   Qty: {o.get('quantity')} | ₹{o.get('price')}\n\n"
        )

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  HELP
# ═══════════════════════════════════════════════════════════════════════════════
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        f"ℹ️ *Help Guide*\n\n"
        f"*Kaise use karein:*\n\n"
        f"1️⃣ *Services* — Instagram, YouTube, Telegram services dekho\n"
        f"2️⃣ *Wallet Recharge* — UPI se balance add karo\n"
        f"3️⃣ *Order karo* — Service chunein, link dein, quantity enter karein\n"
        f"4️⃣ *Track* — My Orders se status check karo\n\n"
        f"*Payment process:*\n"
        f"• UPI ID pe pay karo: `{UPI_ID}`\n"
        f"• UTR number bot mein dalo\n"
        f"• 5-15 min mein balance add hoga\n\n"
        f"*Support ke liye admin se contact karein!*",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════
async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin payment approve kare: /approve UTR USER_ID AMOUNT"""
    if update.effective_user.id not in ADMIN_IDS:
        return

    try:
        args = context.args
        utr = args[0]
        user_id = int(args[1])
        amount = float(args[2])

        new_balance = add_balance(user_id, amount)
        update_payment(utr, "approved")

        await update.message.reply_text(
            f"✅ *Payment Approved!*\n\nUTR: `{utr}`\nUser: `{user_id}`\n"
            f"Amount: ₹{amount}\nNew Balance: ₹{new_balance}",
            parse_mode="Markdown"
        )

        await context.bot.send_message(
            user_id,
            f"✅ *Aapka Payment Verify Ho Gaya!*\n\n"
            f"💰 ₹{amount} aapke wallet mein add ho gaya!\n"
            f"💳 *Current Balance:* ₹{new_balance:.2f}\n\n"
            f"Ab services order karein! 🚀",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\nFormat: /approve UTR USER_ID AMOUNT")

async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin payment reject kare: /reject UTR USER_ID"""
    if update.effective_user.id not in ADMIN_IDS:
        return

    try:
        args = context.args
        utr = args[0]
        user_id = int(args[1])

        update_payment(utr, "rejected")

        await update.message.reply_text(f"❌ Payment rejected! UTR: `{utr}`", parse_mode="Markdown")

        await context.bot.send_message(
            user_id,
            f"❌ *Aapka Payment Reject Ho Gaya!*\n\n"
            f"UTR: `{utr}`\n\n"
            f"Koi problem ho toh admin se contact karein.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin stats: /stats"""
    if update.effective_user.id not in ADMIN_IDS:
        return

    users_col = get_col("users")
    orders_col = get_col("orders")
    payments_col = get_col("payments")

    total_users = users_col.count_documents({}) if users_col else 0
    total_orders = orders_col.count_documents({}) if orders_col else 0
    pending_payments = payments_col.count_documents({"status": "pending"}) if payments_col else 0

    total_revenue = 0
    if payments_col:
        for p in payments_col.find({"status": "approved"}):
            total_revenue += p.get("amount", 0)

    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📦 Total Orders: {total_orders}\n"
        f"⏳ Pending Payments: {pending_payments}\n"
        f"💰 Total Revenue: ₹{total_revenue:.2f}",
        parse_mode="Markdown"
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  BACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_main" or query.data == "main_menu":
        await show_main_menu(update, context, edit=True)
    elif query.data == "browse":
        await browse_services(update, context)
    elif query.data == "add_funds":
        await add_funds(update, context)

    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Order conversation
    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(browse_services, pattern="^browse$")],
        states={
            BROWSE_CATEGORY: [
                CallbackQueryHandler(show_category, pattern="^cat_"),
                CallbackQueryHandler(back_handler, pattern="^back_")
            ],
            SELECT_SERVICE: [
                CallbackQueryHandler(show_service, pattern="^svc_"),
                CallbackQueryHandler(browse_services, pattern="^browse$"),
                CallbackQueryHandler(show_category, pattern="^cat_"),
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
                CallbackQueryHandler(add_funds, pattern="^add_funds$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(back_handler, pattern="^back_main$")
        ],
        allow_reentry=True
    )

    # Payment conversation
    payment_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_funds, pattern="^add_funds$")],
        states={
            ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount),
                CallbackQueryHandler(back_handler, pattern="^back_")
            ],
            ENTER_UTR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_utr)
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(back_handler, pattern="^back_main$")
        ],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve", admin_approve))
    app.add_handler(CommandHandler("reject", admin_reject))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(order_conv)
    app.add_handler(payment_conv)
    app.add_handler(CallbackQueryHandler(my_balance, pattern="^my_balance$"))
    app.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(back_handler, pattern="^back_"))

    logger.info("SMM Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
