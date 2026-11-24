"""Akıllı işlem kâr koruma kilidi – PROFIT LOCK ENGINE (şartname bölüm 14).

Kasa bazlı DEĞİLDİR, her açık pozisyon için ayrı çalışır. Net kâr; brüt fiyat kazancından
gerçekleşen giriş komisyonu ve tahmini çıkış komisyonu düşülerek, pozisyon tutarına (giriş
fiyatı × miktar) oranla yüzde olarak hesaplanır. Spread ve kayma ayrıca düşülmez: çıkış
komisyonu, gerçekleşebilir fiyat (mark fiyat) üzerinden hesaplanır; aynı maliyet iki kez
sayılmaz.

Durum makinesi (14.4):
  0 INITIAL_PROTECTION  başlangıç stopu
  1 BREAK_EVEN          zirve net ≥ %0,5 → stop maliyetler dahil başabaşa
  2 PROFIT_LOCK         zirve net ≥ %1   → kademeli kâr kilidi
  3 TRAILING_PROFIT     zirve net ≥ %2   → ATR trailing de devrede
  4 EXIT_PENDING        fiyat kilit seviyesini zaten aşmış → güvenli çıkış
  5 CLOSED              pozisyon kapandı
  6 ERROR_RECOVERY      emir/bağlantı/veri hatası

Birleşik stop (14.6), long: MAX(önceki stop, başlangıç stopu, etkin kilit stopu, etkin ATR stopu);
short için MIN. Etkin olmayan bileşenler dahil edilmez. Değişmez kurallar (14.9): kilit geri
çekilmez, başlangıç stopu genişletilmez, stop pozisyon miktarını aşamaz (tam pozisyon stopu).
Tablo değerleri başlangıç konfigürasyonudur; backtest ile doğrulanacaktır.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.config import DEFAULT_PROFIT_LOCK_TABLE, PROFIT_LOCK_ABOVE_10_RATIO, ProfitLockStep

ZERO = Decimal(0)
HUNDRED = Decimal(100)

INITIAL, BREAK_EVEN, PROFIT_LOCK, TRAILING, EXIT_PENDING, CLOSED, ERROR = range(7)
STATE_NAMES = {0: "INITIAL_PROTECTION", 1: "BREAK_EVEN", 2: "PROFIT_LOCK", 3: "TRAILING_PROFIT",
               4: "EXIT_PENDING", 5: "CLOSED", 6: "ERROR_RECOVERY"}

# 14.5 – başlangıç test aralıklarının ortası; kesin optimum değildir
ATR_MULT = {"scalp": Decimal("1.25"), "nr7": Decimal("1.75"), "technical": Decimal("2.0"), "trend": Decimal("2.5")}
TRAILING_FROM_PCT = Decimal("2")
MIN_STOP_DISTANCE_PCT = Decimal("0.10")  # stop güncel fiyata en az bu kadar uzak olmalı


def strategy_family(strategy_id: str) -> str:
    s = (strategy_id or "").split("+")[0]
    if s in ("ema9_21_scalp", "vwap_reclaim", "micro_breakout"):
        return "scalp"
    if s.startswith("nr7"):
        return "nr7"
    if s in ("trend_pullback", "donchian_breakout", "supertrend_momentum"):
        return "trend"
    return "technical"


@dataclass
class PositionView:
    long: bool
    qty: Decimal
    entry: Decimal
    entry_fee: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    peak_net_pct: Decimal
    high_water: Decimal          # long: girişten beri en yüksek, short: en düşük fiyat
    state: int
    strategy_id: str
    fee_pct: Decimal             # tek yön taker (%)
    tick: Decimal | None = None


@dataclass
class LockDecision:
    state: int
    peak_net_pct: Decimal
    high_water: Decimal
    net_pct: Decimal
    new_stop: Decimal | None            # None: değişiklik yok
    exit_now: bool = False
    components: dict = field(default_factory=dict)
    protected_net_usd: Decimal | None = None
    reason: str = ""


def net_pnl(p: PositionView, price: Decimal) -> Decimal:
    d = 1 if p.long else -1
    return (price - p.entry) * p.qty * d - p.entry_fee - p.qty * price * p.fee_pct / HUNDRED


def net_pct(p: PositionView, price: Decimal) -> Decimal:
    # 9 basamak: eşik karşılaştırmalarında ondalık bölme artığı (1,999...98) kademeyi kaçırmasın
    return (net_pnl(p, price) / (p.qty * p.entry) * HUNDRED).quantize(Decimal("1e-9"))


def price_for_net_pct(p: PositionView, target_pct: Decimal) -> Decimal:
    """Net kârın target_pct olacağı fiyat (giriş komisyonu ve çıkış komisyonu dahil)."""
    f = p.fee_pct / HUNDRED
    t = target_pct / HUNDRED * p.entry
    fe_unit = p.entry_fee / p.qty
    if p.long:
        return (t + fe_unit + p.entry) / (1 - f)
    return (p.entry - fe_unit - t) / (1 + f)


def lock_pct(peak_pct: Decimal, table: list[ProfitLockStep] = DEFAULT_PROFIT_LOCK_TABLE) -> Decimal | None:
    """Zirve net kâra karşılık korunacak net kâr yüzdesi (14.3). Eşik altında None."""
    if peak_pct > Decimal("10"):
        return peak_pct * PROFIT_LOCK_ABOVE_10_RATIO
    best = None
    for step in table:
        if peak_pct >= step.peak_pct:
            best = step.lock_pct
    return best


def _round_stop(x: Decimal, tick: Decimal | None, long: bool) -> Decimal:
    if not tick or tick <= 0:
        return x.quantize(Decimal("1e-8"))
    mode = ROUND_FLOOR if long else ROUND_CEILING   # stop fiyatı yuvarlanırken hedeflenen kilidin altına inilmez değil, gerçekleşebilir tarafa
    return (x / tick).to_integral_value(rounding=mode) * tick


def evaluate(p: PositionView, price: Decimal, atr: Decimal | None) -> LockDecision:
    if p.state in (CLOSED,):
        return LockDecision(p.state, p.peak_net_pct, p.high_water, ZERO, None, reason="kapalı")
    hw = max(p.high_water, price) if p.long else min(p.high_water, price)
    now_pct = net_pct(p, price)
    peak = max(p.peak_net_pct, now_pct)

    # Durum: zirveye göre (geri gitmez)
    state = p.state if p.state not in (ERROR, EXIT_PENDING) else max(INITIAL, min(p.state, TRAILING))
    if peak >= TRAILING_FROM_PCT:
        state = max(state, TRAILING)
    elif peak >= Decimal("1"):
        state = max(state, PROFIT_LOCK)
    elif peak >= Decimal("0.5"):
        state = max(state, BREAK_EVEN)

    comps: dict[str, Decimal] = {"previous": p.current_stop, "initial": p.initial_stop}
    lp = lock_pct(peak)
    protected = None
    if lp is not None and state >= BREAK_EVEN:
        comps["profit_lock"] = price_for_net_pct(p, lp)
        protected = lp / HUNDRED * p.qty * p.entry
    if state >= TRAILING and atr and atr > 0:
        mult = ATR_MULT[strategy_family(p.strategy_id)]
        comps["atr_trailing"] = hw - atr * mult if p.long else hw + atr * mult

    target = max(comps.values()) if p.long else min(comps.values())
    target = _round_stop(target, p.tick, p.long)
    # Yuvarlama sonrası önceki stopun gerisine düşmesin (kilit geri çekilmez)
    if (p.long and target < p.current_stop) or (not p.long and target > p.current_stop):
        target = p.current_stop

    # Gerçekleşebilirlik: stop fiyata çok yakın veya fiyat kilidi zaten aşmışsa güvenli çıkış
    min_gap = price * MIN_STOP_DISTANCE_PCT / HUNDRED
    breached = (p.long and price - target < min_gap) or (not p.long and target - price < min_gap)
    if breached and target != p.current_stop:
        return LockDecision(EXIT_PENDING, peak, hw, now_pct, None, exit_now=True,
                            components={k: str(v) for k, v in comps.items()}, protected_net_usd=protected,
                            reason=f"fiyat {price} kilit seviyesine {target} ulaştı/aştı: güvenli çıkış")
    new = target if target != p.current_stop else None
    reason = f"{STATE_NAMES[state]}: zirve net %{peak:.2f}, kilit %{lp if lp is not None else 0:.2f}"
    return LockDecision(state, peak, hw, now_pct, new, components={k: str(v) for k, v in comps.items()},
                        protected_net_usd=protected, reason=reason)
