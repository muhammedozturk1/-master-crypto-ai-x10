"""BOT04 – Arbitraj botu (şartname bölüm 7). Gözlem ve raporlama modunda çalışır.

200 USD'lik kasada borsalar arası arbitraj ekonomik değildir ve sermaye borsalara
bölünmez; bu bot işlem sinyali üretmez. Net pozitif bir fırsat görülürse yalnızca kayıt
düşer (BEKLE), ana robot bunu işlem olarak değerlendirmez.
"""

from __future__ import annotations

from app.bots.base import MarketContext, WorkerBot
from app.domain.signals import Action, Signal
from app.engines.arbitrage_service import ArbState

BOT_ID = "BOT04"
REPORT_THRESHOLD_PCT = 0.05


class ArbitrageBot(WorkerBot):
    bot_id = BOT_ID
    timeframes = frozenset({"15m"})
    market_wide = True

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        st: ArbState | None = ctx.extra.get("arbitrage")
        if st is None or st.checked_at is None:
            return []
        best = st.best()
        if not best or best["net_pct"] < REPORT_THRESHOLD_PCT:
            return []
        slot = st.checked_at.strftime("%Y-%m-%dT%H:%M")
        what = best.get("path") or f"{best['symbol']} {best['buy']}→{best['sell']}"
        return [Signal(
            bot_id=BOT_ID, strategy_id="arbitrage_observation", symbol="PIYASA", exchange="coklu",
            timeframe=ctx.timeframe, action=Action.BEKLE, confidence=0,
            reason=f"Gözlem: {what} net %{best['net_pct']:.3f} (komisyon sonrası; transfer ve bacak riski hariç). "
                   "200 USD kasada işlem yapılmaz.",
            risk_warnings=["gözlem modu: işlem önerisi değildir"],
            details={"best": best, "triangular": st.triangular[:3], "cross": st.cross[:3],
                     "dedup_key": f"{BOT_ID}:{slot}"},
        )]
