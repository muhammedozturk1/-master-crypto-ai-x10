"""BOT10 – NR7 volatilite sıkışması ve kırılım botu (şartname bölüm 13).

Uygulanan stratejiler:
  A  Klasik NR7 kırılımı (kapanışla doğrulanmış)
  B  NR7 + EMA trend filtresi           → güven puanını artırır, ters trendde düşürür
  C  NR7 + Inside Bar                   → güven puanını artırır
  D  NR7 + Bollinger/ATR sıkışması      → güven puanını artırır
  E  NR7 kırılım sonrası retest         → ayrı sinyal
Sahte kırılım filtreleri: yalnızca fitil, NR7 aralığına geri kapanış, yetersiz hacim,
aşırı uzama, üst zaman dilimi ters yön, anormal spread.
NR7 sinyali sonraki 3 kapanmış mum boyunca izlenir, sonra geçersiz sayılır.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.bots.base import MarketContext, WorkerBot, htf_trend, px
from app.domain.signals import Action, DataQuality, Signal
from app.indicators import core as I

BOT_ID = "BOT10"


@dataclass
class Nr7Params:
    validity_bars: int = 3
    min_volume_ratio: float = 1.0      # kırılım hacmi / önceki 20 mum ortalaması
    max_extension_atr: float = 1.0     # kırılım seviyesinden en fazla 1 ATR uzaklaşmış olabilir
    stop_buffer_atr: float = 0.1
    target_net_rr: float = 2.05        # maliyetler sonrası hedef ödül/risk (asgari 2,0 + yuvarlama payı)
    max_stop_pct: float = 4.0          # stop mesafesi fiyatın %4'ünden genişse işlem yok
    max_spread_pct: float = 0.10
    retest_tolerance_atr: float = 0.25
    squeeze_bw_pct: float = 20.0       # bant genişliği son 120 mumun en düşük %20'sinde
    squeeze_atr_pct: float = 30.0
    min_bars: int = 60


def is_nr7(ranges: list[float], k: int) -> bool:
    """k. mumun aralığı önceki 6 mumun HER BİRİNDEN küçükse NR7'dir."""
    if k < 6:
        return False
    return all(ranges[k] < ranges[k - j] for j in range(1, 7))


