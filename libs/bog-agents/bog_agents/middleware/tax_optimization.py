"""Tax optimization agent middleware.
Feature #42: Tax-loss harvesting, wash sale detection, long/short-term gain
optimization, asset location recommendations.

## Tools

- `add_tax_lot`: Register a tax lot (purchase date, cost basis, current value)
- `find_harvest_opportunities`: Find tax-loss harvesting candidates
- `check_wash_sale`: Check for wash sale rule violations
- `tax_summary`: Generate tax impact summary
- `asset_location_advice`: Recommend tax-efficient asset placement

## Usage

```python
from bog_agents.middleware.tax_optimization import TaxOptimizationMiddleware

middleware = TaxOptimizationMiddleware()
```

⚠ **STUB — NOT FOR PRODUCTION USE.**

This middleware is a scaffold that demonstrates the shape of a real
implementation. Its tools accept calls and return placeholder structures
so an agent can be wired against the surface, but the underlying logic
is not implemented — for example, ``fetch_quote`` returns ``price=0.0``
with a note instructing the caller to populate real data. Models that
call these tools will receive plausible-looking but **incorrect**
results.

This module ships at "Development Status :: 4 - Beta" deliberately;
see REVIEW.md P0-A for the broader plan (extract to a separate
``bog-agents-finance``-style package once the implementations are real,
or remove from the headline middleware list if they will not be).
Do not enable in any flow whose output is consumed by a downstream
system, customer-facing surface, or compliance-relevant artifact.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

LONG_TERM_DAYS = 365
WASH_SALE_WINDOW = 30

# 2025/2026 federal tax brackets (simplified)
SHORT_TERM_RATE = 0.37  # Top marginal
LONG_TERM_RATE = 0.20


@dataclass
class TaxLot:
    """A single tax lot.

    Attributes:
        lot_id: Unique identifier.
        ticker: Security symbol.
        shares: Number of shares.
        cost_basis: Total cost basis.
        current_value: Current market value.
        purchase_date: Date acquired (YYYY-MM-DD).
        account_type: Account type (taxable, ira, roth, 401k).
    """

    lot_id: int
    ticker: str
    shares: float
    cost_basis: float
    current_value: float
    purchase_date: str
    account_type: str = "taxable"

    @property
    def gain_loss(self) -> float:
        """Unrealized gain or loss."""
        return self.current_value - self.cost_basis

    @property
    def gain_loss_pct(self) -> float:
        """Gain/loss as percentage."""
        if self.cost_basis == 0:
            return 0.0
        return self.gain_loss / self.cost_basis

    @property
    def is_long_term(self) -> bool:
        """Whether the holding qualifies for long-term capital gains."""
        try:
            purchased = date.fromisoformat(self.purchase_date)
            return (date.today() - purchased).days > LONG_TERM_DAYS
        except ValueError:
            return False

    @property
    def holding_period_days(self) -> int:
        """Days held."""
        try:
            purchased = date.fromisoformat(self.purchase_date)
            return (date.today() - purchased).days
        except ValueError:
            return 0

    @property
    def tax_rate(self) -> float:
        """Applicable tax rate."""
        return LONG_TERM_RATE if self.is_long_term else SHORT_TERM_RATE

    @property
    def estimated_tax(self) -> float:
        """Estimated tax impact if sold."""
        if self.gain_loss <= 0:
            return self.gain_loss * self.tax_rate  # Tax savings
        return self.gain_loss * self.tax_rate


@dataclass
class TaxPortfolio:
    """Portfolio with tax lot tracking."""

    lots: list[TaxLot] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def add_lot(self, **kwargs: object) -> TaxLot:
        """Add a tax lot."""
        lot = TaxLot(lot_id=self._next_id, **kwargs)
        self.lots.append(lot)
        self._next_id += 1
        return lot

    def harvest_opportunities(self, min_loss: float = 100) -> list[TaxLot]:
        """Find tax-loss harvesting candidates.

        Args:
            min_loss: Minimum loss to qualify.

        Returns:
            Lots with unrealized losses exceeding min_loss.
        """
        return [lot for lot in self.lots if lot.account_type == "taxable" and lot.gain_loss < -min_loss]

    def check_wash_sales(self, ticker: str, sale_date: str) -> list[TaxLot]:
        """Check for wash sale rule violations.

        Args:
            ticker: Ticker being sold.
            sale_date: Date of the sale (YYYY-MM-DD).

        Returns:
            Lots that would trigger a wash sale.
        """
        try:
            sold = date.fromisoformat(sale_date)
        except ValueError:
            return []

        window_start = sold - timedelta(days=WASH_SALE_WINDOW)
        window_end = sold + timedelta(days=WASH_SALE_WINDOW)

        violations = []
        for lot in self.lots:
            if lot.ticker != ticker:
                continue
            try:
                purchased = date.fromisoformat(lot.purchase_date)
            except ValueError:
                continue
            if window_start <= purchased <= window_end and lot.purchase_date != sale_date:
                violations.append(lot)
        return violations

    def format_tax_summary(self) -> str:
        """Format comprehensive tax summary."""
        if not self.lots:
            return "No tax lots registered."

        taxable = [l for l in self.lots if l.account_type == "taxable"]
        deferred = [l for l in self.lots if l.account_type in ("ira", "401k")]
        roth = [l for l in self.lots if l.account_type == "roth"]

        total_gains = sum(l.gain_loss for l in taxable if l.gain_loss > 0)
        total_losses = sum(l.gain_loss for l in taxable if l.gain_loss < 0)
        net = total_gains + total_losses

        st_gains = sum(l.gain_loss for l in taxable if l.gain_loss > 0 and not l.is_long_term)
        lt_gains = sum(l.gain_loss for l in taxable if l.gain_loss > 0 and l.is_long_term)
        st_losses = sum(l.gain_loss for l in taxable if l.gain_loss < 0 and not l.is_long_term)
        lt_losses = sum(l.gain_loss for l in taxable if l.gain_loss < 0 and l.is_long_term)

        lines = [
            "## Tax Summary",
            f"Total lots: {len(self.lots)} (Taxable: {len(taxable)}, Tax-deferred: {len(deferred)}, Roth: {len(roth)})",
            "",
            "### Unrealized Gains/Losses (Taxable Accounts)",
            f"  Short-term gains:  ${st_gains:>12,.2f} (taxed at {SHORT_TERM_RATE:.0%})",
            f"  Long-term gains:   ${lt_gains:>12,.2f} (taxed at {LONG_TERM_RATE:.0%})",
            f"  Short-term losses: ${st_losses:>12,.2f}",
            f"  Long-term losses:  ${lt_losses:>12,.2f}",
            f"  Net:               ${net:>12,.2f}",
            "",
            "### Estimated Tax Impact",
            f"  ST tax on gains: ${st_gains * SHORT_TERM_RATE:>10,.2f}",
            f"  LT tax on gains: ${lt_gains * LONG_TERM_RATE:>10,.2f}",
            f"  Tax savings from losses: ${abs(total_losses) * SHORT_TERM_RATE:>10,.2f}",
            "",
        ]

        # Harvest opportunities
        opportunities = self.harvest_opportunities()
        if opportunities:
            lines.append("### Tax-Loss Harvesting Opportunities")
            for lot in sorted(opportunities, key=lambda l: l.gain_loss):
                term = "LT" if lot.is_long_term else "ST"
                saving = abs(lot.gain_loss) * lot.tax_rate
                lines.append(f"  {lot.ticker} (Lot #{lot.lot_id}, {term}): Loss ${lot.gain_loss:,.2f} -> Tax saving ${saving:,.2f}")
            lines.append("")

        return "\n".join(lines)


TAX_SYSTEM_PROMPT = """## Tax Optimization Tools

