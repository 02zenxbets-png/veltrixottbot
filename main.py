"""
Telegram OTT Subscription Bot — Bulk Edition
- File-system storage (no database)
- USD pricing, auto USD->INR for UPI QR
- Bulk buying + bulk stock upload
- UPI (QR) + Binance
- Full admin panel
- Auto-removal of sold credentials

Setup:
    cp .env.example .env
    # edit .env with your values
    pip install -r requirements.txt
    python main.py
"""

import os
import json
import uuid
import threading
import logging
from io import BytesIO
from datetime import datetime

import qrcode
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────────
# LOAD .env / HOSTING ENVIRONMENT
# ─────────────────────────────────────────────────────────────
# This bot is designed for background hosting (Render/Railway/etc.).
# It NEVER waits for stdin. Configure the values in the hosting
# provider's Environment Variables or in a local .env file.
load_dotenv(".env")

def _env(key, default=None, cast=str):
    v = os.getenv(key, default)
    if v is None:
        raise SystemExit(f"Missing env var: {key}")
    return cast(v)

BOT_TOKEN      = _env("BOT_TOKEN")
ADMIN_IDS      = [int(x.strip()) for x in _env("ADMIN_IDS").split(",") if x.strip()]

UPI_ID         = _env("UPI_ID")
UPI_NAME       = _env("UPI_NAME")
BINANCE_PAY_ID = _env("BINANCE_PAY_ID", "")

USD_TO_INR     = _env("USD_TO_INR", "84.0", float)
MAX_BULK_QTY   = _env("MAX_BULK_QTY", "20", int)

DATA_DIR       = _env("DATA_DIR", "data")

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ott-bot")

# ─────────────────────────────────────────────────────────────
# FILE STORE
# ─────────────────────────────────────────────────────────────
class FileStore:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.lock = threading.Lock()
        os.makedirs(self.data_dir, exist_ok=True)
        for key, default in (
            ("inventory", []),
            ("orders", []),
            ("users", []),
            ("config", {"maintenance": False}),
            ("plans", [
                {"id": "netflix_1m",  "name": "Netflix 1 Month",  "price": 2.99},
                {"id": "spotify_3m",  "name": "Spotify 3 Months", "price": 3.99},
                {"id": "prime_1m",    "name": "Prime Video 1M",   "price": 1.99},
            ]),
        ):
            if not os.path.exists(self._path(key)):
                self._write_unlocked(key, default)

    def _path(self, key): return os.path.join(self.data_dir, f"{key}.json")

    def _read_unlocked(self, key):
        path = self._path(key)
        if not os.path.exists(path): return []
        try:
            with open(path, "r") as f: return json.load(f)
        except json.JSONDecodeError: return []

    def _write_unlocked(self, key, data):
        path = self._path(key); tmp = path + ".tmp"
        with open(tmp, "w") as f: json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def read(self, key):
        with self.lock: return self._read_unlocked(key)
    def write(self, key, data):
        with self.lock: self._write_unlocked(key, data)
    def append(self, key, item):
        with self.lock:
            d = self._read_unlocked(key); d.append(item)
            self._write_unlocked(key, d)
    def append_many(self, key, items):
        with self.lock:
            d = self._read_unlocked(key); d.extend(items)
            self._write_unlocked(key, d)
    def update_by_id(self, key, id_field, id_value, updates):
        with self.lock:
            d = self._read_unlocked(key)
            for it in d:
                if it.get(id_field) == id_value:
                    it.update(updates); break
            self._write_unlocked(key, d)
    def delete_by_id(self, key, id_field, id_value) -> bool:
        with self.lock:
            d = self._read_unlocked(key)
            new = [x for x in d if x.get(id_field) != id_value]
            removed = len(new) != len(d)
            self._write_unlocked(key, new)
            return removed
    def find_by_id(self, key, id_field, id_value):
        with self.lock:
            for it in self._read_unlocked(key):
                if it.get(id_field) == id_value: return it
        return None
    def find_all(self, key, predicate):
        with self.lock:
            return [x for x in self._read_unlocked(key) if predicate(x)]
    def pop_first_matching(self, key, predicate):
        with self.lock:
            d = self._read_unlocked(key)
            for i, it in enumerate(d):
                if predicate(it):
                    r = d.pop(i); self._write_unlocked(key, d); return r
        return None
    def pop_n_matching(self, key, predicate, n):
        with self.lock:
            d = self._read_unlocked(key)
            removed, kept = [], []
            for it in d:
                if len(removed) < n and predicate(it):
                    removed.append(it)
                else:
                    kept.append(it)
            if removed:
                self._write_unlocked(key, kept)
            return removed
    def count_matching(self, key, predicate):
        with self.lock:
            return sum(1 for x in self._read_unlocked(key) if predicate(x))

store = FileStore()

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS
def load_plans(): return store.read("plans")
def load_config(): return store.read("config")
def save_config(cfg): store.write("config", cfg)

def plan_by_id(pid):
    for p in load_plans():
        if p["id"] == pid: return p
    return None

def stock_count(pid):
    return store.count_matching("inventory", lambda x: x["plan"] == pid)

