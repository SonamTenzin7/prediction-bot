import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

class PaperTrader:
    def __init__(self, state_file: str = "paper_state.json", trade_log: str = "paper_trades.jsonl"):
        self.state_file = state_file
        self.trade_log = trade_log
        self.state = self.load_state()

    def load_state(self) -> Dict:
        """Load persistent paper trading state from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Failed to load paper state: {e}")
                
        return {
            "starting_balance": 500.0,
            "current_balance": 500.0,
            "open_positions": {},
            "closed_trades": [],
            "session_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "start_time": datetime.now(timezone.utc).isoformat()
        }

    def save_state(self):
        """Save current paper trading state to file."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save paper state: {e}")

    def execute(self, signal, size_usdc: float, market_price: float):
        """Simulate trade entry with slippage and fees."""
        # 0.3% buy slippage, 0.2% taker fee
        fill_price = market_price * 1.003
        fee = size_usdc * 0.002
        
        # Deduct cost and fee from virtual balance
        total_cost = size_usdc + fee
        if total_cost > self.state['current_balance']:
            logging.warning("Insufficient virtual balance for paper trade.")
            return

        self.state['current_balance'] -= total_cost
        
        # Track position
        pos = {
            "market_id": signal.market_id,
            "direction": signal.direction,
            "entry_price": fill_price,
            "size_usdc": size_usdc,
            "shares": size_usdc / fill_price,
            "source": getattr(signal, 'source', 'manual'),
            "opened_at": datetime.now(timezone.utc).timestamp()
        }
        
        self.state['open_positions'][signal.market_id] = pos
        self.state['total_trades'] += 1
        
        # Log to trades file
        log_entry = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "action": "open",
            "market_id": signal.market_id,
            "direction": signal.direction,
            "size_usdc": size_usdc,
            "entry_price": fill_price
        }
        self.log_trade(log_entry)
        self.save_state()
        logging.info(f"Paper Trade Opened: {signal.direction} on {signal.market_id} @ {fill_price:.3f}")

    def close_position(self, market_id: str, exit_price: float):
        """Simulate trade exit and compute PnL."""
        if market_id not in self.state['open_positions']:
            return

        pos = self.state['open_positions'].pop(market_id)
        
        # 0.3% sell slippage, 0.2% taker fee
        fill_exit_price = exit_price * 0.997
        gross_payout = pos['shares'] * fill_exit_price
        fee = gross_payout * 0.002
        net_payout = gross_payout - fee
        
        pnl = net_payout - pos['size_usdc']
        self.state['current_balance'] += net_payout
        self.state['session_pnl'] += pnl
        
        if pnl > 0:
            self.state['wins'] += 1
        else:
            self.state['losses'] += 1
            
        # Log resolution
        log_entry = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "action": "close",
            "market_id": market_id,
            "pnl": pnl,
            "exit_price": fill_exit_price,
            "win": pnl > 0
        }
        self.log_trade(log_entry)
        self.state['closed_trades'].append(log_entry)
        self.save_state()
        logging.info(f"Paper Trade Closed: {market_id} | PnL: {pnl:.2f} USDC")

    def log_trade(self, entry: Dict):
        """Append trade event to JSONL file."""
        try:
            with open(self.trade_log, 'a') as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logging.error(f"Failed to log paper trade: {e}")

    def get_performance(self) -> Dict:
        """Return performance summary for display in Streamlit."""
        total = self.state['wins'] + self.state['losses']
        win_rate = (self.state['wins'] / total) if total > 0 else 0
        return {
            "balance": self.state['current_balance'],
            "total_trades": self.state['total_trades'],
            "win_rate": win_rate,
            "session_pnl": self.state['session_pnl'],
            "open_count": len(self.state['open_positions'])
        }
