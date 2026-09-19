# 🏠 CROUS Housing Watcher & Auto-Applicant (Marseille 24/7)

A high-speed, intelligent Python daemon designed to run 24/7 on an Ubuntu VPS. It monitors `trouverunlogement.lescrous.fr` for new student accommodations in Marseille (<= 400€), delivers instant Telegram notifications, and **automatically applies for the room (sniping)** using headless Playwright with your authenticated session.

---

## ⚡ How It Works

- **Ultra-Fast Polling with Zero Overhead:** Uses direct CROUS internal search APIs (~0.3s per check, ~20MB RAM) for continuous 24/7 monitoring.
- **Automated Application (Sniper):** The split-second a new Marseille listing matches your criteria, a headless Playwright worker spawns using your pre-authenticated session, fills in the required form fields, and locks the room into your CROUS cart before anyone else can snipe it!
- **Safe Dry-Run Testing:** Built-in dry-run safety toggle (`AUTO_APPLY_DRY_RUN=true`) that tests navigation, fills all fields, takes a full screenshot, and stops before the final submit button.
- **Smart Time-Based Cadence:** Automatically adjusts polling speed based on French local time:
  - **08:00 – 18:30 (Peak Office Hours):** Fast checks (every 30–40s) when CROUS staff publish rooms.
  - **18:30 – 23:30 (Evening):** Moderate checks (every 60–75s).
  - **23:30 – 08:00 (Night):** Sleep mode (every 4 mins) to cut request volume and protect your IP.
- **Hardened Error & Ban Protection:** Detects HTTP 403 / 429 immediately, triggers instant priority Telegram alerts, and activates automatic safety backoff.
- **Interactive Telegram Bot Commands:**
  - `/status` — View uptime, checks performed, current cadence, and session validity.
  - `/session` — Check if your CROUS / MesServicesEtudiant login session is active.
  - `/test_apply` — Run a safe dry-run test on an available listing and receive the resulting screenshot on your phone.
  - `/check` — Force an immediate search check right now.
  - `/test` — Send a test notification.
  - `/help` — Display command menu.

---

## 🔐 Authentication Setup (One-Time)

Because `MesServicesEtudiant` is protected by Altcha Proof-of-Work and SSO security, the initial login is performed once interactively in a visible browser:

### Step 1: Log in on your local machine

Run:

```bash
python crous_auth.py --login
```

1. A browser window will open displaying the MesServicesEtudiant login page.
2. Enter your email and password, complete the Altcha check and 2FA (if enabled).
3. Once redirected back to `trouverunlogement.lescrous.fr`, the script automatically captures your authenticated cookies & tokens into `session.json`.

### Step 2: Verify the session

Run:

```bash
python crous_auth.py --check
```

It will verify against `/api/health` and output:

```
✅ Session is ACTIVE and authenticated.
```

### Step 3: Copy `session.json` to your VPS

```bash
scp session.json user@your-vps-ip:~/crous-watcher/session.json
```

---

## 🚀 Quick Deployment to Ubuntu VPS

### Step 1: Transfer repository to VPS

```bash
git clone https://github.com/basselfads23/crous-housing.git crous-watcher
cd crous-watcher
```

_(Or copy via `scp` from your local machine)_

### Step 2: Configure `.env`

Create or edit your `.env`:

```bash
nano .env
```

Ensure your Telegram credentials and settings are configured:

```env
# Telegram Bot Settings
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# Watcher Settings
TARGET_CITY=Marseille
MAX_PRICE=400
COLOCATION_ONLY=false
ENABLE_DAILY_HEARTBEAT=true

# Smart Time Cadence
ENABLE_SMART_CADENCE=true
PEAK_CHECK_INTERVAL_SECONDS=35
EVENING_CHECK_INTERVAL_SECONDS=65
NIGHT_CHECK_INTERVAL_SECONDS=240

# Auto-Apply (Sniper)
AUTO_APPLY_ENABLED=true
AUTO_APPLY_DRY_RUN=true            # Set to false when you are ready to live-submit!
PREFERRED_OCCUPATION_MODE=single   # 'single' or 'sharing'
STUDY_LEVEL=3                      # e.g. 1: L1, 2: L2, 3: L3, 4: M1, 5: M2
PURPOSE=studies                    # 'studies' or 'internship'
```

### Step 3: Run the 1-Step Installer

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will automatically:

1. Install Python 3, venv, and Playwright system dependencies.
2. Install Python packages (`playwright`, `requests`, `python-dotenv`).
3. Install Chromium browser binaries for headless execution.
4. Set up and start the persistent `systemd` service (`crous-watcher`).

---

## 🧪 Testing the Setup

1. **Verify session from Telegram:** Send `/session` to your Telegram bot.
2. **Run a Dry-Run test:** Send `/test_apply` to your bot.  
   The bot will fill an application form for an available listing in France, capture a full screenshot, and send it directly to your phone.
3. **Go Live:** Once you've verified the screenshot on Telegram, edit `.env` on your VPS:
   ```bash
   AUTO_APPLY_DRY_RUN=false
   ```
   Restart the service:
   ```bash
   sudo systemctl restart crous-watcher
   ```

---

## 📊 Management Commands on VPS

| Action                   | Command                                |
| ------------------------ | -------------------------------------- |
| **Check service status** | `sudo systemctl status crous-watcher`  |
| **View live logs**       | `journalctl -u crous-watcher -f`       |
| **Restart watcher**      | `sudo systemctl restart crous-watcher` |
| **Stop watcher**         | `sudo systemctl stop crous-watcher`    |
