"""BOT06 – Balina, likidite ve emir akışı botu (şartname bölüm 9).

Kullanılan veriler (Bybit herkese açık): funding geçmişi, open interest, long/short hesap
oranı, son 1000 işlemden taker alış/satış (CVD), emir defteri derinliği, mumlar (OBV, hacim).
On-chain veriler (büyük cüzdan transferleri, borsa giriş/çıkışları, stablecoin hareketleri)
için ücretsiz güvenilir kaynak bağlı DEĞİLDİR; ilgili puan bileşenleri boş kalır ve
sinyalde "on-chain veri yok" olarak belirtilir.

  A  Birikim            : yatay fiyat + OBV/CVD pozitif ayrışma → AL
  B  Dağıtım            : yükselişte zayıflayan hacim + satış yönlü akış → risk uyarısı / SAT
  C  Likidite süpürmesi : önceki dip/tepenin kısa süreli aşılıp mum kapanışıyla geri dönülmesi
  Manipülasyon filtresi : açıklanamayan hacim patlaması, anormal spread → sinyal yok, uyarı kaydı
Kalabalık pozisyon (funding, long/short oranı), OI/fiyat uyumu ve emir defteri dengesizliği
yalnızca güven puanını düzeltir.
"""

from __future__ import annotations

from app.bots.base import MarketContext, WorkerBot
from app.bots.common import Plan, make_signal, pivots, volume_ratio
from app.domain.signals import Action, Signal
from app.indicators import core as I

BOT_ID = "BOT06"
MIN_BARS = 120


def flow_metrics(extra: dict, closes: list[float]) -> dict:
    """Ek verilerden türetilen akış ölçüleri; eksik veri alanı None kalır."""
    m: dict = {"funding": None, "oi_change_pct": None, "long_ratio": None, "taker_buy_ratio": None,
               "cvd": None, "book_imbalance": None}
    fr = extra.get("funding")
    if fr:
        m["funding"] = float(fr[-1][1])
    oi = extra.get("open_interest")
    if oi and len(oi) >= 17 and oi[-17][1] > 0:
        m["oi_change_pct"] = float((oi[-1][1] - oi[-17][1]) / oi[-17][1] * 100)
    lr = extra.get("account_ratio")
    if lr:
        m["long_ratio"] = float(lr[-1][1])
    trades = extra.get("trades")
    if trades:
        buy = sum(float(p * q) for _, side, p, q in trades if side == "Buy")
        sell = sum(float(p * q) for _, side, p, q in trades if side == "Sell")
        if buy + sell > 0:
            m["taker_buy_ratio"] = buy / (buy + sell)
            m["cvd"] = buy - sell
    ob = extra.get("orderbook")
    if ob is not None:
        bid, ask = ob.depth_usd()
        if bid > 0 and ask > 0:
            m["book_imbalance"] = float(bid / ask)
    return m


def crowding_adjustment(long: bool, m: dict, price_change_pct: float, tags: list[str], warns: list[str]) -> int:
    adj = 0
    f = m["funding"]
    if f is not None:
        if f >= 0.0005:   # 8 saatte %0,05 ve üstü: longlar kalabalık
            adj += -10 if long else 5
            warns.append(f"funding yüksek (%{f * 100:.3f})") if long else tags.append("funding")
        elif f <= -0.0003:
            adj += 5 if long else -10
            tags.append("funding") if long else warns.append(f"funding negatif (%{f * 100:.3f})")
    lr = m["long_ratio"]
    if lr is not None:
        if lr >= 0.70:
            adj += -5 if long else 5
        elif lr <= 0.30:
            adj += 5 if long else -5
    oi = m["oi_change_pct"]
    if oi is not None:
        same_dir = (price_change_pct > 0) == long
        if oi > 1.0 and same_dir:
            adj += 5; tags.append("oi")          # yeni pozisyonlar hareketi destekliyor
        elif oi < -1.0 and same_dir:
            adj -= 5; warns.append("hareket pozisyon kapanışından (OI düşüyor)")
    tb = m["taker_buy_ratio"]
    if tb is not None:
        if (tb >= 0.55 and long) or (tb <= 0.45 and not long):
            adj += 5; tags.append("cvd")
        elif (tb <= 0.45 and long) or (tb >= 0.55 and not long):
            adj -= 10; warns.append(f"taker akışı ters (alış payı %{tb * 100:.0f})")
    bi = m["book_imbalance"]
    if bi is not None:
        if (bi >= 1.5 and long) or (bi <= 0.67 and not long):
            adj += 3
        elif (bi <= 0.67 and long) or (bi >= 1.5 and not long):
            adj -= 3
    return adj


