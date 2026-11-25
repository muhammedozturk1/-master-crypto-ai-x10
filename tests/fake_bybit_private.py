"""Testler için sahte Bybit v5 özel API'si: imzayı doğrular, emir/pozisyon durumunu tutar."""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx

from app.exchange.bybit_private import BybitPrivate, sign

KEY, SECRET = "test-key", "test-secret"


class FakeBybitPrivate:
    def __init__(self, fill_price="100", timeout_on_create=False, lose_order=False, stop_supported=True,
                 trading_stop_fails=False, key_permissions=None, key_read_only=0):
        self.fill_price = Decimal(fill_price)
        self.timeout_on_create = timeout_on_create
        self.lose_order = lose_order            # emir kaydı borsada bulunamaz
        self.stop_supported = stop_supported    # emirle gönderilen stop pozisyona işlenir mi
        self.trading_stop_fails = trading_stop_fails
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.closed: dict[str, list] = {}
        self.calls: list[tuple[str, dict]] = []
        self.bad_signatures = 0
        self.key_permissions = key_permissions if key_permissions is not None else {
            "ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": ["AccountTransfer"]}
        self.key_read_only = key_read_only

    def _check_sig(self, req: httpx.Request, payload: str) -> None:
        h = req.headers
        expected = sign(SECRET, h["X-BAPI-TIMESTAMP"] + KEY + h["X-BAPI-RECV-WINDOW"] + payload)
        if h.get("X-BAPI-API-KEY") != KEY or h.get("X-BAPI-SIGN") != expected:
            self.bad_signatures += 1

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET":
            payload = req.url.query.decode()
            params = dict(parse_qsl(payload))
        else:
            payload = req.content.decode()
            params = json.loads(payload)
        self._check_sig(req, payload)
        self.calls.append((path, params))
        ok = lambda result: httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})  # noqa: E731

        if path == "/v5/position/set-leverage":
            return httpx.Response(200, json={"retCode": 110043, "retMsg": "leverage not modified", "result": {}})
        if path == "/v5/order/create":
            link, sym, side, qty = params["orderLinkId"], params["symbol"], params["side"], Decimal(params["qty"])
            if params.get("orderType") == "Limit":
                assert params.get("timeInForce") == "PostOnly"
                self.orders[link] = {"orderId": f"oid-{link}", "orderLinkId": link, "orderStatus": "New",
                                     "cumExecQty": "0", "avgPrice": "", "cumExecFee": "0", "symbol": sym,
                                     "side": side, "qty": str(qty), "price": params["price"],
                                     "stopLoss": params.get("stopLoss", ""), "takeProfit": params.get("takeProfit", "")}
                return ok({"orderId": f"oid-{link}", "orderLinkId": link})
            if not self.lose_order:
                self.orders[link] = {"orderId": f"oid-{link}", "orderLinkId": link, "orderStatus": "Filled",
                                     "cumExecQty": str(qty), "avgPrice": str(self.fill_price),
                                     "cumExecFee": str(qty * self.fill_price * Decimal("0.00055"))}
            if params.get("reduceOnly"):
                pos = self.positions.pop(sym, None)
                if pos:
                    self._record_close(sym, pos, self.fill_price)
            elif not self.lose_order or self.timeout_on_create:
                if not self.lose_order:
                    self.positions[sym] = {"symbol": sym, "side": side, "size": str(qty), "avgPrice": str(self.fill_price),
                                           "markPrice": str(self.fill_price),
                                           "stopLoss": params.get("stopLoss", "") if self.stop_supported else "",
                                           "takeProfit": params.get("takeProfit", "")}
            if self.timeout_on_create:
                raise httpx.ReadTimeout("simüle zaman aşımı", request=req)
            return ok({"orderId": f"oid-{link}", "orderLinkId": link})
        if path == "/v5/order/cancel":
            o = self.orders.get(params["orderLinkId"])
            if o is None or o["orderStatus"] in ("Filled", "Cancelled", "PartiallyFilledCanceled"):
                return httpx.Response(200, json={"retCode": 110001, "retMsg": "order not exists or too late to cancel", "result": {}})
            o["orderStatus"] = "PartiallyFilledCanceled" if Decimal(o["cumExecQty"]) > 0 else "Cancelled"
            return ok({"orderLinkId": o["orderLinkId"]})
        if path in ("/v5/order/realtime", "/v5/order/history"):
            o = self.orders.get(params.get("orderLinkId", ""))
            return ok({"list": [o] if o else []})
        if path == "/v5/position/list":
            sym = params.get("symbol")
            lst = [p for s, p in self.positions.items() if sym in (None, s)]
            return ok({"list": lst})
        if path == "/v5/position/trading-stop":
            if self.trading_stop_fails:
                return httpx.Response(200, json={"retCode": 10001, "retMsg": "stop rejected", "result": {}})
            if params["symbol"] in self.positions:
                self.positions[params["symbol"]]["stopLoss"] = params.get("stopLoss", "")
            return ok({})
        if path == "/v5/position/closed-pnl":
            return ok({"list": self.closed.get(params["symbol"], [])})
        if path == "/v5/user/query-api":
            return ok({"readOnly": self.key_read_only, "permissions": self.key_permissions})
        if path == "/v5/account/wallet-balance":
            return ok({"list": [{"totalEquity": "50000", "totalAvailableBalance": "49000",
                                 "coin": [{"coin": "USDT", "equity": "50000", "walletBalance": "50000"}]}]})
        return httpx.Response(404)

    def _record_close(self, sym: str, pos: dict, exit_price: Decimal) -> None:
        size, entry = Decimal(pos["size"]), Decimal(pos["avgPrice"])
        self.closed.setdefault(sym, []).append({
            "symbol": sym, "closedSize": str(size), "cumEntryValue": str(size * entry),
            "cumExitValue": str(size * exit_price), "avgExitPrice": str(exit_price),
            "closedPnl": str((exit_price - entry) * size), "openFee": str(size * entry * Decimal("0.00055")),
            "closeFee": str(size * exit_price * Decimal("0.00055"))})

    def fill_limit(self, link: str, qty: str | None = None) -> None:
        """Limit emri (tamamen veya kısmen) limit fiyatından maker olarak doldur."""
        o = self.orders[link]
        q = Decimal(qty) if qty is not None else Decimal(o["qty"])
        px = Decimal(o["price"])
        o.update(cumExecQty=str(q), avgPrice=str(px), cumExecFee=str(q * px * Decimal("0.0002")),
                 orderStatus="Filled" if q == Decimal(o["qty"]) else "PartiallyFilled")
        self.positions[o["symbol"]] = {"symbol": o["symbol"], "side": o["side"], "size": str(q), "avgPrice": str(px),
                                       "markPrice": str(px), "stopLoss": o["stopLoss"] if self.stop_supported else "",
                                       "takeProfit": o["takeProfit"]}

    def stop_out(self, sym: str, exit_price: str) -> None:
        """Borsa tarafında stop tetiklenmiş gibi pozisyonu kapat."""
        pos = self.positions.pop(sym)
        self._record_close(sym, pos, Decimal(exit_price))

    def client(self) -> BybitPrivate:
        http = httpx.AsyncClient(base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(self.handler))
        return BybitPrivate(KEY, SECRET, "https://api-demo.bybit.com", client=http)

    def count(self, path: str) -> int:
        return sum(1 for p, _ in self.calls if p == path)
