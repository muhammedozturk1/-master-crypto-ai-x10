"""BOT07 – Risk ve sermaye koruma botu (şartname bölüm 10). Zorunlu güvenlik katmanı.

Ana robotun işlem kararı bu kontrollerden geçmeden uygulanamaz ve ana robot bunları
geçersiz kılamaz. Yapay zekâ / ana robot risk limitlerini yükseltemez; limitler
app/config.py içindeki kilitli tavanlarla sınırlıdır.

Pozisyon büyüklüğü (10.2):
  risk bütçesi      = güncel özkaynak × işlem risk oranı  (yüksek volatilitede yarısı)
  birim başına risk = |giriş − stop| + birim başına tahmini maliyet (komisyon + spread)
  miktar            = risk bütçesi / birim başına risk
ardından tek pozisyon ve toplam teminat sınırı, kullanılabilir bakiye, borsa minimum miktar,
miktar adımı ve toplam açık risk ile sınırlandırılır. Minimum emir miktarını karşılamak için
risk limiti aşılmaz: miktar minimumun altında kalırsa işlem reddedilir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from app.bots.bot08_portfolio import AccountState
from app.config import RiskLimits

ZERO = Decimal(0)


@dataclass
class TradeRequest:
    symbol: str
    long: bool
    entry: Decimal
    stop: Decimal
    target: Decimal
    cost_pct: Decimal                 # giriş+çıkış komisyonu ve spread, fiyatın yüzdesi
    regime: str | None = None
    qty_step: Decimal | None = None
    min_qty: Decimal | None = None
    min_notional: Decimal | None = None


@dataclass
class RiskVerdict:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    halt: str | None = None           # "SAFE_MODE" / "STOP_TRADING": sistem durumunu değiştir
    qty: Decimal = ZERO
    notional: Decimal = ZERO
    margin: Decimal = ZERO
    leverage: int = 1
    risk_usd: Decimal = ZERO
    risk_budget: Decimal = ZERO
    net_rr: Decimal | None = None

    def to_dict(self) -> dict:
        return {"approved": self.approved, "reasons": self.reasons, "halt": self.halt,
                "qty": str(self.qty), "notional": str(self.notional.quantize(Decimal("0.01"))),
                "margin": str(self.margin.quantize(Decimal("0.01"))), "leverage": self.leverage,
                "risk_usd": str(self.risk_usd.quantize(Decimal("0.0001"))),
                "risk_budget": str(self.risk_budget.quantize(Decimal("0.0001"))),
                "net_rr": str(self.net_rr.quantize(Decimal("0.01"))) if self.net_rr is not None else None}


def _round_down(x: Decimal, step: Decimal | None) -> Decimal:
    if not step or step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.lim = limits

    # --- Hesap düzeyi durdurma kontrolleri (10.4) -------------------------
    def account_blocks(self, acc: AccountState, now: datetime | None = None) -> tuple[list[str], str | None]:
        now = now or datetime.now(timezone.utc)
        lim, out, halt = self.lim, [], None
        if acc.drawdown_pct >= lim.max_drawdown_pct:
            out.append(f"azami düşüş sınırı: %{acc.drawdown_pct:.2f} ≥ %{lim.max_drawdown_pct}; güvenli mod, "
                       "kullanıcı onayı olmadan yeniden başlamaz")
            halt = "SAFE_MODE"
        if acc.day_start_equity > 0 and -acc.day_pnl >= acc.day_start_equity * lim.daily_loss_pct / 100:
            out.append(f"günlük zarar sınırı: {acc.day_pnl:.2f} USD; gün sonuna kadar yeni işlem yok")
        if acc.week_start_equity > 0 and -acc.week_pnl >= acc.week_start_equity * lim.weekly_loss_pct / 100:
            out.append(f"haftalık zarar sınırı: {acc.week_pnl:.2f} USD; risk incelemesi gerekli")
            halt = halt or "STOP_TRADING"
        if acc.consecutive_losses >= lim.consecutive_loss_halt:
            out.append(f"art arda {acc.consecutive_losses} zarar: yeni işlemler durduruldu, risk değerlendirmesi bekleniyor")
            halt = halt or "STOP_TRADING"
        elif acc.consecutive_losses >= lim.consecutive_loss_cooldown and acc.last_loss_at:
            until = acc.last_loss_at + timedelta(minutes=lim.cooldown_minutes)
            if now < until:
                out.append(f"art arda {acc.consecutive_losses} zarar: {until.strftime('%H:%M')} UTC'ye kadar bekleme")
        return out, halt

    # --- İşlem düzeyi kontrol ve boyutlama ---------------------------------
    def evaluate(self, req: TradeRequest, acc: AccountState, now: datetime | None = None) -> RiskVerdict:
        lim = self.lim
        reasons, halt = self.account_blocks(acc, now)
        v = RiskVerdict(approved=False, reasons=reasons, halt=halt, leverage=lim.leverage)
        if acc.open_positions >= lim.max_open_positions:
            reasons.append(f"en fazla {lim.max_open_positions} açık pozisyon")
        if req.symbol in acc.open_symbols:
            reasons.append("bu paritede zaten açık pozisyon var (plansız büyütme yasak)")
        if (req.long and req.stop >= req.entry) or (not req.long and req.stop <= req.entry):
            reasons.append("stop girişin yanlış tarafında")
            return v

        budget = acc.equity * lim.risk_per_trade_pct / 100
        if req.regime == "YUKSEK_VOLATILITE":
            budget /= 2
        v.risk_budget = budget
        cost_unit = req.entry * req.cost_pct / 100
        unit_risk = abs(req.entry - req.stop) + cost_unit
        reward = abs(req.target - req.entry) - cost_unit
        v.net_rr = reward / unit_risk if unit_risk > 0 else ZERO
        if v.net_rr < lim.min_reward_risk:
            reasons.append(f"net ödül/risk {v.net_rr:.2f} < {lim.min_reward_risk}")

        # Tasfiye koruması: stop mesafesi, kaldıraçla tasfiye mesafesinin (~%100/kaldıraç) yarısını
        # geçemez; böylece pozisyon stoptan önce tasfiye olmaz.
        stop_pct = abs(req.entry - req.stop) / req.entry * 100
        liq_pct = Decimal(100) / Decimal(lim.leverage)
        if stop_pct > liq_pct / 2:
            reasons.append(f"stop mesafesi %{stop_pct:.2f}, {lim.leverage}x kaldıraçta tasfiye mesafesinin (%{liq_pct:.1f}) yarısını aşıyor")

        qty = budget / unit_risk if unit_risk > 0 else ZERO
        # Teminat sınırları: tek pozisyon %25, toplam %50, kullanılabilir bakiye
        lev = Decimal(lim.leverage)
        max_margin_single = acc.equity * lim.max_position_capital_pct / 100
        max_margin_total = acc.equity * lim.max_total_capital_pct / 100 - acc.margin_in_use
        max_margin = min(max_margin_single, max_margin_total, acc.available)
        if max_margin <= 0:
            reasons.append("teminat için kullanılabilir sermaye yok")
            max_margin = ZERO
        qty = min(qty, max_margin * lev / req.entry)
        # Toplam açık risk: en fazla eş zamanlı pozisyon × işlem başı risk
        risk_room = acc.equity * lim.risk_per_trade_pct / 100 * lim.max_open_positions - acc.open_risk
        if risk_room <= 0:
            reasons.append("toplam açık risk sınırı dolu")
        else:
            qty = min(qty, risk_room / unit_risk)
        qty = _round_down(qty, req.qty_step)

        if req.min_qty and qty < req.min_qty:
            reasons.append(f"miktar {qty} borsa minimumu {req.min_qty} altında; risk limiti aşılmaz")
        if req.min_notional and qty * req.entry < req.min_notional:
            reasons.append(f"emir tutarı {qty * req.entry:.2f} USD borsa minimumu {req.min_notional} altında")
        if qty <= 0:
            reasons.append("hesaplanan miktar sıfır")

        v.qty = qty
        v.notional = qty * req.entry
        v.margin = v.notional / lev
        v.risk_usd = qty * unit_risk
        v.approved = not reasons
        return v
