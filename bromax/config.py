from dataclasses import dataclass
from decimal import Decimal
import os
import re

@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...] = ("1HZ100V",)
    ws_url: str = "wss://api.derivws.com/trading/v1/options/ws/public"
    database_path: str = "data/bromax.sqlite3"
    currency: str = "USD"
    paper_stake: Decimal = Decimal("1.00")
    max_paper_stake: Decimal = Decimal("1.00")
    daily_loss_cap: Decimal = Decimal("10.00")
    max_trades_per_day: int = 100
    max_data_age_seconds: int = 5
    min_history_ticks: int = 1000
    proposal_timeout_seconds: float = 4.0
    max_pending_quote_tasks: int = 20

    def __post_init__(self):
        if not self.symbols or any(not re.fullmatch(r"[A-Za-z0-9_]{2,30}", x) for x in self.symbols):
            raise ValueError("DERIV_SYMBOLS must contain one or more valid Deriv symbol codes")
        if any(not x.startswith("1HZ") for x in self.symbols):
            raise ValueError("This protocol scores the immediately following one-second tick; configure only 1HZ symbols")
        if not self.currency or not re.fullmatch(r"[A-Za-z0-9]{2,20}", self.currency):
            raise ValueError("CURRENCY must be a valid 2–20 character currency code")
        if self.paper_stake <= 0 or self.max_paper_stake <= 0 or self.paper_stake > self.max_paper_stake:
            raise ValueError("Paper stake must be positive and no greater than MAX_PAPER_STAKE")
        if self.daily_loss_cap <= 0 or self.max_trades_per_day < 1:
            raise ValueError("Daily loss cap and max trades must be positive")
        if self.max_data_age_seconds < 1 or self.min_history_ticks < 1000 or self.proposal_timeout_seconds <= 0 or self.max_pending_quote_tasks < 1:
            raise ValueError("Data age, proposal timeout, and minimum 1,000-tick history must be valid")

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        symbols = tuple(x.strip() for x in os.getenv("DERIV_SYMBOLS", "1HZ100V").split(",") if x.strip())
        return cls(
            symbols=symbols,
            ws_url=os.getenv("DERIV_WS_URL", cls.ws_url),
            database_path=os.getenv("DATABASE_PATH", cls.database_path),
            currency=os.getenv("CURRENCY", "USD"),
            paper_stake=Decimal(os.getenv("PAPER_STAKE", "1.00")),
            max_paper_stake=Decimal(os.getenv("MAX_PAPER_STAKE", "1.00")),
            daily_loss_cap=Decimal(os.getenv("DAILY_LOSS_CAP", "10.00")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "100")),
            max_data_age_seconds=int(os.getenv("MAX_DATA_AGE_SECONDS", "5")),
            min_history_ticks=int(os.getenv("MIN_HISTORY_TICKS", "1000")),
            proposal_timeout_seconds=float(os.getenv("PROPOSAL_TIMEOUT_SECONDS", "4")),
            max_pending_quote_tasks=int(os.getenv("MAX_PENDING_QUOTE_TASKS", "20")),
        )
