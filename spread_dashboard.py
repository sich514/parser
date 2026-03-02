#!/usr/bin/env python3
"""Lightweight web dashboard for futures spread monitoring across exchanges.

Base pair: ONUS vs every other discovered exchange.
Designed to be extensible: every folder with *_futures_db.json is treated as an exchange source.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DB_PATTERN = "*_futures_db.json"
KNOWN_QUOTES = ("USDT", "VNDC", "BUSD", "USDC", "USD")
# Real multiplier-style contracts are typically 10x/100x/1000x... and not
# ticker suffixes like C98.
MIN_MULTIPLIER = 10

# =====================================================
# BLACKLIST CONFIG (edit this section)
# - coins: base assets excluded for a specific exchange (after normalization)
# - symbols: exact symbol names excluded for a specific exchange
#
# Example:
# BLACKLIST = {
#     "bybit": {"coins": {"SCR"}, "symbols": {"FOOUSDT"}},
#     "gate": {"coins": {"HMSTR"}},
# }
# =====================================================

BLACKLIST: dict[str, dict[str, set[str]]] = {}

@dataclass(frozen=True)
class CanonicalSymbol:
    base: str
    quote: str | None
    multiplier: int


def discover_exchange_dbs() -> dict[str, Path]:
    """Find exchange DB files under project root.

    Recursive search allows future layouts like:
    - exchange/exchange_futures_db.json
    - exchange/data/exchange_futures_db.json

    The exchange key is always taken from the first folder under ROOT.
    """
    found: dict[str, Path] = {}

    for db_path in ROOT.rglob(DB_PATTERN):
        if not db_path.is_file():
            continue

        try:
            rel = db_path.relative_to(ROOT)
        except ValueError:
            continue

        if not rel.parts:
            continue

        exchange = rel.parts[0].lower()

        # If multiple DB files are found for one exchange, prefer the one
        # closest to the exchange root (shorter relative path).
        current = found.get(exchange)
        if current is None:
            found[exchange] = db_path
            continue

        cur_rel_len = len(current.relative_to(ROOT).parts)
        new_rel_len = len(rel.parts)
        if new_rel_len < cur_rel_len:
            found[exchange] = db_path

    return found


def parse_symbol(symbol: str) -> CanonicalSymbol:
    """Normalize symbol name across exchanges.

    Supports forms like:
    - BTCUSDT
    - 1000PEPEUSDT
    - PEPE1000VNDC
    """
    clean = re.sub(r"[^A-Za-z0-9]", "", symbol.upper())

    quote = None
    body = clean
    for candidate in sorted(KNOWN_QUOTES, key=len, reverse=True):
        if body.endswith(candidate):
            quote = candidate
            body = body[: -len(candidate)]
            break

    multiplier = 1

    def is_valid_multiplier(raw: str) -> bool:
        """Allow only realistic contract multipliers (10, 100, 1000, ...)."""
        try:
            value = int(raw)
        except ValueError:
            return False
        if value < MIN_MULTIPLIER:
            return False
        # treat multiplier as strict power-of-10 style: 10, 100, 1000...
        # avoids false positives like C98 or 1010-style suffixes
        return re.fullmatch(r"10*", raw) is not None

    # Prefix multiplier: 1000PEPE
    prefix = re.match(r"^(\d+)([A-Z]+)$", body)
    if prefix and is_valid_multiplier(prefix.group(1)):
        multiplier = int(prefix.group(1))
        body = prefix.group(2)

    # Suffix multiplier: PEPE1000
    suffix = re.match(r"^([A-Z]+)(\d+)$", body)
    if suffix and is_valid_multiplier(suffix.group(2)):
        body = suffix.group(1)
        multiplier = int(suffix.group(2))

    return CanonicalSymbol(base=body, quote=quote, multiplier=multiplier)


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    return data


def safe_float(value: Any) -> float | None:
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def is_blacklisted(exchange_name: str | None, base: str, symbol: str) -> bool:
    if not exchange_name:
        return False

    config = BLACKLIST.get(exchange_name.lower())
    if not config:
        return False

    blocked_coins = {str(x).upper() for x in config.get("coins", set()) if str(x).strip()}
    # Normalize symbols to ignore separators, so SOLV_USDT and SOLVUSDT are equivalent.
    blocked_symbols = {
        re.sub(r"[^A-Z0-9]", "", str(x).upper())
        for x in config.get("symbols", set())
        if str(x).strip()
    }
    normalized_symbol = re.sub(r"[^A-Z0-9]", "", symbol.upper())

    return base.upper() in blocked_coins or normalized_symbol in blocked_symbols


def build_exchange_index(rows: list[dict[str, Any]], exchange_name: str | None = None) -> dict[str, dict[str, Any]]:
    """Map canonical base asset -> best row.

    If duplicate bases exist, keep one with bigger notional volume when possible.
    """
    index: dict[str, dict[str, Any]] = {}

    def row_score(item: dict[str, Any]) -> float:
        for k in ("volume_quote", "volume_usdt", "quoteVolume"):
            if (v := safe_float(item.get(k))) is not None:
                return v
        return 0.0

    for row in rows:
        symbol = row.get("symbol")
        price = safe_float(row.get("price_usdt"))
        if not symbol or price is None or price <= 0:
            continue

        parsed = parse_symbol(str(symbol))

        if is_blacklisted(exchange_name, parsed.base, str(symbol)):
            continue

        # Per-coin normalization for multiplied contracts (1000SHIB etc.)
        unit_price = price / parsed.multiplier if parsed.multiplier > 0 else price

        normalized_row = dict(row)
        normalized_row["symbol"] = symbol
        normalized_row["_base"] = parsed.base
        normalized_row["_multiplier"] = parsed.multiplier
        normalized_row["_unit_price"] = unit_price

        current = index.get(parsed.base)
        if current is None or row_score(normalized_row) > row_score(current):
            index[parsed.base] = normalized_row

    return index


def iso_age_seconds(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def build_spreads(
    left_name: str,
    right_name: str,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    limit: int = 200,
) -> list[dict[str, Any]]:
    left_idx = build_exchange_index(left_rows, left_name)
    right_idx = build_exchange_index(right_rows, right_name)

    common = sorted(set(left_idx).intersection(right_idx))
    spreads: list[dict[str, Any]] = []

    for base in common:
        l = left_idx[base]
        r = right_idx[base]
        lp = safe_float(l.get("_unit_price"))
        rp = safe_float(r.get("_unit_price"))
        if lp is None or rp is None or lp <= 0 or rp <= 0:
            continue

        # Direction is always: LONG cheaper exchange, SHORT more expensive exchange.
        if lp <= rp:
            long_name = left_name
            short_name = right_name
            long_price = lp
            short_price = rp
        else:
            long_name = right_name
            short_name = left_name
            long_price = rp
            short_price = lp

        spread_pct = (short_price - long_price) / long_price * 100.0
        abs_spread = abs(spread_pct)
        direction = f"LONG {long_name.upper()} / SHORT {short_name.upper()}"

        funding_l = safe_float(l.get("funding_rate"))
        funding_r = safe_float(r.get("funding_rate"))
        funding_diff = (
            (funding_r - funding_l)
            if funding_l is not None and funding_r is not None
            else None
        )

        spreads.append(
            {
                "coin": base,
                "left_symbol": l.get("symbol"),
                "right_symbol": r.get("symbol"),
                "left_price": round(lp, 10),
                "right_price": round(rp, 10),
                "spread_pct": round(spread_pct, 4),
                "abs_spread_pct": round(abs_spread, 4),
                "long_short": direction,
                "funding_left": funding_l,
                "funding_right": funding_r,
                "funding_diff": round(funding_diff, 6) if funding_diff is not None else None,
                "left_age_sec": iso_age_seconds(l.get("timestamp")),
                "right_age_sec": iso_age_seconds(r.get("timestamp")),
            }
        )

    spreads.sort(key=lambda x: x["abs_spread_pct"], reverse=True)
    return spreads[:limit]

def build_spreads_vs_base(
    base_exchange: str,
    exchange_rows: dict[str, list[dict[str, Any]]],
    limit: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build spreads for every exchange against one base exchange."""
    if base_exchange not in exchange_rows:
        return [], {}

    base_rows = exchange_rows[base_exchange]
    summary: dict[str, int] = {}
    merged: list[dict[str, Any]] = []

    for exchange, rows in exchange_rows.items():
        if exchange == base_exchange:
            continue

        pair_rows = build_spreads(base_exchange, exchange, base_rows, rows, limit=max(limit, 2000))
        summary[exchange] = len(pair_rows)

        for row in pair_rows:
            merged_row = dict(row)
            merged_row["base_exchange"] = base_exchange
            merged_row["compare_exchange"] = exchange
            merged.append(merged_row)

    merged.sort(key=lambda x: x["abs_spread_pct"], reverse=True)
    return merged[:limit], summary






