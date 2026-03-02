#!/usr/bin/env python3
import json
import time
from datetime import datetime, timezone
from urllib.request import urlopen

TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
CONTRACTS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"

DB_FILE = "gate_futures_db.json"

MARKET_INTERVAL = 1
LIMITS_INTERVAL = 60


def fetch_json(url: str, timeout: int = 15):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def update_market_limits() -> dict[str, float | None]:
    limits: dict[str, float | None] = {}
    try:
        contracts = fetch_json(CONTRACTS_URL, timeout=20)
        for item in contracts:
            symbol = item.get("name")
            if not symbol:
                continue

            normalized_symbol = symbol.replace("_", "")
            max_qty = safe_float(item.get("order_size_max"))
            limits[normalized_symbol] = max_qty

        print(f"[LIMITS UPDATED] {len(limits)} symbols")
        return limits
    except Exception as error:
        print("[LIMITS ERROR]", error)
        return {}


def get_tickers():
    return fetch_json(TICKERS_URL, timeout=20)


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
                # Gate uses symbols like BTC_USDT. Convert to BTCUSDT for consistency.
                raw_contract = ticker.get("contract")
                if not raw_contract:
                    continue
                symbol = raw_contract.replace("_", "")

                price = safe_float(ticker.get("last"))
                if price is None or price <= 0:
                    continue

                funding_rate = safe_float(ticker.get("funding_rate"))
                if funding_rate is not None:
                    # keep percentage format similar to Binance parser output
                    funding_rate = round(funding_rate * 100, 4)

                rows.append(
                    {
                        "symbol": symbol,
                        "price_usdt": price,
                        "funding_rate": funding_rate,
                        "mark_price": safe_float(ticker.get("mark_price")),
                        "index_price": safe_float(ticker.get("index_price")),
                        "next_funding_time": None,
                        "volume_base": safe_float(ticker.get("volume_24h_base")),
                        "volume_quote": safe_float(ticker.get("volume_24h_quote"))
                        or safe_float(ticker.get("volume_24h_usdt"))
                        or safe_float(ticker.get("volume_24h")),
                        "maxQty": market_limits.get(symbol),
                        "timestamp": timestamp,
                    }
                )

            save_db(rows)
            print(f"[MARKET UPDATED] {len(rows)} futures symbols")

        except Exception as error:
            print("[MARKET ERROR]", error)

        time.sleep(MARKET_INTERVAL)
