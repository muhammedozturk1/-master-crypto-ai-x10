"""Ana robot sinyal birleştirme ve karar motoru (şartname 15 ve 17).

100 puanlık başlangıç puanlaması (15.1):
  teknik analiz 20 · trend ve momentum 20 · hacim ve likidite 15 · strateji/rejim uyumu 15
  haber duyarlılığı 10 · akış/on-chain 10 · geçmiş strateji performansı 10
Kategori içinde benzer sinyaller TOPLANMAZ, en güçlüsü alınır (aynı veriden türeyen
indikatörlerin bağımsız kanıt gibi tekrar sayılmasını önler).

Karar seviyeleri (15.2): 0-49 red · 50-64 izleme · 65-74 ek doğrulama · 75-84 risk
değerlendirmesi · 85-100 yüksek öncelik. 75 üstü tek başına işlem açmak için yeterli
değildir: risk katmanı (Aşama 5) onaylamadan emir oluşmaz.
Güven puanı kazanma olasılığı DEĞİLDİR; kalibrasyon Aşama 8'de yapılacak.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.bots.base import MarketContext
from app.domain.signals import Action, DataQuality, Signal
from app.master_robot.market_regime import NO_NEW_TRADES, RegimeResult, detect_regime
from app.master_robot.strategy_selector import base_strategy, strategy_weight

MODEL_VERSION = "kural-v1"

CATEGORY = {"BOT01": "teknik", "BOT03": "teknik", "BOT10": "teknik", "BOT02": "trend", "BOT06": "akis"}
CAPS = {"teknik": 20, "trend": 20, "hacim_likidite": 15, "rejim_uyumu": 15, "haber": 10, "akis": 10, "performans": 10}

OUTCOMES = [(85, "YUKSEK_ONCELIK"), (75, "RISK_KONTROLU"), (65, "DOGRULAMA"), (50, "IZLEME"), (0, "REDDEDILDI")]
RISK_THRESHOLD_RANGE = (65, 85)   # ayarlanabilir risk eşiği bu aralığın dışına çıkamaz


def outcome_for(score: float, risk_threshold: float = 75) -> str:
    """Puan → karar seviyesi. Risk değerlendirmesine gönderme eşiği ayarlanabilir (varsayılan 75)."""
    th = min(max(risk_threshold, RISK_THRESHOLD_RANGE[0]), RISK_THRESHOLD_RANGE[1])
    if score >= 85:
        return "YUKSEK_ONCELIK"
    if score >= th:
        return "RISK_KONTROLU"
    if score >= 65:
        return "DOGRULAMA"
    return "IZLEME" if score >= 50 else "REDDEDILDI"


@dataclass
class MarketInputs:
    news_veto: bool = False
    veto_reason: str | None = None
    sentiment: int | None = None               # -100..+100
    bot_weights: dict[str, float] = field(default_factory=dict)
    trading_enabled: bool = True
    safe_mode: bool = False
    risk_threshold: float = 75                 # bu puan ve üstü BOT07 risk değerlendirmesine gider


@dataclass
class Decision:
    symbol: str
    timeframe: str
    bar_start: int
    outcome: str
    score: int
    direction: str | None
    regime: RegimeResult
    reason: str
    contributions: dict
    plan: dict | None
    signal_keys: list[str]
    strategy: str | None = None
    model_version: str = MODEL_VERSION

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.timeframe}:{self.bar_start}"


def _eff_conf(s: Signal, weights: dict[str, float]) -> float:
    q = 1.0 if s.data_quality == DataQuality.IYI else 0.7   # eksik/eski veri güveni düşürür
    return max(0.0, min(1.0, s.confidence / 100 * weights.get(s.bot_id, 1.0) * q))


def score_direction(long: bool, sigs: list[Signal], regime: RegimeResult, spread: float | None,
                    mi: MarketInputs) -> tuple[float, dict, Signal | None]:
    act = Action.AL if long else Action.SAT
    mine = [s for s in sigs if s.action == act]
    c: dict[str, float] = {}

    def best_in(cat: str) -> Signal | None:
        xs = [s for s in mine if CATEGORY.get(s.bot_id) == cat]
        return max(xs, key=lambda s: _eff_conf(s, mi.bot_weights)) if xs else None

    t = best_in("teknik")
    c["teknik"] = CAPS["teknik"] * _eff_conf(t, mi.bot_weights) if t else 0.0
    tr = best_in("trend")
    aligned = regime.direction == (1 if long else -1)
    c["trend"] = max(CAPS["trend"] * _eff_conf(tr, mi.bot_weights) if tr else 0.0, 10.0 if aligned else 0.0)

    # Hacim/likidite: spread kalitesi + işlem sinyalinin hacim teyidi
    liq = 3.0 if spread is None else (8.0 if spread <= 0.02 else 6.0 if spread <= 0.05 else 3.0)
    vr = max((float(s.details.get("volume_ratio") or 0) for s in mine), default=0.0)
    liq += 7.0 if vr >= 2 else 4.0 if vr >= 1.5 else 2.0 if vr >= 1.2 else 0.0
    c["hacim_likidite"] = min(CAPS["hacim_likidite"], liq)

    best = max(mine, key=lambda s: _eff_conf(s, mi.bot_weights) * strategy_weight(regime.regime, s.strategy_id, long),
               default=None)
    w = strategy_weight(regime.regime, best.strategy_id, long) if best else 0.0
    c["rejim_uyumu"] = CAPS["rejim_uyumu"] * max(0.0, min(1.0, (w - 0.4) / 0.9))

    senti = mi.sentiment or 0
    c["haber"] = max(0.0, min(10.0, 5 + (senti if long else -senti) / 40))

    a = best_in("akis")
    c["akis"] = max(0.0, min(10.0, 5 + 5 * (_eff_conf(a, mi.bot_weights) - 0.5) / 0.5)) if a else 5.0  # on-chain yok → nötr

    perf_w = mi.bot_weights.get(best.bot_id, 1.0) if best else 1.0
    c["performans"] = CAPS["performans"] * max(0.0, min(1.0, perf_w / 2))   # geçmiş veri yok → nötr 5

    return sum(c.values()), {k: round(v, 1) for k, v in c.items()}, best


def decide(ctx: MarketContext, sigs: list[Signal], mi: MarketInputs) -> Decision | None:
    """Bir sembol ve zaman dilimi için işlem sinyalleri varsa karar üretir."""
    trade = [s for s in sigs if s.action != Action.BEKLE and s.symbol == ctx.symbol]
    if not trade:
        return None
    regime = detect_regime(ctx, news_veto=mi.news_veto)
    long_score, long_c, long_best = score_direction(True, trade, regime, ctx.spread_pct, mi)
    short_score, short_c, short_best = score_direction(False, trade, regime, ctx.spread_pct, mi)
    long = long_score >= short_score if long_best and short_best else bool(long_best)
    score, contrib, best = (long_score, long_c, long_best) if long else (short_score, short_c, short_best)
    other = short_score if long else long_score
    other_has = bool(short_best if long else long_best)
    notes = []

    outcome = outcome_for(score, mi.risk_threshold)
    if other_has:
        if other >= 50 and abs(score - other) < 15:
            outcome = "CELISKI"
            notes.append(f"çelişkili sinyaller: AL {long_score:.0f} / SAT {short_score:.0f}")
        else:
            penalty = min(10.0, other / 5)
            score -= penalty
            contrib["celiski_cezasi"] = round(-penalty, 1)
            outcome = outcome_for(score, mi.risk_threshold)
    w = strategy_weight(regime.regime, best.strategy_id, long)
    if regime.regime in NO_NEW_TRADES:
        outcome = "VETO" if mi.news_veto else "REDDEDILDI"
        notes.append(mi.veto_reason or f"rejim yeni işleme uygun değil: {regime.regime.value}")
    elif w == 0:
        outcome = "REDDEDILDI"
        notes.append(f"{base_strategy(best.strategy_id)} {regime.regime.value} rejiminde bu yönde kullanılmaz")
    if not mi.trading_enabled or mi.safe_mode:
        outcome = "SISTEM_DURDU"
        notes.append("yeni işlemler durdurulmuş / güvenli mod")

    direction = "LONG" if long else "SHORT"
    supporters = [s for s in trade if s.action == (Action.AL if long else Action.SAT)]
    plan = None
    if best:
        plan = {"strategy": best.strategy_id, "bot_id": best.bot_id,
                "entry_low": str(best.entry_low), "entry_high": str(best.entry_high),
                "stop_loss": str(best.stop_loss), "take_profit": str(best.take_profit),
                "expected_net_rr": str(best.expected_net_rr), "estimated_cost_pct": str(best.estimated_cost_pct)}
    head = {"YUKSEK_ONCELIK": "Yüksek öncelikli fırsat, risk değerlendirmesine gönderildi",
            "RISK_KONTROLU": "Risk değerlendirmesine gönderildi",
            "DOGRULAMA": "Ek doğrulama bekleniyor", "IZLEME": "İzleme listesine alındı",
            "REDDEDILDI": "Reddedildi", "CELISKI": "Reddedildi (çelişki)", "VETO": "Veto", "SISTEM_DURDU": "Sistem durdurulmuş"}[outcome]
    reason = (f"{head}: {direction} puan {score:.0f}/100, rejim {regime.regime.value}, "
              f"destekleyen {len(supporters)} sinyal ({', '.join(sorted({s.bot_id for s in supporters}))})"
              + (f"; {'; '.join(notes)}" if notes else ""))
    return Decision(
        symbol=ctx.symbol, timeframe=ctx.timeframe, bar_start=ctx.candles[-1].start_ms, outcome=outcome,
        score=int(round(max(0.0, min(100.0, score)))), direction=direction, regime=regime, reason=reason,
        contributions={"puanlar": contrib, "AL": round(long_score, 1), "SAT": round(short_score, 1),
                       "rejim": regime.to_dict(),
                       "destekleyenler": [{"bot": s.bot_id, "strateji": s.strategy_id, "guven": s.confidence} for s in supporters],
                       "notlar": notes + ["akış/on-chain: on-chain veri yok, nötr puan", "performans: geçmiş veri yok, nötr puan"]},
        plan=plan, signal_keys=[s.details.get("dedup_key") for s in trade if s.details.get("dedup_key")],
        strategy=best.strategy_id[:64] if best else None,
    )