def find_row_by_symbol(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    target = symbol.upper()
    for row in rows:
        raw = str(row.get("symbol", "")).upper()
        if raw != target:
            continue
        price = safe_float(row.get("price_usdt"))
        if price is None or price <= 0:
            continue
        parsed = parse_symbol(raw)
        unit_price = price / parsed.multiplier if parsed.multiplier > 0 else price

        normalized = dict(row)
        normalized["_base"] = parsed.base
        normalized["_multiplier"] = parsed.multiplier
        normalized["_unit_price"] = unit_price
        return normalized
    return None


def list_exchange_symbols(rows: list[dict[str, Any]], coin: str) -> list[str]:
    clean_coin = re.sub(r"[^A-Za-z0-9]", "", coin.upper())
    out: list[str] = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue
        parsed = parse_symbol(symbol)
        if parsed.base == clean_coin and not is_blacklisted("onus", parsed.base, symbol):
            out.append(symbol)
    return sorted(set(out))

def compute_custom_spread(
    coin: str,
    long_exchange: str,
    short_exchange: str,
    exchange_rows: dict[str, list[dict[str, Any]]],
    onus_symbol: str | None = None,
) -> dict[str, Any]:
    """Compute spread for one coin and selected long/short exchanges."""
    clean_coin = re.sub(r"[^A-Za-z0-9]", "", coin.upper())
    if not clean_coin:
        return {"error": "coin is required"}

    if long_exchange == short_exchange:
        return {"error": "long and short exchanges must differ"}

    if long_exchange not in exchange_rows or short_exchange not in exchange_rows:
        return {"error": "exchange not found"}

    long_idx = build_exchange_index(exchange_rows[long_exchange], long_exchange)
    short_idx = build_exchange_index(exchange_rows[short_exchange], short_exchange)

    if clean_coin not in long_idx:
        return {"error": f"coin {clean_coin} not found on {long_exchange}"}
    if clean_coin not in short_idx:
        return {"error": f"coin {clean_coin} not found on {short_exchange}"}

    l = long_idx[clean_coin]
    r = short_idx[clean_coin]

    # ONUS can have multiple symbols for same base (e.g. BTCUSDT and BTCVNDC)
    # allow explicit symbol override from UI.
    if onus_symbol:
        if long_exchange == "onus":
            picked = find_row_by_symbol(exchange_rows["onus"], onus_symbol)
            if picked is None:
                return {"error": f"onus symbol {onus_symbol} not found"}
            if picked.get("_base") != clean_coin:
                return {"error": f"onus symbol {onus_symbol} does not match coin {clean_coin}"}
            l = picked
        if short_exchange == "onus":
            picked = find_row_by_symbol(exchange_rows["onus"], onus_symbol)
            if picked is None:
                return {"error": f"onus symbol {onus_symbol} not found"}
            if picked.get("_base") != clean_coin:
                return {"error": f"onus symbol {onus_symbol} does not match coin {clean_coin}"}
            r = picked
    long_price = safe_float(l.get("_unit_price"))
    short_price = safe_float(r.get("_unit_price"))
    if long_price is None or short_price is None or long_price <= 0 or short_price <= 0:
        return {"error": "invalid price data"}

    # Positive means short side is priced higher than long side.
    spread_pct = (short_price - long_price) / long_price * 100.0

    funding_long = safe_float(l.get("funding_rate"))
    funding_short = safe_float(r.get("funding_rate"))
    funding_diff = (
        funding_short - funding_long
        if funding_long is not None and funding_short is not None
        else None
    )

    return {
        "coin": clean_coin,
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "long_symbol": l.get("symbol"),
        "short_symbol": r.get("symbol"),
        "long_price": round(long_price, 10),
        "short_price": round(short_price, 10),
        "spread_pct": round(spread_pct, 4),
        "funding_long": funding_long,
        "funding_short": funding_short,
        "funding_diff": round(funding_diff, 6) if funding_diff is not None else None,
        "long_age_sec": iso_age_seconds(l.get("timestamp")),
        "short_age_sec": iso_age_seconds(r.get("timestamp")),
    }

HTML_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Futures Spread Monitor</title>
  <style>
    :root { --bg:#0c1016; --card:#131923; --text:#e7edf7; --muted:#98a5bd; --green:#00d26a; --red:#ff5c73; --yellow:#ffd166; --line:#273247; --accent:#5b8cff; --gold:#f2c14e; }
    body { background:var(--bg); color:var(--text); font-family:Inter,Arial,sans-serif; margin:0; }
    .wrap { max-width:1240px; margin:24px auto; padding:0 16px; }
    h1 { margin:0 0 12px; font-size:24px; }
    .sub { color:var(--muted); margin-bottom:16px; font-size:14px; }
    .tabs { display:flex; gap:8px; margin-bottom:12px; }
    .tab { background:#0f141d; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:8px 14px; cursor:pointer; }
    .tab.active { background:var(--accent); border-color:var(--accent); font-weight:600; }
    .panel { display:none; }
    .panel.active { display:block; }
    .toolbar { display:grid; grid-template-columns:repeat(4, minmax(120px, 1fr)); gap:10px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; margin-bottom:16px; }
    .toolbar label { display:flex; flex-direction:column; gap:6px; font-size:12px; color:var(--muted); }
    input, select, button { background:#0f141d; color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:14px; }
    button { cursor:pointer; background:var(--accent); border-color:var(--accent); font-weight:600; }
    table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    th, td { padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; }
    th { color:#c4d0e3; position:sticky; top:0; background:#121a26; }
    .num { text-align:right; font-variant-numeric:tabular-nums; }
    .pos { color:var(--green); font-weight:600; }
    .neg { color:var(--red); font-weight:600; }
    .warn { color:var(--yellow); }
    .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
    .coin-ticker { color:var(--gold); font-weight:700; }
    .legend { color:var(--muted); margin:10px 0 14px; font-size:12px; }
    .result { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
    .result-grid { display:grid; grid-template-columns:repeat(3, minmax(180px, 1fr)); gap:10px; }
    .metric { background:#0f141d; border:1px solid var(--line); border-radius:10px; padding:10px; }
    .metric .k { color:var(--muted); font-size:12px; }
    .metric .v { margin-top:4px; font-weight:700; }
    .clickable-row { cursor:pointer; }
    .clickable-row:hover { background:#151f2d; }
    @media (max-width: 1024px) { .toolbar { grid-template-columns:repeat(2, minmax(120px,1fr)); } .result-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<div class="wrap">
  <h1>Спреды фьючерсов</h1>
  <div class="sub">ONUS vs все остальные биржи + ручной калькулятор спреда.</div>

  <div class="tabs">
    <button class="tab active" id="tab-monitor-btn" onclick="switchTab('monitor')">Монитор</button>
    <button class="tab" id="tab-custom-btn" onclick="switchTab('custom')">Спред</button>
  </div>

  <section id="panel-monitor" class="panel active">
    <div class="toolbar">
      <label>Режим
        <input id="fixedMode" type="text" value="ONUS vs все биржи" disabled>
      </label>
      <label>Мин. |спред| %
        <input id="minSpread" type="number" step="0.01" value="0.2">
      </label>
      <label>Лимит строк
        <input id="limit" type="number" min="10" max="1000" value="200">
      </label>
      <label>Обновление
        <button id="refresh">Обновить</button>
      </label>
    </div>

    <div class="legend" id="meta">Загрузка...</div>

    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Биржа</th>
          <th>Coin</th>
          <th>LONG / SHORT</th>
          <th class="num">Вход (ONUS|EXCH)</th>
          <th class="num">Спред %</th>
          <th class="num">Фандинг (ONUS|EXCH)</th>
          <th class="num">Δ funding</th>
          <th class="num">Age, сек</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </section>

  <section id="panel-custom" class="panel">
    <div class="toolbar">
      <label>Монета (base)
        <input id="coinInput" type="text" placeholder="BTC" list="coinHints" value="BTC">
        <datalist id="coinHints"></datalist>
      </label>
      <label>Long биржа
        <select id="longExchange"></select>
      </label>
      <label>Short биржа
        <select id="shortExchange"></select>
      </label>
      <label>ONUS тикер
        <select id="onusSymbol"></select>
      </label>
      <label>Автообновление
        <select id="customAuto">
          <option value="off" selected>Выкл</option>
          <option value="on">Вкл</option>
        </select>
      </label>
      <label>Интервал (мс)
        <input id="customIntervalMs" type="number" min="500" step="100" value="1000">
      </label>
      <label>Действие
        <button id="buildSpread">Построить</button>
      </label>
    </div>

    <div class="result" id="customResult">Выберите монету и биржи, затем нажмите «Построить».</div>
  </section>
</div>

<script>
const state = { exchanges: [], spreadRows: [], onusSymbols: [], customLastRun: 0 };

function pct(v, digits = 3) { if (v === null || v === undefined || Number.isNaN(v)) return '—'; return `${v.toFixed(digits)}%`; }
function clsBySign(v) { if (v === null || v === undefined || Number.isNaN(v)) return ''; return v >= 0 ? 'pos' : 'neg'; }


function parseDirection(dirText) {
  const m = /LONG\s+([A-Z0-9_\-]+)\s*\/\s*SHORT\s+([A-Z0-9_\-]+)/i.exec(dirText || '');
  if (!m) return null;
  return { longEx: m[1].toLowerCase(), shortEx: m[2].toLowerCase() };
}

async function jumpToCustomSpread(row) {
  if (!row) return;

  const coinInput = document.getElementById('coinInput');
  const longSel = document.getElementById('longExchange');
  const shortSel = document.getElementById('shortExchange');
  const onusSel = document.getElementById('onusSymbol');
  const autoSel = document.getElementById('customAuto');

  coinInput.value = row.coin || '';

  const dir = parseDirection(row.long_short);
  if (dir) {
    longSel.value = dir.longEx;
    shortSel.value = dir.shortEx;
  }

  await updateOnusSymbolOptions();

  if (row.left_symbol && (longSel.value === 'onus' || shortSel.value === 'onus')) {
    const normalized = String(row.left_symbol).toUpperCase();
    const option = Array.from(onusSel.options).find(o => String(o.value).toUpperCase() === normalized);
    if (option) onusSel.value = option.value;
  }

  autoSel.value = 'on';
  state.customLastRun = 0;
  switchTab('custom');
  await buildCustomSpread();
}
function switchTab(name) {
  document.getElementById('panel-monitor').classList.toggle('active', name === 'monitor');
  document.getElementById('panel-custom').classList.toggle('active', name === 'custom');
  document.getElementById('tab-monitor-btn').classList.toggle('active', name === 'monitor');
  document.getElementById('tab-custom-btn').classList.toggle('active', name === 'custom');
  if (name === 'custom' && document.getElementById('customAuto').value === 'on') {
    buildCustomSpread();
  }
}

function renderExchangeSelectors(exchanges) {
  const longSel = document.getElementById('longExchange');
  const shortSel = document.getElementById('shortExchange');
  const options = exchanges.map(e => `<option value="${e}">${e.toUpperCase()}</option>`).join('');
  longSel.innerHTML = options;
  shortSel.innerHTML = options;

  if (exchanges.includes('binance')) longSel.value = 'binance';
  if (exchanges.includes('onus')) shortSel.value = 'onus';
  if (longSel.value === shortSel.value && exchanges.length > 1) {
    shortSel.value = exchanges.find(e => e !== longSel.value);
  }
}

function renderCoinHints(rows) {
  const hints = Array.from(new Set((rows || []).map(r => r.coin).filter(Boolean))).sort();
  const datalist = document.getElementById('coinHints');
  datalist.innerHTML = hints.map(c => `<option value="${c}"></option>`).join('');
}

function renderOnusSymbols(symbols) {
  state.onusSymbols = symbols || [];
  const sel = document.getElementById('onusSymbol');
  const options = state.onusSymbols.map(s => `<option value="${s}">${s}</option>`).join('');
  sel.innerHTML = options || '<option value="">—</option>';
}

async function updateOnusSymbolOptions() {
  const coin = (document.getElementById('coinInput').value || '').trim();
  const longEx = document.getElementById('longExchange').value;
  const shortEx = document.getElementById('shortExchange').value;
  const sel = document.getElementById('onusSymbol');
  const usesOnus = longEx === 'onus' || shortEx === 'onus';
  sel.disabled = !usesOnus;
  if (!usesOnus || !coin) {
    renderOnusSymbols([]);
    return;
  }

  const res = await fetch(`/api/onus_symbols?coin=${encodeURIComponent(coin)}`);
  const data = await res.json();
  renderOnusSymbols(data.symbols || []);
}

async function loadMeta() {
  const res = await fetch('/api/exchanges');
  const data = await res.json();
  state.exchanges = data.exchanges || [];

  if (!state.exchanges.includes('onus')) {
    document.getElementById('meta').textContent = 'Ошибка: не найдена биржа ONUS (файл onus_futures_db.json).';
    return;
  }

  const compareExchanges = state.exchanges.filter(e => e !== 'onus');
  if (compareExchanges.length < 1) {
    document.getElementById('meta').textContent = 'Нужна хотя бы 1 дополнительная биржа кроме ONUS.';
    return;
  }

  document.getElementById('fixedMode').value = `ONUS vs ${compareExchanges.map(x => x.toUpperCase()).join(', ')}`;
  renderExchangeSelectors(state.exchanges);
  await updateOnusSymbolOptions();
}

async function loadTable() {
  const minSpread = document.getElementById('minSpread').value || '0';
  const limit = document.getElementById('limit').value || '200';

  const res = await fetch(`/api/spreads?min_spread=${encodeURIComponent(minSpread)}&limit=${encodeURIComponent(limit)}`);
  const data = await res.json();
  state.spreadRows = data.rows || [];
  renderCoinHints(state.spreadRows);
  await updateOnusSymbolOptions();

  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';
  state.spreadRows.forEach((r, idx) => {
    const tr = document.createElement('tr');
    tr.classList.add('clickable-row');
    tr.title = 'Клик: открыть в вкладке Спред с авто-параметрами';
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td><b>${(r.compare_exchange || '').toUpperCase()}</b></td>
      <td><div><b class="coin-ticker">${r.coin}</b></div><div class="mono" style="color:#90a4bf">${r.left_symbol} | ${r.right_symbol}</div></td>
      <td>${r.long_short}</td>
      <td class="num mono">${r.left_price} | ${r.right_price}</td>
      <td class="num ${clsBySign(r.spread_pct)}">${pct(r.spread_pct, 4)}</td>
      <td class="num mono">${pct(r.funding_left, 4)} | ${pct(r.funding_right, 4)}</td>
      <td class="num ${clsBySign(r.funding_diff)}">${pct(r.funding_diff, 4)}</td>
      <td class="num ${Math.max(r.left_age_sec || 0, r.right_age_sec || 0) > 20 ? 'warn' : ''}">${r.left_age_sec ?? '—'} | ${r.right_age_sec ?? '—'}</td>
    `;
    tr.addEventListener('click', () => jumpToCustomSpread(r));
    tbody.appendChild(tr);
  });

  const byExchange = data.by_exchange || {};
  const breakdown = Object.keys(byExchange).sort().map(name => `${name.toUpperCase()}: ${byExchange[name]}`).join(' · ');
  document.getElementById('meta').textContent = `База: ${(data.base || 'ONUS').toUpperCase()} · Показано: ${state.spreadRows.length} · Совпадения по биржам: ${breakdown || '—'} · Обновлено: ${new Date().toLocaleTimeString()}`;
}

async function buildCustomSpread() {
  const coin = (document.getElementById('coinInput').value || '').trim();
  const longEx = document.getElementById('longExchange').value;
  const shortEx = document.getElementById('shortExchange').value;
  const onusSymbol = document.getElementById('onusSymbol').value;
  const result = document.getElementById('customResult');

  if (!coin) {
    result.textContent = 'Укажи монету, например BTC.';
    return;
  }

  const res = await fetch(`/api/custom_spread?coin=${encodeURIComponent(coin)}&long=${encodeURIComponent(longEx)}&short=${encodeURIComponent(shortEx)}&onus_symbol=${encodeURIComponent(onusSymbol || "")}`);
  const data = await res.json();

  if (!res.ok || data.error) {
    result.innerHTML = `<span class="neg">Ошибка: ${data.error || 'не удалось посчитать спред'}</span>`;
    return;
  }

  state.customLastRun = Date.now();
  result.innerHTML = `
    <div style="margin-bottom:10px;"><b class="coin-ticker">${data.coin}</b> · LONG ${data.long_exchange.toUpperCase()} / SHORT ${data.short_exchange.toUpperCase()} · обновлено ${new Date(state.customLastRun).toLocaleTimeString()}</div>
    <div class="result-grid">
      <div class="metric"><div class="k">Символы</div><div class="v mono">${data.long_symbol} | ${data.short_symbol}</div></div>
      <div class="metric"><div class="k">Вход (long|short)</div><div class="v mono">${data.long_price} | ${data.short_price}</div></div>
      <div class="metric"><div class="k">Текущий спред</div><div class="v ${clsBySign(data.spread_pct)}">${pct(data.spread_pct, 4)}</div></div>
      <div class="metric"><div class="k">Фандинг (long|short)</div><div class="v mono">${pct(data.funding_long, 4)} | ${pct(data.funding_short, 4)}</div></div>
      <div class="metric"><div class="k">Δ funding</div><div class="v ${clsBySign(data.funding_diff)}">${pct(data.funding_diff, 4)}</div></div>
      <div class="metric"><div class="k">Age (сек)</div><div class="v ${Math.max(data.long_age_sec || 0, data.short_age_sec || 0) > 20 ? 'warn' : ''}">${data.long_age_sec ?? '—'} | ${data.short_age_sec ?? '—'}</div></div>
    </div>
  `;
}



function customTabAutoTick() {
  const panelCustom = document.getElementById('panel-custom');
  if (!panelCustom.classList.contains('active')) return;
  if (document.getElementById('customAuto').value !== 'on') return;

  const intervalMs = Math.max(500, Number(document.getElementById('customIntervalMs').value || 1000));
  if (Date.now() - state.customLastRun < intervalMs) return;

  buildCustomSpread();
}

async function bootstrap() {
  await loadMeta();
  await loadTable();
  document.getElementById('refresh').addEventListener('click', loadTable);
  document.getElementById('buildSpread').addEventListener('click', buildCustomSpread);
  document.getElementById('coinInput').addEventListener('input', updateOnusSymbolOptions);
  document.getElementById('longExchange').addEventListener('change', updateOnusSymbolOptions);
  document.getElementById('shortExchange').addEventListener('change', updateOnusSymbolOptions);
  document.getElementById('onusSymbol').addEventListener('change', () => { if (document.getElementById('customAuto').value === 'on') buildCustomSpread(); });
  document.getElementById('customAuto').addEventListener('change', () => {
    if (document.getElementById('customAuto').value === 'on') {
      state.customLastRun = 0;
      buildCustomSpread();
    }
  });
  document.getElementById('customIntervalMs').addEventListener('change', () => {
    const el = document.getElementById('customIntervalMs');
    if (Number(el.value) < 500) el.value = '500';
  });

  setInterval(loadTable, 3000);
  setInterval(customTabAutoTick, 250);
}

bootstrap().catch(err => {
  document.getElementById('meta').textContent = `Ошибка: ${err.message}`;
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(HTML_PAGE)
            return

        if path == "/api/exchanges":
            exchange_map = discover_exchange_dbs()
            exchanges = sorted(exchange_map)
            self._send_json({"exchanges": exchanges, "db_files": {k: str(v) for k, v in exchange_map.items()}})
            return

        if path == "/api/spreads":
            query = parse_qs(parsed.query)
            exchanges = discover_exchange_dbs()

            base = "onus"
            limit = int(query.get("limit", ["200"])[0])
            min_spread = float(query.get("min_spread", ["0"])[0])

            if base not in exchanges:
                self._send_json(
                    {
                        "error": "base exchange not found",
                        "base": base,
                        "available": sorted(exchanges),
                    },
                    status=404,
                )
                return

            compare_exchanges = [name for name in exchanges if name != base]
            if not compare_exchanges:
                self._send_json(
                    {
                        "error": "no exchanges to compare",
                        "base": base,
                        "available": sorted(exchanges),
                    },
                    status=400,
                )
                return

            try:
                rows_map = {name: load_rows(path) for name, path in exchanges.items()}
            except (OSError, json.JSONDecodeError) as error:
                self._send_json({"error": f"db read error: {error}"}, status=500)
                return

            rows, summary = build_spreads_vs_base(
                base_exchange=base,
                exchange_rows=rows_map,
                limit=max(10, min(limit, 2000)),
            )
            filtered = [r for r in rows if r["abs_spread_pct"] >= min_spread]

            self._send_json(
                {
                    "base": base,
                    "compared_exchanges": sorted(compare_exchanges),
                    "by_exchange": summary,
                    "rows": filtered,
                }
            )
            return

        if path == "/api/custom_spread":
            query = parse_qs(parsed.query)
            exchanges = discover_exchange_dbs()

            coin = query.get("coin", [""])[0]
            long_exchange = query.get("long", [""])[0].lower()
            short_exchange = query.get("short", [""])[0].lower()
            onus_symbol = query.get("onus_symbol", [""])[0]

            try:
                rows_map = {name: load_rows(path) for name, path in exchanges.items()}
            except (OSError, json.JSONDecodeError) as error:
                self._send_json({"error": f"db read error: {error}"}, status=500)
                return

            payload = compute_custom_spread(
                coin=coin,
                long_exchange=long_exchange,
                short_exchange=short_exchange,
                exchange_rows=rows_map,
                onus_symbol=onus_symbol or None,
            )
            status = 400 if payload.get("error") else 200
            self._send_json(payload, status=status)
            return

        if path == "/api/onus_symbols":
            query = parse_qs(parsed.query)
            coin = query.get("coin", [""])[0]
            exchanges = discover_exchange_dbs()
            onus_db = exchanges.get("onus")
            if not onus_db:
                self._send_json({"symbols": []})
                return
            try:
                rows = load_rows(onus_db)
            except (OSError, json.JSONDecodeError) as error:
                self._send_json({"error": f"db read error: {error}"}, status=500)
                return
            self._send_json({"symbols": list_exchange_symbols(rows, coin)})
            return

        self._send_json({"error": "not found"}, status=404)


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), Handler)

    print("Spread dashboard started.")
    print(f"Bind address: {host}:{port}")
    if host == "0.0.0.0":
        print(f"Open in browser: http://127.0.0.1:{port}")
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
            if lan_ip and lan_ip not in {"127.0.0.1", "0.0.0.0"}:
                print(f"Or from LAN: http://{lan_ip}:{port}")
        except OSError:
            pass
    else:
        print(f"Open in browser: http://{host}:{port}")

    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Futures spread web dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default: 8080)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(host=args.host, port=args.port)
