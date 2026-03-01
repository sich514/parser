"""
import requests
import time

BASE_URL = "https://api-pro.goonus.io/market/v2"

def get_all_tickers():
    url = f"{BASE_URL}/ticker/24hr"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    while True:
        try:
            tickers = get_all_tickers()

            print("=" * 120)

            for t in tickers:
                print(
                    f"{t['symbol']:15} | "
                    f"Last: {t['lastPrice']:>12} | "
                    f"24h %: {t['priceChangePercent']:>8} | "
                    f"High: {t['highPrice']:>12} | "
                    f"Low: {t['lowPrice']:>12} | "
                    f"Vol USDT: {t['volumeUsdt']:>12} | "
                    f"Trades: {t['totalTrade']}"
                )

        except Exception as e:
            print("Ошибка:", e)

        time.sleep(1)
"""

#V2
"""
import requests
import time
import json
from datetime import datetime

# ================= НАСТРОЙКИ =================
FUTURES_URL = "https://api-pro.goonus.io/market/v2/market?sf=1"
TICKER_URL = "https://api-pro.goonus.io/market/v2/ticker/24hr"
SPOT_RATE_URL = "https://spot-markets.goonus.io/ticker-stats?names=USDT_VNDC"

RATE_FILE = "vdncusdt.json"
DB_FILE = "futures_db.json"

MARKET_INTERVAL = 1      # рынок — 1 сек
RATE_INTERVAL = 10       # курс — 10 сек
MARKET_LIMITS_INTERVAL = 10
# ============================================


def update_vndc_rate():
    """"Обновляем курс USDT -> VNDC""""
    try:
        r = requests.get(SPOT_RATE_URL, timeout=5)
        data = r.json()[0]
        best_bid = float(data["b"])

        with open(RATE_FILE, "w") as f:
            json.dump({"vdnc": best_bid}, f)

        print(f"[RATE UPDATED] 1 USDT = {best_bid} VNDC")

    except Exception as e:
        print("[RATE ERROR]", e)


def load_vndc_rate():
    with open(RATE_FILE, "r") as f:
        return float(json.load(f)["vdnc"])


def get_funding_market():
    return requests.get(FUTURES_URL, timeout=10).json()


def get_tickers():
    return requests.get(TICKER_URL, timeout=10).json()


def convert_price(symbol, price, vndc_rate):
    price = float(price)
    if symbol.endswith("VNDC"):
        return price / vndc_rate
    return price


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)


if __name__ == "__main__":
    last_rate_time = 0

    while True:
        try:
            now = time.time()

            # ====== обновляем курс каждые 10 секунд ======
            if now - last_rate_time >= RATE_INTERVAL:
                update_vndc_rate()
                last_rate_time = now

            vndc_rate = load_vndc_rate()

            # ====== рынок (1 сек) ======
            tickers = get_tickers()
            funding = get_funding_market()

            funding_dict = {f["symbol"]: f for f in funding}

            db = []
            timestamp = datetime.utcnow().isoformat()

            for t in tickers:
                symbol = t["symbol"]
                price_usdt = convert_price(symbol, t["lastPrice"], vndc_rate)

                fdata = funding_dict.get(symbol)
                funding_rate = float(fdata["fundingRate"]) if fdata else None
                funding_interval = fdata["fundingInterval"] if fdata else None

                db.append({
                    "symbol": symbol,
                    "price_usdt": round(price_usdt, 8),
                    "funding_rate": funding_rate,
                    "funding_interval": funding_interval,
                    "volume_usdt": float(t["volumeUsdt"]),
                    "timestamp": timestamp
                })

            save_db(db)
            print(f"[MARKET UPDATED] {len(db)} symbols")

        except Exception as e:
            print("Ошибка:", e)

        time.sleep(MARKET_INTERVAL)
        
"""

#V3

import requests
import time
import json
from datetime import datetime, timezone

# ================= НАСТРОЙКИ =================
TICKER_URL = "https://api-pro.goonus.io/market/v2/ticker/24hr"
FUNDING_URL = "https://api-pro.goonus.io/market/v2/market?sf=1"
EXCHANGE_INFO_URL = "https://api-pro.goonus.io/market/v2/exchangeInfo"
SPOT_RATE_URL = "https://spot-markets.goonus.io/ticker-stats?names=USDT_VNDC"

RATE_FILE = "vdncusdt.json"
DB_FILE = "futures_db.json"

MARKET_INTERVAL = 1
RATE_INTERVAL = 10
LIMITS_INTERVAL = 10
# ============================================


# ===== КУРС VNDC =====
def update_vndc_rate():
    try:
        r = requests.get(SPOT_RATE_URL, timeout=5)
        data = r.json()[0]
        best_bid = float(data["b"])

        with open(RATE_FILE, "w") as f:
            json.dump({"vdnc": best_bid}, f)

        print(f"[RATE UPDATED] 1 USDT = {best_bid} VNDC")

    except Exception as e:
        print("[RATE ERROR]", e)


def load_vndc_rate():
    try:
        with open(RATE_FILE, "r") as f:
            return float(json.load(f)["vdnc"])
    except:
        return 1


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


def convert_price(symbol, price, vndc_rate):
    price = float(price)
    if symbol.endswith("VNDC"):
        return price / vndc_rate
    return price


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

            # ===== курс VNDC (10 сек) =====
            if now - last_rate_time >= RATE_INTERVAL:
                update_vndc_rate()
                last_rate_time = now

            # ===== maxLoSize (10 сек) =====
            if now - last_limits_time >= LIMITS_INTERVAL:
                market_limits = update_market_limits()
                last_limits_time = now

            vndc_rate = load_vndc_rate()

            # ===== рынок (1 сек) =====
            tickers = get_tickers()
            funding = get_funding()

            funding_dict = {f["symbol"]: f for f in funding}

            db = []
            timestamp = datetime.now(timezone.utc).isoformat()

            for t in tickers:
                symbol = t["symbol"]

                price_usdt = convert_price(symbol, t["lastPrice"], vndc_rate)

                fdata = funding_dict.get(symbol)
                funding_rate = float(fdata["fundingRate"]) if fdata else None
                funding_interval = fdata["fundingInterval"] if fdata else None

                max_lo_size = market_limits.get(symbol)

                db.append({
                    "symbol": symbol,
                    "price_usdt": round(price_usdt, 8),
                    "funding_rate": funding_rate,
                    "funding_interval": funding_interval,
                    "volume_usdt": float(t["volumeUsdt"]),
                    "maxLoSize": max_lo_size,
                    "timestamp": timestamp
                })

            save_db(db)
            print(f"[MARKET UPDATED] {len(db)} symbols")

        except Exception as e:
            print("Ошибка:", e)

        time.sleep(MARKET_INTERVAL)