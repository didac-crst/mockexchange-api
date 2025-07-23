# constants.py

OPEN_STATUS = ("new", "partially_filled")  # open orders
CLOSED_STATUS = ("filled", "canceled", "partially_canceled", "expired", "rejected")  # closed orders
ALL_STATUS = OPEN_STATUS + CLOSED_STATUS  # all orders

# # ─── Domain constants ────────────────────────────────────────────────────
OrderSide  = type("OrderSide",  (), {"BUY": "buy",
                                     "SELL": "sell"})
OrderType  = type("OrderType",  (), {"MARKET": "market",
                                     "LIMIT": "limit"})
OrderState = type("OrderState", (), {"NEW": "new",
                                     "PARTIALLY_FILLED": "partially_filled",
                                     "FILLED": "filled",
                                     "CANCELED": "canceled",
                                     "PARTIALLY_CANCELED": "partially_canceled",
                                     "EXPIRED": "expired",
                                     "REJECTED": "rejected",
                                     })