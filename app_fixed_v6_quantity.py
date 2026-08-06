import base64
import io
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import telebot
from telebot import types
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from payos import PayOS
from payos.types import CreatePaymentLinkRequest
import qrcode

# =========================
# ENV CONFIG
# =========================
APP_VERSION = "2026-08-06-v6-quantity-order"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@min_max18344").strip()  # @username
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "7540411330"))  # optional - recommended
SHOP_NAME = os.getenv("SHOP_NAME", "VUSMILE").strip()

BANK_NAME = os.getenv("BANK_NAME", "Ngân hàng Phương Đông").strip()
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "PHAM DINH MINH VU").strip()
ACCOUNT_NO = os.getenv("ACCOUNT_NO", "0812810305").strip()

# Google Sheets is the persistent source of truth for stock and orders.
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()

PAYOS_CLIENT_ID = os.getenv("PAYOS_CLIENT_ID", "").strip()
PAYOS_API_KEY = os.getenv("PAYOS_API_KEY", "").strip()
PAYOS_CHECKSUM_KEY = os.getenv("PAYOS_CHECKSUM_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PAYMENT_EXPIRE_MINUTES = int(os.getenv("PAYMENT_EXPIRE_MINUTES", "15"))
MAX_ORDER_QUANTITY = int(os.getenv("MAX_ORDER_QUANTITY", "100"))

SHEET_INVENTORY = os.getenv("SHEET_INVENTORY", "INVENTORY").strip()
SHEET_ORDERS = os.getenv("SHEET_ORDERS", "ORDERS").strip()
SHEET_VISITORS = os.getenv("SHEET_VISITORS", "VISITORS").strip()
SHEET_IMAGES = os.getenv("SHEET_IMAGES", "IMAGES").strip()

INVENTORY_HEADERS = [
    "item_id", "resource", "status", "order_code", "buyer_username",
    "buyer_id", "sold_at", "reserved_until", "note",
]
ORDERS_HEADERS = [
    "order_code", "item_id", "product_name", "amount", "status", "chat_id",
    "user_id", "username", "inventory_row", "checkout_url", "payment_link_id",
    "created_at", "expires_at", "paid_at", "delivered_at", "reference", "error",
    "quantity", "unit_amount", "inventory_rows",
]
VISITORS_HEADERS = [
    "user_id", "username", "full_name", "first_seen", "last_seen",
    "start_count", "source",
]
IMAGES_HEADERS = ["key", "file_id", "updated_at"]

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
server = Flask(__name__)
state_lock = threading.RLock()
print(f"[STARTUP] VuSmile bot version={APP_VERSION}")

# Reuse one Google Sheets client per thread. googleapiclient clients are not
# thread-safe, so each worker thread keeps its own client instance.
_sheets_local = threading.local()
_image_cache = {}

# Track recently accepted Telegram updates so retries are ignored.
_recent_update_ids = set()
_recent_update_order = deque(maxlen=2000)
_recent_update_lock = threading.Lock()

# Temporary state used when a customer chooses "Khác" and types a quantity.
_pending_custom_quantity = {}
_pending_custom_quantity_lock = threading.Lock()
CUSTOM_QUANTITY_TIMEOUT_SECONDS = 10 * 60

payos_client = None
if PAYOS_CLIENT_ID and PAYOS_API_KEY and PAYOS_CHECKSUM_KEY:
    payos_client = PayOS(
        client_id=PAYOS_CLIENT_ID,
        api_key=PAYOS_API_KEY,
        checksum_key=PAYOS_CHECKSUM_KEY,
    )

# =========================
# Google Sheets storage
# =========================
def now_vn() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))


def iso_now() -> str:
    return now_vn().isoformat(timespec="seconds")


def _service_account_info() -> dict:
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw and GOOGLE_SERVICE_ACCOUNT_JSON_B64:
        raw = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    return json.loads(raw)


def sheets_service():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    service = getattr(_sheets_local, "service", None)
    if service is None:
        creds = service_account.Credentials.from_service_account_info(
            _service_account_info(),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build(
            "sheets", "v4", credentials=creds, cache_discovery=False
        )
        _sheets_local.service = service

    return service


def ensure_google_sheets():
    if not GOOGLE_SHEET_ID or not (GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64):
        return
    service = sheets_service()
    meta = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEET_ID).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    requests_body = []
    for title in (SHEET_INVENTORY, SHEET_ORDERS, SHEET_VISITORS, SHEET_IMAGES):
        if title not in existing:
            requests_body.append({"addSheet": {"properties": {"title": title}}})
    if requests_body:
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body={"requests": requests_body},
        ).execute()

    for title, headers in (
        (SHEET_INVENTORY, INVENTORY_HEADERS),
        (SHEET_ORDERS, ORDERS_HEADERS),
        (SHEET_VISITORS, VISITORS_HEADERS),
        (SHEET_IMAGES, IMAGES_HEADERS),
    ):
        values = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{title}'!1:1",
        ).execute().get("values", [])
        if not values or values[0] != headers:
            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=f"'{title}'!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()


def sheet_rows(title: str, headers: list[str]) -> list[tuple[int, dict]]:
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{title}'!A2:ZZ",
    ).execute().get("values", [])
    result = []
    for row_number, row in enumerate(values, start=2):
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        result.append((row_number, dict(zip(headers, padded[:len(headers)]))))
    return result


def append_sheet_row(title: str, headers: list[str], data: dict):
    row = [str(data.get(h, "")) for h in headers]
    sheets_service().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{title}'!A:ZZ",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_sheet_row(title: str, headers: list[str], row_number: int, changes: dict):
    current = [""] * len(headers)
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{title}'!A{row_number}:ZZ{row_number}",
    ).execute().get("values", [])
    if values:
        for i, value in enumerate(values[0][:len(headers)]):
            current[i] = value
    indexes = {h: i for i, h in enumerate(headers)}
    for key, value in changes.items():
        if key in indexes:
            current[indexes[key]] = str(value)
    sheets_service().spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{title}'!A{row_number}",
        valueInputOption="RAW",
        body={"values": [current]},
    ).execute()


def set_image(key: str, file_id: str):
    key = key.upper().strip()
    with state_lock:
        _image_cache[key] = file_id
        for row_number, row in sheet_rows(SHEET_IMAGES, IMAGES_HEADERS):
            if row.get("key", "").upper() == key:
                update_sheet_row(SHEET_IMAGES, IMAGES_HEADERS, row_number, {
                    "file_id": file_id, "updated_at": iso_now(),
                })
                return
        append_sheet_row(SHEET_IMAGES, IMAGES_HEADERS, {
            "key": key, "file_id": file_id, "updated_at": iso_now(),
        })


def get_image(key: str):
    key = key.upper().strip()

    if key in _image_cache:
        return _image_cache[key] or None

    if not GOOGLE_SHEET_ID:
        return None

    try:
        for _, row in sheet_rows(SHEET_IMAGES, IMAGES_HEADERS):
            image_key = row.get("key", "").upper().strip()
            file_id = row.get("file_id", "").strip()
            if image_key:
                _image_cache[image_key] = file_id

        return _image_cache.get(key) or None
    except Exception as exc:
        print(f"[GET_IMAGE] error: {exc}")
        return None


def load_image_cache():
    if not GOOGLE_SHEET_ID:
        return
    try:
        for _, row in sheet_rows(SHEET_IMAGES, IMAGES_HEADERS):
            key = row.get("key", "").upper().strip()
            if key:
                _image_cache[key] = row.get("file_id", "").strip()
    except Exception as exc:
        print(f"[IMAGE_CACHE_INIT] error: {exc}")


def parse_price_vnd(price: str):
    # Use the first displayed amount only. This avoids turning a range such as
    # "450.000đ – 1.500.000đ" into one invalid giant number.
    import re

    match = re.search(r"\d[\d.]*", str(price or ""))
    if not match:
        return None
    return int(match.group(0).replace(".", ""))


def release_expired_reservations():
    now = now_vn()
    for row_number, row in sheet_rows(SHEET_INVENTORY, INVENTORY_HEADERS):
        if row.get("status", "").upper() != "RESERVED":
            continue
        try:
            expired = datetime.fromisoformat(row.get("reserved_until", "")) <= now
        except Exception:
            expired = True
        if expired:
            update_sheet_row(SHEET_INVENTORY, INVENTORY_HEADERS, row_number, {
                "status": "AVAILABLE", "order_code": "", "buyer_username": "",
                "buyer_id": "", "reserved_until": "",
            })


def available_resource_count(item_id: str) -> int:
    release_expired_reservations()
    return sum(
        1
        for _, row in sheet_rows(SHEET_INVENTORY, INVENTORY_HEADERS)
        if row.get("item_id", "").strip().upper() == item_id.upper()
        and row.get("status", "").strip().upper() in ("", "AVAILABLE")
        and row.get("resource", "").strip()
    )


