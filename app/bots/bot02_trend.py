"""BOT02 – Trend ve momentum botu (şartname bölüm 5).

  A  Trend pullback      : trend yönünde EMA20/EMA50'ye düşük hacimli geri çekilme + hacimli dönüş
  B  Donchian breakout   : önceki 20 kapanmış mumun kanalının kapanışla kırılması (güncel mum hariç)
  C  Supertrend momentum : Supertrend yön değişimi + EMA50 eğimi + ADX/DI + RSI (aşırı uzamamış)
  D  Göreceli güç        : BTC'ye göre performans; tek başına giriş değil, A-C'nin puanını düzeltir
"""

from __future__ import annotations

from app.bots.base import MarketContext, WorkerBot, htf_trend
from app.bots.common import Plan, make_signal, volume_ratio
from app.domain.signals import Signal
from app.indicators import core as I

BOT_ID = "BOT02"
MIN_BARS = 220


class TrendBot(WorkerBot):
    bot_id = BOT_ID

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        if len(ctx.candles) < MIN_BARS:
            return []
        O = [float(c.open) for c in ctx.candles]
        H, L, C, V = ctx.highs, ctx.lows, ctx.closes, ctx.volumes
        i = len(C) - 1
        f = {
            "O": O, "H": H, "L": L, "C": C, "V": V, "i": i,
            "e20": I.ema(C, 20), "e50": I.ema(C, 50), "e200": I.ema(C, 200),
            "atr": I.atr(H, L, C, 14), "rsi": I.rsi(C, 14), "hist": I.macd(C)[2],
        }
        f["adx"], f["pdi"], f["mdi"] = I.adx(H, L, C, 14)
        f["st"], f["st_dir"] = I.supertrend(H, L, C, 10, 3.0)
        f["dc_up"], f["dc_lo"] = I.donchian(H, L, 20)
        if any(f[k][i] is None for k in ("e20", "e50", "e200", "atr", "rsi", "adx", "st", "dc_up")):
            return []
        htf = htf_trend(ctx.htf_candles)
        rs = self._relative_strength(ctx, C, i)
        out = []
        for fn in (self._pullback, self._donchian, self._supertrend):
            sig = fn(ctx, f, htf, rs)
            if sig:
                out.append(sig)
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _relative_strength(ctx: MarketContext, C: list[float], i: int) -> float | None:
        """Kısa ve uzun pencerede coinin BTC'ye göre fazla getirisi (yüzde puan)."""
        btc = ctx.extra.get("btc_closes")
        if not btc or ctx.symbol == "BTCUSDT" or len(btc) < 100 or len(C) < 100:
            return None
        bars_short, bars_long = 16, 96
        def ret(s, n):
            return (s[-1] / s[-1 - n] - 1) * 100
        return (ret(C, bars_short) - ret(btc, bars_short)) + 0.5 * (ret(C, bars_long) - ret(btc, bars_long))

    @staticmethod
    def _score(base: int, long: bool, f: dict, htf: str | None, rs: float | None, vr: float,
               tags: list[str], warns: list[str]) -> int:
        i = f["i"]
        s = base
        if f["adx"][i] >= 25:
            s += 5; tags.append("adx25")
        if htf == ("UP" if long else "DOWN"):
            s += 5
        elif htf == ("DOWN" if long else "UP"):
            s -= 15; warns.append("üst zaman dilimi ters yönde")
        if rs is not None:
            if (rs > 1.0 and long) or (rs < -1.0 and not long):
                s += 5; tags.append("rs")
            elif (rs < -1.0 and long) or (rs > 1.0 and not long):
                s -= 5; warns.append("BTC'ye göre göreceli güç ters")
        if vr >= 2.0:
            s += 5
        return s

    def _pullback(self, ctx, f, htf, rs) -> Signal | None:
        i, C, O, H, L, V = f["i"], f["C"], f["O"], f["H"], f["L"], f["V"]
        e20, e50, e200, atr = f["e20"], f["e50"], f["e200"], f["atr"][i]
        for long in (True, False):
            trend_ok = (C[i] > e200[i] and e20[i] > e50[i] and e50[i] > e50[i - 5]) if long else \
                       (C[i] < e200[i] and e20[i] < e50[i] and e50[i] < e50[i - 5])
            if not trend_ok or f["adx"][i] < 20:
                continue
            if htf == ("DOWN" if long else "UP"):
                continue  # şartname 5.2: 4s/1s trend uyumu şart
            w = range(i - 5, i)  # geri çekilme penceresi (son mum hariç)
            touched = any((L[k] <= e20[k] + 0.1 * atr) if long else (H[k] >= e20[k] - 0.1 * atr) for k in w)
            if not touched:
                continue
            pull_vol = sum(V[k] for k in w) / 5
            base_vol = sum(V[i - 25:i - 5]) / 20
            if base_vol <= 0 or pull_vol >= base_vol:
                continue  # geri çekilmede satış hacmi azalmalı
            reversal = (C[i] > O[i] and C[i] > e20[i] and V[i] > V[i - 1]) if long else \
                       (C[i] < O[i] and C[i] < e20[i] and V[i] > V[i - 1])
            if not reversal:
                continue
            swing = min(L[i - 5:i + 1]) if long else max(H[i - 5:i + 1])
            prev_swing = min(L[i - 25:i - 5]) if long else max(H[i - 25:i - 5])
            if (long and swing <= prev_swing) or (not long and swing >= prev_swing):
                continue  # son higher low / lower high korunmalı
            stop = swing - 0.1 * atr if long else swing + 0.1 * atr
            tags, warns = [], []
            vr = volume_ratio(V, i)
            score = self._score(60, long, f, htf, rs, vr, tags, warns)
            plan = Plan(long, min(e20[i], C[i]), C[i], stop) if long else Plan(long, C[i], max(e20[i], C[i]), stop)
            return make_signal(ctx, BOT_ID, "trend_pullback" + "".join(f"+{t}" for t in tags), plan, score,
                               f"{'Yükseliş' if long else 'Düşüş'} trendinde EMA20'ye düşük hacimli geri çekilme ve "
                               f"hacimli dönüş mumu; ADX {f['adx'][i]:.1f}",
                               {"adx": f["adx"][i], "volume_ratio": round(vr, 2), "pullback_volume_ratio": round(pull_vol / base_vol, 2),
                                "relative_strength": rs, "htf_trend": htf}, warns, "TREND")
        return None

    def _donchian(self, ctx, f, htf, rs) -> Signal | None:
        i, C, V = f["i"], f["C"], f["V"]
        up, lo, atr = f["dc_up"][i], f["dc_lo"][i], f["atr"][i]
        for long in (True, False):
            level = up if long else lo
            crossed = (C[i] > level and C[i - 1] <= f["dc_up"][i - 1]) if long else \
                      (C[i] < level and C[i - 1] >= f["dc_lo"][i - 1])
            if not crossed:
                continue
            vr = volume_ratio(V, i)
            momentum = (f["hist"][i] or 0) > 0 if long else (f["hist"][i] or 0) < 0
            if vr < 1.3 or not momentum:
                continue
            if abs(C[i] - level) > 1.0 * atr:
                continue  # geç kalınmış kırılım
            stop = level - 1.2 * atr if long else level + 1.2 * atr
            tags, warns = [], []
            score = self._score(55, long, f, htf, rs, vr, tags, warns)
            plan = Plan(long, level, C[i], stop) if long else Plan(long, C[i], level, stop)
            return make_signal(ctx, BOT_ID, "donchian_breakout" + "".join(f"+{t}" for t in tags), plan, score,
                               f"20 mumluk Donchian kanalı {'üstünde' if long else 'altında'} kapanış; hacim {vr:.2f}x, "
                               f"MACD histogramı {'pozitif' if long else 'negatif'}",
                               {"channel_level": level, "volume_ratio": round(vr, 2), "relative_strength": rs,
                                "htf_trend": htf}, warns, "TREND")
        return None

    def _supertrend(self, ctx, f, htf, rs) -> Signal | None:
        i, C, V = f["i"], f["C"], f["V"]
        d, line, atr = f["st_dir"], f["st"], f["atr"][i]
        if d[i - 1] is None or d[i] == d[i - 1]:
            return None
        long = d[i] == 1
        e50, e20 = f["e50"], f["e20"]
        slope_ok = e50[i] > e50[i - 5] if long else e50[i] < e50[i - 5]
        di_ok = f["pdi"][i] > f["mdi"][i] if long else f["mdi"][i] > f["pdi"][i]
        r = f["rsi"][i]
        rsi_ok = 50 <= r <= 70 if long else 30 <= r <= 50
        not_late = abs(C[i] - e20[i]) <= 1.5 * atr
        if not (slope_ok and di_ok and f["adx"][i] >= 20 and rsi_ok and not_late):
            return None
        stop = line[i]
        tags, warns = [], []
        vr = volume_ratio(V, i)
        score = self._score(55, long, f, htf, rs, vr, tags, warns)
        plan = Plan(long, C[i], C[i], stop)
        return make_signal(ctx, BOT_ID, "supertrend_momentum" + "".join(f"+{t}" for t in tags), plan, score,
                           f"Supertrend {'yükselişe' if long else 'düşüşe'} döndü; EMA50 eğimi, ADX {f['adx'][i]:.1f} ve "
                           f"RSI {r:.1f} destekliyor",
                           {"supertrend": stop, "rsi": r, "adx": f["adx"][i], "relative_strength": rs,
                            "htf_trend": htf}, warns, "TREND")
