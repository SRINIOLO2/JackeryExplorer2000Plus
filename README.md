# Jackery Home Assistant & Telegram Bridge Stack

This repository provides a containerized Python service designed to connect directly to a Jackery Explorer 2000 Plus power station via the Jackery cloud API. It publishes status values to an MQTT Broker (for automatic integration into Home Assistant) and runs an interactive Telegram Bot to send alerts and toggle outputs.

---

## Features

1.  **Home Assistant Integration**: Natively integrates with Home Assistant using **MQTT Auto-Discovery**. No custom plugins or components need to be installed inside Home Assistant.
2.  **Solar vs. Grid Tracking**:
    *   **AC wall input** power is mapped directly to `acip`.
    *   **Solar PV input** is calculated as the active charge power input when AC input is inactive.
3.  **Battery settings**: Exposes battery saving modes, emergency fast charging, charging speed settings, auto-shutdown timers, screen timeouts, and light modes.
4.  **Telegram Bot Integration**:
    *   **Push Alerts**: Sends warning notifications on low battery (`<20%`), critical battery (`<5%`), and hot battery (`>45°C`).
    *   **Two-Way Commands**: Run `/status` or `/refresh` to get reports. Click Telegram Inline Keyboard buttons to toggle AC and DC ports.
5.  **GitOps Deployment**: Fully compatible with **Dockhand** for automated rebuilds and redeployments triggered by GitHub Webhooks.
6.  **Secrets Verification**: An automated pre-execution check (`verify_no_secrets.py`) stops Docker builds if any sensitive API keys or passwords are accidentally hardcoded in code files.

---

## Directory Structure

```
├── .env.example              # Template config variables
├── .gitignore                # Prevents committing secrets (.env, token caches)
├── Dockerfile                # Lightweight multi-stage build running security validation
├── compose.yaml              # Orchestration with restart policy and data persistence
├── requirements.txt          # Python dependency pin list
├── jackery_api.py            # Login API client with encryption and token caching
├── verify_no_secrets.py      # Secrets sniffer script
└── main.py                   # State loop, MQTT publisher, and Telegram Bot handler
```

---

## ⚙️ Configuration Setup

1.  **Duplicate the template**:
    ```bash
    cp .env.example .env
    ```
2.  **Edit `.env`** and fill in your secrets:
    *   `JACKERY_USERNAME` & `JACKERY_PASSWORD`: Your Jackery mobile app credentials.
    *   `JACKERY_DEVICE_ID`: (Optional) The specific serial number/ID of your Explorer 2000 Plus. If left empty, it auto-discovers all units bound to your account.
    *   `MQTT_BROKER`, `MQTT_USERNAME`, `MQTT_PASSWORD`: Connection parameters for your MQTT broker (e.g., Mosquitto running as a Home Assistant add-on).
    *   `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Credentials for your Telegram bot (created via `@BotFather`).
    *   `POLL_INTERVAL_SEC`: Rate of polling Jackery API (default: 60 seconds).
    *   `LOW_BATTERY_THRESHOLD` & `CRITICAL_BATTERY_THRESHOLD`: SoC trigger limits for warnings (default: 20% and 5%).

---

## 🚀 Running Locally or in Docker

### Running Locally (For Debugging)
1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
2.  Run the secrets sniffer:
    ```bash
    python verify_no_secrets.py
    ```
3.  Start the bridge:
    ```bash
    python main.py
    ```

### Running with Docker Compose
Run the container locally or on your server:
```bash
docker compose up -d --build
```
*Note: The build will fail if `verify_no_secrets.py` detects any hardcoded keys in your files.*

---

## ⚓ Deployment via Dockhand (GitOps Webhook)

Dockhand makes it easy to set up continuous deployment:

1.  **Push to GitHub**: Push this repository to a GitHub repository. Keep the repo private if desired, but since all secrets are in `.env` (which is git-ignored), a public repository is perfectly secure.
2.  **Add webhook**: In Dockhand, register your GitHub repository. Dockhand will generate a Webhook URL and secret.
3.  **Configure GitHub**: Add this webhook URL to your GitHub repository under **Settings** → **Webhooks**. Select `application/json` and trigger on `push` events.
4.  **Environment Variables in Dockhand**: Provide your `.env` variables inside the Dockhand stack configuration UI. Dockhand will inject them during container creation.

---

## 📊 Home Assistant Integration

Once the container runs and connects to the MQTT broker:
1.  Home Assistant automatically detects the Jackery Explorer 2000 Plus.
2.  Look in Home Assistant under **Settings** → **Devices & Services** → **MQTT** to find your new device.
3.  All sensors (battery, charging speed, input power, solar harvested, binary switches) are populated.
4.  You can add these sensors directly to your Lovelace UI cards, gauges, or historical charts.
5.  **AC/DC switches**: To use the Telegram bot's buttons, write an automation in Home Assistant that triggers when a message is received on `jackery/<device_id>/command/oac` (AC) or `jackery/<device_id>/command/odc` (DC) to toggle your smart plugs or Bluetooth relays.
