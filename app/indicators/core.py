"""Teknik indikatörler.

Tüm fonksiyonlar eskiden yeniye sıralı float listeleri alır ve girdiyle aynı
uzunlukta liste döndürür. Hesaplanamayan baştaki değerler None'dır; böylece
bir indikatörün "ısınma" dönemi asla sahte değerle doldurulmaz.
Yumuşatmalar (RSI, ATR, ADX) Wilder yöntemini kullanır.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def _check(period: int) -> None:
    if period < 1:
        raise ValueError("period en az 1 olmalı")


def sma(values: Sequence[float], period: int) -> Series:
    _check(period)
    out: Series = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """İlk değer SMA ile tohumlanır (yaygın borsa grafiği uygulaması)."""
    _check(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], period: int, start: int = 0) -> Series:
    """values[start:] üzerinde Wilder yumuşatması (RMA)."""
    out: Series = [None] * len(values)
    if len(values) - start < period:
        return out
    prev = sum(values[start:start + period]) / period
    out[start + period - 1] = prev
    for i in range(start + period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    _check(period)
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    ag, al = _wilder(gains, period, 1), _wilder(losses, period, 1)
    for i in range(n):
        if ag[i] is None:
            continue
        if al[i] == 0:
            out[i] = 100.0 if ag[i] > 0 else 50.0
        else:
            out[i] = 100 - 100 / (1 + ag[i] / al[i])
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        pc = closes[i - 1]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    _check(period)
    if not closes:
        return []
    return _wilder(true_range(highs, lows, closes), period)


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> tuple[Series, Series, Series]:
    """(ADX, +DI, -DI) döndürür."""
    _check(period)
    n = len(closes)
    none: Series = [None] * n
    if n < 2 * period + 1:
        return none, none[:], none[:]
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = true_range(highs, lows, closes)
    s_tr, s_p, s_m = _wilder(tr, period, 1), _wilder(plus_dm, period, 1), _wilder(minus_dm, period, 1)
    pdi: Series = [None] * n
    mdi: Series = [None] * n
    dx = [0.0] * n
    first = None
    for i in range(n):
        if s_tr[i] is None:
            continue
        first = i if first is None else first
        if s_tr[i] == 0:
            pdi[i] = mdi[i] = 0.0
        else:
            pdi[i] = 100 * s_p[i] / s_tr[i]
            mdi[i] = 100 * s_m[i] / s_tr[i]
        tot = pdi[i] + mdi[i]
        dx[i] = 0.0 if tot == 0 else 100 * abs(pdi[i] - mdi[i]) / tot
    adx_s = _wilder(dx, period, first) if first is not None else none
    return adx_s, pdi, mdi


def stdev(values: Sequence[float], period: int) -> Series:
    _check(period)
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        m = sum(w) / period
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / period)  # popülasyon (Bollinger standardı)
    return out


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> tuple[Series, Series, Series, Series]:
    """(orta, üst, alt, bant genişliği) – genişlik = (üst - alt) / orta."""
    mid, sd = sma(closes, period), stdev(closes, period)
    up: Series = [None] * len(closes)
    lo: Series = [None] * len(closes)
    width: Series = [None] * len(closes)
    for i in range(len(closes)):
        if mid[i] is None:
            continue
        up[i] = mid[i] + mult * sd[i]
        lo[i] = mid[i] - mult * sd[i]
        width[i] = (up[i] - lo[i]) / mid[i] if mid[i] else None
    return mid, up, lo, width


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    """(MACD, sinyal, histogram)."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if start is not None:
        tail = ema([v for v in line[start:]], signal)  # type: ignore[misc]
        for j, v in enumerate(tail):
            sig[start + j] = v
    hist: Series = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], volumes: Sequence[float]) -> Series:
    """Serinin başından itibaren birikimli VWAP (tipik fiyat ile)."""
    out: Series = []
    pv = vol = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        pv += (h + l + c) / 3 * v
        vol += v
        out.append(pv / vol if vol else None)
    return out


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        d = 1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0
        out.append(out[-1] + d * volumes[i])
    return out


def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20) -> tuple[Series, Series]:
    """(üst, alt) – i. değer, i. mumu DAHİL ETMEDEN önceki `period` kapanmış mumdan hesaplanır
    (şartname 5.3: güncel sinyal mumu kanal hesabına dahil edilmez)."""
    _check(period)
    up: Series = [None] * len(highs)
    lo: Series = [None] * len(highs)
    for i in range(period, len(highs)):
        up[i] = max(highs[i - period:i])
        lo[i] = min(lows[i - period:i])
    return up, lo


def percentile_rank(values: Sequence[float | None], i: int, lookback: int) -> float | None:
    """values[i]'nin önceki `lookback` değer içindeki yüzdelik sırası (0-100).
    Eşit değerler yarım sayılır (orta sıra): sabit bir seri 50 verir, 0 değil."""
    window = [v for v in values[max(0, i - lookback):i] if v is not None]
    cur = values[i]
    if cur is None or len(window) < lookback // 2:
        return None
    eps = 1e-12 * max(1.0, abs(cur))
    less = sum(1 for v in window if v < cur - eps)
    equal = sum(1 for v in window if abs(v - cur) <= eps)
    return 100 * (less + 0.5 * equal) / len(window)


def slope(values: Series, i: int, bars: int = 5) -> float | None:
    """Son `bars` mumdaki yüzde değişim."""
    if i - bars < 0 or values[i] is None or values[i - bars] is None or values[i - bars] == 0:
        return None
    return (values[i] - values[i - bars]) / values[i - bars] * 100


def supertrend(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int = 10, mult: float = 3.0) -> tuple[Series, list[int | None]]:
    """(Supertrend çizgisi, yön) – yön +1 yükseliş, -1 düşüş. Standart nihai bant yöntemi."""
    n = len(closes)
    a = atr(highs, lows, closes, period)
    line: Series = [None] * n
    dirn: list[int | None] = [None] * n
    fu = fl = None
    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu is None:
            fu, fl, d = bu, bl, 1 if closes[i] >= hl2 else -1
        else:
            pc = closes[i - 1]
            fu = bu if (bu < fu or pc > fu) else fu
            fl = bl if (bl > fl or pc < fl) else fl
            prev = dirn[i - 1]
            if prev == -1 and closes[i] > fu:
                d = 1
            elif prev == 1 and closes[i] < fl:
                d = -1
            else:
                d = prev
        dirn[i] = d
        line[i] = fl if d == 1 else fu
    return line, dirn


def stoch_rsi(closes: Sequence[float], period: int = 14, k: int = 3) -> Series:
    r = rsi(closes, period)
    raw: Series = [None] * len(closes)
    for i in range(len(closes)):
        w = [x for x in r[max(0, i - period + 1):i + 1] if x is not None]
        if len(w) == period and r[i] is not None:
            lo, hi = min(w), max(w)
            raw[i] = 0.0 if hi == lo else 100 * (r[i] - lo) / (hi - lo)
    start = next((i for i, v in enumerate(raw) if v is not None), None)
    out: Series = [None] * len(closes)
    if start is not None:
        sm = sma([v for v in raw[start:]], k)  # type: ignore[misc]
        for j, v in enumerate(sm):
            out[start + j] = v
    return out
