"""Binance public, read-only market data. No authenticated/order endpoints exist here."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
import math
import time
from typing import Any

import httpx


class MarketError(RuntimeError):
    """A missing, inconsistent, stale or unreachable public market feed."""


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_available: bool = True


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    time_ms: int
    conversion_rate: float = 1.0
    currency: str = 'USDT'
    market_status: str = 'TRADEABLE'
    conversion_bid: float | None = None
    conversion_ask: float | None = None


@dataclass(frozen=True)
class Rules:
    symbol: str
    min_qty: float
    max_qty: float
    step_size: float
    min_notional: float
    max_notional: float
    tick_size: float = 0.0
    asset_class: str = 'crypto'
    provider: str = 'binance'
    currency: str = 'USDT'
    multiplier: float = 1.0
    min_margin_pct: float = 100.0
    allow_short: bool = False
    financing_daily: float = 0.0
    maintenance_margin_pct: float = 50.0


_INTERVALS = {"1s": 1000, "1m": 60000, "3m": 180000, "5m": 300000,
              "15m": 900000, "30m": 1800000, "1h": 3600000, "2h": 7200000,
              "4h": 14400000, "6h": 21600000, "8h": 28800000,
              "12h": 43200000, "1d": 86400000, "3d": 259200000,
              "1w": 604800000}


def _positive(value: Any, zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise MarketError("Donnée Binance non finie ou négative.")
    return value


def _common_step(steps: list[float]) -> float:
    # A quantity must respect BOTH LOT_SIZE and MARKET_LOT_SIZE where enabled.
    ds = [Decimal(str(x)) for x in steps if x > 0]
    if not ds:
        raise MarketError("Précision de quantité Binance absente.")
    scale = 10 ** max(max(0, -d.as_tuple().exponent) for d in ds)
    units = [int(d * scale) for d in ds]
    result = units[0]
    for n in units[1:]:
        result = math.lcm(result, n)
    return float(Decimal(result) / scale)


class BinanceClient:
    def __init__(self, base_url: str = "https://data-api.binance.vision",
                 client: httpx.AsyncClient | None = None):
        # Only the public-data host is supported; this cannot silently become a trading client.
        if base_url.rstrip("/") != "https://data-api.binance.vision":
            raise ValueError("Seul https://data-api.binance.vision est autorisé.")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0),
                                                  follow_redirects=False)
        self.last_error: str | None = None
        self._clock_ms: float | None = None
        self._clock_monotonic = 0.0
        self._retry_at = 0.0
        self._rules_cache: dict[str, Rules] = {}
        self._rules_at = 0.0
        self._clock_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, endpoint: str, params: dict | None = None) -> Any:
        if endpoint not in {"time", "exchangeInfo", "klines", "ticker/bookTicker", "ticker/24hr"}:
            raise MarketError("Point d'accès non autorisé.")
        if time.monotonic() < self._retry_at:
            raise MarketError("Binance impose une pause de requêtes ; reconnexion différée.")
        for attempt in range(3):
            try:
                async with self._request_lock:
                    # Pace departures (at most 16 per second), never hold up
                    # other positions while one metadata response is in flight.
                    if time.monotonic() < self._retry_at:
                        raise MarketError("Binance impose une pause de requêtes ; reconnexion différée.")
                    delay = .065 - (time.monotonic() - self._last_request)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # An in-flight request may have received a 429 during pacing.
                    if time.monotonic() < self._retry_at:
                        raise MarketError("Binance impose une pause de requêtes ; reconnexion différée.")
                    started = time.monotonic()
                    self._last_request = started
                response = await self.client.get(f"{self.base_url}/api/v3/{endpoint}", params=params)
                if time.monotonic() - started > 15:
                    raise MarketError("Réponse Binance trop lente ; cotation rejetée.")
                if response.status_code in {418, 429}:
                    try:
                        retry = max(1., min(86400., float(response.headers.get("Retry-After", "60"))))
                    except ValueError:
                        retry = 60.
                    self._retry_at = time.monotonic() + retry
                    raise MarketError(f"Limite Binance HTTP {response.status_code} ; pause {retry:g} s.")
                if response.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(.5 * 2**attempt)
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and "code" in data and data.get("code", 0) < 0:
                    raise MarketError("Binance a refusé la requête de données publiques.")
                self.last_error = None
                return data
            except MarketError as exc:
                self.last_error = str(exc)
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    self.last_error = f"Binance inaccessible (HTTP {exc.response.status_code})."
                    raise MarketError(self.last_error) from None
                if attempt < 2:
                    await asyncio.sleep(.5 * 2**attempt)
                    continue
                self.last_error = "Connexion Binance impossible ou réponse invalide. Aucune donnée fabriquée."
                raise MarketError(self.last_error) from None
        raise MarketError("Données Binance indisponibles.")

    async def liquid_symbols(self, limit: int = 30) -> list[str]:
        """Rank active USDT Spot markets by real 24h turnover; no synthetic instruments."""
        info = await self._get("exchangeInfo")
        tickers = await self._get("ticker/24hr", {"type": "MINI"})
        # Measure freshness AFTER the network fetches. Otherwise recent tickers
        # can appear future-dated compared to a clock read before metadata I/O.
        now = await self.server_time()
        if not isinstance(info, dict) or not isinstance(info.get("symbols"), list) or not isinstance(tickers, list):
            raise MarketError("Liste des marchés indisponible.")
        excluded = {"USDC", "USDP", "TUSD", "FDUSD", "DAI", "USDE", "USD1", "BUSD", "AEUR", "EURI", "EUR", "PAXG", "XAUT"}
        # Explicit crypto universe: never treat tokenized stocks, fiat or leveraged
        # exchange products as ordinary crypto because their symbol ends in USDT.
        supported = set("BTC ETH SOL BNB XRP ADA DOGE AVAX DOT LINK LTC BCH XLM ATOM NEAR UNI AAVE ICP FIL ETC TRX SUI APT ARB OP INJ SEI TIA RUNE FET RENDER TAO IMX GRT ALGO VET HBAR QNT EOS XTZ EGLD THETA FLOW KAVA CRV LDO MKR SNX COMP ZEC DASH XMR ENA WLD PEPE SHIB BONK FLOKI PENGU ONDO JUP PUMP HYPE JTO STX TON".split())
        allowed = set()
        for row in info["symbols"]:
            base = row.get("baseAsset", "")
            if (row.get("status") == "TRADING" and row.get("isSpotTradingAllowed") is True
                and row.get("quoteAsset") == "USDT" and base in supported and base not in excluded
                and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))):
                allowed.add(row["symbol"])
        ranked = []
        for row in tickers:
            try:
                symbol = row["symbol"]
                self._validate_symbol(symbol)
                turnover = _positive(row["quoteVolume"], True)
                if symbol in allowed and turnover >= 5_000_000 and -5000 <= now - int(row["closeTime"]) <= 120000:
                    ranked.append((turnover, symbol))
            except (KeyError, TypeError, ValueError, MarketError):
                continue
        result = [s for _, s in sorted(ranked, reverse=True)[:max(3, min(40, limit))]]
        if not result:
            raise MarketError("Aucun marché liquide avec statistiques récentes.")
        return result

    async def server_time(self) -> int:
        async with self._clock_lock:
            now = time.monotonic()
            if self._clock_ms is None or now - self._clock_monotonic >= 60:
                started = time.monotonic()
                data = await self._get("time")
                received = time.monotonic()
                try:
                    server = int(data["serverTime"])
                    if server < 1500000000000 or received - started > 15:
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise MarketError("Horodatage du serveur Binance invalide.") from None
                self._clock_ms = server + (received - started) * 500
                self._clock_monotonic = received
            return int(self._clock_ms + (time.monotonic() - self._clock_monotonic) * 1000)

    async def quote(self, symbol: str) -> Quote:
        self._validate_symbol(symbol)
        await self.server_time()
        data = await self._get("ticker/bookTicker", {"symbol": symbol})
        received_ms = int(time.time() * 1000)
        try:
            if data["symbol"] != symbol:
                raise MarketError("Symbole de cotation incohérent.")
            bid, ask = _positive(data["bidPrice"]), _positive(data["askPrice"])
            if ask < bid or ask / bid > 1.05:
                raise MarketError("Cotation bid/ask incohérente ou spread supérieur à 5 %.")
            # bookTicker REST has no event time. Use the actual local receipt
            # clock, also used by execution freshness checks. The estimated
            # exchange clock may be ahead and is only for candle boundaries.
            return Quote(symbol, bid, ask, received_ms)
        except (KeyError, TypeError, ValueError):
            raise MarketError("Cotation Binance incomplète.") from None

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not isinstance(symbol, str) or not symbol.endswith("USDT") or not symbol.isalnum() or not symbol.isupper() or len(symbol) > 24:
            raise MarketError("Symbole Spot USDT invalide.")

    async def rules(self, symbols: list[str]) -> dict[str, Rules]:
        if not symbols:
            return {}
        for symbol in symbols:
            self._validate_symbol(symbol)
        if time.monotonic() - self._rules_at < 300 and all(s in self._rules_cache for s in symbols):
            return {s: self._rules_cache[s] for s in symbols}
        data = await self._get("exchangeInfo", {"symbols": json.dumps(sorted(set(symbols)), separators=(",", ":"))})
        found = {}
        try:
            for item in data["symbols"]:
                symbol = item["symbol"]
                if item["status"] != "TRADING" or item.get("quoteAsset") != "USDT" or item.get("isSpotTradingAllowed") is not True:
                    raise MarketError(f"Marché {symbol} non disponible au comptant.")
                filters = {f["filterType"]: f for f in item["filters"]}
                lots = [filters[n] for n in ["LOT_SIZE", "MARKET_LOT_SIZE"] if n in filters]
                if not lots:
                    raise MarketError("Contraintes Binance de quantité absentes.")
                minimums = [_positive(f["minQty"], True) for f in lots]
                maximums = [_positive(f["maxQty"], True) for f in lots]
                step = _common_step([_positive(f["stepSize"], True) for f in lots])
                min_notional, max_notional = 0., 1e30
                if "MIN_NOTIONAL" in filters:
                    f = filters["MIN_NOTIONAL"]
                    if f.get("applyToMarket", True):
                        min_notional = max(min_notional, _positive(f["minNotional"], True))
                if "NOTIONAL" in filters:
                    f = filters["NOTIONAL"]
                    if f.get("applyMinToMarket", True):
                        min_notional = max(min_notional, _positive(f["minNotional"], True))
                    if f.get("applyMaxToMarket", True):
                        max_notional = min(max_notional, _positive(f["maxNotional"]))
                tick = _positive(filters.get("PRICE_FILTER", {}).get("tickSize", 0), True)
                max_qty = min((n for n in maximums if n > 0), default=1e30)
                if max_qty < max(minimums) or max_notional < min_notional:
                    raise MarketError("Contraintes Binance contradictoires.")
                found[symbol] = Rules(symbol, max(minimums), max_qty, step, min_notional, max_notional, tick)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise MarketError("Contraintes de marché Binance invalides ou incomplètes.") from None
        if set(found) != set(symbols):
            raise MarketError("Un des marchés demandés est absent de Binance.")
        self._rules_cache.update(found)
        self._rules_at = time.monotonic()
        return {s: found[s] for s in symbols}

    async def candles(self, symbol: str, interval: str, limit: int = 150,
                      start_time: int | None = None, end_time: int | None = None) -> list[Candle]:
        self._validate_symbol(symbol)
        if interval not in _INTERVALS or not 1 <= limit <= 1000:
            raise MarketError("Intervalle ou nombre de bougies invalide.")
        now = await self.server_time()
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = min(int(end_time), now)
        rows = await self._get("klines", params)
        result: list[Candle] = []
        duration = _INTERVALS[interval]
        try:
            if not isinstance(rows, list):
                raise MarketError("Bougies Binance invalides.")
            previous_open = None
            for row in rows:
                opened, closed = int(row[0]), int(row[6])
                if opened < 0 or closed != opened + duration - 1:
                    raise MarketError("Horodatage de bougie incohérent.")
                if previous_open is not None and opened != previous_open + duration:
                    raise MarketError("Bougies manquantes, désordonnées ou dupliquées.")
                previous_open = opened
                if closed >= now:
                    continue
                op, hi, lo, cl = [_positive(v) for v in row[1:5]]
                volume = _positive(row[5], True)
                if not lo <= min(op, cl) <= max(op, cl) <= hi:
                    raise MarketError("Prix OHLC incohérents.")
                result.append(Candle(opened, closed, op, hi, lo, cl, volume))
        except (ValueError, TypeError, IndexError, OverflowError):
            raise MarketError("Bougies Binance incomplètes ou invalides.") from None
        # A historical reconciliation request may deliberately end in the past.
        if end_time is None and start_time is None:
            if not result or now - result[-1].close_time > duration + 15000:
                raise MarketError("Dernière bougie Binance trop ancienne.")
        return result