def new_order_id(): return "ORD-" + uuid.uuid4().hex[:8].upper()
def now_iso(): return datetime.utcnow().isoformat()
def is_maintenance(): return load_config().get("maintenance", False)

def fmt_usd(amount):
    try: return f"${float(amount):.2f}"
    except (TypeError, ValueError): return f"${amount}"

def to_inr(amount_usd):
    return int(round(float(amount_usd) * USD_TO_INR))

def fmt_inr(amount_usd): return f"₹{to_inr(amount_usd):,}"

def register_user(uid, username):
    users = store.read("users")
    for u in users:
        if u["user_id"] == uid: return
    users.append({"user_id": uid, "username": username or "", "joined_at": now_iso()})
    store.write("users", users)

def build_upi_link(amount_inr, order_id):
    return (f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&am={amount_inr}"
            f"&cu=INR&tn={order_id}")

def make_upi_qr(amount_inr, order_id) -> BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=3)
    qr.add_data(build_upi_link(amount_inr, order_id))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    buf.name = f"{order_id}.png"
    return buf

# ─────────────────────────────────────────────────────────────
# USER KEYBOARDS
# ─────────────────────────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Browse Plans", callback_data="browse")],
        [InlineKeyboardButton("My Orders",    callback_data="my_orders")],
        [InlineKeyboardButton("Help",         callback_data="help")],
    ])

def plans_kb():
    rows = []
    for p in load_plans():
        stock = stock_count(p["id"])
        label = f"{p['name']} — {fmt_usd(p['price'])}"
        if stock == 0: label += "  (Out of stock)"
        rows.append([InlineKeyboardButton(label, callback_data=f"plan:{p['id']}")])
    rows.append([InlineKeyboardButton("Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

def quantity_kb(plan_id, max_available):
    cap = min(max_available, MAX_BULK_QTY)
    presets = [q for q in (1, 2, 3, 5, 10, 20) if q <= cap]
    rows, row = [], []
    for q in presets:
        row.append(InlineKeyboardButton(str(q), callback_data=f"qty:{plan_id}:{q}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    if cap > 0:
        rows.append([InlineKeyboardButton("Custom quantity",
                                          callback_data=f"qtyc:{plan_id}")])
    rows.append([InlineKeyboardButton("Back", callback_data="browse")])
    return InlineKeyboardMarkup(rows)

def payment_method_kb(plan_id, qty):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("UPI (QR)",    callback_data=f"pay_upi:{plan_id}:{qty}")],
        [InlineKeyboardButton("Binance Pay",  callback_data=f"pay_binance:{plan_id}:{qty}")],
        [InlineKeyboardButton("Back",         callback_data=f"plan:{plan_id}")],
    ])

# ─────────────────────────────────────────────────────────────
# ADMIN KEYBOARDS
# ─────────────────────────────────────────────────────────────
def admin_panel_kb():
    cfg = load_config()
    maint = "ON" if cfg.get("maintenance") else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Plans",   callback_data="adm:plans"),
         InlineKeyboardButton("Stock",   callback_data="adm:stock")],
        [InlineKeyboardButton("Orders",  callback_data="adm:orders"),
         InlineKeyboardButton("Stats",   callback_data="adm:stats")],
        [InlineKeyboardButton("Broadcast", callback_data="adm:broadcast")],
        [InlineKeyboardButton(f"Maintenance: {maint}", callback_data="adm:maint")],
        [InlineKeyboardButton("Close", callback_data="adm:close")],
    ])

def admin_plans_kb():
    rows = [[InlineKeyboardButton("Add New Plan", callback_data="adm:plan_add")]]
    for p in load_plans():
        rows.append([
            InlineKeyboardButton(f"* {p['name']} — {fmt_usd(p['price'])}",
                                 callback_data=f"adm:plan:{p['id']}")
        ])
    rows.append([InlineKeyboardButton("Back", callback_data="adm:back")])
    return InlineKeyboardMarkup(rows)

def admin_plan_detail_kb(pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Change Price", callback_data=f"adm:plan_price:{pid}")],
        [InlineKeyboardButton("Rename",       callback_data=f"adm:plan_name:{pid}")],
        [InlineKeyboardButton("Delete Plan",  callback_data=f"adm:plan_del:{pid}")],
        [InlineKeyboardButton("Back",         callback_data="adm:plans")],
    ])

def admin_stock_kb():
    rows = [
        [InlineKeyboardButton("Add Stock (text)",   callback_data="adm:stock_add")],
        [InlineKeyboardButton("Upload .txt file",    callback_data="adm:stock_file")],
        [InlineKeyboardButton("Bulk Remove",         callback_data="adm:stock_bulk_rm")],
        [InlineKeyboardButton("Clear a Plan's Stock", callback_data="adm:stock_clear")],
    ]
    for p in load_plans():
        c = stock_count(p["id"])
        rows.append([
            InlineKeyboardButton(f"{p['id']} ({c})",
                                 callback_data=f"adm:stock_view:{p['id']}")
        ])
    rows.append([InlineKeyboardButton("Back", callback_data="adm:back")])
    return InlineKeyboardMarkup(rows)