def release_reserved_rows(row_numbers: list[int]):
    for row_number in row_numbers:
        update_sheet_row(
            SHEET_INVENTORY,
            INVENTORY_HEADERS,
            row_number,
            {
                "status": "AVAILABLE",
                "order_code": "",
                "buyer_username": "",
                "buyer_id": "",
                "reserved_until": "",
            },
        )


def reserve_resources(
    item_id: str,
    order_code: int,
    user,
    quantity: int,
) -> tuple[list[tuple[int, str]], int]:
    """Reserve exactly quantity resources, or reserve none when stock is short."""
    release_expired_reservations()
    expires_at = now_vn() + timedelta(minutes=PAYMENT_EXPIRE_MINUTES)

    candidates = []
    for row_number, row in sheet_rows(SHEET_INVENTORY, INVENTORY_HEADERS):
        if row.get("item_id", "").strip().upper() != item_id.upper():
            continue
        if row.get("status", "").strip().upper() not in ("", "AVAILABLE"):
            continue
        resource = row.get("resource", "").strip()
        if not resource:
            continue
        candidates.append((row_number, resource))

    available_count = len(candidates)
    if available_count < quantity:
        return [], available_count

    selected = candidates[:quantity]
    reserved_rows = []
    try:
        for row_number, _resource in selected:
            update_sheet_row(
                SHEET_INVENTORY,
                INVENTORY_HEADERS,
                row_number,
                {
                    "status": "RESERVED",
                    "order_code": order_code,
                    "buyer_username": user_tag(user),
                    "buyer_id": user.id,
                    "reserved_until": expires_at.isoformat(timespec="seconds"),
                },
            )
            reserved_rows.append(row_number)
    except Exception:
        if reserved_rows:
            release_reserved_rows(reserved_rows)
        raise

    return selected, available_count


def find_order(order_code: int):
    for row_number, row in sheet_rows(SHEET_ORDERS, ORDERS_HEADERS):
        if str(row.get("order_code", "")) == str(order_code):
            return row_number, row
    return None


def next_order_code() -> int:
    candidate = int(time.time())
    while find_order(candidate):
        candidate += 1
    return candidate


def record_start_visit(message):
    source = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        source = parts[1][:100]
    uid = str(message.from_user.id)
    now = iso_now()
    rows = sheet_rows(SHEET_VISITORS, VISITORS_HEADERS)
    for row_number, row in rows:
        if str(row.get("user_id", "")) == uid:
            try:
                count = int(row.get("start_count", "0") or 0) + 1
            except ValueError:
                count = 1
            update_sheet_row(SHEET_VISITORS, VISITORS_HEADERS, row_number, {
                "username": user_tag(message.from_user),
                "full_name": " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])),
                "last_seen": now,
                "start_count": count,
                "source": source or row.get("source", ""),
            })
            return False, count, source
    append_sheet_row(SHEET_VISITORS, VISITORS_HEADERS, {
        "user_id": uid,
        "username": user_tag(message.from_user),
        "full_name": " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])),
        "first_seen": now,
        "last_seen": now,
        "start_count": 1,
        "source": source,
    })
    return True, 1, source


def notify_admin_start(message):
    if not ADMIN_CHAT_ID or is_admin(message.from_user):
        return
    try:
        is_new, count, source = record_start_visit(message)
        title = "🆕 KHÁCH MỚI VỪA VÀO BOT" if is_new else "🔔 KHÁCH VỪA NHẤN /start"
        full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])) or "Không có"
        text = (
            f"{title}\n\n"
            f"👤 Tên: {full_name}\n"
            f"🔗 Username: {user_tag(message.from_user)}\n"
            f"🆔 Telegram ID: {message.from_user.id}\n"
            f"🔁 Số lần /start: {count}\n"
            f"📣 Nguồn: {source or 'trực tiếp'}\n"
            f"🕐 Thời gian: {now_vn().strftime('%d/%m/%Y %H:%M:%S')}"
        )
        bot.send_message(ADMIN_CHAT_ID, text)
    except Exception as exc:
        print(f"[START_NOTIFY] error: {exc}")


def manual_resource_markup(item_id: str, quantity: int | None = None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    found = ITEM_BY_ID.get(item_id)
    if found:
        _, it = found
        qty_text = f" | SL: {quantity}" if quantity else ""
        msg = f"MUA | {it['group']} | {it['name']}{qty_text} | User cần admin hỗ trợ"
        kb.add(types.InlineKeyboardButton("📩 NHẮN TELEGRAM ADMIN", url=build_prefilled_admin_link(msg)))
    else:
        kb.add(types.InlineKeyboardButton("📩 NHẮN TELEGRAM ADMIN", url=admin_url()))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại danh mục", callback_data=f"BACKCAT|{item_id}"))
    return kb


def payment_markup(checkout_url: str, item_id: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💳 MỞ TRANG THANH TOÁN", url=checkout_url))
    kb.add(types.InlineKeyboardButton("📩 Liên hệ Admin", url=admin_url()))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại danh mục", callback_data=f"BACKCAT|{item_id}"))
    return kb


def quantity_menu_markup(item_id: str):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.row(
        types.InlineKeyboardButton("1", callback_data=f"QTY|{item_id}|1"),
        types.InlineKeyboardButton("2", callback_data=f"QTY|{item_id}|2"),
        types.InlineKeyboardButton("3", callback_data=f"QTY|{item_id}|3"),
    )
    kb.row(
        types.InlineKeyboardButton("4", callback_data=f"QTY|{item_id}|4"),
        types.InlineKeyboardButton("5", callback_data=f"QTY|{item_id}|5"),
        types.InlineKeyboardButton("Khác", callback_data=f"QTYOTHER|{item_id}"),
    )
    kb.add(types.InlineKeyboardButton("⏪ Quay lại sản phẩm", callback_data=f"ITEM|{item_id}"))
    return kb


def buy_confirmation_markup(item_id: str, quantity: int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "✅ MUA NGAY",
            callback_data=f"BUYQTY|{item_id}|{quantity}",
        )
    )
    kb.add(types.InlineKeyboardButton("🔢 Chọn lại số lượng", callback_data=f"QTYMENU|{item_id}"))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại sản phẩm", callback_data=f"ITEM|{item_id}"))
    return kb


def quantity_summary_text(item_id: str, quantity: int) -> str:
    found = ITEM_BY_ID.get(item_id)
    if not found:
        return "❌ Sản phẩm không tồn tại."
    _, item = found
    unit_amount = parse_price_vnd(item.get("price", ""))
    if not unit_amount:
        return (
            f"📦 **Sản phẩm:** {item['name']}\n"
            f"🔢 **Số lượng:** {quantity}\n\n"
            "Giá sản phẩm cần liên hệ Admin để xác nhận."
        )
    total_amount = unit_amount * quantity
    return (
        f"📦 **Sản phẩm:** {item['name']}\n"
        f"🔢 **Số lượng:** {quantity}\n"
        f"💵 **Đơn giá:** {unit_amount:,}đ\n"
        f"💰 **Tổng thanh toán:** {total_amount:,}đ\n\n"
        "Nhấn **MUA NGAY** để bot kiểm tra kho và tạo mã QR."
    ).replace(",", ".")


def _quantity_state_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return int(chat_id), int(user_id)


def set_pending_custom_quantity(chat_id: int, user_id: int, item_id: str):
    key = _quantity_state_key(chat_id, user_id)
    with _pending_custom_quantity_lock:
        _pending_custom_quantity[key] = {
            "item_id": item_id,
            "expires_at": time.monotonic() + CUSTOM_QUANTITY_TIMEOUT_SECONDS,
        }


def get_pending_custom_quantity(chat_id: int, user_id: int):
    key = _quantity_state_key(chat_id, user_id)
    with _pending_custom_quantity_lock:
        state = _pending_custom_quantity.get(key)
        if not state:
            return None
        if state["expires_at"] <= time.monotonic():
            _pending_custom_quantity.pop(key, None)
            return None
        return dict(state)


def clear_pending_custom_quantity(chat_id: int, user_id: int):
    key = _quantity_state_key(chat_id, user_id)
    with _pending_custom_quantity_lock:
        _pending_custom_quantity.pop(key, None)


def edit_status_message(
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup=None,
    parse_mode=None,
) -> bool:
    if not message_id:
        return False
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except Exception as exc:
        print(f"[STATUS EDIT] ignored: {exc}")
        return False


def send_long_plain(chat_id: int, text: str):
    max_len = 3800
    remaining = str(text)
    while remaining:
        if len(remaining) <= max_len:
            bot.send_message(chat_id, remaining)
            return
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 1:
            cut = max_len
        bot.send_message(chat_id, remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")


def find_active_order(user_id: int, item_id: str):
    now = now_vn()

    for row_number, row in sheet_rows(SHEET_ORDERS, ORDERS_HEADERS):
        if str(row.get("user_id", "")).strip() != str(user_id):
            continue

        if str(row.get("item_id", "")).strip().upper() != item_id.upper():
            continue

        status = str(row.get("status", "")).strip().upper()

        if status not in ("CREATING", "PENDING"):
            continue

        expires_raw = str(row.get("expires_at", "")).strip()

        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)

                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=now.tzinfo)

                if expires_at <= now:
                    continue

            except ValueError:
                continue

        return row_number, row

    return None

