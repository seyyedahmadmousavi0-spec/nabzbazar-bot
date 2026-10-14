import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from marketintel.senders import TelegramSender
from marketintel.publisher import fmt_number, fmt_time
from marketintel.jalali import to_jalali_date
from marketintel.textnorm import to_persian_digits

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

TOKEN = "8655204622:AAFBscWNgQ___h4iPnc4-byv9bwk4ly2xEE"
CHANNEL = "@nabzbazar360"

def get_latest_market_data():
    conn = sqlite3.connect("data/marketintel.sqlite3")
    conn.row_factory = sqlite3.Row
    
    # Get the latest price for each asset_key
    query = """
    SELECT r.*, s.name as source_name, s.attribution
    FROM records r
    JOIN sources s ON s.id = r.source_id
    WHERE r.amount_canonical IS NOT NULL AND r.amount > 0
    ORDER BY r.id DESC
    """
    rows = conn.execute(query).fetchall()
    
    seen = set()
    latest_items = []
    for r in rows:
        key = (r["asset_key"], r["side"])
        if key not in seen:
            seen.add(key)
            latest_items.append(r)
            
    return latest_items

def build_market_bulletin(items):
    now = datetime.now(timezone.utc)
    # Tehran time is UTC + 3:30
    tehran_time = now + timedelta(hours=3, minutes=30)
    jy, jm, jd = to_jalali_date(tehran_time)
    
    lines = [
        "📊 <b>نبض بازار</b>",
        f"📅 تاریخ: {to_persian_digits(f'{jy}/{jm:02d}/{jd:02d}')} — ساعت: {to_persian_digits(tehran_time.strftime('%H:%M'))}",
        "────────────────────"
    ]
    
    # Group by category or asset
    currencies = []
    gold_coins = []
    cryptos = []
    
    for it in items:
        cat = it["category"]
        name = it["asset_name"] or it["asset_key"]
        side_fa = " (خرید)" if it["side"] == "buy" else (" (فروش)" if it["side"] == "sell" else "")
        unit = it["stated_unit"] or "تومان"
        unit_str = "تومان" if unit == "IRT" else ("ریال" if unit == "IRR" else unit)
        amt_str = fmt_number(it['amount'], sep=",")
        
        entry = f"• <b>{name}{side_fa}</b>: {amt_str} {unit_str}"
        
        if cat == "currency":
            currencies.append(entry)
        elif cat in ("gold", "coin"):
            gold_coins.append(entry)
        elif cat == "crypto":
            cryptos.append(entry)
            
    if currencies:
        lines.append("💵 <b>ارز و اسکناس:</b>")
        lines.extend(currencies[:6])
        lines.append("")
        
    if gold_coins:
        lines.append("🪙 <b>طلا و مسکوکات:</b>")
        lines.extend(gold_coins[:4])
        lines.append("")
        
    if cryptos:
        lines.append("🌐 <b>رمزارزها:</b>")
        lines.extend(cryptos[:5])
        lines.append("")
        
    lines.append("────────────────────")
    lines.append("📈 @nabzbazar360 | نبض بازار ۳۶۰")
    lines.append("⚡ سامانه پایش و انتشار لحظه‌ای نرخ‌ها")
    
    return "\n".join(lines)

if __name__ == "__main__":
    items = get_latest_market_data()
    print(f"Loaded {len(items)} unique asset quotes from sources.")
    bulletin = build_market_bulletin(items)
    print("\n--- BULLETIN PREVIEW ---")
    print(bulletin)
    
    sender = TelegramSender(TOKEN)
    msg_id1 = sender.send(CHANNEL, bulletin, [])
    print(f"SUCCESS! Market bulletin sent with ID: {msg_id1}")
    
    # Also send a sample single currency card with clean format
    conn = sqlite3.connect("data/marketintel.sqlite3")
    conn.row_factory = sqlite3.Row
    usd_rec = conn.execute("SELECT * FROM records WHERE asset_key='usd' AND amount_canonical IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if usd_rec:
        from marketintel.publisher import render_record
        single_text, buttons = render_record(conn, usd_rec, "Asia/Tehran", None, 1, "📊 <b>اعلام قیمت بازار — دلار</b>")
        msg_id2 = sender.send(CHANNEL, single_text, buttons)
        print(f"SUCCESS! Single post sent with ID: {msg_id2}")