def admin_stock_view_kb(pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add to this plan", callback_data=f"adm:stock_add:{pid}")],
        [InlineKeyboardButton("Remove last item",  callback_data=f"adm:stock_pop:{pid}")],
        [InlineKeyboardButton("Clear all",         callback_data=f"adm:stock_clearp:{pid}")],
        [InlineKeyboardButton("Back",              callback_data="adm:stock")],
    ])

def admin_bulk_rm_plans_kb():
    rows = [[InlineKeyboardButton(f"{p['id']} ({stock_count(p['id'])})",
                                  callback_data=f"adm:stock_rm:{p['id']}")]
            for p in load_plans()]
    rows.append([InlineKeyboardButton("Back", callback_data="adm:stock")])
    return InlineKeyboardMarkup(rows)

def admin_clear_plans_kb():
    rows = [[InlineKeyboardButton(f"{p['id']} ({stock_count(p['id'])})",
                                  callback_data=f"adm:stock_clearp:{p['id']}")]
            for p in load_plans()]
    rows.append([InlineKeyboardButton("Back", callback_data="adm:stock")])
    return InlineKeyboardMarkup(rows)

def admin_orders_kb():
    pending   = len(store.find_all("orders", lambda o: o["status"] == "pending_admin_approval"))
    completed = len(store.find_all("orders", lambda o: o["status"] == "completed"))
    rejected  = len(store.find_all("orders", lambda o: o["status"] == "rejected"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Pending ({pending})",   callback_data="adm:ord:pending")],
        [InlineKeyboardButton(f"Completed ({completed})", callback_data="adm:ord:completed")],
        [InlineKeyboardButton(f"Rejected ({rejected})",   callback_data="adm:ord:rejected")],
        [InlineKeyboardButton("Back", callback_data="adm:back")],
    ])

# ─────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register_user(u.id, u.username)
    if is_maintenance() and not is_admin(u.id):
        await update.message.reply_text(
            "The store is currently under maintenance. Please try again later.")
        return
    await update.message.reply_text(
        "Welcome to the OTT Store!\n\nBrowse plans below.",
        parse_mode="Markdown", reply_markup=main_menu_kb())

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("admin_state", None)
    ctx.user_data.pop("admin_payload", None)
    ctx.user_data.pop("awaiting_custom_qty", None)
    await update.message.reply_text("Cancelled any pending action.",
                                    reply_markup=main_menu_kb())

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: `{update.effective_user.id}`",
                                    parse_mode="Markdown")

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized."); return
    await update.message.reply_text("Admin Panel", parse_mode="Markdown",
                                    reply_markup=admin_panel_kb())

