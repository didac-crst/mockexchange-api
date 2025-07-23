# __init__.py
from importlib.metadata import version, PackageNotFoundError
from .logging_config import logger
from ._types import OrderSide, OrderType, OrderState, AssetBalance, Order
from .engine_actors import start_engine, ExchangeEngineActor  # <-- required

try:
    __version__ = version("mockexchange")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = [
    "start_engine",
    "ExchangeEngineActor",
    "OrderSide",
    "OrderType",
    "OrderState",
    "AssetBalance",
    "Order",
    "logger",
    "__version__",
]