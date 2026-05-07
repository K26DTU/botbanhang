# SHOP X Telegram Bot (Stage 1)

Tính năng:
- Menu Session + bảng giá
- Tạo mã đơn DHxxxxx (lưu SQLite)
- Hiển thị thông tin thanh toán VCB + nội dung CK theo mã đơn
- Nút nhắn Admin: @min_max1834

## Chạy local
```bash
python -m venv .venv
source .venv/bin/activate  # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

export BOT_TOKEN="..."
python app.py