# ─────────────────────────────────────────────────────────────
# USER CALLBACKS
# ─────────────────────────────────────────────────────────────
async def handle_user_callback(q, ctx, data, user):
    if data == "menu":
        await q.edit_message_text("Main Menu", parse_mode="Markdown",
                                  reply_markup=main_menu_kb()); return True
    if data == "browse":
        await q.edit_message_text("Available Plans", parse_mode="Markdown",
                                  reply_markup=plans_kb()); return True
    if data == "help":
        await q.edit_message_text(
            "Help\n\n"
            "1. Choose a plan\n"
            "2. Pick quantity\n"
            "3. Scan UPI QR or pay via Binance\n"
            "4. Send screenshot / UTR\n"
            "5. Wait for approval\n"
            "6. Receive all credentials\n\n"
            "All prices in USD. UPI charged in INR.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data="menu")]]))
        return True
    if data == "my_orders":
        orders = [o for o in store.read("orders") if o["user_id"] == user.id]
        if not orders:
            await q.edit_message_text("You have no orders yet.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back", callback_data="menu")]]))
            return True
        lines = ["Your Orders\n"]
        for o in orders[-10:]:
            qty = o.get("quantity", 1)
            lines.append(f"- {o['order_id']} - {o['plan_name']} x {qty} - "
                         f"{fmt_usd(o['amount'])} - {o['status']}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data="menu")]]))
        return True

    if data.startswith("plan:"):
        pid = data.split(":", 1)[1]; plan = plan_by_id(pid)
        if not plan: await q.edit_message_text("Plan not found."); return True
        avail = stock_count(pid)
        if avail == 0:
            await q.edit_message_text("Out of stock.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back", callback_data="browse")]]))
            return True
        txt = (f"{plan['name']}\n"
               f"Unit price: {fmt_usd(plan['price'])} ({fmt_inr(plan['price'])})\n"
               f"In stock: {avail}\n\n"
               f"How many do you want?")
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=quantity_kb(pid, avail))
        return True

    if data.startswith("qty:"):
        _, pid, q = data.split(":"); qty = int(q)
        plan = plan_by_id(pid)
        if not plan: await q.edit_message_text("Plan not found."); return True
        avail = stock_count(pid)
        if qty > avail:
            await q.edit_message_text(
                f"Only {avail} in stock. Please pick a smaller quantity.",
                reply_markup=quantity_kb(pid, avail)); return True
        total_usd = round(plan["price"] * qty, 2)
        txt = (f"{plan['name']}\n"
               f"Quantity: {qty}\n"
               f"Unit: {fmt_usd(plan['price'])}\n"
               f"Total: {fmt_usd(total_usd)} ({fmt_inr(total_usd)})\n\n"
               f"Choose payment method:")
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=payment_method_kb(pid, qty))
        return True

    if data.startswith("qtyc:"):
        pid = data.split(":", 1)[1]
        ctx.user_data["awaiting_custom_qty"] = pid
        avail = stock_count(pid)
        await q.edit_message_text(
            f"Send the quantity (1-{min(avail, MAX_BULK_QTY)}).\n"
            f"Type /cancel to abort.")
        return True

    if data.startswith("pay_upi:"):
        _, pid, q = data.split(":"); qty = int(q)
        plan = plan_by_id(pid)
        total_usd = round(plan["price"] * qty, 2)
        total_inr = to_inr(total_usd)
        oid = new_order_id()

        store.append("orders", {
            "order_id": oid, "user_id": user.id, "username": user.username or "",
            "plan": pid, "plan_name": plan["name"],
            "unit_price": plan["price"], "quantity": qty,
            "amount": total_usd, "amount_inr": total_inr,
            "payment_method": "UPI", "status": "awaiting_proof",
            "created_at": now_iso(),
        })

        try: await q.message.delete()
        except Exception: pass

        caption = (
            f"UPI Payment\n\n"
            f"Order ID: {oid}\n"
            f"Plan: {plan['name']} x {qty}\n"
            f"Total: {fmt_usd(total_usd)}\n"
            f"Pay in INR: Rs.{total_inr:,}\n"
            f"(converted at Rs.{USD_TO_INR}/$)\n\n"
            f"Scan the QR below with any UPI app\n"
            f"UPI ID: {UPI_ID}\n"
            f"Name: {UPI_NAME}\n\n"
            f"After paying, send the screenshot or UTR here."
        )
        await ctx.bot.send_photo(chat_id=user.id,
                                 photo=make_upi_qr(total_inr, oid),
                                 caption=caption)
        ctx.user_data["awaiting_proof_order"] = oid
        return True

    if data.startswith("pay_binance:"):
        _, pid, q = data.split(":"); qty = int(q)
        plan = plan_by_id(pid)
        total_usd = round(plan["price"] * qty, 2)
        oid = new_order_id()

        store.append("orders", {
            "order_id": oid, "user_id": user.id, "username": user.username or "",
            "plan": pid, "plan_name": plan["name"],
            "unit_price": plan["price"], "quantity": qty,
            "amount": total_usd, "payment_method": "Binance",
            "status": "awaiting_proof", "created_at": now_iso(),
        })

        await q.edit_message_text(
            f"Binance Pay\n\n"
            f"Order ID: {oid}\n"
            f"Plan: {plan['name']} x {qty}\n"
            f"Total: {fmt_usd(total_usd)}\n"
            f"(pay equivalent USDT)\n\n"
            f"Send to Binance Pay ID: {BINANCE_PAY_ID}\n"
            f"Note/Reference: {oid}\n\n"
            f"After paying, send the screenshot or TX ID here.")
        ctx.user_data["awaiting_proof_order"] = oid
        return True
    return False

