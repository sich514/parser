#!/usr/bin/env python3
"""Lightweight web dashboard for futures spread monitoring across exchanges.

Default pair: Binance vs Onus.
Designed to be extensible: every folder with *_futures_db.json is treated as an exchange source.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
HOST = "0.0.0.0"
PORT = 8080
DB_PATTERN = "*_futures_db.json"
KNOWN_QUOTES = ("USDT", "VNDC", "BUSD", "USDC", "USD")


@dataclass(frozen=True)
class CanonicalSymbol:
    base: str
    quote: str | None
    multiplier: int


def discover_exchange_dbs() -> dict[str, Path]:
    """Find all exchange DB files under project root."""
    found: dict[str, Path] = {}
    for db_path in ROOT.glob(f"*/{DB_PATTERN}"):
        exchange = db_path.parent.name.lower()
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

    # Prefix multiplier: 1000PEPE
    prefix = re.match(r"^(\d+)([A-Z]+)$", body)
    if prefix:
        multiplier = int(prefix.group(1))
        body = prefix.group(2)

    # Suffix multiplier: PEPE1000
    suffix = re.match(r"^([A-Z]+)(\d+)$", body)
    if suffix:
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


def build_exchange_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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
    left_idx = build_exchange_index(left_rows)
    right_idx = build_exchange_index(right_rows)

    common = sorted(set(left_idx).intersection(right_idx))
    spreads: list[dict[str, Any]] = []

    for base in common:
        l = left_idx[base]
        r = right_idx[base]
        lp = safe_float(l.get("_unit_price"))
        rp = safe_float(r.get("_unit_price"))
        if lp is None or rp is None or lp <= 0 or rp <= 0:
            continue

        spread_pct = (rp - lp) / lp * 100.0
        abs_spread = abs(spread_pct)

        if lp < rp:
            direction = f"LONG {left_name.upper()} / SHORT {right_name.upper()}"
        else:
            direction = f"LONG {right_name.upper()} / SHORT {left_name.upper()}"

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


HTML_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Futures Spread Monitor</title>
  <style>
    :root {
      --bg: #0c1016;
      --card: #131923;
      --text: #e7edf7;
      --muted: #98a5bd;
      --green: #00d26a;
      --red: #ff5c73;
      --yellow: #ffd166;
      --line: #273247;
      --accent: #5b8cff;
    }
    body { background: var(--bg); color: var(--text); font-family: Inter, Arial, sans-serif; margin: 0; }
    .wrap { max-width: 1240px; margin: 24px auto; padding: 0 16px; }
    h1 { margin: 0 0 12px; font-size: 24px; }
    .sub { color: var(--muted); margin-bottom: 16px; font-size: 14px; }
    .toolbar {
      display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px;
      background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px;
      margin-bottom: 16px;
    }
    .toolbar label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--muted); }
    select, input, button {
      background: #0f141d; color: var(--text); border: 1px solid var(--line);
      border-radius: 8px; padding: 8px 10px; font-size: 14px;
    }
    button { cursor: pointer; background: var(--accent); border-color: var(--accent); font-weight: 600; }
    table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; }
    th { color: #c4d0e3; position: sticky; top: 0; background: #121a26; }
    tr:hover { background: #151f2d; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .pos { color: var(--green); font-weight: 600; }
    .neg { color: var(--red); font-weight: 600; }
    .warn { color: var(--yellow); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .legend { color: var(--muted); margin: 10px 0 14px; font-size: 12px; }
    @media (max-width: 1024px) {
      .toolbar { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
      th, td { font-size: 12px; padding: 8px 6px; }
    }
  </style>
</head>
<body>
<div class="wrap">
  <h1>Спреды фьючерсов</h1>
  <div class="sub">Сравнение цен между биржами с подсказкой направления LONG/SHORT и фондированием.</div>

  <div class="toolbar">
    <label>Левая биржа
      <select id="left"></select>
    </label>
    <label>Правая биржа
      <select id="right"></select>
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
        <th>Coin</th>
        <th>LONG / SHORT</th>
        <th class="num">Вход (left|right)</th>
        <th class="num">Спред %</th>
        <th class="num">Фандинг (left|right)</th>
        <th class="num">Δ funding</th>
        <th class="num">Age, сек</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
</div>

<script>
const state = { exchanges: [] };

function pct(v, digits = 3) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v.toFixed(digits)}%`;
}

function clsBySign(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '';
  return v >= 0 ? 'pos' : 'neg';
}

function renderExchanges(exchanges) {
  state.exchanges = exchanges;
  const left = document.getElementById('left');
  const right = document.getElementById('right');

  const options = exchanges.map(e => `<option value="${e}">${e.toUpperCase()}</option>`).join('');
  left.innerHTML = options;
  right.innerHTML = options;

  if (exchanges.includes('binance')) left.value = 'binance';
  if (exchanges.includes('onus')) right.value = 'onus';

  if (left.value === right.value && exchanges.length > 1) {
    right.value = exchanges.find(e => e !== left.value);
  }
}

async function loadMeta() {
  const res = await fetch('/api/exchanges');
  const data = await res.json();
  renderExchanges(data.exchanges || []);
}

async function loadTable() {
  const left = document.getElementById('left').value;
  const right = document.getElementById('right').value;
  const minSpread = document.getElementById('minSpread').value || '0';
  const limit = document.getElementById('limit').value || '200';

  const url = `/api/spreads?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}&min_spread=${encodeURIComponent(minSpread)}&limit=${encodeURIComponent(limit)}`;
  const res = await fetch(url);
  const data = await res.json();

  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';

  const rows = data.rows || [];
  rows.forEach((r, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td><div><b>${r.coin}</b></div><div class="mono" style="color:#90a4bf">${r.left_symbol} | ${r.right_symbol}</div></td>
      <td>${r.long_short}</td>
      <td class="num mono">${r.left_price} | ${r.right_price}</td>
      <td class="num ${clsBySign(r.spread_pct)}">${pct(r.spread_pct, 4)}</td>
      <td class="num mono">${pct(r.funding_left, 4)} | ${pct(r.funding_right, 4)}</td>
      <td class="num ${clsBySign(r.funding_diff)}">${pct(r.funding_diff, 4)}</td>
      <td class="num ${Math.max(r.left_age_sec || 0, r.right_age_sec || 0) > 20 ? 'warn' : ''}">${r.left_age_sec ?? '—'} | ${r.right_age_sec ?? '—'}</td>
    `;
    tbody.appendChild(tr);
  });

  const meta = document.getElementById('meta');
  meta.textContent = `Пара: ${data.pair || '-'} · Совпавших монет: ${data.total_matched ?? 0} · Показано: ${rows.length} · Обновлено: ${new Date().toLocaleTimeString()}`;
}

async function bootstrap() {
  await loadMeta();
  await loadTable();
  document.getElementById('refresh').addEventListener('click', loadTable);
  setInterval(loadTable, 3000);
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            exchanges = sorted(discover_exchange_dbs())
            self._send_json({"exchanges": exchanges})
            return

        if path == "/api/spreads":
            query = parse_qs(parsed.query)
            exchanges = discover_exchange_dbs()

            left = query.get("left", ["binance"])[0].lower()
            right = query.get("right", ["onus"])[0].lower()
            limit = int(query.get("limit", ["200"])[0])
            min_spread = float(query.get("min_spread", ["0"])[0])

            if left == right:
                self._send_json({"error": "left and right exchanges must differ"}, status=400)
                return
            if left not in exchanges or right not in exchanges:
                self._send_json(
                    {
                        "error": "exchange not found",
                        "available": sorted(exchanges),
                    },
                    status=404,
                )
                return

            try:
                left_rows = load_rows(exchanges[left])
                right_rows = load_rows(exchanges[right])
            except (OSError, json.JSONDecodeError) as error:
                self._send_json({"error": f"db read error: {error}"}, status=500)
                return

            rows = build_spreads(left, right, left_rows, right_rows, limit=max(10, min(limit, 2000)))
            filtered = [r for r in rows if r["abs_spread_pct"] >= min_spread]

            self._send_json(
                {
                    "pair": f"{left.upper()} vs {right.upper()}",
                    "total_matched": len(rows),
                    "rows": filtered,
                }
            )
            return

        self._send_json({"error": "not found"}, status=404)


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spread dashboard running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    run()