def create_payment_for_item(
    call,
    item_id: str,
    quantity: int = 1,
    status_message_id: int | None = None,
):
    chat_id = call.message.chat.id
    quantity = int(quantity)

    if quantity < 1 or quantity > MAX_ORDER_QUANTITY:
        text = f"⚠️ Số lượng phải từ 1 đến {MAX_ORDER_QUANTITY}."
        if not edit_status_message(chat_id, status_message_id, text):
            bot.send_message(chat_id, text)
        return

    if not payos_client:
        raise RuntimeError(
            "Chưa cấu hình PAYOS_CLIENT_ID / PAYOS_API_KEY / PAYOS_CHECKSUM_KEY"
        )
    if not PUBLIC_BASE_URL:
        raise RuntimeError("Chưa cấu hình PUBLIC_BASE_URL")
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Chưa cấu hình GOOGLE_SHEET_ID")

    found = ITEM_BY_ID.get(item_id)
    if not found:
        text = "❌ Sản phẩm không tồn tại."
        if not edit_status_message(chat_id, status_message_id, text):
            bot.send_message(chat_id, text)
        return

    _, item = found
    unit_amount = parse_price_vnd(item.get("price", ""))
    if not unit_amount:
        text = (
            "⚠️ Sản phẩm này chưa có giá cố định trên bot. "
            "Vui lòng nhắn Telegram Admin để được báo giá."
        )
        markup = manual_resource_markup(item_id, quantity)
        if not edit_status_message(chat_id, status_message_id, text, reply_markup=markup):
            bot.send_message(chat_id, text, reply_markup=markup)
        return

    total_amount = unit_amount * quantity

    with state_lock:
        active_order = find_active_order(call.from_user.id, item_id)
        if active_order:
            _, existing_order = active_order
            existing_status = str(existing_order.get("status", "")).strip().upper()
            existing_order_code = str(existing_order.get("order_code", "")).strip()
            checkout_url = str(existing_order.get("checkout_url", "")).strip()
            existing_quantity = int(existing_order.get("quantity", "1") or 1)

            if existing_status == "PENDING" and checkout_url:
                text = (
                    "🧾 Bạn đang có một đơn chưa thanh toán cho sản phẩm này.\n\n"
                    f"🔢 Số lượng: {existing_quantity}\n"
                    f"🧾 Mã đơn: {existing_order_code}\n"
                    "👉 Bot gửi lại trang thanh toán hiện tại."
                )
                markup = payment_markup(checkout_url, item_id)
                if not edit_status_message(chat_id, status_message_id, text, reply_markup=markup):
                    bot.send_message(chat_id, text, reply_markup=markup)
            else:
                text = "⏳ Đơn hàng trước của bạn đang được tạo. Vui lòng chờ vài giây."
                if not edit_status_message(chat_id, status_message_id, text):
                    bot.send_message(chat_id, text)
            return

        order_code = next_order_code()
        reserved, available_count = reserve_resources(
            item_id,
            order_code,
            call.from_user,
            quantity,
        )

        if not reserved:
            if available_count <= 0:
                text = (
                    "⚠️ Tài nguyên này không được up sẵn trên bot, "
                    "nhắn tele admin để nhận tài nguyên."
                )
                markup = manual_resource_markup(item_id, quantity)
            else:
                text = (
                    f"⚠️ Kho hiện chỉ còn {available_count} tài nguyên, "
                    f"không đủ số lượng {quantity}.\n"
                    "Vui lòng chọn lại số lượng hoặc liên hệ Admin."
                )
                markup = quantity_menu_markup(item_id)

            if not edit_status_message(chat_id, status_message_id, text, reply_markup=markup):
                bot.send_message(chat_id, text, reply_markup=markup)
            return

        inventory_rows = [row_number for row_number, _resource in reserved]
        created_at = now_vn()
        expires_at = created_at + timedelta(minutes=PAYMENT_EXPIRE_MINUTES)

        append_sheet_row(
            SHEET_ORDERS,
            ORDERS_HEADERS,
            {
                "order_code": order_code,
                "item_id": item_id,
                "product_name": item["name"],
                "amount": total_amount,
                "status": "CREATING",
                "chat_id": chat_id,
                "user_id": call.from_user.id,
                "username": user_tag(call.from_user),
                "inventory_row": inventory_rows[0],
                "created_at": created_at.isoformat(timespec="seconds"),
                "expires_at": expires_at.isoformat(timespec="seconds"),
                "quantity": quantity,
                "unit_amount": unit_amount,
                "inventory_rows": ",".join(str(row) for row in inventory_rows),
            },
        )

        order_found = find_order(order_code)
        if not order_found:
            release_reserved_rows(inventory_rows)
            raise RuntimeError(f"Không tìm thấy đơn vừa tạo: {order_code}")

        order_row, _ = order_found
        edit_status_message(
            chat_id,
            status_message_id,
            f"✅ Kho đủ {quantity} tài nguyên. Bot đang tạo mã QR thanh toán...",
        )

        try:
            payment_request = CreatePaymentLinkRequest(
                order_code=order_code,
                amount=total_amount,
                description=f"VS{order_code}",
                cancel_url=f"{PUBLIC_BASE_URL.rstrip('/')}/payment/cancel",
                return_url=f"{PUBLIC_BASE_URL.rstrip('/')}/payment/success",
                expired_at=int(expires_at.timestamp()),
            )
            payment_link = payos_client.payment_requests.create(payment_request)
            checkout_url = payment_link.checkout_url
            payment_link_id = payment_link.payment_link_id
            qr_text = payment_link.qr_code

            update_sheet_row(
                SHEET_ORDERS,
                ORDERS_HEADERS,
                order_row,
                {
                    "status": "PENDING",
                    "checkout_url": checkout_url,
                    "payment_link_id": payment_link_id,
                },
            )
        except Exception as exc:
            release_reserved_rows(inventory_rows)
            update_sheet_row(
                SHEET_ORDERS,
                ORDERS_HEADERS,
                order_row,
                {"status": "FAILED", "error": str(exc)[:500]},
            )
            edit_status_message(
                chat_id,
                status_message_id,
                "⚠️ Bot chưa thể tạo mã QR. Vui lòng thử lại hoặc liên hệ Admin.",
            )
            raise

    qr_image = qrcode.make(qr_text)
    buffer = io.BytesIO()
    buffer.name = f"QR_{order_code}.png"
    qr_image.save(buffer, format="PNG")
    buffer.seek(0)

    caption = (
        f"🧾 **ĐƠN HÀNG #{order_code}**\n\n"
        f"📦 **Sản phẩm:** {item['name']}\n"
        f"🔢 **Số lượng:** {quantity}\n"
        f"💵 **Đơn giá:** {unit_amount:,}đ\n"
        f"💰 **Thanh toán:** {total_amount:,}đ\n"
        f"⏳ **Thời hạn:** {PAYMENT_EXPIRE_MINUTES} phút\n\n"
        "Quét QR để thanh toán.\n"
        "Khi ngân hàng xác nhận, bot sẽ tự động giao tài khoản/mật khẩu."
    ).replace(",", ".")

    bot.send_photo(
        chat_id,
        buffer,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=payment_markup(checkout_url, item_id),
    )
    edit_status_message(
        chat_id,
        status_message_id,
        f"✅ Đã kiểm tra kho và tạo đơn #{order_code} thành công.",
    )


