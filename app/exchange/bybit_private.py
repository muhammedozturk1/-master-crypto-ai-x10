"""Bybit v5 imzalı (özel) API istemcisi – USDT perpetual (linear).

İmza: HMAC_SHA256(secret, timestamp + api_key + recv_window + (queryString | jsonBody)), küçük
harf hex. Demo hesap anahtarları yalnız https://api-demo.bybit.com ile çalışır.
Bu istemci para çekme uçlarını İÇERMEZ; API anahtarına çekme yetkisi verilmemelidir.
Gönderme sırasında bağlantı koparsa istek TEKRARLANMAZ (çift emir riski); çağıran taraf
emrin durumunu orderLinkId ile sorgular.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.exchange.bybit_public import BybitError

RECV_WINDOW = "10000"
LEVERAGE_NOT_MODIFIED = 110043


class BybitAuthError(BybitError):
    pass


class BybitTimeout(BybitError):
    """İstek gönderildi ama yanıt alınamadı: sonuç bilinmiyor."""


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BybitPrivate:
    def __init__(self, api_key: str, api_secret: str, base_url: str, category: str = "linear",
                 client: httpx.AsyncClient | None = None):
        if not api_key or not api_secret:
            raise BybitAuthError("API anahtarı tanımlı değil")
        self.key, self.secret, self.category = api_key, api_secret, category
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": sign(self.secret, ts + self.key + RECV_WINDOW + payload),
                "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, params: dict, ok_codes: tuple[int, ...] = (0,)) -> dict:
        try:
            if method == "GET":
                qs = urlencode({k: v for k, v in params.items() if v is not None})
                r = await self.client.get(f"{path}?{qs}" if qs else path, headers=self._headers(qs))
            else:
                body = json.dumps({k: v for k, v in params.items() if v is not None}, separators=(",", ":"))
                r = await self.client.post(path, content=body, headers=self._headers(body))
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise BybitTimeout(f"{path}: yanıt alınamadı ({exc.__class__.__name__})") from exc
        except httpx.TransportError as exc:
            raise BybitError(f"{path}: bağlantı hatası ({exc})") from exc
        if r.status_code in (401, 403):
            raise BybitAuthError(f"{path}: yetki hatası HTTP {r.status_code}")
        if r.status_code >= 500 or r.status_code == 429:
            raise BybitError(f"{path}: HTTP {r.status_code}")
        body = r.json()
        code = body.get("retCode")
        if code in (10003, 10004, 10005, 33004):
            raise BybitAuthError(f"{path}: retCode {code} {body.get('retMsg')}")
        if code not in ok_codes:
            raise BybitError(f"{path}: retCode {code} {body.get('retMsg')}")
        return body.get("result") or {}

    # --- Hesap ----------------------------------------------------------
    async def wallet_balance(self) -> dict:
        res = await self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acc = (res.get("list") or [{}])[0]
        usdt = next((c for c in acc.get("coin", []) if c.get("coin") == "USDT"), {})
        return {"total_equity": acc.get("totalEquity"), "available": acc.get("totalAvailableBalance"),
                "usdt_equity": usdt.get("equity"), "usdt_wallet": usdt.get("walletBalance")}

    async def api_key_info(self) -> dict:
        """Anahtar yetkileri: işlem için Contract Order+Position (vadeli) / SpotTrade (spot) gerekir,
        para çekme yetkisi OLMAMALI."""
        res = await self._call("GET", "/v5/user/query-api", {})
        perms = res.get("permissions") or {}
        flat = {p for v in perms.values() if isinstance(v, list) for p in v}
        if self.category == "spot":
            can_trade = "SpotTrade" in set(perms.get("Spot") or [])
        else:
            ct = set(perms.get("ContractTrade") or [])
            can_trade = {"Order", "Position"} <= ct
        read_only = str(res.get("readOnly")) == "1"
        return {"read_only": read_only, "can_trade": can_trade and not read_only,
                "withdraw": "Withdraw" in flat, "permissions": perms}

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._call("POST", "/v5/position/set-leverage",
                         {"category": self.category, "symbol": symbol,
                          "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
                         ok_codes=(0, LEVERAGE_NOT_MODIFIED))

    # --- Emir -----------------------------------------------------------
    async def create_order(self, symbol: str, side: str, qty: Decimal, order_link_id: str,
                           stop_loss: Decimal | None = None, take_profit: Decimal | None = None,
                           reduce_only: bool = False, limit_price: Decimal | None = None) -> dict:
        """limit_price verilirse PostOnly limit emir (yalnız maker; karşı tarafı alacaksa borsa reddeder)."""
        p = {"category": self.category, "symbol": symbol, "side": side, "orderType": "Market",
             "qty": str(qty), "orderLinkId": order_link_id, "positionIdx": 0, "timeInForce": "IOC"}
        if limit_price is not None:
            p.update({"orderType": "Limit", "price": str(limit_price), "timeInForce": "PostOnly"})
        if reduce_only:
            p["reduceOnly"] = True
        else:
            if stop_loss is not None:
                p.update({"stopLoss": str(stop_loss), "slTriggerBy": "MarkPrice", "tpslMode": "Full"})
            if take_profit is not None:
                p.update({"takeProfit": str(take_profit), "tpTriggerBy": "LastPrice", "tpslMode": "Full"})
        return await self._call("POST", "/v5/order/create", p)

    async def cancel_order(self, symbol: str, order_link_id: str) -> None:
        """Emir iptali. Emir zaten dolmuş/iptal edilmişse (110001) hata sayılmaz; durum ayrıca sorgulanır."""
        await self._call("POST", "/v5/order/cancel",
                         {"category": self.category, "symbol": symbol, "orderLinkId": order_link_id},
                         ok_codes=(0, 110001))

    async def get_order(self, order_link_id: str) -> dict | None:
        res = await self._call("GET", "/v5/order/realtime",
                               {"category": self.category, "orderLinkId": order_link_id})
        lst = res.get("list") or []
        if lst:
            return lst[0]
        try:  # kapanmış emirler sunucu yeniden başlatmasından sonra yalnız geçmişte görünür
            res = await self._call("GET", "/v5/order/history",
                                   {"category": self.category, "orderLinkId": order_link_id})
        except BybitAuthError:
            raise
        except BybitError:
            return None
        lst = res.get("list") or []
        return lst[0] if lst else None

    # --- Pozisyon -------------------------------------------------------
    async def positions(self, symbol: str | None = None) -> list[dict]:
        p = {"category": self.category, "symbol": symbol} if symbol else {"category": self.category, "settleCoin": "USDT"}
        res = await self._call("GET", "/v5/position/list", p)
        return [x for x in res.get("list", []) if Decimal(x.get("size") or "0") > 0]

    async def trading_stop(self, symbol: str, stop_loss: Decimal | None, take_profit: Decimal | None = None) -> None:
        p = {"category": self.category, "symbol": symbol, "positionIdx": 0, "tpslMode": "Full"}
        if stop_loss is not None:
            p.update({"stopLoss": str(stop_loss), "slTriggerBy": "MarkPrice"})
        if take_profit is not None:
            p.update({"takeProfit": str(take_profit), "tpTriggerBy": "LastPrice"})
        await self._call("POST", "/v5/position/trading-stop", p)

    async def closed_pnl(self, symbol: str, start_ms: int) -> list[dict]:
        res = await self._call("GET", "/v5/position/closed-pnl",
                               {"category": self.category, "symbol": symbol, "startTime": start_ms, "limit": 50})
        return res.get("list", [])
