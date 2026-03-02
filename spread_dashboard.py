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
        # treat multiplier as power-of-10 style to avoid false positives (e.g. C98)
        return set(raw) == {"1", "0"} and raw[0] == "1"

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
  <div class="sub">ONUS vs все остальные биржи (v2). Сравнение цен между биржами с подсказкой направления LONG/SHORT и фондированием.</div>

  <div class="toolbar">
    <label>Базовая биржа
      <input id="baseExchange" type="text" value="ONUS" disabled>
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

async function loadMeta() {
  const res = await fetch('/api/exchanges');
  const data = await res.json();
  state.exchanges = data.exchanges || [];

  if (!state.exchanges.includes('onus')) {
    const meta = document.getElementById('meta');
    meta.textContent = 'Ошибка: не найдена биржа ONUS (файл onus_futures_db.json).';
    return;
  }

  const compareExchanges = state.exchanges.filter(e => e !== 'onus');
  if (compareExchanges.length < 1) {
    const meta = document.getElementById('meta');
    meta.textContent = 'Нужна хотя бы 1 дополнительная биржа кроме ONUS.';
  }
}

async function loadTable() {
  const minSpread = document.getElementById('minSpread').value || '0';
  const limit = document.getElementById('limit').value || '200';

  const url = `/api/spreads?min_spread=${encodeURIComponent(minSpread)}&limit=${encodeURIComponent(limit)}`;
  const res = await fetch(url);
  const data = await res.json();

  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';

  const rows = data.rows || [];
  rows.forEach((r, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td><b>${(r.compare_exchange || '').toUpperCase()}</b></td>
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

  const byExchange = data.by_exchange || {};
  const breakdown = Object.keys(byExchange)
    .sort()
    .map(name => `${name.toUpperCase()}: ${byExchange[name]}`)
    .join(' · ');

  const meta = document.getElementById('meta');
  meta.textContent = `База: ${(data.base || 'ONUS').toUpperCase()} · Показано: ${rows.length} · Совпадения по биржам: ${breakdown || '—'} · Обновлено: ${new Date().toLocaleTimeString()}`;
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