def deliver_paid_order(order_code: int, amount: int, reference: str):
    with state_lock:
        found = find_order(order_code)
        if not found:
            raise RuntimeError(f"Order {order_code} not found")
        order_row, order = found
        if order.get("status") == "DELIVERED":
            return

        expected_amount = int(order.get("amount", "0") or 0)
        if expected_amount != int(amount):
            update_sheet_row(
                SHEET_ORDERS,
                ORDERS_HEADERS,
                order_row,
                {
                    "status": "AMOUNT_MISMATCH",
                    "reference": reference,
                    "error": f"Expected {expected_amount}, received {amount}",
                },
            )
            raise RuntimeError("Payment amount mismatch")

        quantity = int(order.get("quantity", "1") or 1)
        inventory_rows_raw = str(order.get("inventory_rows", "")).strip()
        if inventory_rows_raw:
            inventory_rows = [
                int(value.strip())
                for value in inventory_rows_raw.split(",")
                if value.strip().isdigit()
            ]
        else:
            inventory_rows = [int(order.get("inventory_row", "0") or 0)]

        inventory_rows = [row for row in inventory_rows if row > 0]
        if not inventory_rows:
            raise RuntimeError("Order has no reserved inventory rows")
        if len(inventory_rows) != quantity:
            raise RuntimeError(
                f"Reserved inventory count mismatch: expected {quantity}, got {len(inventory_rows)}"
            )

        ranges = [f"'{SHEET_INVENTORY}'!A{row}:I{row}" for row in inventory_rows]
        response = sheets_service().spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEET_ID,
            ranges=ranges,
        ).execute()
        value_ranges = response.get("valueRanges", [])
        if len(value_ranges) != len(inventory_rows):
            raise RuntimeError("Could not load all reserved inventory rows")

        resources = []
        for row_number, value_range in zip(inventory_rows, value_ranges):
            values = value_range.get("values", [])
            if not values:
                raise RuntimeError(f"Reserved inventory row {row_number} not found")
            row_values = values[0] + [""] * (len(INVENTORY_HEADERS) - len(values[0]))
            inventory = dict(zip(INVENTORY_HEADERS, row_values[:len(INVENTORY_HEADERS)]))
            if str(inventory.get("order_code", "")) != str(order_code):
                raise RuntimeError(
                    f"Inventory row {row_number} reservation does not match order"
                )
            resource = inventory.get("resource", "").strip()
            if not resource:
                raise RuntimeError(f"Inventory row {row_number} resource is empty")
            resources.append(resource)

        paid_at = iso_now()
        update_sheet_row(
            SHEET_ORDERS,
            ORDERS_HEADERS,
            order_row,
            {"status": "PAID", "paid_at": paid_at, "reference": reference},
        )
        for inventory_row in inventory_rows:
            update_sheet_row(
                SHEET_INVENTORY,
                INVENTORY_HEADERS,
                inventory_row,
                {
                    "status": "SOLD",
                    "sold_at": paid_at,
                    "reserved_until": "",
                },
            )

    resource_lines = "\n".join(
        f"{index}. {resource}"
        for index, resource in enumerate(resources, start=1)
    )
    customer_text = (
        "✅ THANH TOÁN THÀNH CÔNG\n\n"
        f"🧾 Mã đơn: {order_code}\n"
        f"📦 Sản phẩm: {order.get('product_name', '')}\n"
        f"🔢 Số lượng: {quantity}\n\n"
        "🔐 TÀI NGUYÊN\n"
        f"{resource_lines}\n\n"
        "⚠️ Vui lòng lưu lại thông tin và đổi mật khẩu sau khi đăng nhập."
    )

    try:
        send_long_plain(int(order["chat_id"]), customer_text)
    except Exception as exc:
        update_sheet_row(
            SHEET_ORDERS,
            ORDERS_HEADERS,
            order_row,
            {"status": "DELIVERY_FAILED", "error": str(exc)[:500]},
        )
        if ADMIN_CHAT_ID:
            bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ GIAO HÀNG THẤT BẠI\nMã đơn: {order_code}\n"
                f"Khách: {order.get('username', '')}\nLỗi: {exc}",
            )
        raise

    delivered_at = iso_now()
    update_sheet_row(
        SHEET_ORDERS,
        ORDERS_HEADERS,
        order_row,
        {"status": "DELIVERED", "delivered_at": delivered_at},
    )

    remaining = available_resource_count(order.get("item_id", ""))
    admin_text = (
        "✅ BOT ĐÃ GIAO TÀI NGUYÊN\n\n"
        f"🧾 Mã đơn: {order_code}\n"
        f"👤 Khách: {order.get('username', '')}\n"
        f"🆔 Telegram ID: {order.get('user_id', '')}\n"
        f"📦 Sản phẩm: {order.get('product_name', '')}\n"
        f"🔢 Số lượng: {quantity}\n"
        f"💰 Thanh toán: {amount:,}đ\n"
        f"📊 Kho còn lại: {remaining}\n\n"
        "🔐 Tài nguyên đã giao:\n"
        f"{resource_lines}"
    ).replace(",", ".")

    if ADMIN_CHAT_ID:
        try:
            send_long_plain(ADMIN_CHAT_ID, admin_text)
        except Exception as exc:
            print(f"[ADMIN DELIVERY NOTIFY] error: {exc}")


# Initialize sheet structure when configuration is available.
try:
    ensure_google_sheets()
    load_image_cache()
except Exception as exc:
    print(f"[GOOGLE_SHEETS_INIT] error: {exc}")

# =========================
# Helpers
# =========================
def admin_username_clean() -> str:
    return ADMIN_USERNAME.lstrip("@")


def admin_url() -> str:
    return f"https://t.me/{admin_username_clean()}"


def is_admin(user) -> bool:
    if ADMIN_CHAT_ID and user.id == ADMIN_CHAT_ID:
        return True
    admin_u = admin_username_clean().lower()
    u = (user.username or "").lower()
    return u == admin_u


