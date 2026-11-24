"""BOT03 – Scalping botu (şartname bölüm 6). 5 dakikalık mumlar, 15 dakikalık yön doğrulaması.

  A  EMA 9/21       : EMA9 > EMA21, fiyat günlük VWAP üstünde, kısa geri çekilme sonrası momentum
  B  VWAP reclaim   : VWAP altından hacimli dönüş, ardından başarılı VWAP retesti
  C  Mikro kırılım  : son 12 mumun dar aralığı (hacim azalarak) hacimli kapanışla kırılır
Güvenlik: spread ≤ %0,05; beklenen hareket (ATR) işlem maliyetinin en az 4 katı; hedef maliyet
sonrası 1:2. Başarısız işlem sonrası bekleme süresi, günlük scalping sınırı ve "beklenen değer
negatifse otomatik pasif" kuralı işlem sonuçlarına ihtiyaç duyar; Aşama 5 ve 8'de ana robot
tarafında uygulanacak.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.bots.base import MarketContext, WorkerBot, htf_trend
from app.bots.common import Plan, make_signal, volume_ratio
from app.domain.signals import Signal
from app.indicators import core as I

BOT_ID = "BOT03"
MIN_BARS = 120
MAX_SPREAD_PCT = 0.05
MIN_ATR_COST_MULT = 4.0


def session_vwap(cs, H, L, C, V) -> list[float | None]:
    """UTC gün başında sıfırlanan VWAP."""
    out, pv, vol, day = [], 0.0, 0.0, None
    for c, h, l, cl, v in zip(cs, H, L, C, V):
        d = datetime.fromtimestamp(c.start_ms / 1000, timezone.utc).date()
        if d != day:
            pv = vol = 0.0
            day = d
        pv += (h + l + cl) / 3 * v
        vol += v
        out.append(pv / vol if vol else None)
    return out


class ScalpingBot(WorkerBot):
    bot_id = BOT_ID
    timeframes = frozenset({"5m"})

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        cs = ctx.candles
        if len(cs) < MIN_BARS:
            return []
        if ctx.spread_pct is not None and ctx.spread_pct > MAX_SPREAD_PCT:
            return []
        O = [float(c.open) for c in cs]
        H, L, C, V = ctx.highs, ctx.lows, ctx.closes, ctx.volumes
        i = len(C) - 1
        atr = I.atr(H, L, C, 14)[i]
        if atr is None:
            return []
        cost = ctx.cost_per_unit(C[i])
        if atr < MIN_ATR_COST_MULT * cost:
            return []  # beklenen hareket maliyetleri karşılamıyor
        f = {"O": O, "H": H, "L": L, "C": C, "V": V, "i": i, "atr": atr,
             "e9": I.ema(C, 9), "e21": I.ema(C, 21), "vwap": session_vwap(cs, H, L, C, V)}
        if f["e21"][i] is None or f["vwap"][i] is None:
            return []
        htf = htf_trend(ctx.htf_candles)
        out = []
        for fn in (self._ema_cross, self._vwap_reclaim, self._micro_breakout):
            sig = fn(ctx, f, htf)
            if sig:
                out.append(sig)
        return out

    @staticmethod
    def _dir_ok(long: bool, htf: str | None) -> bool:
        return htf == ("UP" if long else "DOWN")  # 15 dk yön doğrulaması zorunlu

    def _ema_cross(self, ctx, f, htf):
        i, C, H, L, O, V, a = f["i"], f["C"], f["H"], f["L"], f["O"], f["V"], f["atr"]
        e9, e21, vw = f["e9"], f["e21"], f["vwap"]
        vr = volume_ratio(V, i)
        for long in (True, False):
            if not self._dir_ok(long, htf) or vr < 1.2:
                continue
            if long:
                ok = (e9[i] > e21[i] and C[i] > vw[i] and min(L[i - 3:i]) <= e9[i - 1]
                      and C[i] > max(H[i - 3:i]) and C[i] > O[i])
                stop = min(L[i - 5:i + 1]) - 0.1 * a
            else:
                ok = (e9[i] < e21[i] and C[i] < vw[i] and max(H[i - 3:i]) >= e9[i - 1]
                      and C[i] < min(L[i - 3:i]) and C[i] < O[i])
                stop = max(H[i - 5:i + 1]) + 0.1 * a
            if not ok:
                continue
            return make_signal(ctx, BOT_ID, "ema9_21_scalp", Plan(long, C[i], C[i], stop), 55,
                               f"EMA9/21 {'yukarı' if long else 'aşağı'} dizilimde geri çekilme sonrası momentum; "
                               f"VWAP {'üstü' if long else 'altı'}, 15 dk yön uyumlu, hacim {vr:.2f}x",
                               {"vwap": vw[i], "volume_ratio": round(vr, 2), "htf_trend": htf}, [], "SCALP", valid_bars=1)
        return None

    def _vwap_reclaim(self, ctx, f, htf):
        i, C, H, L, O, V, a, vw = f["i"], f["C"], f["H"], f["L"], f["O"], f["V"], f["atr"], f["vwap"]
        for long in (True, False):
            if not self._dir_ok(long, htf):
                continue
            # 1-3 mum önce hacimli geri alma, bu mumda retest ve seviyenin korunması
            for j in range(i - 3, i):
                if vw[j] is None or vw[j - 1] is None:
                    continue
                if long:
                    reclaimed = C[j - 1] < vw[j - 1] and C[j] > vw[j] and volume_ratio(V, j) >= 1.5
                    held = all(C[k] > vw[k] for k in range(j, i + 1))
                    retest = L[i] <= vw[i] + 0.1 * a and C[i] > vw[i] and C[i] > O[i]
                    stop = min(L[j:i + 1]) - 0.1 * a
                else:
                    reclaimed = C[j - 1] > vw[j - 1] and C[j] < vw[j] and volume_ratio(V, j) >= 1.5
                    held = all(C[k] < vw[k] for k in range(j, i + 1))
                    retest = H[i] >= vw[i] - 0.1 * a and C[i] < vw[i] and C[i] < O[i]
                    stop = max(H[j:i + 1]) + 0.1 * a
                if reclaimed and held and retest:
                    return make_signal(ctx, BOT_ID, "vwap_reclaim", Plan(long, C[i], C[i], stop), 55,
                                       f"VWAP {'üstüne' if long else 'altına'} hacimli dönüş ve başarılı retest",
                                       {"vwap": vw[i], "reclaim_bar": j - i, "htf_trend": htf}, [], "SCALP")
        return None

    def _micro_breakout(self, ctx, f, htf):
        i, C, H, L, O, V, a = f["i"], f["C"], f["H"], f["L"], f["O"], f["V"], f["atr"]
        w = 12
        hi, lo = max(H[i - w:i]), min(L[i - w:i])
        if hi - lo > 2.5 * a:
            return None  # dar aralık değil
        vol_falling = sum(V[i - 6:i]) < sum(V[i - 12:i - 6])
        vr = volume_ratio(V, i)
        cost = ctx.cost_per_unit(C[i])
        if not vol_falling or vr < 1.5 or (hi - lo) < 3 * cost:
            return None
        for long in (True, False):
            if not self._dir_ok(long, htf):
                continue
            brk = C[i] > hi and C[i] > O[i] if long else C[i] < lo and C[i] < O[i]
            if not brk or abs(C[i] - (hi if long else lo)) > 0.8 * a:
                continue
            stop = (hi + lo) / 2 if long else (hi + lo) / 2
            return make_signal(ctx, BOT_ID, "micro_breakout",
                               Plan(long, hi, C[i], stop) if long else Plan(long, C[i], lo, stop), 55,
                               f"{w} mumluk dar aralık (hacim azalarak) {'yukarı' if long else 'aşağı'} kırıldı, hacim {vr:.2f}x",
                               {"range_high": hi, "range_low": lo, "volume_ratio": round(vr, 2), "htf_trend": htf}, [], "SCALP")
        return None
