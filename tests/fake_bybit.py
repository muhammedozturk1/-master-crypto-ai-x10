"""Testler için sahte Bybit v5 sunucusu (httpx.MockTransport)."""

from __future__ import annotations

import time

import httpx

from app.exchange.bybit_public import BybitPublic

DAY = 86_400_000


def make_klines(n: int, interval_min: int = 15, include_forming: bool = True, base: float = 60000.0,
                end_ms: int | None = None) -> list[list[str]]:
    step = interval_min * 60_000
    now = int(time.time() * 1000) if end_ms is None else end_ms
    forming_start = now - (now % step)
    last_start = forming_start if include_forming else forming_start - step
    rows = []
    for k in range(n):
        start = last_start - k * step
        o = base + k
        rows.append([str(start), str(o), str(o + 20), str(o - 20), str(o + 5), "12.5", str(12.5 * o)])
    return rows  # Bybit gibi: en yeni ilk sırada


def tickers_payload(now_ms: int) -> tuple[list[dict], list[dict]]:
    tickers = [
        {"symbol": "BTCUSDT", "lastPrice": "60000", "bid1Price": "59999.9", "ask1Price": "60000.1", "turnover24h": "9000000000", "price24hPcnt": "-0.012", "fundingRate": "0.0001", "openInterestValue": "5000000000"},
        {"symbol": "ETHUSDT", "lastPrice": "1900", "bid1Price": "1899.99", "ask1Price": "1900.01", "turnover24h": "4000000000", "price24hPcnt": "0.02", "fundingRate": "0.00005", "openInterestValue": "1"},
        {"symbol": "LOWVOLUSDT", "lastPrice": "1", "bid1Price": "0.999", "ask1Price": "1.001", "turnover24h": "500000", "price24hPcnt": "0", "fundingRate": "0"},
        {"symbol": "WIDEUSDT", "lastPrice": "1", "bid1Price": "0.99", "ask1Price": "1.01", "turnover24h": "50000000", "price24hPcnt": "0", "fundingRate": "0"},
        {"symbol": "NEWUSDT", "lastPrice": "1", "bid1Price": "0.9999", "ask1Price": "1.0001", "turnover24h": "80000000", "price24hPcnt": "0.5", "fundingRate": "0"},
        {"symbol": "HALTUSDT", "lastPrice": "1", "bid1Price": "0.9999", "ask1Price": "1.0001", "turnover24h": "80000000", "price24hPcnt": "0", "fundingRate": "0"},
        {"symbol": "SOLUSDT", "lastPrice": "150", "bid1Price": "149.99", "ask1Price": "150.01", "turnover24h": "900000000", "price24hPcnt": "0.03", "fundingRate": "0.0001"},
    ]
    old = str(now_ms - 400 * DAY)
    def inst(sym, status="Trading", launch=old):
        return {"symbol": sym, "status": status, "quoteCoin": "USDT", "contractType": "LinearPerpetual",
                "launchTime": launch, "priceFilter": {"tickSize": "0.1"},
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
    instruments = [inst("BTCUSDT"), inst("ETHUSDT"), inst("LOWVOLUSDT"), inst("WIDEUSDT"),
                   inst("NEWUSDT", launch=str(now_ms - 3 * DAY)), inst("HALTUSDT", status="Settling"), inst("SOLUSDT")]
    return tickers, instruments


class FakeBybit:
    def __init__(self, fail_first: int = 0, ret_code: int = 0, kline_rows=None):
        self.calls: list[str] = []
        self.kline_rows = kline_rows  # (symbol, interval, limit) -> satırlar
        self.fail_first = fail_first
        self.ret_code = ret_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503)
        if self.ret_code:
            return httpx.Response(200, json={"retCode": self.ret_code, "retMsg": "params error", "result": {}})
        now = int(time.time() * 1000)
        path, q = request.url.path, request.url.params
        if path == "/v5/market/kline":
            rows = None
            if self.kline_rows:
                rows = self.kline_rows(q["symbol"], q["interval"], int(q["limit"]))
            if rows is None:
                end = int(q["end"]) if "end" in q else None
                rows = make_klines(int(q["limit"]), int(q["interval"]), end_ms=end)
                if "start" in q:
                    rows = [r for r in rows if int(r[0]) >= int(q["start"])]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        tickers, instruments = tickers_payload(now)
        if path == "/v5/market/tickers":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": tickers}})
        if path == "/v5/market/instruments-info":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": instruments, "nextPageCursor": ""}})
        if path == "/v5/market/orderbook":
            return httpx.Response(200, json={"retCode": 0, "result": {"b": [["99.9", "10"], ["99.0", "5"]], "a": [["100.1", "8"], ["101.5", "3"]], "ts": now}})
        # Bybit gibi en yeniden eskiye
        if path == "/v5/market/open-interest":
            rows = [{"openInterest": str(1000 + k), "timestamp": str(now - k * 900_000)} for k in range(int(q["limit"]))]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        if path == "/v5/market/funding/history":
            rows = [{"fundingRate": "0.0001", "fundingRateTimestamp": str(now - k * 28_800_000)} for k in range(int(q["limit"]))]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        if path == "/v5/market/account-ratio":
            rows = [{"symbol": q["symbol"], "buyRatio": "0.6", "sellRatio": "0.4", "timestamp": str(now - k * 3_600_000)} for k in range(int(q["limit"]))]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        if path == "/v5/market/recent-trade":
            rows = [{"execId": str(k), "price": "100", "size": "1", "side": "Buy" if k % 3 else "Sell", "time": str(now - k * 1000)} for k in range(30)]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        return httpx.Response(404)

    def client(self) -> BybitPublic:
        http = httpx.AsyncClient(base_url="https://api.bybit.com", transport=httpx.MockTransport(self.handler))
        return BybitPublic(client=http, retries=2)