def send_with_optional_photo(chat_id: int, img_key: str, caption: str, reply_markup=None):
    file_id = get_image(img_key)

    if file_id:
        try:
            bot.send_photo(
                chat_id,
                file_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            # An old/invalid Telegram file_id must not prevent the menu from
            # appearing. Clear the cached value and fall back to plain text.
            print(f"[SEND PHOTO] key={img_key} failed, fallback to text: {exc}")
            _image_cache.pop(img_key.upper().strip(), None)

    bot.send_message(
        chat_id,
        caption,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


def safe_send_markdown(chat_id: int, text: str, reply_markup=None):
    # message limit ~4096; keep margin
    if len(text) <= 3500:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        return
    parts = text.split("\n\n")
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 2 > 3500:
            bot.send_message(chat_id, buf, parse_mode="Markdown")
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        bot.send_message(chat_id, buf, parse_mode="Markdown", reply_markup=reply_markup)


def build_prefilled_admin_link(text: str) -> str:
    # Opens admin chat with prefilled message
    return f"https://t.me/{admin_username_clean()}?text={quote(text)}"


def user_tag(from_user) -> str:
    return f"@{from_user.username}" if from_user.username else "@username"


# =========================
# Catalog (menu 6 mục, bên trong có sản phẩm nhỏ)
# =========================
CATALOG = [
    {
        "cat_id": "TELE",
        "title": "📱 TELE",
        "desc": "📱 **TELE – Danh mục sản phẩm**\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "TELE_CLONE",
                "group": "TELE",
                "name": "Tài khoản Telegram Spam nhóm",
                "price": "35.000đ",
                "detail": "🐙 **Tài khoản Telegram cơ bản**\n💰 Giá: **35.000đ**\n📌 Hỗ trợ đăng nhập ban đầu\n🎁 bảo hành 1 đổi 1 nếu tài khoản bị đóng băng",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "TELE_VIP",
                "group": "TELE",
                "name": "Tài khoản tele có sẵn sao 1 tháng",
                "price": "200.000đ",
                "detail": "🐙 **Tài khoản Telegram tiện ích nâng cao**\n💰 Giá: **200.000đ**\n📌 Hỗ trợ đăng nhập ban đầu\n🎁 bảo hành 1 đổi 1 nếu tài khoản bị đóng băng",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "TELE_PACK",
                "group": "TELE",
                "name": "📌TELE CÀO 50 SỐ - CỔ TRÂU",
                "price": "80.000đ",
                "detail": (
                    "✅ Hỗ trợ đăng nhập ban đầu\n"
                    "✅ Tài khoản cứng, ổn định, sử dụng bền\n"
                    "✅ Có thể dùng làm boss theo nhu cầu\n"
                    "🎁 Bảo hành 1 đổi 1 trong 24h nếu tài khoản bị đóng băng đúng điều kiện\n\n"
                    "📌 Lưu ý khi sử dụng:\n"
                    "🔹 Chỉ log bằng file 1 lần duy nhất\n"
                    "🔹 Muốn log sang thiết bị khác cần log thủ công bằng SĐT\n"
                    "🔹 Log 2 thiết bị bằng file dẫn đến acc bị đăng xuất sẽ không bảo hành\n\n"
                    "📣 Điều kiện bảo hành:\n"
                    "🎥 Cần quay video từ lúc mở acc đến quá trình kiểm tra sử dụng để shop hỗ trợ bảo hành.\n\n"
                    "⚠️ Tuyệt đối không dán file .exe vào thư mục khi file chưa được giải nén hoàn toàn."
                ),
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "TELE_UPSTAR",
                "group": "TELE",
                "name": "Nâng sao Telegram theo tháng",
                "price": "Xem chi tiết",
                "detail": (
                    "**🐙 NÂNG CẤP TELEGRAM**\n\n"
                    "✅ 1 tháng: **125.000đ**\n"
                    "✅ 3 tháng: **360.000đ**\n"
                    "✅ 6 tháng: **550.000đ**\n"
                    "✅ 1 năm: **850.000đ**\n\n"
                    "📌 Bảo hành số ngày theo gói nâng cấp, không bảo hành tài khoản  bị đóng băng"
                ),
                "require_hint": "Ghi chú: gói ... tháng (1m/3m/6m/1y), Số lượng :  ",
            },
            {
                "item_id": "TELE_GROUP",
                "group": "TELE",
                "name": " Kênh Telegram (bảng size)",
                "price": "Xem chi tiết",
                "detail": (
                    "👥 ** KÊNH TELEGRAM**\n\n"
                    "📱 1K7–2K mem: **150.000đ**\n"
                    "📱 5K mem: **400.000đ**\n"
                    "📱 10K mem: **800.000đ**\n"
                    "📱 20K mem: **1.500.000đ**\n\n"
                    "🎁 Mua 8 tặng 1 (cùng loại)\n"
                    "📌 Bàn giao quyền sở hữu theo quy trình"
                ),
                "require_hint": "Ghi chú: size kênh, Số lượng :  ",
            },
            {
                "item_id": "TELE_GROUP_ONLINE",
                "group": "TELE",
                "name": "Nhóm tele có mem online ngày đêm ",
                "price": "Xem chi tiết",
                "detail": (
                    "🔥 ** MEM ONLINE**\n\n"
                    "📱 500 Mem online : **400.000đ**\n"
                    "📱 1K Mem online : **800.000đ**\n"
                    "📱 2K Mem online : **1.500.000đ**\n"
                    "📱 5K Mem online : **4.000.000đ**\n"
                    "📱 10K Mem online : **7.500.000đ**\n\n"
                    "🎁 THỜI HẠN 30 NGÀY , BẢO HÀNH KHI TUỘT MEM ONLINE\n"
                    "⚠️ CUNG CẤP NHÓM CÓ SỐ LƯỢNG MEM THEO YÊU CẦU. BÀN GIAO BẰNG CÁCH CHUYỂN QUYỀN CHỦ SỞ HỮU NHÓM - CÓ HỖ TRỢ CẦM CHỦ SỞ HỮU."
                ),
                "require_hint": "Yêu cầu: size nhóm, Số lượng :  ",
            },
        ],
        "img_key": "CAT_TELE",
    },
    {
        "cat_id": "DOMAIN",
        "title": "🌐 TÊN MIỀN",
        "desc": (
            "🌐 **Giá – 370K / 1 domain + free landing page có sẵn**\n\n📌 .VIP .TOP .LIVE .PRO .WIN .INFO .FUN    .US .CC\n📌 .CLICK  .LOVE  .ONLINE .ONL    .SHOP .STORE .TECH .XYZ .ONE .CASINO .SITE .LINK .LOL .ASIA .CLUB .RUN .BIO .NYC .PLUS "
            "\n✅ Bảo hành suốt thời gian sử dụng\n"
            "✅ Đổi hậu đài ~ 3 phút\n"
            "👉 Chọn mục bên dưới 👇"
        ),
        "items": [
            {
                "item_id": "DOMAIN_370",
                "group": "TÊN MIỀN",
                "name": "Tên miền đồng giá 370k, free landing page có sẵn",
                "price": "370.000đ",
                "detail": (
                    "✅ Bảo hành suốt thời gian sử dụng\n"
                    "✅ Đổi hậu đài ~ 3 phút\n\n"
                    "📌 Khi mua, ghi rõ **đuôi** (...) và **keyword**."
                ),
                "require_hint": "Ghi chú keyword/đuôi : ...",
            },
        ],
        "img_key": "CAT_DOMAIN",
    },
    {
        "cat_id": "FB",
        "title": "📘 VIA - PAGE FACEBOOK",
        "desc": "📘 **PAGE CỔ KHÁNG & LIVESTREAM**\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "FB_ACTIVE",
                "group": "FACEBOOK",
                "name": "CHUYÊN SPAM NGON",
                "price": "150.000đ",
                "detail": "🟢 **Chuyên spam ngon, không bảo hành**\n💰 Giá: **150.000đ**\n📌 Phù hợp nhu cầu đăng bài / quản lý nội dung",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "FB_PAGE_MANAGER",
                "group": "FACEBOOK",
                "name": "VIA NẮM PAGE - TRÂU HƠN",
                "price": "250.000đ",
                "detail": "🟢 **KHÔNG NÊN THAY TÊN ĐỔI ẢNH VÌ ĐÃ ĐC XMDT - ĐỔI ĐỂ DIE ACC KHÔNG BH - BH NGÂM 24 TIẾNG**\n💰 Giá: **250.000đ**\n📌 bao back 1 đổi 1 trong 24h",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "FB_OLD",
                "group": "FACEBOOK",
                "name": "CỔ LÂU NĂM CÓ BÀI ĐĂNG",
                "price": "450.000đ – 1.500.000đ",
                "detail": "🟢 **THÍCH HỢP XÂY DỰNG NHÂN VẬT : TỪ 2019 ~ 2024 CÓ BÀI ĐĂNG ĐỂ CHỈNH SỬA : 450 ~ 1M5 ( CÓ ID CHECK LỰA )**\n💰 Giá: **450.000đ – 1.500.000đ**\n📌 Có lựa chọn theo nhu cầu",
                "require_hint": "Ghi chú: năm/tiêu chí lựa chọn, Số lượng :  ",
            },
            {
                "item_id": "FB_VERIFY",
                "group": "FACEBOOK",
                "name": "FB TÍCH XANH 500K",
                "price": "500.000đ (duy trì 200k/tháng)",
                "detail": "🟢 **PHÍ DUY TRÌ TÍCH 200/THÁNG**\n💰 Giá: **500.000đ**\n📌 Duy trì: **200.000đ/tháng**",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "PAGE_LIVE",
                "group": "FACEBOOK",
                "name": "LIVESTREAM 1K FLOW",
                "price": "750.000đ",
                "detail": "📄 **CÓ TÍNH NĂNG QC LIVESTREAM**\n💰 Giá: **750.000đ**\n📌 Bàn giao quyền quản trị theo quy trình",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "PAGE_VERIFY",
                "group": "FACEBOOK",
                "name": "PAGE TÍCH XANH",
                "price": "1.500.000đ",
                "detail": "📄 **PAGE TÍCH XANH**\n💰 Giá: **1.500.000đ**",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "PAGE_BASIC",
                "group": "FACEBOOK",
                "name": "PAGE TRẮNG",
                "price": "150.000đ",
                "detail": "📄 **PAGE TRẮNG**\n💰 Giá: **150.000đ**\n📌 0 follow",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "PAGE_5K",
                "group": "FACEBOOK",
                "name": "PAGE CỐ KHÁNG 5K FLOW",
                "price": "300.000đ",
                "detail": "📄 **CỐ KHÁNG 5K FLOW**\n💰 Giá: **450.000đ**",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
            {
                "item_id": "PAGE_10K",
                "group": "FACEBOOK",
                "name": "PAGE CỐ KHÁNG 10K FLOW",
                "price": "750.000đ",
                "detail": "📄 **CỐ KHÁNG 10K FLOW**\n💰 Giá: **750.000đ**",
                "require_hint": "Ghi chú: . . ., Số lượng :  ",
            },
        ],
        "img_key": "CAT_FB",
    },
    {
        "cat_id": "ZALO",
        "title": "💬 ZALO",
        "desc": "💬 **ZALO – Danh mục sản phẩm**\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "ZALO_TRUST",
                "group": "ZALO",
                "name": "ZALO NGÂM TRUST – ĐÃ XMDT",
                "price": "500.000đ",
                "detail": (
                    "✅ **ZALO NGÂM TRUST – ĐÃ XMDT** ✅\n\n"
                    "💎 Trust Device: **500.000đ**\n"
                    "📌 Đã XMDT\n"
                    "🌐 Kèm Proxy\n"
                    "🛡️ Tài khoản đã ngâm Trust, phù hợp cho anh em cần độ ổn định cao hơn\n\n"
                    "🔐 **CHẾ ĐỘ BẢO HÀNH**\n\n"
                    "✅ Zalo các loại chỉ bảo hành khi treo ngâm đủ 3 ngày\n"
                    "✅ Zalo bảo hành SIM 10 ngày kể từ khi giao hàng\n"
                    "✅ Shop hỗ trợ đá và đổi số cho khách\n\n"
                    "⚠️ **Lưu ý quan trọng:**\n"
                    "❌ Khi đã thay đổi thông tin hoặc đem đi cào sẽ không bảo hành\n"
                    "📌 Trong 10 ngày giữ SIM, khách cần đổi SIM của mình vào tài khoản.\n"
                    "⏰ Sau 10 ngày nếu khách chưa đổi SIM và không vào được Zalo, shop xin phép không chịu trách nhiệm."
                ),
                "require_hint": "Ghi chú: số lượng, nhu cầu sử dụng: ",
            },
        ],
        "img_key": "CAT_ZALO",
    },
    {
        "cat_id": "TIKTOK",
        "title": "🎵 TIKTOK",
        "desc": "🎵 **TIKTOK – Danh mục sản phẩm**\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "TIKTOK_BUILD",
                "group": "TIKTOK",
                "name": "Tiktok xây kênh 1-2K follow ",
                "price": "200.000đ",
                "detail": "🎵 **Tiktok xây kênh 1K - 2K follow**\n💰 Giá: **200.000đ**\n📌 Quốc gia: **Việt - US - UK**",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
            {
                "item_id": "TIKTOK_BUILDS",
                "group": "TIKTOK",
                "name": "Tiktok US nhiều FL xây kênh ",
                "price": "300.000đ",
                "detail": "🔥 **Kênh TikTok full chức năng Live**\n✅ Nhắn admin lấy link kênh và giá từng kênh tùy follow\n✅ Bao back – bao login\n🛡️ Bảo hành **1 đổi 1 trong 24 giờ** kể từ khi giao kênh nếu kênh chưa Live nhưng bị cấm Live vĩnh viễn\n⚠️ Không bảo hành trường hợp đang Live thì bị sập Live\n🛠️ Lưu ý: **Khách tự fix Live**\n💰 Giá: **150.000đ/kênh**",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
            {
                "item_id": "TIKTOK_LIVE_BASIC",
                "group": "TIKTOK",
                "name": "Tiktok LIVE CHAY STUDIO (Việt Cổ)",
                "price": "150.000đ",
                "detail": "🎵 **Tài khoản Tiktok LIVE**\n💰 Giá: **150.000đ**\n📌 Quốc gia: **Việt - US - UK**\n📌 Bao log, bao back, live chay tốt vì hàng việt cổ, online lại dễ lên đề xuất, chỉ Live Studio, buff đô sẽ bị ngắt.",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
            {
                "item_id": "TIKTOK_LIVE_VIET_OLD",
                "group": "TIKTOK",
                "name": "Tiktok Việt Cổ Bao Camp Đầu -> Bán chạy",
                "price": "250.000đ",
                "detail": "🔥 **TikTok Việt cổ – hỗ trợ Live & Ads**\n✅ **Bao back:** Sau khi mua, vui lòng đăng nhập trên điện thoại và ngâm tài khoản **3–4 ngày** để có thể thay đổi thông tin. Sau 4 ngày nếu khách chưa đổi thông tin và phát sinh vấn đề, khách tự chịu trách nhiệm\n📢 **Bao duyệt chiến dịch Ads đầu tiên**\n🛡️ **Bao hạn chế toàn vẹn 5 phút** \n⏱️ **Bao ngắt Live dưới 30 phút** cho phiên Live đầu tiên trong ngày\n⚠️ Không áp dụng bao ngắt với Live buff quảng bá hoặc tài khoản quảng cáo Live chay\n❌ Không bao ngắt Ads hoặc việc Ads có cắn tiền hay không; kết quả phụ thuộc setup, IP và hệ thống TikTok quét vi phạm\n🕒 Tài khoản cần được sử dụng trong ngày, tính từ thời điểm bàn giao\n💰 Giá: **250.000đ/1 tài khoản Việt cổ**",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
            {
                "item_id": "TIKTOK_SCAN_450",
                "group": "TIKTOK",
                "name": "Tiktok người dùng - buff đô bao cắn phiên đầu -> TOP",
                "price": "450.000đ",
                "detail": "🔥 **Kênh TikTok Scan cổ – User thật, Follow thật**\n🎯  Live chay tốt, buff đô bao cắn phiên đầu \n💰 Giá: **450.000đ/kênh**\n\n🎁 **Đặc quyền khi mua kênh**\n👁️ Tặng 30 mắt Live miễn phí, tự động cộng view thật từ thiết bị của shop(Không phải buff) và duy trì lượng xem ổn định 24/24, hỗ trợ comment tăng tương tác theo yêu cầu của khách và bắt xu hướng thả rương\n\n✅ Live không giới hạn số phiên\n✅ **Bao gỡ vi phạm lần đầu:** Hỗ trợ mở khóa lỗi 3 ngày, 7 ngày hoặc 30 ngày miễn phí\n🔐 Đây là kênh Scan từ người dùng thật, khách bắt buộc đổi **Mail + Password** sau khi ngâm đủ 72 giờ. Quá 72 giờ kể từ lúc bàn giao mà chưa đổi thông tin, nếu bị back hoặc mất kênh, team không chịu trách nhiệm",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
            {
                "item_id": "TIKTOK_SCAN_500",
                "group": "TIKTOK",
                "name": "Tiktok người dùng - bao hạn chế 7 ngày -> TOP",
                "price": "500.000đ",
                "detail": "🔥 **Kênh TikTok Scan cổ – User thật, buff sẵn Follow kích Studio**\n🎯 Chuyên dùng cho Live chay\n💰 Giá: **500.000đ/kênh**\n\n🎁 **Đặc quyền khi mua kênh**\n👁️ Tặng mắt Live miễn phí, tự động buff và duy trì lượng xem ổn định 24/24, hỗ trợ tăng tương tác và bắt xu hướng thả rương\n\n🛡️ **Bảo hành Bao Live trong 7 ngày**\n✅ Live không giới hạn số phiên\n✅ **Bao ngắt luồng:** Live chay thả rương dưới 60 phút bị ngắt sẽ được đổi kênh mới\n✅ **Bao mất đề xuất:** Phiên Live chay không buff, view thực tế dưới 100 sẽ được đổi kênh mới\n✅ **Bao kháng hạn chế:** Hỗ trợ kháng trong 60 phút, không khôi phục được sẽ đổi kênh mới\n✅ **Bao gỡ vi phạm lần đầu:** Hỗ trợ mở khóa lỗi 3 ngày, 7 ngày hoặc 30 ngày miễn phí\n✅ **Bao cấm Live vĩnh viễn:** Hỗ trợ kháng và kéo mở lại quyền Live\n✅ **Bao die tài khoản:** Tài khoản đã mở khóa nhưng tiếp tục bị quét sập sẽ được cấp lại 1 tài khoản mới\n\n⚠️ **Điều khoản từ chối bảo hành**\n❌ Với lỗi **Dịch vụ và hàng hóa bị cấm**, sau khi team đã hỗ trợ mở khóa lần đầu sẽ không bao ngắt Live cho các phiên tiếp theo. Các chính sách bảo hành khác vẫn áp dụng đủ 7 ngày\n🔐 Đây là kênh Scan từ người dùng thật, khách bắt buộc đổi **Mail + Password** sau khi ngâm đủ 72 giờ. Quá 72 giờ kể từ lúc bàn giao mà chưa đổi thông tin, nếu bị back hoặc mất kênh, team không chịu trách nhiệm",
                "require_hint": "Yêu cầu: quốc gia | SL",
            },
        ],
        "img_key": "CAT_TIKTOK",
    },
    {
        "cat_id": "WEB",
        "title": "🖥️ LÀM WEB",
        "desc": "🖥️ **LÀM WEBSITE THEO YÊU CẦU **\n💬 ** WEB vòng quay may mắn : mẫu https://u888-vongquaymayman.online/, http://gg88k.xyz/\n💬 **Giá:** thương lượng theo nhu cầu\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "WEB_QUOTE",
                "group": "LÀM WEB",
                "name": "Tư vấn & báo giá website",
                "price": "Thương lượng",
                "detail": (
                    "🖥️ **TƯ VẤN & BÁO GIÁ WEBSITE**\n\n"
                    "📌 Bạn gửi admin các thông tin:\n"
                    "- Loại web (landing/bán hàng/giới thiệu)\n"
                    "- Chức năng cần có\n"
                    "- Mẫu tham khảo\n"
                    "- Thời gian mong muốn\n"
                ),
                "require_hint": "Yêu cầu: loại web/chức năng/mẫu, Số lượng :  ",
            },
        ],
        "img_key": "CAT_WEB",
    },
    {
        "cat_id": "MB",
        "title": "🏦 STK MB BANK",
        "desc": "🏦 **Mua tk MB Bank để đăng ký tài khoản game**\n💰 13K / 1 TK\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "MB_13K",
                "group": "MB BANK",
                "name": "TK MB Bank",
                "price": "13.000đ",
                "detail": "🏦 **Bạn cần có tài khoản MB Bank để admin tạo thêm tài khoản MB mới cho bạn, hoặc không thì khi chơi phải rút tiền về tk của ad**\n💰 Giá: **13.000đ / 1 TK**\n📌 Dùng theo nhu cầu tạo tài khoản game lấy nạp đầu, đánh đối lấy chỉ tiêu,...",
                "require_hint": "Yêu cầu: SL",
            },
        ],
        "img_key": "CAT_MB",
    },
    {
        "cat_id": "OTP",
        "title": "📲 OTP SĐT",
        "desc": "📲 **Ad gửi sdt nhận được OTP**\n💰 7K / 1 OTP\n👉 Chọn mục bên dưới 👇",
        "items": [
            {
                "item_id": "OTP_7K",
                "group": "OTP",
                "name": "OTP SĐT đăng ký game",
                "price": "7.000đ",
                "detail": "📲 **OTP SĐT đăng ký game**\n💰 Giá: **7.000đ / 1 OTP**\n📌 Khi mua, ghi rõ nền tảng/game cần OTP.",
                "require_hint": "Yêu cầu: nền tảng/game",
            },
        ],
        "img_key": "CAT_OTP",
    },
    {
        "cat_id": "BOT",
        "title": "🤖🧠 BOT SPAM NHẬN KM NẠP ĐẦU",
        "desc": (
            "🤖🧠 **BOT SPAM NHẬN KM NẠP ĐẦU**\n\n"
            "👉 Ví dụ giống bot: `@GG88codefree_bot`\n"
            "💰 **Giá:** 500.000đ / 1 bot\n"
            "👉 Chọn mục bên dưới 👇"
        ),
        "items": [
            {
                "item_id": "bot_spam",
                "group": "BOT SPAM",
                "name": "Bot Spam Nạp Đầu",
                "price": "500.000đ",
                "detail": (
                    "🤖🧠 **BOT SPAM NẠP ĐẦU**\n\n"
                    "👉 Khi khách hàng nhấn vào bot, bot sẽ chạy kịch bản hướng dẫn khách đăng ký đúng link.\n\n"
                    "📌 Khách gửi bill chuyển khoản vào bot.\n"
                    "📌 Bot sẽ chuyển tiếp thông tin về Telegram admin của bạn, gồm:\n"
                    "- Tên tài khoản game\n"
                    "- Thời gian đăng ký\n"
                    "- Bill chuyển khoản của khách hàng\n\n"
                    "✅ Phù hợp để admin treo bill và xử lý đơn nhanh hơn."
                ),
                "require_hint": "Yêu cầu: SL",
            },
        ],
        "img_key": "CAT_BOT",
    }
]

