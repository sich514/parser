#!/usr/bin/env python3
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"

DB_FILE = "bybit_futures_db.json"

MARKET_INTERVAL = 1
LIMITS_INTERVAL = 60
CATEGORY = "linear"


def fetch_json(url: str, params: dict | None = None, timeout: int = 10):
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    with urlopen(full_url, timeout=timeout) as response:
        return json.load(response)


def parse_ms_timestamp(ms_value):
    if not ms_value:
        return None
    try:
        return datetime.fromtimestamp(int(ms_value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def update_market_limits() -> dict[str, float | None]:
    """Load max order quantity for linear futures contracts."""
    limits: dict[str, float | None] = {}
    cursor = None

    try:
        while True:
            params = {"category": CATEGORY, "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            data = fetch_json(INSTRUMENTS_URL, params=params, timeout=15)
            result = data.get("result", {})

            for item in result.get("list", []):
                symbol = item.get("symbol")
                lot = item.get("lotSizeFilter", {})
                raw_max = lot.get("maxOrderQty") or lot.get("maxMktOrderQty")

                max_qty = None
                if raw_max is not None:
                    try:
                        max_qty = float(raw_max)
                    except (TypeError, ValueError):
                        max_qty = None

                if symbol:
                    limits[symbol] = max_qty

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

        print(f"[LIMITS UPDATED] {len(limits)} symbols")
        return limits

    except Exception as error:
        print("[LIMITS ERROR]", error)
        return {}


def get_tickers():
    data = fetch_json(TICKERS_URL, params={"category": CATEGORY}, timeout=15)
    return data.get("result", {}).get("list", [])


def save_db(rows):
    with open(DB_FILE, "w", encoding="utf-8") as file:
        json.dump(rows, file, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    last_limits_time = 0
    market_limits: dict[str, float | None] = {}

    while True:
        try:
            now = time.time()

            if now - last_limits_time >= LIMITS_INTERVAL:
                market_limits = update_market_limits()
                last_limits_time = now

            tickers = get_tickers()
            timestamp = datetime.now(timezone.utc).isoformat()

            rows = []
            for ticker in tickers:
                symbol = ticker.get("symbol")
                last_price = ticker.get("lastPrice")

                if not symbol or last_price in (None, ""):
                    continue

                try:
                    price_usdt = float(last_price)
                except (TypeError, ValueError):
                    continue

                raw_funding = ticker.get("fundingRate")
                funding_rate = None
                if raw_funding not in (None, ""):
                    try:
                        # Keep percentage format, similar to Binance parser output.
                        funding_rate = round(float(raw_funding) * 100, 4)
                    except (TypeError, ValueError):
                        funding_rate = None

                volume_quote = None
                raw_turnover = ticker.get("turnover24h")
                if raw_turnover not in (None, ""):
                    try:
                        volume_quote = float(raw_turnover)
                    except (TypeError, ValueError):
                        volume_quote = None

                rows.append(
                    {
                        "symbol": symbol,
                        "price_usdt": price_usdt,
                        "funding_rate": funding_rate,
                        "next_funding_time": parse_ms_timestamp(ticker.get("nextFundingTime")),
                        "volume_quote": volume_quote,
                        "maxQty": market_limits.get(symbol),
                        "timestamp": timestamp,
                    }
                )

            save_db(rows)
            print(f"[MARKET UPDATED] {len(rows)} futures symbols")

        except Exception as error:
            print("[MARKET ERROR]", error)

        time.sleep(MARKET_INTERVAL)
