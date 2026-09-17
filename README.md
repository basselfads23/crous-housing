# 🏠 CROUS Housing Watcher (Marseille 24/7)

A high-speed, lightweight Python daemon designed to run 24/7 on an Ubuntu VPS. It monitors `trouverunlogement.lescrous.fr` for new accommodations in Marseille (<= 400€) and delivers instant notifications with one-tap application links to your Telegram.

---

## ⚡ How It Works

* **Zero Browser Overhead:** Uses direct CROUS internal search APIs (~0.3s per check, ~20MB RAM, no headless Chrome needed).
* **Instant Alerts:** Dispatches high-priority push notifications with direct links right to your Telegram.
* **Interactive Bot Commands:** Send commands to your bot directly from Telegram:
  * `/status` — View uptime, checks performed, and total listings.
  * `/check` — Force an immediate manual check right now.
  * `/test` — Send a test notification to verify delivery.
  * `/help` — Display command menu.
* **Always-On Systemd Service:** Ubuntu automatically restarts the script if the VPS reboots or network drops.

---

## 🚀 Quick Deployment to Ubuntu VPS

### Step 1: Transfer to VPS
You can either clone the repo or copy via `scp`:

**Option A: Git Clone**
```bash
git clone https://github.com/basselfads23/crous-housing.git crous-watcher
cd crous-watcher
```

**Option B: SCP from your local machine**
```bash
scp -r /path/to/crous-watcher user@your-vps-ip:~/crous-watcher
ssh user@your-vps-ip
cd crous-watcher
```

---

### Step 2: Configure `.env`
Create or verify your `.env` file:
```bash
nano .env
```
Ensure your Telegram credentials and search settings are set:
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
CHECK_INTERVAL_SECONDS=45
TARGET_CITY=Marseille
MAX_PRICE=400
COLOCATION_ONLY=false
ENABLE_DAILY_HEARTBEAT=true
```

---

### Step 3: Run the 1-Step Installer
Run:
```bash
chmod +x deploy.sh
./deploy.sh
```

That's it! The script will:
1. Install Python 3 and virtualenv dependencies.
2. Install Python packages (`requests`, `python-dotenv`).
3. Set up and start a persistent `systemd` service (`crous-watcher`).
4. Send a startup message to your Telegram bot confirming that 24/7 monitoring is active.

---

## 📊 Management Commands on VPS

| Action | Command |
|---|---|
| **Check service status** | `sudo systemctl status crous-watcher` |
| **View live logs** | `journalctl -u crous-watcher -f` |
| **Restart watcher** | `sudo systemctl restart crous-watcher` |
| **Stop watcher** | `sudo systemctl stop crous-watcher` |