CAT_BY_ID = {c["cat_id"]: c for c in CATALOG}
ITEM_BY_ID = {}
for c in CATALOG:
    for it in c.get("items", []):
        ITEM_BY_ID[it["item_id"]] = (c["cat_id"], it)

# =========================
# UI (menu chính 2 cột)
# =========================
def kb_main():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔥 TIKTOK NỔI BẬT", callback_data="CAT|TIKTOK"),
    )
    kb.add(
        types.InlineKeyboardButton("🌐 TÊN MIỀN", callback_data="CAT|DOMAIN"),
        types.InlineKeyboardButton("📦 MỤC KHÁC", callback_data="OTHER"),
    )
    return kb


def kb_other():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 TELE", callback_data="CAT|TELE"),
        types.InlineKeyboardButton("📘 FACEBOOK", callback_data="CAT|FB"),
        types.InlineKeyboardButton("💬 ZALO", callback_data="CAT|ZALO"),
        types.InlineKeyboardButton("🖥️ LÀM WEB", callback_data="CAT|WEB"),
        types.InlineKeyboardButton("🤖🧠 BOT SPAM CHO SALE", callback_data="CAT|BOT"),
        types.InlineKeyboardButton("📲 OTP SĐT", callback_data="CAT|OTP"),
        types.InlineKeyboardButton("🏦 STK MB BANK", callback_data="CAT|MB"),
    )
    kb.add(
        types.InlineKeyboardButton("💳 Thanh toán", callback_data="PAY"),
        types.InlineKeyboardButton("📩 Admin", url=admin_url()),
    )
    kb.add(types.InlineKeyboardButton("⏪ Quay lại menu", callback_data="BACK_MAIN"))
    return kb


