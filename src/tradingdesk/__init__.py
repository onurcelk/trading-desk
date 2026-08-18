"""tradingdesk — a paper-trading equity research desk for AI agent teams.

Market data comes from the OpenBB Platform; the desk is exposed to agents as an
MCP server so a Paperclip-orchestrated team can research, predict, and trade a
simulated book while every call is graded against real prices.
"""

from .config import Settings, load_settings
from .domain import Direction, Prediction, PredictionStatus, Side
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "Direction",
    "Prediction",
    "PredictionStatus",
    "Settings",
    "Side",
    "Store",
    "load_settings",
    "__version__",
]
