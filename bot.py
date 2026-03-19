import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Import core engines
from core.wallet_scanner import WalletScanner
from core.signal_engine import SignalEngine
from core.copy_engine import CopyEngine
from core.alert_engine import AlertEngine
from core.paper_trader import PaperTrader
from core.risk_manager import RiskManager
from core.execution import OrderExecutor
from core.daily_report import daily_report_loop

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger("PredictBot")

async def main():
    logger.info("Starting Polymarket BTC Predict Bot Orchestrator...")
    load_dotenv()
    
    # 1. Environment Check
    trading_mode = os.getenv("TRADING_MODE", "paper")
    logger.info(f"Trading Mode: {trading_mode.upper()}")
    
    # 2. Instantiate Engines
    scanner = WalletScanner()
    signal_engine = SignalEngine()
    copy_engine = CopyEngine(scanner=scanner)
    # Wire copy engine into signal engine so predictions can use whale signals
    signal_engine.copy_engine = copy_engine
    paper_trader = PaperTrader()
    risk_manager = RiskManager()
    
    order_executor = None
    if trading_mode == 'live':
        order_executor = OrderExecutor(risk_manager=risk_manager)
        
    alert_engine = AlertEngine(
        scanner=scanner,
        copy_engine=copy_engine,
        signal_engine=signal_engine,
        paper_trader=paper_trader,
        order_executor=order_executor,
        risk_manager=risk_manager
    )

    # 3. Schedule windows
    windows = alert_engine.generate_windows(hours_ahead=24)
    logger.info(f"Scheduled {len(windows)} windows for the next 24 hours")

    # 4. Launch Concurrent Tasks
    tasks = [
        scanner.run_loop(),
        signal_engine.run_loop(),
        copy_engine.run_loop(),
        daily_report_loop(),          # sends report at midnight BT + on startup
    ]
    
    # Add tasks for each window
    for window in windows:
        tasks.append(alert_engine.run_window(window))
        
    logger.info("All engines and window tasks launched.")
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"Critical error in main loop: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