def kb_category(cat_id: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    cat = CAT_BY_ID.get(cat_id)
    if not cat:
        kb.add(types.InlineKeyboardButton("⏪ Quay lại", callback_data="BACK_MAIN"))
        return kb

    for it in cat.get("items", []):
        label = f"{it['name']} | {it['price']}"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ITEM|{it['item_id']}"))

    kb.add(types.InlineKeyboardButton("💳 Thanh toán", callback_data="PAY"))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại menu", callback_data="BACK_MAIN"))
    return kb


def kb_item(item_id: str, buy_url: str = ""):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔢 SỐ LƯỢNG MUA", callback_data=f"QTYMENU|{item_id}"))
    kb.add(types.InlineKeyboardButton("💳 Thanh toán", callback_data="PAY"))
    kb.add(types.InlineKeyboardButton("📩 Nhắn Admin", url=admin_url()))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại danh mục", callback_data=f"BACKCAT|{item_id}"))
    return kb


def kb_payment():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📩 Gửi bill cho Admin", url=admin_url()))
    kb.add(types.InlineKeyboardButton("⏪ Quay lại menu", callback_data="BACK_MAIN"))
    return kb


# =========================
# Text
# =========================
def text_start():
    return (
        "👋 **Chào mừng bạn đến với Store của VuSmile**\n\n"
        "✅ Bảng giá rõ ràng – hỗ trợ nhanh – xử lý gọn\n"
        "👉 Chọn danh mục bên dưới 👇"
    )


def text_payment():
    return (
        f"💳 **THÔNG TIN THANH TOÁN – {SHOP_NAME}**\n\n"
        f"🏦 **Ngân hàng:** OCB ({BANK_NAME})\n"
        f"👤 **Chủ TK:** {ACCOUNT_NAME}\n"
        f"🔢 **STK:** {ACCOUNT_NO}\n\n"
        "✅ **NỘI DUNG CHUYỂN KHOẢN (BẮT BUỘC):**\n"
        "`tra tien hoa`\n\n"
        "📌 Chuyển xong, chụp bill gửi admin để xác nhận nhanh."
    )


def category_message(cat_id: str):
    cat = CAT_BY_ID.get(cat_id)
    if not cat:
        return "❌ Danh mục không tồn tại."
    return f"**{cat['title']}**\n\n{cat['desc']}"


def item_message(item_id: str):
    found = ITEM_BY_ID.get(item_id)
    if not found:
        return "❌ Sản phẩm không tồn tại."
    _, it = found
    return f"✅ **{it['name']}**\n💰 **Giá:** **{it['price']}**\n\n{it['detail']}"


def build_buy_text(from_user, group: str, product: str, price: str, require_hint: str):
    u = user_tag(from_user)
    return f"MUA | {group} | {product} | SL: 1 | {price} | Yêu cầu: {require_hint} | User: {u}"


# =========================
# Commands
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    print(
        f"[START] received user_id={message.from_user.id} "
        f"chat_id={message.chat.id}"
    )

    # Send the customer menu first. Logging/admin notification runs separately
    # so Google Sheets does not delay the /start response.
    send_with_optional_photo(
        message.chat.id,
        "START",
        text_start(),
        reply_markup=kb_main(),
    )
    print(f"[START] menu sent chat_id={message.chat.id}")

    threading.Thread(
        target=notify_admin_start,
        args=(message,),
        name=f"start-notify-{message.from_user.id}",
        daemon=True,
    ).start()


@bot.message_handler(commands=["getid"])
def cmd_getid(message):
    bot.send_message(
        message.chat.id,
        "📌 **/getid**: Gửi **1 ảnh** vào đây, bot sẽ trả `file_id`.\n\n"
        "Admin gắn ảnh theo KEY bằng:\n"
        "`/setimg KEY`\n"
        "Xem KEY: `/listkeys`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["listkeys"])
def cmd_listkeys(message):
    keys = ["START", "PAYMENT"]
    for c in CATALOG:
        keys.append(f"CAT_{c['cat_id']}")
        for it in c.get("items", []):
            keys.append(f"ITEM_{it['item_id']}")
    text = "🗂️ **Danh sách KEY ảnh có thể gắn:**\n\n" + "\n".join([f"- `{k}`" for k in keys])
    safe_send_markdown(message.chat.id, text)


admin_waiting_img_key = {}  # chat_id -> key


@bot.message_handler(commands=["setimg"])
def cmd_setimg(message):
    if not is_admin(message.from_user):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành cho admin.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "✅ Dùng: `/setimg KEY`\nXem KEY: `/listkeys`", parse_mode="Markdown")
        return

    key = parts[1].strip().upper()
    admin_waiting_img_key[message.chat.id] = key
    bot.reply_to(message, f"📷 OK. Giờ hãy gửi ảnh để gắn vào KEY: {key}.")



def _is_waiting_custom_quantity(message) -> bool:
    text = str(message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    return get_pending_custom_quantity(message.chat.id, message.from_user.id) is not None


@bot.message_handler(func=_is_waiting_custom_quantity, content_types=["text"])
def handle_custom_quantity(message):
    state = get_pending_custom_quantity(message.chat.id, message.from_user.id)
    if not state:
        return

    raw = str(message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        bot.reply_to(
            message,
            f"⚠️ Vui lòng nhập một số nguyên từ 6 đến {MAX_ORDER_QUANTITY}.",
        )
        return

    if quantity <= 5:
        bot.reply_to(
            message,
            "⚠️ Với số lượng từ 1 đến 5, vui lòng dùng các nút chọn số lượng.",
            reply_markup=quantity_menu_markup(state["item_id"]),
        )
        return

    if quantity > MAX_ORDER_QUANTITY:
        bot.reply_to(
            message,
            f"⚠️ Số lượng tối đa trên bot là {MAX_ORDER_QUANTITY}. "
            "Vui lòng nhập lại hoặc liên hệ Admin.",
        )
        return

    item_id = state["item_id"]
    clear_pending_custom_quantity(message.chat.id, message.from_user.id)
    bot.send_message(
        message.chat.id,
        quantity_summary_text(item_id, quantity),
        parse_mode="Markdown",
        reply_markup=buy_confirmation_markup(item_id, quantity),
    )


@bot.message_handler(content_types=["photo"])
def on_photo(message):
    file_id = message.photo[-1].file_id

    bot.reply_to(message, f"✅ file_id:\n`{file_id}`", parse_mode="Markdown")

    key = admin_waiting_img_key.get(message.chat.id)
    if key and is_admin(message.from_user):
        set_image(key, file_id)
        admin_waiting_img_key.pop(message.chat.id, None)
        bot.reply_to(message, f"✅ Đã gắn ảnh cho {key}.")


# =========================
# Callbacks
# =========================
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    try:
        data = call.data
        chat_id = call.message.chat.id

        # Không để callback hết hạn làm dừng chức năng mua hàng
        try:
            bot.answer_callback_query(call.id)
        except Exception as ack_error:
            print(
                f"[CALLBACK ACK] ignored "
                f"id={call.id} data={data}: {ack_error}"
            )

        if data == "BACK_MAIN":
            send_with_optional_photo(
                chat_id,
                "START",
                text_start(),
                reply_markup=kb_main(),
            )
            return

        if data == "OTHER":
            bot.send_message(
                chat_id,
                "📦 **MỤC KHÁC**\n\n👉 Chọn danh mục bên dưới 👇",
                parse_mode="Markdown",
                reply_markup=kb_other(),
            )
            return

        if data == "PAY":
            send_with_optional_photo(chat_id, "PAYMENT", text_payment(), reply_markup=kb_payment())
            return

        if data.startswith("CAT|"):
            cat_id = data.split("|", 1)[1]
            text = category_message(cat_id)
            img_key = f"CAT_{cat_id}"
            send_with_optional_photo(chat_id, img_key, text, reply_markup=kb_category(cat_id))
            return

        if data.startswith("ITEM|"):
            item_id = data.split("|", 1)[1]
            found = ITEM_BY_ID.get(item_id)
            if not found:
                bot.send_message(chat_id, "❌ Sản phẩm không tồn tại.")
                return
            _, it = found

            text = item_message(item_id)

            img_key = f"ITEM_{item_id}"
            send_with_optional_photo(chat_id, img_key, text, reply_markup=kb_item(item_id))
            return

        if data.startswith("QTYMENU|"):
            item_id = data.split("|", 1)[1]
            if item_id not in ITEM_BY_ID:
                bot.send_message(chat_id, "❌ Sản phẩm không tồn tại.")
                return
            clear_pending_custom_quantity(chat_id, call.from_user.id)
            bot.send_message(
                chat_id,
                "🔢 **CHỌN SỐ LƯỢNG MUA**\n\n"
                "Chọn từ 1 đến 5, hoặc chọn **Khác** để nhập số lượng lớn hơn 5.",
                parse_mode="Markdown",
                reply_markup=quantity_menu_markup(item_id),
            )
            return

        if data.startswith("QTYOTHER|"):
            item_id = data.split("|", 1)[1]
            if item_id not in ITEM_BY_ID:
                bot.send_message(chat_id, "❌ Sản phẩm không tồn tại.")
                return
            set_pending_custom_quantity(chat_id, call.from_user.id, item_id)
            bot.send_message(
                chat_id,
                f"⌨️ Hãy nhập số lượng muốn mua từ 6 đến {MAX_ORDER_QUANTITY}.",
                reply_markup=types.ForceReply(selective=True),
            )
            return

        if data.startswith("QTY|"):
            parts = data.split("|")
            if len(parts) != 3:
                bot.send_message(chat_id, "❌ Dữ liệu số lượng không hợp lệ.")
                return
            item_id = parts[1]
            quantity = int(parts[2])
            if item_id not in ITEM_BY_ID or quantity not in (1, 2, 3, 4, 5):
                bot.send_message(chat_id, "❌ Số lượng không hợp lệ.")
                return
            clear_pending_custom_quantity(chat_id, call.from_user.id)
            bot.send_message(
                chat_id,
                quantity_summary_text(item_id, quantity),
                parse_mode="Markdown",
                reply_markup=buy_confirmation_markup(item_id, quantity),
            )
            return

        if data.startswith("BUYQTY|"):
            parts = data.split("|")
            if len(parts) != 3:
                bot.send_message(chat_id, "❌ Dữ liệu đơn hàng không hợp lệ.")
                return
            item_id = parts[1]
            quantity = int(parts[2])
            status_message = bot.send_message(
                chat_id,
                f"⏳ Bot đang kiểm tra kho cho số lượng {quantity}. Vui lòng đợi...",
            )
            create_payment_for_item(
                call,
                item_id,
                quantity=quantity,
                status_message_id=status_message.message_id,
            )
            return

        # Backward compatibility for old messages that still have BUY|item_id.
        if data.startswith("BUY|"):
            item_id = data.split("|", 1)[1]
            status_message = bot.send_message(
                chat_id,
                "⏳ Bot đang kiểm tra kho cho số lượng 1. Vui lòng đợi...",
            )
            create_payment_for_item(
                call,
                item_id,
                quantity=1,
                status_message_id=status_message.message_id,
            )
            return

        if data.startswith("BACKCAT|"):
            item_id = data.split("|", 1)[1]
            found = ITEM_BY_ID.get(item_id)
            if not found:
                send_with_optional_photo(chat_id, "START", text_start(), reply_markup=kb_main())
                return
            cat_id, _ = found
            text = category_message(cat_id)
            img_key = f"CAT_{cat_id}"
            send_with_optional_photo(chat_id, img_key, text, reply_markup=kb_category(cat_id))
            return

        bot.send_message(chat_id, "❓ Không hiểu thao tác. Gõ /start để bắt đầu lại.")

    except Exception as e:
        print(f"[CALLBACK ERROR] data={getattr(call, 'data', '')}: {e}")
        try:
            bot.send_message(call.message.chat.id, f"⚠️ Có lỗi nhỏ xảy ra.\nChi tiết: {e}")
        except Exception:
            pass


# =========================
# Flask endpoints
# =========================
@server.get("/")
def home():
    return "OK", 200


@server.get("/health")
def health():
    return "OK", 200


@server.get("/version")
def version():
    return jsonify({"version": APP_VERSION, "status": "running"}), 200


@server.before_request
def log_ping():
    if request.path in ("/", "/health"):
        print(
            f"[PING] {datetime.utcnow().isoformat()} "
            f"from={request.headers.get('X-Forwarded-For','')} "
            f"ua={request.headers.get('User-Agent','')}"
        )


@server.get("/payment/success")
def payment_success():
    return "Thanh toán đã được ghi nhận. Bạn có thể quay lại Telegram để nhận tài nguyên.", 200


@server.get("/payment/cancel")
def payment_cancel():
    return "Đơn thanh toán đã bị huỷ. Bạn có thể quay lại Telegram.", 200


@server.post("/payment/webhook")
def payment_webhook():
    if not payos_client:
        return jsonify({"message": "payOS not configured"}), 503
    try:
        webhook_data = payos_client.webhooks.verify(request.get_data())
        order_code = int(webhook_data.order_code)
        amount = int(webhook_data.amount)
        reference = str(webhook_data.reference or "")
        if not find_order(order_code):
            # payOS sends a signed sample transaction while confirming webhook URL.
            print(f"[PAYMENT_WEBHOOK] verified unknown/sample order: {order_code}")
            return jsonify({"message": "Webhook verified"}), 200
        deliver_paid_order(order_code, amount, reference)
        return jsonify({"message": "OK"}), 200
    except Exception as exc:
        print(f"[PAYMENT_WEBHOOK] error: {exc}")
        return jsonify({"message": "Invalid or failed webhook"}), 400


def _remember_update(update_id: int) -> bool:
    """Return False when Telegram retries an update already accepted."""
    with _recent_update_lock:
        if update_id in _recent_update_ids:
            return False

        if len(_recent_update_order) == _recent_update_order.maxlen:
            oldest = _recent_update_order.popleft()
            _recent_update_ids.discard(oldest)

        _recent_update_order.append(update_id)
        _recent_update_ids.add(update_id)
        return True


@server.post("/webhook")
def telegram_webhook():
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict) or "update_id" not in payload:
            print("[WEBHOOK] invalid Telegram payload")
            return "OK", 200

        update_id = int(payload["update_id"])
        if not _remember_update(update_id):
            print(f"[WEBHOOK] duplicate update ignored: {update_id}")
            return "OK", 200

        update = types.Update.de_json(payload)
        update_kind = (
            "callback_query" if payload.get("callback_query") else
            "message" if payload.get("message") else
            "other"
        )
        print(
            f"[WEBHOOK] processing update_id={update_id} "
            f"kind={update_kind}"
        )

        # Process the update in this Gunicorn request thread. Gunicorn already
        # provides multiple gthread request threads, so a second internal
        # TeleBot thread pool is unnecessary and could leave updates queued.
        bot.process_new_updates([update])
        print(f"[WEBHOOK] processed update_id={update_id}")
        return "OK", 200

    except Exception as exc:
        print(f"[WEBHOOK] processing error: {exc}")
        # Always acknowledge Telegram to avoid an endless retry loop.
        return "OK", 200