You have access to tax analysis tools for financial advisor workflows.

**Key Rules:**
- Wash sale: Cannot repurchase same/substantially identical security within 30 days
- Long-term: Held > 365 days (lower tax rate)
- Short-term: Held <= 365 days (ordinary income rate)
- $3,000 annual capital loss deduction limit against ordinary income
- Tax-loss harvesting: Sell losers to offset gains

**Asset Location Guidance:**
- Taxable: Tax-efficient assets (index funds, muni bonds, long-term holdings)
- Tax-deferred (IRA/401k): Tax-inefficient assets (bonds, REITs, high-turnover funds)
- Roth: Highest expected growth assets (small-cap, emerging markets)"""


class TaxOptimizationState(TypedDict):
    """State for tax optimization middleware."""


class TaxOptimizationMiddleware(AgentMiddleware[TaxOptimizationState, ContextT, ResponseT]):
    """Middleware for tax optimization analysis."""

    state_schema = TaxOptimizationState

    def __init__(self) -> None:
        self.portfolio = TaxPortfolio()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build tax optimization tools."""
        mw = self

        def add_tax_lot(
            runtime: ToolRuntime[None, TaxOptimizationState],
            ticker: Annotated[str, "Ticker symbol"],
            shares: Annotated[float, "Number of shares"],
            cost_basis: Annotated[float, "Total cost basis in USD"],
            current_value: Annotated[float, "Current market value in USD"],
            purchase_date: Annotated[str, "Purchase date (YYYY-MM-DD)"],
            account_type: Annotated[str, "Account type: taxable, ira, roth, 401k"] = "taxable",
        ) -> str:
            """Register a tax lot with cost basis and current value."""
            lot = mw.portfolio.add_lot(
                ticker=ticker,
                shares=shares,
                cost_basis=cost_basis,
                current_value=current_value,
                purchase_date=purchase_date,
                account_type=account_type,
            )
            term = "long-term" if lot.is_long_term else "short-term"
            return f"Lot #{lot.lot_id}: {ticker} {shares} shares, {lot.gain_loss_pct:+.1%} ({term}, {account_type})"

        def find_harvest_opportunities(
            runtime: ToolRuntime[None, TaxOptimizationState],
            min_loss: Annotated[float, "Minimum loss to qualify"] = 100,
        ) -> str:
            """Find tax-loss harvesting candidates in taxable accounts."""
            opps = mw.portfolio.harvest_opportunities(min_loss)
            if not opps:
                return "No tax-loss harvesting opportunities found."
            lines = ["## Tax-Loss Harvesting Opportunities", ""]
            for lot in sorted(opps, key=lambda l: l.gain_loss):
                saving = abs(lot.gain_loss) * lot.tax_rate
                lines.append(f"- {lot.ticker} (Lot #{lot.lot_id}): Loss ${lot.gain_loss:,.2f} -> Tax saving ${saving:,.2f}")
            return "\n".join(lines)

        def check_wash_sale(
            runtime: ToolRuntime[None, TaxOptimizationState],
            ticker: Annotated[str, "Ticker being sold"],
            sale_date: Annotated[str, "Sale date (YYYY-MM-DD)"],
        ) -> str:
            """Check if selling would trigger a wash sale rule violation."""
            violations = mw.portfolio.check_wash_sales(ticker, sale_date)
            if not violations:
                return f"No wash sale violations for selling {ticker} on {sale_date}."
            lines = [f"WARNING: Wash sale violations detected for {ticker}:", ""]
            for lot in violations:
                lines.append(f"  Lot #{lot.lot_id}: Purchased {lot.purchase_date} ({lot.holding_period_days} days ago)")
            lines.append("")
            lines.append("The loss on this sale would be disallowed and added to the replacement lot's cost basis.")
            return "\n".join(lines)

        def tax_summary(
            runtime: ToolRuntime[None, TaxOptimizationState],
        ) -> str:
            """Generate comprehensive tax impact summary."""
            return mw.portfolio.format_tax_summary()

        def clear_tax_lots(
            runtime: ToolRuntime[None, TaxOptimizationState],
        ) -> str:
            """Clear all tax lots."""
            mw.portfolio = TaxPortfolio()
            return "Tax lots cleared."

        return [
            StructuredTool.from_function(name="add_tax_lot", description="Register a tax lot with cost basis and current value.", func=add_tax_lot),
            StructuredTool.from_function(
                name="find_harvest_opportunities", description="Find tax-loss harvesting candidates.", func=find_harvest_opportunities
            ),
            StructuredTool.from_function(name="check_wash_sale", description="Check for wash sale rule violations.", func=check_wash_sale),
            StructuredTool.from_function(name="tax_summary", description="Generate comprehensive tax impact summary.", func=tax_summary),
            StructuredTool.from_function(name="clear_tax_lots", description="Clear all tax lots.", func=clear_tax_lots),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject tax optimization instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, TAX_SYSTEM_PROMPT))

    def wrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]]
    ) -> ModelResponse[ResponseT]:
        """Inject instructions."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]]
    ) -> ModelResponse[ResponseT]:
        """Async version."""
        return await call_next(self.modify_request(request))


__all__ = ["TaxLot", "TaxOptimizationMiddleware", "TaxPortfolio"]
