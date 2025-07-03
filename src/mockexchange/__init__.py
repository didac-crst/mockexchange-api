from importlib.metadata import version, PackageNotFoundError
from .engine import ExchangeEngine
from ._types import OrderSide, OrderType, OrderState, AssetBalance, Order

try:
    __version__ = version("mockexchange")
except PackageNotFoundError:  # dev-editable install
    __version__ = "0.0.0-dev"

__all__ = [
    "ExchangeEngine",
    "OrderSide",
    "OrderType",
    "OrderState",
    "AssetBalance",
    "Order",
]