# ─────────────────────────────────────────────────────────────
# ADMIN CALLBACKS
# ─────────────────────────────────────────────────────────────
async def handle_admin_callback(q, ctx, data, user):
    payload = data[4:]

    if payload == "close":
        await q.message.delete(); return True
    if payload == "back":
        await q.edit_message_text("Admin Panel",
                                  reply_markup=admin_panel_kb()); return True
    if payload == "plans":
        await q.edit_message_text("Manage Plans",
                                  reply_markup=admin_plans_kb()); return True
    if payload == "stock":
        await q.edit_message_text("Manage Stock",
                                  reply_markup=admin_stock_kb()); return True
    if payload == "orders":
        await q.edit_message_text("Orders",
                                  reply_markup=admin_orders_kb()); return True
    if payload == "stats":
        await q.edit_message_text(admin_stats_text(),
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("Back", callback_data="adm:back")]]))
        return True
    if payload == "maint":
        cfg = load_config(); cfg["maintenance"] = not cfg.get("maintenance", False)
        save_config(cfg)
        await q.edit_message_text("Admin Panel",
                                  reply_markup=admin_panel_kb()); return True
    if payload == "broadcast":
        ctx.user_data["admin_state"] = "broadcast"
        await q.edit_message_text(
            "Send the message you want to broadcast to all users.\n"
            "Send /cancel to abort.")
        return True

    if payload == "plan_add":
        ctx.user_data["admin_state"] = "add_plan_id"
        ctx.user_data["admin_payload"] = {}
        await q.edit_message_text(
            "Add New Plan\n\nStep 1/3: Send a short ID (lowercase, no spaces).\n"
            "Example: netflix_6m")
        return True
    if payload.startswith("plan:"):
        pid = payload.split(":", 1)[1]; plan = plan_by_id(pid)
        if not plan: await q.edit_message_text("Plan not found."); return True
        await q.edit_message_text(
            f"{plan['name']}\nID: {pid}\n"
            f"Price: {fmt_usd(plan['price'])} ({fmt_inr(plan['price'])})\n"
            f"Stock: {stock_count(pid)}",
            reply_markup=admin_plan_detail_kb(pid))
        return True
    if payload.startswith("plan_price:"):
        pid = payload.split(":", 1)[1]
        ctx.user_data["admin_state"] = "edit_plan_price"
        ctx.user_data["admin_payload"] = {"plan_id": pid}
        await q.edit_message_text("Send the new price in USD (e.g. 2.99).")
        return True
    if payload.startswith("plan_name:"):
        pid = payload.split(":", 1)[1]
        ctx.user_data["admin_state"] = "edit_plan_name"
        ctx.user_data["admin_payload"] = {"plan_id": pid}
        await q.edit_message_text("Send the new name.")
        return True
    if payload.startswith("plan_del:"):
        pid = payload.split(":", 1)[1]
        store.delete_by_id("plans", "id", pid)
        await q.edit_message_text(f"Plan {pid} deleted.",
                                  reply_markup=admin_plans_kb())
        return True

    if payload == "stock_add":
        ctx.user_data["admin_state"] = "add_stock"
        ctx.user_data["admin_payload"] = {}
        await q.edit_message_text(
            "Add Stock\n\nSend in this format (one per line):\n"
            "plan_id | username | password | extra\n\n"
            "Example:\n"
            "netflix_1m | user1@mail.com | Pass123 | Profile-A\n"
            "netflix_1m | user2@mail.com | Pass456 |")
        return True
    if payload.startswith("stock_add:"):
        pid = payload.split(":", 1)[1]
        ctx.user_data["admin_state"] = "add_stock"
        ctx.user_data["admin_payload"] = {"plan_id": pid}
        await q.edit_message_text(
            f"Add stock for {pid}.\n\nSend each credential on its own line:\n"
            "username | password | extra")
        return True

    if payload == "stock_file":
        ctx.user_data["admin_state"] = "bulk_file"
        await q.edit_message_text(
            "Upload .txt file\n\n"
            "Each line must be:\n"
            "plan_id | username | password | extra\n\n"
            "Send the file as a document now.\n"
            "Type /cancel to abort.")
        return True

    if payload == "stock_bulk_rm":
        await q.edit_message_text("Bulk Remove - pick the plan:",
                                  reply_markup=admin_bulk_rm_plans_kb())
        return True
    if payload.startswith("stock_rm:"):
        pid = payload.split(":", 1)[1]
        ctx.user_data["admin_state"] = "bulk_rm_qty"
        ctx.user_data["admin_payload"] = {"plan_id": pid}
        await q.edit_message_text(
            f"How many items to remove from {pid}?\n"
            f"Available: {stock_count(pid)}\n"
            f"Send a number.")
        return True

    if payload == "stock_clear":
        await q.edit_message_text("Clear entire plan's stock - pick the plan:",
                                  reply_markup=admin_clear_plans_kb())
        return True
    if payload.startswith("stock_clearp:"):
        pid = payload.split(":", 1)[1]
        with store.lock:
            d = store._read_unlocked("inventory")
            new = [x for x in d if x["plan"] != pid]
            removed = len(d) - len(new)
            store._write_unlocked("inventory", new)
        await q.edit_message_text(
            f"Cleared {removed} items from {pid}.",
            reply_markup=admin_stock_kb())
        return True

    if payload.startswith("stock_view:"):
        pid = payload.split(":", 1)[1]
        await q.edit_message_text(
            f"{pid} - {stock_count(pid)} items in stock.",
            reply_markup=admin_stock_view_kb(pid))
        return True
    if payload.startswith("stock_pop:"):
        pid = payload.split(":", 1)[1]
        removed = store.pop_first_matching("inventory", lambda x: x["plan"] == pid)
        if removed:
            await q.edit_message_text(
                f"Removed 1 item from {pid}. New count: {stock_count(pid)}",
                reply_markup=admin_stock_view_kb(pid))
        else:
            await q.edit_message_text(f"No items in {pid}.")
        return True

    if payload.startswith("ord:"):
        kind = payload.split(":", 1)[1]
        status_map = {"pending": "pending_admin_approval",
                      "completed": "completed", "rejected": "rejected"}
        status = status_map[kind]
        orders = store.find_all("orders", lambda o: o["status"] == status)[-15:]
        if not orders:
            await q.edit_message_text(f"No {kind} orders.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back", callback_data="adm:orders")]]))
            return True
        rows = []
        for o in orders:
            qty = o.get("quantity", 1)
            rows.append([InlineKeyboardButton(
                f"{o['order_id']} - {o['plan_name']} x{qty} - {fmt_usd(o['amount'])}",
                callback_data=f"adm:o:{o['order_id']}")])
        rows.append([InlineKeyboardButton("Back", callback_data="adm:orders")])
        await q.edit_message_text(f"{kind.title()} Orders",
                                  reply_markup=InlineKeyboardMarkup(rows))
        return True
    if payload.startswith("o:"):
        oid = payload.split(":", 1)[1]
        order = store.find_by_id("orders", "order_id", oid)
        if not order: await q.edit_message_text("Order not found."); return True
        qty = order.get("quantity", 1)
        text = (f"Order {oid}\n"
                f"User: {order['user_id']} (@{order.get('username','-')})\n"
                f"Plan: {order['plan_name']}\n"
                f"Quantity: {qty}\n"
                f"Total: {fmt_usd(order['amount'])}"
                + (f"  (Rs.{order['amount_inr']:,})" if order.get("amount_inr") else "")
                + f"\nMethod: {order['payment_method']}\n"
                f"Status: {order['status']}\n"
                f"Stock available: {stock_count(order['plan'])}\n"
                f"Proof: {order.get('proof_text','-')}")
        kb_rows = []
        if order["status"] == "pending_admin_approval":
            kb_rows.append([
                InlineKeyboardButton("Approve", callback_data=f"approve:{oid}"),
                InlineKeyboardButton("Reject",  callback_data=f"reject:{oid}"),
            ])
        kb_rows.append([InlineKeyboardButton("Back", callback_data="adm:orders")])
        kb = InlineKeyboardMarkup(kb_rows)
        if order.get("proof_file_id"):
            try: await q.message.delete()
            except Exception: pass
            await ctx.bot.send_photo(chat_id=q.from_user.id,
                                     photo=order["proof_file_id"], caption=text,
                                     reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb)
        return True
    return False

