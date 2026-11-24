"""Footprint toplayıcı: Bybit herkese açık websocket işlem akışından (publicTrade) fiyat
seviyesi başına agresif alış / satış hacmi.

REST'teki "son 1000 işlem" büyük paritelerde bir mumun yalnız birkaç saniyesini kapsar; gerçek
footprint için akış sürekli dinlenir ve dakikalık kovalarda tutulur. Bir mumun footprint'i
yalnızca o mumun TAMAMI kesintisiz dinlendiyse verilir; aksi halde None döner ve strateji mum
yapısından tahmine geçer (sinyalde hangi kaynağın kullanıldığı yazılır).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import defaultdict

log = logging.getLogger(__name__)

WS_URL = {"linear": "wss://stream.bybit.com/v5/public/linear", "spot": "wss://stream.bybit.com/v5/public/spot"}
STEP_PCT = 0.0005          # fiyat seviyesi genişliği ≈ fiyatın %0,05'i
KEEP_MINUTES = 200         # 1 saatlik mum + pay
MINUTE = 60_000


def nice_step(price: float) -> float:
    raw = price * STEP_PCT
    e = math.floor(math.log10(raw))
    base = raw / 10 ** e
    return (1 if base < 1.5 else 2 if base < 3.5 else 5 if base < 7.5 else 10) * 10 ** e


class FootprintCollector:
    def __init__(self, category: str = "linear", symbols_fn=None, max_symbols: int = 30):
        self.category = category
        self.symbols_fn = symbols_fn or (lambda: [])
        self.max_symbols = max_symbols
        # sembol → dakika başlangıcı → seviye indeksi → [alış, satış]
        self.minutes: dict[str, dict[int, dict[int, list[float]]]] = defaultdict(dict)
        self.step: dict[str, float] = {}
        self.since: dict[str, int] = {}    # sembolün kesintisiz dinlendiği başlangıç (ms)
        self.subscribed: list[str] = []
        self.connected = False
        self.trades_seen = 0
        self.last_error: str | None = None

    # ---- veri işleme (ağdan bağımsız, test edilebilir) ----
    def on_message(self, msg: dict, now_ms: int | None = None) -> None:
        topic = msg.get("topic", "")
        if not topic.startswith("publicTrade."):
            return
        sym = topic.split(".", 1)[1]
        now_ms = now_ms or int(time.time() * 1000)
        if sym not in self.since:
            self.since[sym] = now_ms
        for t in msg.get("data", []):
            try:
                p, q, side, ts = float(t["p"]), float(t["v"]), t["S"], int(t["T"])
            except (KeyError, TypeError, ValueError):
                continue
            step = self.step.setdefault(sym, nice_step(p))
            m = ts - ts % MINUTE
            lv = self.minutes[sym].setdefault(m, {}).setdefault(int(round(p / step)), [0.0, 0.0])
            lv[0 if side == "Buy" else 1] += q * p          # USDT cinsinden
            self.trades_seen += 1
        self._prune(sym, now_ms)

    def _prune(self, sym: str, now_ms: int) -> None:
        old = now_ms - KEEP_MINUTES * MINUTE
        mins = self.minutes[sym]
        if mins and min(mins) < old:
            for k in [k for k in mins if k < old]:
                del mins[k]

    def reset_coverage(self) -> None:
        """Bağlantı koptu: o ana kadarki kapsama artık kesintisiz sayılmaz."""
        self.since.clear()

    def bar(self, symbol: str, start_ms: int, end_ms: int) -> dict | None:
        """[start, end) mumunun footprint'i; mum tamamen dinlenmediyse None."""
        since = self.since.get(symbol)
        if since is None or since > start_ms or symbol not in self.step:
            return None
        agg: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for m, levels in self.minutes[symbol].items():
            if start_ms <= m < end_ms:
                for k, (b, s) in levels.items():
                    agg[k][0] += b; agg[k][1] += s
        if not agg:
            return None
        step = self.step[symbol]
        levels = [(k * step, b, s) for k, (b, s) in sorted(agg.items())]
        buy = sum(x[1] for x in levels); sell = sum(x[2] for x in levels)
        return {"source": "gercek", "step": step, "levels": levels, "buy": buy, "sell": sell,
                "delta": buy - sell, "volume": buy + sell}

    def status(self) -> dict:
        return {"connected": self.connected, "symbols": len(self.subscribed),
                "covered": len(self.since), "trades_seen": self.trades_seen, "last_error": self.last_error}

    # ---- ağ ----
    def wanted(self) -> list[str]:
        return list(dict.fromkeys(["BTCUSDT", *self.symbols_fn()]))[: self.max_symbols]

    async def loop(self) -> None:
        import websockets
        backoff = 2
        while True:
            try:
                async with websockets.connect(WS_URL[self.category], ping_interval=None, open_timeout=15) as ws:
                    self.connected, backoff, self.subscribed = True, 2, []
                    await self._sync(ws)
                    last_ping = last_sync = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            self.on_message(json.loads(raw))
                        except asyncio.TimeoutError:
                            pass
                        now = time.monotonic()
                        if now - last_ping > 20:
                            await ws.send(json.dumps({"op": "ping"})); last_ping = now
                        if now - last_sync > 300:
                            await self._sync(ws); last_sync = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 – bağlantı hatası: kapsama sıfırlanır, yeniden denenir
                self.last_error = str(exc)[:200]
                log.warning("Footprint akışı koptu: %s", self.last_error)
            self.connected = False
            self.reset_coverage()
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def _sync(self, ws) -> None:
        want = self.wanted()
        add = [s for s in want if s not in self.subscribed]
        drop = [s for s in self.subscribed if s not in want]
        for op, syms in (("unsubscribe", drop), ("subscribe", add)):
            for k in range(0, len(syms), 10):
                await ws.send(json.dumps({"op": op, "args": [f"publicTrade.{s}" for s in syms[k:k + 10]]}))
        for s in drop:
            self.since.pop(s, None); self.minutes.pop(s, None)
        self.subscribed = want
        if add or drop:
            log.info("Footprint akışı: %d parite dinleniyor (+%d / -%d)", len(want), len(add), len(drop))
