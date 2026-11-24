"""BOT01 – Teknik analiz ve formasyon botu (şartname bölüm 4).

  A  EMA + RSI trend devamı   : EMA200 üstü, EMA20>EMA50, EMA50 yükseliyor, RSI 45-65,
                                MACD histogramı artıyor, destekten tepki, hacim doğrulaması
  B  Bollinger mean reversion : yalnız yatay piyasada (ADX<20); bant dışından içeri kapanış,
                                RSI aşırı bölgeden dönüş; hedef orta bant (net 1:2 sağlanmazsa yok)
  C  Destek/direnç kırılımı   : en az 2 temaslı seviyenin hacimli, uzun fitilsiz kapanışla kırılması;
                                bir sonraki seviye hedefe engel olmamalı
  D  Mum formasyonları        : tek başına sinyal üretmez, A-C'nin güven puanını düzeltir
"""

from __future__ import annotations

from app.bots.base import MarketContext, WorkerBot, htf_trend
from app.bots.common import Plan, candle_pattern, cluster_levels, make_signal, pivots, target_for, volume_ratio
from app.domain.signals import Signal
from app.indicators import core as I

BOT_ID = "BOT01"
MIN_BARS = 220


class TechnicalBot(WorkerBot):
    bot_id = BOT_ID

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        if len(ctx.candles) < MIN_BARS:
            return []
        O = [float(c.open) for c in ctx.candles]
        H, L, C, V = ctx.highs, ctx.lows, ctx.closes, ctx.volumes
        i = len(C) - 1
        mid, bup, blo, _ = I.bollinger(C, 20, 2.0)
        f = {"O": O, "H": H, "L": L, "C": C, "V": V, "i": i,
             "e20": I.ema(C, 20), "e50": I.ema(C, 50), "e200": I.ema(C, 200),
             "atr": I.atr(H, L, C, 14), "rsi": I.rsi(C, 14), "hist": I.macd(C)[2],
             "adx": I.adx(H, L, C, 14)[0], "bb_mid": mid, "bb_up": bup, "bb_lo": blo}
        if any(f[k][i] is None or f[k][i - 1] is None for k in ("e200", "atr", "rsi", "hist", "adx", "bb_mid")):
            return []
        htf = htf_trend(ctx.htf_candles)
        pattern, pdir = candle_pattern(O, H, L, C, i)
        out = []
        for fn in (self._ema_rsi, self._mean_reversion, self._sr_breakout):
            sig = fn(ctx, f, htf, pattern, pdir)
            if sig:
                out.append(sig)
        return out

    @staticmethod
    def _adjust(score: int, long: bool, htf: str | None, pattern: str | None, pdir: int,
                tags: list[str], warns: list[str]) -> int:
        if htf == ("UP" if long else "DOWN"):
            score += 5
        elif htf == ("DOWN" if long else "UP"):
            score -= 15; warns.append("üst zaman dilimi ters yönde")
        if pattern and pdir == (1 if long else -1):
            score += 5; tags.append(pattern)
        elif pattern and pdir == (-1 if long else 1):
            score -= 10; warns.append(f"ters mum formasyonu: {pattern}")
        return score

    def _ema_rsi(self, ctx, f, htf, pattern, pdir) -> Signal | None:
        i, C, L, H, V = f["i"], f["C"], f["L"], f["H"], f["V"]
        e20, e50, e200, atr, r, hist = f["e20"], f["e50"], f["e200"], f["atr"][i], f["rsi"][i], f["hist"]
        for long in (True, False):
            if long:
                trend = C[i] > e200[i] and e20[i] > e50[i] and e50[i] > e50[i - 5]
                rsi_ok = 45 <= r <= 65
                mom = hist[i] > hist[i - 1]
                # Son 3 mumda EMA20/EMA50 bölgesine dokunup bu mumda yukarı kapanış (destekten tepki)
                support = (min(L[i - 2:i + 1]) <= e20[i] + 0.1 * atr and C[i] > e20[i]
                           and C[i] > f["O"][i] and C[i] > C[i - 1])
            else:
                trend = C[i] < e200[i] and e20[i] < e50[i] and e50[i] < e50[i - 5]
                rsi_ok = 35 <= r <= 55
                mom = hist[i] < hist[i - 1]
                support = (max(H[i - 2:i + 1]) >= e20[i] - 0.1 * atr and C[i] < e20[i]
                           and C[i] < f["O"][i] and C[i] < C[i - 1])
            vr = volume_ratio(V, i)
            if not (trend and rsi_ok and mom and support and vr >= 1.2):
                continue
            stop = (min(L[i - 2:i + 1]) - 0.2 * atr) if long else (max(H[i - 2:i + 1]) + 0.2 * atr)
            tags, warns = [], []
            score = self._adjust(55, long, htf, pattern, pdir, tags, warns)
            return make_signal(ctx, BOT_ID, "ema_rsi_trend" + "".join(f"+{t}" for t in tags),
                               Plan(long, C[i], C[i], stop), score,
                               f"{'Yükseliş' if long else 'Düşüş'} trendi devam: EMA dizilimi uygun, RSI {r:.1f}, "
                               f"MACD histogramı {'artıyor' if long else 'azalıyor'}, EMA bölgesinden tepki, hacim {vr:.2f}x",
                               {"rsi": r, "volume_ratio": round(vr, 2), "pattern": pattern, "htf_trend": htf},
                               warns, "TREND")
        return None

    def _mean_reversion(self, ctx, f, htf, pattern, pdir) -> Signal | None:
        i, C, O, L, H = f["i"], f["C"], f["O"], f["L"], f["H"]
        if f["adx"][i] >= 20:
            return None  # yalnız yatay piyasa rejimi
        atr, mid, up, lo, rs = f["atr"][i], f["bb_mid"], f["bb_up"], f["bb_lo"], f["rsi"]
        for long in (True, False):
            if long:
                was_out = C[i - 1] < lo[i - 1] or L[i - 1] < lo[i - 1]
                back_in = C[i] > lo[i] and C[i] > O[i]
                oversold = rs[i - 1] < 30 or (rs[i - 1] < 35 and rs[i] > rs[i - 1])
                strong_down = C[i] < f["e200"][i] and f["e50"][i] < f["e50"][i - 10] * 0.98
                ok = was_out and back_in and oversold and not strong_down
                stop = min(L[i - 1], L[i]) - 0.2 * atr
                target = mid[i]
            else:
                was_out = C[i - 1] > up[i - 1] or H[i - 1] > up[i - 1]
                back_in = C[i] < up[i] and C[i] < O[i]
                oversold = rs[i - 1] > 70 or (rs[i - 1] > 65 and rs[i] < rs[i - 1])
                strong_up = C[i] > f["e200"][i] and f["e50"][i] > f["e50"][i - 10] * 1.02
                ok = was_out and back_in and oversold and not strong_up
                stop = max(H[i - 1], H[i]) + 0.2 * atr
                target = mid[i]
            if not ok:
                continue
            # Orta bant 1:2 vermiyorsa, uygun koşulda karşı banda kadar izin ver
            cost = ctx.cost_per_unit(C[i])
            needed = target_for(C[i], stop, cost, long)
            far = up[i] if long else lo[i]
            if (long and needed > target) or (not long and needed < target):
                if (long and needed <= far) or (not long and needed >= far):
                    target = needed
                else:
                    continue
            tags, warns = [], []
            score = self._adjust(55, long, htf, pattern, pdir, tags, warns)
            return make_signal(ctx, BOT_ID, "bollinger_mean_reversion" + "".join(f"+{t}" for t in tags),
                               Plan(long, C[i], C[i], stop, target), score,
                               f"Yatay piyasa (ADX {f['adx'][i]:.1f}); fiyat {'alt' if long else 'üst'} Bollinger bandı "
                               f"dışından içeri kapandı, RSI {rs[i]:.1f}",
                               {"adx": f["adx"][i], "rsi": rs[i], "bb_mid": mid[i], "pattern": pattern, "htf_trend": htf},
                               warns, "YATAY")
        return None

    def _sr_breakout(self, ctx, f, htf, pattern, pdir) -> Signal | None:
        i, C, O, H, L, V = f["i"], f["C"], f["O"], f["H"], f["L"], f["V"]
        atr = f["atr"][i]
        tops, bottoms = pivots(H, L, i, lookback=60, wing=2)
        res = [lv for lv in cluster_levels(tops) if lv[1] >= 2]
        sup = [lv for lv in cluster_levels(bottoms) if lv[1] >= 2]
        vr = volume_ratio(V, i)
        rng = H[i] - L[i] or 1e-12
        for long in (True, False):
            levels = res if long else sup
            broken = [lv for lv in levels if (C[i] > lv[0] >= C[i - 1]) if long] if long else \
                     [lv for lv in levels if C[i] < lv[0] <= C[i - 1]]
            if not broken:
                continue
            level, touches = max(broken, key=lambda x: x[1])
            wick = (H[i] - C[i]) / rng if long else (C[i] - L[i]) / rng
            body_dir = C[i] > O[i] if long else C[i] < O[i]
            if vr < 1.5 or wick > 0.5 or not body_dir or abs(C[i] - level) > 1.0 * atr:
                continue
            stop = level - 0.5 * atr if long else level + 0.5 * atr
            cost = ctx.cost_per_unit(C[i])
            tgt = target_for(C[i], stop, cost, long)
            # Hedefin önünde güçlü bir sonraki seviye var mı?
            nxt = [lv[0] for lv in (res if long else sup) if (C[i] < lv[0] < tgt if long else tgt < lv[0] < C[i])]
            if nxt:
                continue
            tags, warns = [], []
            score = self._adjust(55 + min(touches - 2, 3) * 3, long, htf, pattern, pdir, tags, warns)
            return make_signal(ctx, BOT_ID, "sr_breakout" + "".join(f"+{t}" for t in tags),
                               Plan(long, level, C[i], stop) if long else Plan(long, C[i], level, stop), score,
                               f"{touches} temaslı {'direnç' if long else 'destek'} {level:.6g} hacimli kapanışla kırıldı "
                               f"(hacim {vr:.2f}x)",
                               {"level": level, "touches": touches, "volume_ratio": round(vr, 2), "pattern": pattern,
                                "htf_trend": htf}, warns, None)
        return None
