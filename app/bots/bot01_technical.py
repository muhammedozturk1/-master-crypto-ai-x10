"""BOT01 – Teknik analiz ve formasyon botu (şartname bölüm 4).

  A  EMA + RSI trend devamı   : EMA200 üstü, EMA20>EMA50, EMA50 yükseliyor, RSI 45-65,
                                MACD histogramı artıyor, destekten tepki, hacim doğrulaması
  B  Footprint emilim/tükenme + hacim profili (VAH/VAL/POC) — Bollinger dönüşünün yerine
                                (backtest'te en zayıf stratejiydi: 15m −0,45R, 1h −0,47R):
                                fiyat değer alanı kenarını (VAL/VAH) test eder; uçta agresif hacim
                                emilir veya uçta hacim tükenir; kapanış değer alanı içinde → hedef POC.
                                Gerçek footprint Bybit websocket işlem akışından gelir; mum tam
                                dinlenmediyse (ve backtest'te) mum yapısından TAHMİN kullanılır ve
                                sinyalde yazılır. ADX≥25 güçlü trendde çalışmaz.
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
STRATEGY = "footprint_absorption"
VP_BARS = 96          # hacim profili penceresi: 15m'de 24 saat, 1h'de 4 gün


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
        for fn in (self._ema_rsi, self._footprint, self._sr_breakout):
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

    def _footprint(self, ctx, f, htf, pattern, pdir) -> Signal | None:
        """Hacim profili kenarında (VAL/VAH) emilim veya tükenme → POC'a dönüş."""
        i, C, O, L, H, V = f["i"], f["C"], f["O"], f["L"], f["H"], f["V"]
        if f["adx"][i] >= 25:
            return None  # güçlü trendde değer alanı kenarından dönüş işlemi yok
        vp = I.volume_profile(H, L, V, i - VP_BARS, i)
        if vp is None:
            return None
        poc, vah, val = vp
        atr, rng = f["atr"][i], (H[i] - L[i]) or 1e-12
        fp = ctx.extra.get("footprint")
        if fp and not fp.get("levels"):
            fp = None
        est = [V[k] * ((C[k] - L[k]) - (H[k] - C[k])) / ((H[k] - L[k]) or 1e-12) for k in range(i - 5, i + 1)]
        vr = volume_ratio(V, i)
        for long in (True, False):
            if long:
                at_edge = L[i] <= val + 0.15 * atr and val <= C[i] < poc
                strong_against = C[i] < f["e200"][i] and f["e50"][i] < f["e50"][i - 10] * 0.98
                new_extreme = L[i] <= min(L[i - 5:i])
                close_pos = (C[i] - L[i]) / rng
            else:
                at_edge = H[i] >= vah - 0.15 * atr and poc < C[i] <= vah
                strong_against = C[i] > f["e200"][i] and f["e50"][i] > f["e50"][i - 10] * 1.02
                new_extreme = H[i] >= max(H[i - 5:i])
                close_pos = (H[i] - C[i]) / rng
            if not at_edge or strong_against:
                continue
            if fp:
                absorption, exhaustion, fpd = self._fp_read(fp, long, L[i], H[i], close_pos)
                source = "gerçek footprint (Bybit işlem akışı)"
            else:
                # Tahmini: hacimli, uzun fitilli, kapanışı ters tarafta mum (emilim); yeni uç ama
                # tahmini delta bir önceki uca göre zayıf (tükenme)
                absorption = vr >= 1.5 and close_pos >= 0.6
                prev = est[:-1]
                exhaustion = new_extreme and close_pos >= 0.5 and (
                    est[-1] > min(prev) if long else est[-1] < max(prev))
                fpd = {"tahmini_delta": round(est[-1], 4)}
                source = "tahmini (mum yapısından; işlem akışı yok)"
            if not (absorption or exhaustion):
                continue
            stop = L[i] - 0.25 * atr if long else H[i] + 0.25 * atr
            cost = ctx.cost_per_unit(C[i])
            needed = target_for(C[i], stop, cost, long)
            target = poc
            if (long and needed > poc) or (not long and needed < poc):
                if (long and needed <= vah) or (not long and needed >= val):
                    target = needed
                else:
                    continue
            tags, warns = [], []
            if absorption:
                tags.append("emilim")
            if exhaustion:
                tags.append("tukenme")
            score = 55 + (8 if absorption and exhaustion else 0) + (7 if fp else 0)
            score = self._adjust(score, long, htf, pattern, pdir, tags, warns)
            if not fp:
                warns.append("footprint tahmini: mum boyunca işlem akışı dinlenmedi")
            edge = "VAL" if long else "VAH"
            what = " ve ".join(x for x, ok in (("emilim", absorption), ("tükenme", exhaustion)) if ok)
            return make_signal(ctx, BOT_ID, STRATEGY + "".join(f"+{t}" for t in tags),
                               Plan(long, C[i], C[i], stop, target), score,
                               f"Fiyat değer alanı {'altı' if long else 'üstü'} ({edge} {val if long else vah:.6g}) test edildi, "
                               f"{what} görüldü; hedef POC {poc:.6g}. Kaynak: {source}",
                               {"poc": poc, "vah": vah, "val": val, "absorption": absorption, "exhaustion": exhaustion,
                                "footprint_source": "gercek" if fp else "tahmini", "volume_ratio": round(vr, 2),
                                "pattern": pattern, "htf_trend": htf, **fpd}, warns, "YATAY")
        return None

    @staticmethod
    def _fp_read(fp: dict, long: bool, low: float, high: float, close_pos: float) -> tuple[bool, bool, dict]:
        """Gerçek footprint okuması.
        Emilim : mumun uç %30'luk bölgesinde agresif (ters yönlü) hacim karşı tarafın 1,5 katı,
                 buna rağmen fiyat o bölgede kalmadı (kapanış mumun öbür yarısında).
        Tükenme: en uç fiyat seviyesindeki hacim, seviye başına ortalamanın %35'inden az
                 (agresif taraf uca vardığında alıcı/satıcı kalmamış)."""
        lv = fp["levels"]
        rng = (high - low) or 1e-12
        zone = [x for x in lv if (x[0] - low) / rng <= 0.30] if long else [x for x in lv if (high - x[0]) / rng <= 0.30]
        aggressive = sum(x[2] if long else x[1] for x in zone)       # longda: dipte satış baskısı
        passive_side = sum(x[1] if long else x[2] for x in zone)
        absorption = bool(zone) and aggressive >= 1.5 * max(passive_side, 1e-12) and close_pos >= 0.5
        extreme = lv[0] if long else lv[-1]
        avg = fp["volume"] / len(lv) if lv else 0
        exhaustion = len(lv) >= 4 and (extreme[1] + extreme[2]) <= 0.35 * avg
        return absorption, exhaustion, {"delta_usdt": round(fp["delta"], 2), "uc_bolge_agresif": round(aggressive, 2),
                                        "uc_bolge_karsi": round(passive_side, 2), "fp_seviye": len(lv)}

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
