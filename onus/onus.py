# V4

import requests
import time
import json
from datetime import datetime, timezone

# ================= НАСТРОЙКИ =================
TICKER_URL = "https://api-pro.goonus.io/market/v2/ticker/24hr"
FUNDING_URL = "https://api-pro.goonus.io/market/v2/market?sf=1"
EXCHANGE_INFO_URL = "https://api-pro.goonus.io/market/v2/exchangeInfo"

RATE_FILE = "vdncusdt.json"
DB_FILE = "onus_futures_db.json"

MARKET_INTERVAL = 1
RATE_INTERVAL = 10
LIMITS_INTERVAL = 10
# ============================================


def update_vndc_rate():
    """Save per-futures-symbol USD conversion rates from exchangeInfo.

    Example:
    - BTCUSDT -> rate=1
    - AXSVNDC -> rate=24000
    """
    try:
        r = requests.get(EXCHANGE_INFO_URL, timeout=10)
        data = r.json()

        rates = {}
        for item in data:
            symbol = item.get("symbol")
            raw_rate = item.get("rate")
            if not symbol:
                continue

            try:
                rates[symbol] = float(raw_rate)
            except (TypeError, ValueError):
                # Fallback: for USDT pairs conversion rate is 1
                rates[symbol] = 1.0 if symbol.endswith("USDT") else None

        with open(RATE_FILE, "w") as f:
            json.dump({"updated_at": datetime.now(timezone.utc).isoformat(), "rates": rates}, f, indent=2)

        print(f"[RATE UPDATED] {len(rates)} symbol rates")

    except Exception as e:
        print("[RATE ERROR]", e)


def load_vndc_rate():
    """Load per-symbol conversion rates from local file."""
    try:
        with open(RATE_FILE, "r") as f:
            payload = json.load(f)
            rates = payload.get("rates", {})
            if isinstance(rates, dict):
                return rates
    except Exception:
        pass
    return {}


# ===== ЛИМИТЫ maxLoSize =====
def update_market_limits():
    try:
        r = requests.get(EXCHANGE_INFO_URL, timeout=10)
        data = r.json()

        limits = {}
        for item in data:
            symbol = item["symbol"]
            limits[symbol] = float(item["maxLoSize"])

        print(f"[LIMITS UPDATED] {len(limits)} symbols")
        return limits

    except Exception as e:
        print("[LIMITS ERROR]", e)
        return {}


# ===== РЫНОК =====
def get_tickers():
    return requests.get(TICKER_URL, timeout=10).json()


def get_funding():
    return requests.get(FUNDING_URL, timeout=10).json()


def convert_price(symbol, price, symbol_rates):
    price = float(price)
    if not symbol.endswith("VNDC"):
        return price

    rate = symbol_rates.get(symbol)
    if rate in (None, 0):
        # fallback if rate is temporarily missing
        return price
    return price / float(rate)


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)


# ================= MAIN =================
if __name__ == "__main__":

    last_rate_time = 0
    last_limits_time = 0

    market_limits = {}

    while True:
        try:
            now = time.time()

            # ===== курсы из exchangeInfo (10 сек) =====
            if now - last_rate_time >= RATE_INTERVAL:
                update_vndc_rate()
                last_rate_time = now

            # ===== maxLoSize (10 сек) =====
            if now - last_limits_time >= LIMITS_INTERVAL:
                market_limits = update_market_limits()
                last_limits_time = now

            symbol_rates = load_vndc_rate()

            # ===== рынок (1 сек) =====
            tickers = get_tickers()
            funding = get_funding()

            funding_dict = {f["symbol"]: f for f in funding}

            db = []
            timestamp = datetime.now(timezone.utc).isoformat()

            for t in tickers:
                symbol = t["symbol"]

                price_usdt = convert_price(symbol, t["lastPrice"], symbol_rates)

                fdata = funding_dict.get(symbol)
                funding_rate = float(fdata["fundingRate"]) if fdata else None
                funding_interval = fdata["fundingInterval"] if fdata else None

                max_lo_size = market_limits.get(symbol)

                db.append(
                    {
                        "symbol": symbol,
                        "price_usdt": round(price_usdt, 8),
                        "funding_rate": funding_rate,
                        "funding_interval": funding_interval,
                        "volume_usdt": float(t["volumeUsdt"]),
                        "maxLoSize": max_lo_size,
                        "timestamp": timestamp,
                    }
                )

            save_db(db)
            print(f"[MARKET UPDATED] {len(db)} symbols")

        except Exception as e:
            print("Ошибка:", e)

        time.sleep(MARKET_INTERVAL)
