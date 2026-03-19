# 🚀 Deployment Guide — Predict Bot 24/7

## Option A: Docker (Recommended — works on any VPS or Mac)

### Prerequisites

- Docker + Docker Compose installed
- A VPS (DigitalOcean, Hetzner, AWS EC2, etc.) **OR** your Mac

### Steps

```bash
# 1. Clone/copy your project to the server
scp -r /Users/sonamtenzin/Desktop/predict-bot ubuntu@YOUR_SERVER_IP:~/predict-bot

# 2. SSH into the server
ssh ubuntu@YOUR_SERVER_IP

# 3. Go to project directory
cd ~/predict-bot

# 4. Make sure .env file has your keys (never commit this)
nano .env

# 5. Create empty data files if they don't exist
touch signal_log.jsonl wallets_cache.json bot.log paper_trades.jsonl

# 6. Build and start everything
docker-compose up -d

# 7. Check logs
docker-compose logs -f bot

# 8. View dashboard at http://YOUR_SERVER_IP:8501
```

### Useful Docker commands

```bash
docker-compose ps                  # check status
docker-compose logs -f bot         # live bot logs
docker-compose restart bot         # restart after code change
docker-compose down && docker-compose up -d --build   # full rebuild
```

---

## Option B: Systemd (Ubuntu VPS, no Docker)

```bash
# 1. Copy files to server
scp -r /Users/sonamtenzin/Desktop/predict-bot ubuntu@YOUR_SERVER_IP:~/predict-bot

# 2. SSH in
ssh ubuntu@YOUR_SERVER_IP

# 3. Install Python + create virtualenv
cd ~/predict-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 4. Copy the service file
sudo cp predict-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Enable + start
sudo systemctl enable predict-bot
sudo systemctl start predict-bot

# 6. Check status
sudo systemctl status predict-bot
sudo journalctl -u predict-bot -f

# 7. Run dashboard separately (in a screen session)
screen -S dashboard
venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
# Ctrl+A, D to detach
```

---

## Option C: Run locally on your Mac (simple, no server needed)

```bash
# Start the bot in the background, auto-restart on crash
cd /Users/sonamtenzin/Desktop/predict-bot

# Using a simple restart loop (paste into terminal, runs forever)
while true; do python3 bot.py >> bot.log 2>&1; echo "Bot crashed, restarting in 5s..."; sleep 5; done &

# Start dashboard in a separate terminal
streamlit run app.py
```

---

## Recommended VPS Specs (cheapest that work)

| Provider     | Plan              | Cost  | Notes                       |
| ------------ | ----------------- | ----- | --------------------------- |
| Hetzner      | CX11              | $4/mo | Best value, Germany/Finland |
| DigitalOcean | Basic Droplet 1GB | $6/mo | Easy setup                  |
| Vultr        | Regular 1GB       | $5/mo | Global locations            |

**Minimum**: 1 vCPU, 512MB RAM, 10GB SSD, Ubuntu 22.04

---

## What runs 24/7

| Component             | What it does                                          |
| --------------------- | ----------------------------------------------------- |
| `bot.py`              | Orchestrates all engines                              |
| `signal_engine.py`    | Binance WS feed → 5-min predictions → Telegram alerts |
| `copy_engine.py`      | Watches whale wallets → copy signals                  |
| `wallet_scanner.py`   | Re-ranks top wallets every 60 min                     |
| `daily_report_loop()` | Sends accuracy report to Telegram every midnight BT   |
| `app.py` (Streamlit)  | Web dashboard at :8501                                |

---

## Daily Report (Telegram)

Sent automatically every **midnight Bhutan Time** and once on startup.

Includes:

- ✅ Overall accuracy (correct/total)
- 📈 UP vs DOWN breakdown
- 🔥 Win streaks
- 🔍 Confidence calibration (low/med/high)
- 📡 Signal source performance (model vs whale copy)
- 📉 BTC price summary for the day
- 🏆 Performance verdict

---

## Keeping your .env safe on a server

```bash
# On the server, restrict .env permissions
chmod 600 .env
```

Never commit `.env` to git. Add it to `.gitignore`:

```
.env
signal_log.jsonl
bot.log
wallets_cache.json
__pycache__/
```