def admin_stats_text():
    orders = store.read("orders")
    completed = [o for o in orders if o["status"] == "completed"]
    pending   = [o for o in orders if o["status"] == "pending_admin_approval"]
    rejected  = [o for o in orders if o["status"] == "rejected"]
    revenue_usd = sum(float(o["amount"]) for o in completed)
    units_sold  = sum(int(o.get("quantity", 1)) for o in completed)
    users     = len(store.read("users"))
    inv       = len(store.read("inventory"))
    return (f"Store Stats\n\n"
            f"Users: {users}\n"
            f"Total orders: {len(orders)}\n"
            f"Completed: {len(completed)}\n"
            f"Pending: {len(pending)}\n"
            f"Rejected: {len(rejected)}\n"
            f"Units sold: {units_sold}\n"
            f"Revenue: {fmt_usd(revenue_usd)}  (~ {fmt_inr(revenue_usd)})\n"
            f"Stock remaining: {inv}")

# ─────────────────────────────────────────────────────────────
# CALLBACK ROUTER
# ─────────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = q.from_user

    if data.startswith("approve:") or data.startswith("reject:"):
        if not is_admin(user.id):
            await q.edit_message_text("Unauthorized."); return
        action, oid = data.split(":", 1)
        order = store.find_by_id("orders", "order_id", oid)
        if not order:
            await q.edit_message_text("Order not found."); return
        qty = order.get("quantity", 1)
        if action == "approve":
            avail = stock_count(order["plan"])
            if avail < qty:
                await q.edit_message_text(
                    f"Need {qty} but only {avail} in stock for {order['plan']}.\n"
                    f"Add stock then retry, or reject.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Retry", callback_data=f"approve:{oid}"),
                        InlineKeyboardButton("Reject", callback_data=f"reject:{oid}")]]))
                return
            delivered = await deliver_order(order, ctx)
            if delivered:
                store.update_by_id("orders", "order_id", oid,
                                   {"status": "completed", "completed_at": now_iso()})
                await q.edit_message_text(
                    f"Order {oid} approved & {qty} credential(s) delivered.")
            else:
                await q.edit_message_text("Delivery failed.",
                                          reply_markup=InlineKeyboardMarkup([[
                                              InlineKeyboardButton("Retry",
                                                                   callback_data=f"approve:{oid}"),
                                              InlineKeyboardButton("Reject",
                                                                   callback_data=f"reject:{oid}")]]))
        else:
            store.update_by_id("orders", "order_id", oid,
                               {"status": "rejected", "rejected_at": now_iso()})
            await q.edit_message_text(f"Order {oid} rejected.")
            try:
                await ctx.bot.send_message(chat_id=order["user_id"],
                    text=f"Your order {oid} was rejected.")
            except Exception: pass
        return

    if data.startswith("adm:"):
        if not is_admin(user.id):
            await q.edit_message_text("Unauthorized."); return
        await handle_admin_callback(q, ctx, data, user); return

    await handle_user_callback(q, ctx, data, user)