class Nr7Bot(WorkerBot):
    bot_id = BOT_ID

    def __init__(self, params: Nr7Params | None = None):
        self.p = params or Nr7Params()

    # ------------------------------------------------------------------
    def analyze(self, ctx: MarketContext) -> list[Signal]:
        cs = ctx.candles
        n = len(cs)
        if n < self.p.min_bars:
            return []
        H, L, C, V = ctx.highs, ctx.lows, ctx.closes, ctx.volumes
        ranges = [h - l for h, l in zip(H, L)]

        # En yakın NR7 mumunu geçerlilik penceresi içinde ara
        k = next((i for i in range(n - 1, n - 2 - self.p.validity_bars, -1) if is_nr7(ranges, i)), None)
        if k is None:
            return []

        atr = I.atr(H, L, C, 14)
        ema20, ema50, ema200 = I.ema(C, 20), I.ema(C, 50), I.ema(C, 200)
        _, _, _, bw = I.bollinger(C, 20, 2.0)
        last = n - 1
        nr_hi, nr_lo = H[k], L[k]

        base = {
            "nr7_start": cs[k].start_ms, "nr7_high": nr_hi, "nr7_low": nr_lo,
            "nr7_range": ranges[k], "inside_bar": H[k] <= H[k - 1] and L[k] >= L[k - 1],
            "bars_since_nr7": last - k,
            "valid_until_bar": k + self.p.validity_bars,
        }
        trend = self._trend(C, ema20, ema50, ema200, last)
        htf = htf_trend(ctx.htf_candles)
        base.update({"trend": trend, "htf_trend": htf})

        # NR7'den sonraki ilk kapanışla kırılımı bul
        brk, direction = None, None
        for j in range(k + 1, n):
            if C[j] > nr_hi:
                brk, direction = j, "UP"
                break
            if C[j] < nr_lo:
                brk, direction = j, "DOWN"
                break

        if brk is None:
            if last - k < self.p.validity_bars:
                return [self._waiting(ctx, base, k)]
            return []  # süre doldu, sinyal iptal

        base["breakout_bar_start"] = cs[brk].start_ms
        base["breakout"] = direction

        if brk == last:
            sig = self._breakout(ctx, base, k, last, direction, atr, ema20, ema50, ema200, bw, htf, retest=False)
            return [sig] if sig else []

        # Kırılım daha önce olduysa retest ara (strateji E) – kırılımdan sonraki 3 mum içinde
        if last - brk <= self.p.validity_bars:
            sig = self._retest(ctx, base, k, brk, last, direction, atr, ema20, ema50, ema200, bw, htf)
            return [sig] if sig else []
        return []

    # ------------------------------------------------------------------
    @staticmethod
    def _trend(C, e20, e50, e200, i) -> str:
        if e200[i] is None or e50[i] is None or e20[i] is None:
            return "BILINMIYOR"
        if C[i] > e200[i] and e20[i] > e50[i]:
            return "UP"
        if C[i] < e200[i] and e20[i] < e50[i]:
            return "DOWN"
        return "KARISIK"

    def _waiting(self, ctx: MarketContext, base: dict, k: int) -> Signal:
        return Signal(
            bot_id=BOT_ID, strategy_id="nr7_watch", symbol=ctx.symbol, exchange=ctx.exchange,
            timeframe=ctx.timeframe, action=Action.BEKLE, confidence=0,
            valid_for_seconds=self._tf_seconds(ctx) * (self.p.validity_bars - base["bars_since_nr7"]) or 60,
            reason=f"NR7 tespit edildi (aralık {base['nr7_range']:.6g}); "
                   f"{base['nr7_high']:.6g} üstü veya {base['nr7_low']:.6g} altı kapanış bekleniyor",
            details={**base, "status": "KIRILIM_BEKLENIYOR",
                     "dedup_key": f"{BOT_ID}:{ctx.symbol}:{ctx.timeframe}:watch:{base['nr7_start']}"},
        )

    def _breakout(self, ctx, base, k, i, direction, atr, e20, e50, e200, bw, htf, retest: bool) -> Signal | None:
        C, V = ctx.closes, ctx.volumes
        p = self.p
        a = atr[i]
        if a is None or a <= 0:
            return None
        long = direction == "UP"
        if not long and not ctx.allow_short:
            return None  # spot: aşağı kırılım yalnızca risk uyarısıdır

        level = base["nr7_high"] if long else base["nr7_low"]
        far = base["nr7_low"] if long else base["nr7_high"]
        close = C[i]
        warnings: list[str] = []
        reject: list[str] = []

        prior = V[max(0, i - 20):i]
        vol_ratio = V[i] / (sum(prior) / len(prior)) if prior and sum(prior) > 0 else 0.0
        extension = abs(close - level) / a
        if not retest and vol_ratio < p.min_volume_ratio:
            reject.append(f"hacim yetersiz ({vol_ratio:.2f}x)")
        if extension > p.max_extension_atr:
            reject.append(f"fiyat kırılım seviyesinden aşırı uzaklaşmış ({extension:.2f} ATR)")
        if ctx.spread_pct is not None and ctx.spread_pct > p.max_spread_pct:
            reject.append(f"spread anormal (%{ctx.spread_pct:.3f})")

        stop = far - p.stop_buffer_atr * a if long else far + p.stop_buffer_atr * a
        risk = abs(close - stop)
        if risk / close * 100 > p.max_stop_pct:
            reject.append(f"stop mesafesi çok geniş (%{risk / close * 100:.2f})")
        cost = ctx.cost_per_unit(close)
        # Net ödül/risk = (ödül - maliyet) / (risk + maliyet) = hedef  →  ödül = hedef·(risk+maliyet) + maliyet
        reward = p.target_net_rr * (risk + cost) + cost
        tp = close + reward if long else close - reward
        net_rr = (reward - cost) / (risk + cost)

        if reject:
            return None  # sahte kırılım filtresi: sinyal iptal

        # --- Güven puanı -------------------------------------------------
        score, tags = 55, []
        trend = base["trend"]
        if (long and trend == "UP") or (not long and trend == "DOWN"):
            score += 10; tags.append("ema")
        elif (long and trend == "DOWN") or (not long and trend == "UP"):
            score -= 15; warnings.append("ana trende ters kırılım")
        if base["inside_bar"]:
            score += 5; tags.append("ib")
        bw_rank = I.percentile_rank(bw, k, 120)
        atr_rank = I.percentile_rank(atr, k, 120)
        squeeze = bw_rank is not None and atr_rank is not None and bw_rank <= p.squeeze_bw_pct and atr_rank <= p.squeeze_atr_pct
        if squeeze:
            score += 10; tags.append("squeeze")
        if vol_ratio >= 2.0:
            score += 10
        elif vol_ratio >= 1.5:
            score += 5
        if htf == ("UP" if long else "DOWN"):
            score += 5
        elif htf == ("DOWN" if long else "UP"):
            score -= 15; warnings.append("üst zaman dilimi ters yönde")
        if retest:
            score += 5
        score = max(0, min(100, score))

        kind = "nr7_retest" if retest else "nr7_breakout"
        strategy = kind + "".join(f"+{t}" for t in tags)
        action = Action.AL if long else Action.SAT
        entry_lo, entry_hi = (min(level, close), max(level, close))
        details = {
            **base, "status": "RETEST" if retest else "KIRILIM", "volume_ratio": round(vol_ratio, 2),
            "extension_atr": round(extension, 2), "atr": a, "bw_rank": bw_rank, "atr_rank": atr_rank,
            "squeeze": squeeze, "cost_per_unit": cost, "gross_rr": round((reward) / risk, 2),
            "dedup_key": f"{BOT_ID}:{ctx.symbol}:{ctx.timeframe}:{kind}:{base['nr7_start']}:{action.value}",
        }
        reason = (f"{'Yukarı' if long else 'Aşağı'} NR7 {'retest' if retest else 'kırılımı'} kapanışla doğrulandı; "
                  f"hacim {vol_ratio:.2f}x, trend {trend}, üst ZD {htf or 'bilinmiyor'}"
                  + (", Bollinger/ATR sıkışması" if squeeze else "")
                  + (", inside bar" if base["inside_bar"] else ""))
        return Signal(
            bot_id=BOT_ID, strategy_id=strategy, symbol=ctx.symbol, exchange=ctx.exchange,
            timeframe=ctx.timeframe, action=action, confidence=score,
            entry_low=px(entry_lo), entry_high=px(entry_hi), stop_loss=px(stop), take_profit=px(tp),
            expected_net_rr=px(net_rr), estimated_cost_pct=px(cost / close * 100),
            market_regime="VOLATILITE_SIKISMASI" if squeeze else None,
            valid_for_seconds=self._tf_seconds(ctx), reason=reason,
            data_quality=DataQuality.IYI, risk_warnings=warnings, details=details,
        )

    def _retest(self, ctx, base, k, brk, i, direction, atr, e20, e50, e200, bw, htf) -> Signal | None:
        C, H, L, O = ctx.closes, ctx.highs, ctx.lows, [float(c.open) for c in ctx.candles]
        a = atr[i]
        if a is None:
            return None
        long = direction == "UP"
        level = base["nr7_high"] if long else base["nr7_low"]
        tol = self.p.retest_tolerance_atr * a
        # Kırılımla son mum arasında seviye kaybedilmemiş olmalı
        between = range(brk + 1, i)
        if long:
            if any(C[j] < level for j in between):
                return None
            touched = L[i] <= level + tol
            held = C[i] > level and C[i] > O[i]
        else:
            if any(C[j] > level for j in between):
                return None
            touched = H[i] >= level - tol
            held = C[i] < level and C[i] < O[i]
        if not (touched and held):
            return None
        return self._breakout(ctx, base, k, i, direction, atr, e20, e50, e200, bw, htf, retest=True)

    @staticmethod
    def _tf_seconds(ctx: MarketContext) -> int:
        from app.data.candles import INTERVAL_MINUTES
        return INTERVAL_MINUTES[ctx.candles[-1].interval] * 60
