import os
import logging
from dataclasses import dataclass
from typing import Dict, Tuple

@dataclass
class ValidationResult:
    ok: bool = False
    reason: str = ""

class RiskManager:
    def __init__(self):
        self.max_position_pct = float(os.getenv('MAX_POSITION_PCT', 0.05))
        self.max_open_positions = int(os.getenv('MAX_OPEN_POSITIONS', 3))
        self.daily_loss_limit_pct = float(os.getenv('DAILY_LOSS_LIMIT_PCT', 0.15))
        self.max_total_exposure_pct = float(os.getenv('MAX_TOTAL_EXPOSURE_PCT', 0.25))
        
        self.open_positions = {}
        self.starting_balance = 500.0  # Should be passed in

    def validate(self, signal, current_balance: float) -> ValidationResult:
        """Apply risk checks against a new trade signal."""
        # Check max open positions
        if len(self.open_positions) >= self.max_open_positions:
            return ValidationResult(False, f"Max open positions ({self.max_open_positions}) reached.")

        # Check total exposure
        current_exposure = sum([p['size'] for p in self.open_positions.values()])
        if current_exposure / current_balance >= self.max_total_exposure_pct:
            return ValidationResult(False, f"Max total exposure ({self.max_total_exposure_pct:.0%}) reached.")

        # Check for drawdown
        if current_balance < self.starting_balance * (1 - self.daily_loss_limit_pct):
            return ValidationResult(False, f"Daily loss limit (drawdown) reached.")

        return ValidationResult(True, "All risk checks passed.")

    def add_position(self, market_id: str, size: float, direction: str, price: float):
        self.open_positions[market_id] = {
            "size": size,
            "direction": direction,
            "entry_price": price,
            "timestamp": os.getlogin() # Simplified placeholder
        }

    def close_position(self, market_id: str):
        if market_id in self.open_positions:
            del self.open_positions[market_id]

    def set_starting_balance(self, balance: float):
        self.starting_balance = balance