# ─────────────────────────────────────────────────────────────
# INCOMING MESSAGES
# ─────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if is_admin(user.id):
        state = ctx.user_data.get("admin_state")
        if state:
            if await handle_admin_state(update, ctx, state):
                return

    pid = ctx.user_data.get("awaiting_custom_qty")
    if pid and update.message.text:
        try: qty = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Send a number.")
            return
        avail = stock_count(pid)
        cap = min(avail, MAX_BULK_QTY)
        if qty < 1 or qty > cap:
            await update.message.reply_text(f"Quantity must be 1-{cap}.")
            return
        plan = plan_by_id(pid)
        ctx.user_data.pop("awaiting_custom_qty", None)
        total_usd = round(plan["price"] * qty, 2)
        txt = (f"{plan['name']}\n"
               f"Quantity: {qty}\n"
               f"Unit: {fmt_usd(plan['price'])}\n"
               f"Total: {fmt_usd(total_usd)} ({fmt_inr(total_usd)})\n\n"
               f"Choose payment method:")
        await update.message.reply_text(txt,
                                        reply_markup=payment_method_kb(pid, qty))
        return

    oid = ctx.user_data.get("awaiting_proof_order")
    if not oid: return
    order = store.find_by_id("orders", "order_id", oid)
    if not order or order["user_id"] != user.id: return

    proof_text = update.message.text or update.message.caption or "(photo)"
    file_id = update.message.photo[-1].file_id if update.message.photo else None
    store.update_by_id("orders", "order_id", oid, {
        "status": "pending_admin_approval",
        "proof_text": proof_text, "proof_file_id": file_id, "proof_at": now_iso(),
    })
    ctx.user_data.pop("awaiting_proof_order", None)
    await update.message.reply_text(
        f"Proof received for {oid}. Awaiting admin approval.")
    await notify_admins(ctx, oid)

# ─────────────────────────────────────────────────────────────
# ADMIN DOCUMENT UPLOAD (.txt bulk stock)
# ─────────────────────────────────────────────────────────────
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id): return
    if ctx.user_data.get("admin_state") != "bulk_file":
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("Send a .txt file.")
        return

    file = await ctx.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    added, errors = 0, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            errors.append(line); continue
        pid, uname, pwd = parts[0], parts[1], parts[2]
        extra = parts[3] if len(parts) > 3 else ""
        if not plan_by_id(pid):
            errors.append(f"{line} (unknown plan)"); continue
        store.append("inventory", {
            "plan": pid, "username": uname, "password": pwd,
            "extra": extra, "added_at": now_iso()})
        added += 1

    ctx.user_data.pop("admin_state", None)
    ctx.user_data.pop("admin_payload", None)
    msg = f"Added {added} credential(s) from file."
    if errors:
        msg += f"\nSkipped {len(errors)} line(s). First 3:\n" + \
               "\n".join(f"{e}" for e in errors[:3])
    await update.message.reply_text(msg, reply_markup=admin_panel_kb())

# ─────────────────────────────────────────────────────────────
# ADMIN STATE HANDLER
# ─────────────────────────────────────────────────────────────
async def handle_admin_state(update, ctx, state) -> bool:
    text = (update.message.text or "").strip()
    payload = ctx.user_data.get("admin_payload", {})
    def clear():
        ctx.user_data.pop("admin_state", None)
        ctx.user_data.pop("admin_payload", None)

    if state == "bulk_file":
        if update.message.document:
            return False
        await update.message.reply_text("Please send a .txt file, or /cancel.")
        return True

    if state == "broadcast":
        users = store.read("users")
        sent = 0
        for u in users:
            try:
                await ctx.bot.send_message(chat_id=u["user_id"],
                                           text=f"Announcement\n\n{text}")
                sent += 1
            except Exception: pass
        await update.message.reply_text(f"Broadcast sent to {sent}/{len(users)} users.")
        clear(); return True

    if state == "add_plan_id":
        if not text.replace("_", "").isalnum():
            await update.message.reply_text("ID must be alphanumeric.")
            return True
        if plan_by_id(text):
            await update.message.reply_text("ID already exists.")
            return True
        payload["id"] = text.lower(); ctx.user_data["admin_payload"] = payload
        ctx.user_data["admin_state"] = "add_plan_name"
        await update.message.reply_text("Step 2/3: Send the display name.")
        return True
    if state == "add_plan_name":
        payload["name"] = text; ctx.user_data["admin_payload"] = payload
        ctx.user_data["admin_state"] = "add_plan_price"
        await update.message.reply_text("Step 3/3: Send the price in USD (e.g. 2.99).")
        return True
    if state == "add_plan_price":
        try: price = float(text)
        except ValueError:
            await update.message.reply_text("Send a number like 2.99."); return True
        plans = load_plans()
        plans.append({"id": payload["id"], "name": payload["name"], "price": price})
        store.write("plans", plans); clear()
        await update.message.reply_text(
            f"Plan {payload['id']} added - {fmt_usd(price)} ({fmt_inr(price)}).",
            reply_markup=admin_panel_kb())
        return True

    if state == "edit_plan_price":
        try: price = float(text)
        except ValueError:
            await update.message.reply_text("Send a number like 2.99."); return True
        store.update_by_id("plans", "id", payload["plan_id"], {"price": price})
        clear()
        await update.message.reply_text(
            f"Price updated to {fmt_usd(price)} ({fmt_inr(price)}).",
            reply_markup=admin_panel_kb()); return True

    if state == "edit_plan_name":
        store.update_by_id("plans", "id", payload["plan_id"], {"name": text})
        clear()
        await update.message.reply_text(f"Renamed to {text}.",
                                        reply_markup=admin_panel_kb()); return True

    if state == "add_stock":
        added, errors = 0, []
        forced_pid = payload.get("plan_id")
        for line in text.splitlines():
            line = line.strip()
            if not line: continue
            parts = [p.strip() for p in line.split("|")]
            if forced_pid:
                if len(parts) < 2: errors.append(line); continue
                pid, uname, pwd = forced_pid, parts[0], parts[1]
                extra = parts[2] if len(parts) > 2 else ""
            else:
                if len(parts) < 3: errors.append(line); continue
                pid, uname, pwd = parts[0], parts[1], parts[2]
                extra = parts[3] if len(parts) > 3 else ""
            if not plan_by_id(pid):
                errors.append(f"{line} (unknown plan)"); continue
            store.append("inventory", {
                "plan": pid, "username": uname, "password": pwd,
                "extra": extra, "added_at": now_iso()})
            added += 1
        clear()
        msg = f"Added {added} credential(s)."
        if errors:
            msg += f"\nSkipped {len(errors)} line(s):\n" + "\n".join(errors[:5])
        await update.message.reply_text(msg, reply_markup=admin_panel_kb())
        return True

    if state == "bulk_rm_qty":
        try: n = int(text)
        except ValueError:
            await update.message.reply_text("Send a number."); return True
        pid = payload["plan_id"]
        removed = store.pop_n_matching("inventory", lambda x: x["plan"] == pid, n)
        clear()
        await update.message.reply_text(
            f"Removed {len(removed)} item(s) from {pid}.\n"
            f"Remaining: {stock_count(pid)}",
            reply_markup=admin_panel_kb())
        return True

    return False