class FlowBot(WorkerBot):
    bot_id = BOT_ID
    timeframes = frozenset({"15m", "1h"})
    needs_derivatives = True

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        cs = ctx.candles
        if len(cs) < MIN_BARS:
            return []
        O = [float(c.open) for c in cs]
        H, L, C, V = ctx.highs, ctx.lows, ctx.closes, ctx.volumes
        i = len(C) - 1
        atr = I.atr(H, L, C, 14)
        if atr[i] is None:
            return []
        m = flow_metrics(ctx.extra, C)
        warn_base = [] if any(ctx.extra.get(k) for k in ("onchain",)) else ["on-chain veri yok"]

        # --- Manipülasyon filtresi ----------------------------------------
        vr = volume_ratio(V, i)
        move_atr = abs(C[i] - O[i]) / atr[i]
        if (vr >= 8 and move_atr >= 3) or (ctx.spread_pct is not None and ctx.spread_pct > 0.2):
            return [Signal(
                bot_id=BOT_ID, strategy_id="manipulation_filter", symbol=ctx.symbol, exchange=ctx.exchange,
                timeframe=ctx.timeframe, action=Action.BEKLE, confidence=0,
                reason=f"Manipülasyon şüphesi: hacim {vr:.1f}x, hareket {move_atr:.1f} ATR, spread %{ctx.spread_pct or 0:.3f}",
                risk_warnings=["manipülasyon şüphesi: işlem skorunu düşür / geçici kara liste"],
                details={**m, "volume_ratio": vr, "suspicious": True,
                         "dedup_key": f"{BOT_ID}:{ctx.symbol}:{ctx.timeframe}:manip:{cs[-1].start_ms}"},
            )]

        out = []
        for fn in (self._sweep, self._accumulation):
            for sig in fn(ctx, O, H, L, C, V, atr, i, m, warn_base):
                out.append(sig)
        return out

    # ------------------------------------------------------------------
    def _sweep(self, ctx, O, H, L, C, V, atr, i, m, warn_base):
        tops, bottoms = pivots(H, L, i, lookback=40, wing=3)
        vr = volume_ratio(V, i)
        rng = H[i] - L[i] or 1e-12
        a = atr[i]
        res = []
        for long in (True, False):
            pts = bottoms if long else tops
            if not pts:
                continue
            level = min(p for _, p in pts) if long else max(p for _, p in pts)
            swept = L[i] < level < C[i] if long else H[i] > level > C[i]
            wick = (min(O[i], C[i]) - L[i]) / rng if long else (H[i] - max(O[i], C[i])) / rng
            if not (swept and wick >= 0.5 and vr >= 1.5):
                continue
            stop = L[i] - 0.2 * a if long else H[i] + 0.2 * a
            tags, warns = [], list(warn_base)
            chg = (C[i] / C[i - 16] - 1) * 100
            score = 55 + crowding_adjustment(long, m, chg, tags, warns)
            sig = make_signal(ctx, BOT_ID, "liquidity_sweep" + "".join(f"+{t}" for t in tags),
                              Plan(long, C[i], C[i], stop), score,
                              f"{'Dip' if long else 'Tepe'} likidite süpürmesi: {level:.6g} seviyesi fitille aşıldı, "
                              f"kapanış geri döndü (fitil %{wick * 100:.0f}, hacim {vr:.2f}x)",
                              {**m, "swept_level": level, "volume_ratio": round(vr, 2)}, warns, None)
            if sig:
                res.append(sig)
        return res

    def _accumulation(self, ctx, O, H, L, C, V, atr, i, m, warn_base):
        n = 24
        hi, lo = max(H[i - n:i + 1]), min(L[i - n:i + 1])
        width = (hi - lo) / C[i] * 100
        adx = I.adx(H, L, C, 14)[0][i]
        if width > 3.0 or adx is None or adx >= 20:
            return []  # yatay piyasa değil
        obv = I.obv(C, V)
        price_chg = (C[i] / C[i - n] - 1) * 100
        span = max(abs(x) for x in obv[i - n:i + 1]) or 1.0
        obv_chg = (obv[i] - obv[i - n]) / span  # -2..2 arası normalize eğim
        tb = m["taker_buy_ratio"]
        a = atr[i]
        res = []
        for long in (True, False):
            if long:
                diverge = obv_chg > 0.5 and abs(price_chg) < 0.5 and (tb is None or tb >= 0.52)
                trigger = C[i] > (hi + lo) / 2 and C[i] > O[i]
                stop = lo - 0.2 * a
                name, text = "accumulation", "Birikim"
            else:
                diverge = obv_chg < -0.5 and abs(price_chg) < 0.5 and (tb is None or tb <= 0.48)
                trigger = C[i] < (hi + lo) / 2 and C[i] < O[i]
                stop = hi + 0.2 * a
                name, text = "distribution", "Dağıtım"
            if not (diverge and trigger):
                continue
            tags, warns = [], list(warn_base)
            if not long:
                warns.append("dağıtım: açık long pozisyonların riskini azalt")
            score = 50 + crowding_adjustment(long, m, price_chg, tags, warns)
            sig = make_signal(ctx, BOT_ID, name + "".join(f"+{t}" for t in tags),
                              Plan(long, C[i], C[i], stop), score,
                              f"{text}: {n} mumdur %{width:.2f} bantta yatay fiyat, OBV {'pozitif' if long else 'negatif'} "
                              f"ayrışıyor" + (f", taker alış payı %{tb * 100:.0f}" if tb is not None else ""),
                              {**m, "range_high": hi, "range_low": lo, "obv_slope": round(obv_chg, 2)}, warns, "YATAY")
            if sig:
                res.append(sig)
        return res
