from pathlib import Path
p=Path("bot.py")
s=p.read_text()
marker="

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:"
code="""

async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        html = await _http_get("https://bloxfruitswiki.org/wiki/stock", timeout=15)
        start = html.find("<h2>Current Stock</h2>")
        if start == -1:
            await update.effective_message.reply_text("❌ Current Stock topilmadi.")
            return
        end = html.find("<h2>", start + 5)
        block = html[start:] if end == -1 else html[start:end]
        pattern = re.compile(r'<a[^>]+title=\"([^\"]+)\"[^>]*>.*?</a>.*?<a[^>]*>([^<]+)</a>.*?<span[^>]*>([^<]+)</span>.*?__money\.webp.*?</span>\s*([\d,]+).*?__robux\.webp.*?</span>\s*([\d,]+)', re.S | re.I)
        entries = []
        for m in pattern.finditer(block):
            name = unescape(m.group(1)).strip()
            display = unescape(m.group(2)).strip()
            if name.lower() == display.lower():
                entries.append((display, unescape(m.group(3)).strip(), m.group(4), m.group(5)))
        if not entries:
            await update.effective_message.reply_text("❌ Hozirgi stockni o‘qib bo‘lmadi.")
            return
        reset = re.search(r'\((\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\)', block)
        lines = ["🛒 Blox Fruits — Current Stock", f"🔄 Reset: {reset.group(1) if reset else 'Noma’lum'}", ""]
        for name, rarity, money, robux in entries:
            lines.append(f"🍎 {name}\n   {rarity}\n   💰 {money} Beli | 💎 {robux} Robux")
        await update.effective_message.reply_text("\n".join(lines)[:4000])
    except Exception as e:
        logging.exception("Stock error")
        await update.effective_message.reply_text(f"❌ Stockni olishda xatolik: {type(e).__name__}")
"""
assert marker in s
p.write_text(s.replace(marker, code+marker, 1))