# ─────────────────────────────────────────────────────────────
# ADMIN NOTIFY
# ─────────────────────────────────────────────────────────────
async def notify_admins(ctx, oid):
    order = store.find_by_id("orders", "order_id", oid)
    if not order: return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"approve:{oid}"),
        InlineKeyboardButton("Reject",  callback_data=f"reject:{oid}"),
    ]])
    qty = order.get("quantity", 1)
    amount_line = f"Amount: {fmt_usd(order['amount'])}"
    if order.get("amount_inr"):
        amount_line += f" (Rs.{order['amount_inr']:,})"
    caption = (f"New payment\n\n"
               f"Order: {oid}\n"
               f"User: {order['user_id']}\n"
               f"Plan: {order['plan_name']} x {qty}\n"
               f"{amount_line}\n"
               f"Method: {order['payment_method']}\n"
               f"Stock available: {stock_count(order['plan'])}\n"
               f"Proof: {order.get('proof_text','-')}")
    for aid in ADMIN_IDS:
        try:
            if order.get("proof_file_id"):
                await ctx.bot.send_photo(chat_id=aid, photo=order["proof_file_id"],
                                         caption=caption, reply_markup=kb)
            else:
                await ctx.bot.send_message(chat_id=aid, text=caption,
                                           reply_markup=kb)
        except Exception as e:
            log.warning("Admin notify failed: %s", e)

# ─────────────────────────────────────────────────────────────
# DELIVER ORDER (bulk)
# ─────────────────────────────────────────────────────────────
def _chunk(items, max_len=3500):
    chunks, cur = [], ""
    for line in items:
        if len(cur) + len(line) + 2 > max_len:
            chunks.append(cur); cur = line
        else:
            cur = (cur + "\n\n" + line) if cur else line
    if cur: chunks.append(cur)
    return chunks

async def deliver_order(order, ctx) -> bool:
    qty = int(order.get("quantity", 1))
    items = store.pop_n_matching("inventory",
                                 lambda x: x["plan"] == order["plan"], qty)
    if len(items) < qty:
        store.append_many("inventory", items)
        return False

    store.update_by_id("orders", "order_id", order["order_id"],
                       {"delivered_credentials": items})

    header = (f"Order Completed\n\n"
              f"Order: {order['order_id']}\n"
              f"Plan: {order['plan_name']} x {qty}\n"
              f"Paid: {fmt_usd(order['amount'])}"
              + (f" (Rs.{order['amount_inr']:,})" if order.get("amount_inr") else "")
              + f"\n\nYour credentials:\n")

    lines = []
    for i, it in enumerate(items, 1):
        line = f"{i}. Username: {it['username']}\n   Password: {it['password']}"
        if it.get("extra"):
            line += f"\n   Note: {it['extra']}"
        lines.append(line)

    chunks = _chunk(lines)
    try:
        first = header + chunks[0]
        await ctx.bot.send_message(chat_id=order["user_id"], text=first)
        for c in chunks[1:]:
            await ctx.bot.send_message(chat_id=order["user_id"], text=c)
        await ctx.bot.send_message(chat_id=order["user_id"],
                                   text="Thank you for your purchase!")
    except Exception as e:
        log.warning("Delivery failed: %s", e); return False
    return True

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO, on_message))

    log.info("Bot starting... Admins: %s  USD->INR: %s  MaxBulk: %s",
             ADMIN_IDS, USD_TO_INR, MAX_BULK_QTY)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
