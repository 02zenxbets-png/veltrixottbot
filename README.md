# OTT Bot

## Run locally

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env`.

3. Fill ALL configuration values in `.env`.

4. Start:
```bash
python main.py
```

## Render / Railway / other background hosting

This bot uses Telegram polling and must NOT wait for terminal input.

Add these Environment Variables in your hosting dashboard:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `UPI_ID`
- `UPI_NAME`
- `BINANCE_PAY_ID` (optional)
- `USD_TO_INR`
- `MAX_BULK_QTY`
- `DATA_DIR`

Example values are provided in `.env.example`.

**Do not commit `.env` or expose your bot token.**
