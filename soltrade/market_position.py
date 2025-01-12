import os
import json

class MarketPosition:
    """
    Tracks whether a position is open, plus current stoploss (sl) and takeprofit (tp).
    """
    def __init__(self, path: str):
        self.path = path
        self.is_open = False
        self.sl = 0
        self.tp = 0
        self.load_position()
        # Ensure file is updated with default info if missing
        self.update_position(self.is_open, self.sl, self.tp)

    def load_position(self):
        if os.path.exists(self.path):
            with open(self.path, 'r') as file:
                position_data = json.load(file)
                self.is_open = position_data["is_open"]
                self.sl = position_data["sl"]
                self.tp = position_data["tp"]
        else:
            self.update_position(self.is_open, self.sl, self.tp)

    def update_position(self, position: bool, stoploss: float, takeprofit: float):
        self.is_open = position
        self.sl = stoploss
        self.tp = takeprofit
        position_obj = {
            "is_open": self.is_open,
            "sl": self.sl,
            "tp": self.tp
        }
        with open(self.path, 'w') as file:
            json.dump(position_obj, file)

    @property
    def position(self) -> bool:
        return self.is_open


_market_instance = None

def market(path: str = None) -> MarketPosition:
    global _market_instance
    if _market_instance is None and path is not None:
        _market_instance = MarketPosition(path)
    return _market_instance

