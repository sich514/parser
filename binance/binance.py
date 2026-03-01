import json
import time
from datetime import datetime, timezone
from urllib.request import urlopen

# ================= SETTINGS =================
FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

DB_FILE = "binance_futures_db.json"

MARKET_INTERVAL = 1
LIMITS_INTERVAL = 60
# ============================================


def fetch_json(url, timeout=10):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def update_market_limits():
    """Loads futures symbol limits from exchangeInfo."""
    try:
        data = fetch_json(EXCHANGE_INFO_URL, timeout=10)

        limits = {}
        for item in data.get("symbols", []):
            symbol = item.get("symbol")
            max_qty = None

            for filt in item.get("filters", []):
                if filt.get("filterType") == "LOT_SIZE":
                    max_qty = float(filt.get("maxQty"))
                    break

            limits[symbol] = max_qty

        print(f"[LIMITS UPDATED] {len(limits)} symbols")
        return limits

    except Exception as error:
        print("[LIMITS ERROR]", error)
        return {}


def get_tickers():
    return fetch_json(FUTURES_TICKER_URL, timeout=10)


def get_funding():
    return fetch_json(FUNDING_URL, timeout=10)


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    last_limits_time = 0
    market_limits = {}

    while True:
        try:
            now = time.time()

            if now - last_limits_time >= LIMITS_INTERVAL:
                market_limits = update_market_limits()
                last_limits_time = now

            tickers = get_tickers()
            funding = get_funding()

            funding_dict = {item["symbol"]: item for item in funding if "symbol" in item}

            db = []
            timestamp = datetime.now(timezone.utc).isoformat()

            for ticker in tickers:
                symbol = ticker["symbol"]
                funding_item = funding_dict.get(symbol, {})

                next_funding_time = funding_item.get("nextFundingTime")
                if next_funding_time:
                    next_funding_time = datetime.fromtimestamp(
                        int(next_funding_time) / 1000, tz=timezone.utc
                    ).isoformat()

                raw_funding_rate = funding_item.get("lastFundingRate")
                funding_rate = (
                    round(float(raw_funding_rate) * 100, 4)
                    if raw_funding_rate is not None
                    else None
                )

                db.append(
                    {
                        "symbol": symbol,
                        "price_usdt": float(ticker["lastPrice"]),
                        "funding_rate": funding_rate,
                        "mark_price": float(funding_item["markPrice"])
                        if funding_item.get("markPrice") is not None
                        else None,
                        "next_funding_time": next_funding_time,
                        "volume_base": float(ticker["volume"]),
                        "volume_quote": float(ticker["quoteVolume"]),
                        "count": int(ticker["count"]),
                        "maxQty": market_limits.get(symbol),
                        "timestamp": timestamp,
                    }
                )

            save_db(db)
            print(f"[MARKET UPDATED] {len(db)} futures symbols")

        except Exception as error:
            print("[MARKET ERROR]", error)

        time.sleep(MARKET_INTERVAL)
