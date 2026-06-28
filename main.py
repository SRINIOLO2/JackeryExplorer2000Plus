import os
import sys
import time
import logging
import threading
from typing import Dict, Any, Optional
import requests
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

from jackery_api import JackeryAPI, JackeryAuthenticationError

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
_LOGGER = logging.getLogger("jackery_bridge")

# Load environment variables
load_dotenv()

JACKERY_USERNAME = os.getenv("JACKERY_USERNAME")
JACKERY_PASSWORD = os.getenv("JACKERY_PASSWORD")
JACKERY_DEVICE_ID = os.getenv("JACKERY_DEVICE_ID")

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 60))
LOW_BATTERY_THRESHOLD = int(os.getenv("LOW_BATTERY_THRESHOLD", 20))
CRITICAL_BATTERY_THRESHOLD = int(os.getenv("CRITICAL_BATTERY_THRESHOLD", 5))

# Global states
running = True
api_client: Optional[JackeryAPI] = None
mqtt_client: Optional[mqtt.Client] = None
device_states: Dict[str, Dict[str, Any]] = {}  # device_id -> properties
alert_states: Dict[str, Dict[str, bool]] = {}   # device_id -> alert_name -> triggered

def send_telegram_message(text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    """Send a telegram message using the Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        _LOGGER.warning("Telegram configuration missing. Cannot send message.")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        _LOGGER.error("Failed to send Telegram message: %s", e)
        return False

def make_control_keyboard(device_id: str) -> Dict[str, Any]:
    """Generate the inline keyboard with toggle buttons."""
    # Find current states to label buttons clearly
    states = device_states.get(device_id, {})
    ac_label = "🔴 Turn AC OFF" if states.get("oac") == 1 else "🟢 Turn AC ON"
    dc_label = "🔴 Turn DC OFF" if states.get("odc") == 1 else "🟢 Turn DC ON"
    
    return {
        "inline_keyboard": [
            [
                {"text": ac_label, "callback_data": f"toggle_ac_{device_id}"},
                {"text": dc_label, "callback_data": f"toggle_dc_{device_id}"}
            ],
            [
                {"text": "🔄 Refresh Status", "callback_data": f"refresh_{device_id}"}
            ]
        ]
    }

def handle_callback_query(callback_query: Dict[str, Any]):
    """Process incoming button clicks from Telegram."""
    query_id = callback_query["id"]
    data = callback_query["data"]
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    _LOGGER.info("Received Telegram button callback: %s", data)

    # Acknowledge callback immediately to remove loading state in Telegram
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        requests.post(url, json={"callback_query_id": query_id}, timeout=5)
    except Exception as e:
        _LOGGER.warning("Could not answer callback query: %s", e)

    # Parse action and device id
    action = ""
    device_id = ""
    if data.startswith("toggle_ac_"):
        action = "ac"
        device_id = data.replace("toggle_ac_", "")
    elif data.startswith("toggle_dc_"):
        action = "dc"
        device_id = data.replace("toggle_dc_", "")
    elif data.startswith("refresh_"):
        action = "refresh"
        device_id = data.replace("refresh_", "")

    if not device_id:
        return

    states = device_states.get(device_id, {})
    
    if action == "ac":
        current_state = states.get("oac", 0)
        target = "OFF" if current_state == 1 else "ON"
        topic = f"jackery/{device_id}/command/oac"
        if mqtt_client:
            mqtt_client.publish(topic, target, retain=False)
            reply = f"✉️ Published command `{target}` to MQTT topic `{topic}`. (AC output toggle requested)"
        else:
            reply = "❌ MQTT broker disconnected! Cannot send command."
        send_telegram_message(reply)
        
    elif action == "dc":
        current_state = states.get("odc", 0)
        target = "OFF" if current_state == 1 else "ON"
        topic = f"jackery/{device_id}/command/odc"
        if mqtt_client:
            mqtt_client.publish(topic, target, retain=False)
            reply = f"✉️ Published command `{target}` to MQTT topic `{topic}`. (DC output toggle requested)"
        else:
            reply = "❌ MQTT broker disconnected! Cannot send command."
        send_telegram_message(reply)
        
    elif action == "refresh":
        _LOGGER.info("Manual refresh triggered by bot user")
        # Trigger immediate API fetch asynchronously
        threading.Thread(target=poll_device, args=(device_id, True)).start()

def handle_telegram_message(message: Dict[str, Any]):
    """Process incoming text messages to the Telegram Bot."""
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if text == "/start" or text == "/help":
        help_text = (
            "🤖 *Jackery Telegram Bridge Bot*\n\n"
            "Commands:\n"
            "/status \- View current state & access toggle controls\n"
            "/refresh \- Trigger an immediate query of the API"
        )
        send_telegram_message(help_text)
        
    elif text == "/status":
        if not device_states:
            send_telegram_message("❌ No device telemetry collected yet. Please wait...")
            return
            
        for dev_id, states in device_states.items():
            status_text = format_status_message(dev_id, states)
            keyboard = make_control_keyboard(dev_id)
            send_telegram_message(status_text, reply_markup=keyboard)
            
    elif text == "/refresh":
        if not device_states:
            send_telegram_message("❌ No devices registered to refresh.")
            return
        send_telegram_message("🔄 Telemetry refresh requested. Fetching...")
        for dev_id in device_states.keys():
            threading.Thread(target=poll_device, args=(dev_id, True)).start()

def format_status_message(device_id: str, states: Dict[str, Any]) -> str:
    """Format status values into a user-friendly Telegram markdown message."""
    batt = states.get("rb", 0)
    temp = states.get("bt", 0)
    op = states.get("op", 0)
    ip = states.get("ip", 0)
    acip = states.get("acip", 0)
    
    # Calculate Solar vs AC Input
    if acip > 0:
        ac_input = acip
        solar_input = 0
        charging_source = "🔌 Wall (AC)"
    elif ip > 0:
        ac_input = 0
        solar_input = ip
        charging_source = "☀️ Solar (PV)"
    else:
        ac_input = 0
        solar_input = 0
        charging_source = "None"
        
    ac_out = "ON 🟢" if states.get("oac") == 1 else "OFF 🔴"
    dc_out = "ON 🟢" if states.get("odc") == 1 else "OFF 🔴"
    
    # settings
    eco_mode = "ON 🟢" if states.get("pm") == 1 else "OFF 🔴"
    charge_speed = states.get("cs", "Unknown")
    
    return (
        f"🔋 *Jackery Status* (ID: `{device_id}`)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• *Battery level*: `{batt}%`\n"
        f"• *Temperature*: `{temp}°C`\n"
        f"• *Output Power*: `{op} W` (AC Output: {ac_out} | DC Output: {dc_out})\n"
        f"• *Input Power*: `{ip} W` (Source: {charging_source})\n"
        f"  - AC Wall input: `{ac_input} W`\n"
        f"  - Solar Harvest: `{solar_input} W`\n"
        f"• *Eco-mode (PM)*: {eco_mode}\n"
        f"• *Charging speed*: `{charge_speed}`\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def telegram_polling_loop():
    """Loop to poll Telegram Bot API for messages & button callbacks."""
    _LOGGER.info("Starting Telegram Bot listener thread...")
    offset = 0
    while running:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(5)
            continue
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            res = requests.get(url, params=params, timeout=25)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        
                        if "callback_query" in update:
                            handle_callback_query(update["callback_query"])
                        elif "message" in update:
                            handle_telegram_message(update["message"])
                            
        except Exception as e:
            _LOGGER.debug("Telegram polling exception: %s", e)
            
        time.sleep(1)

def on_mqtt_connect(client, userdata, flags, rc):
    """Callback when connecting to the MQTT broker."""
    if rc == 0:
        _LOGGER.info("Connected to MQTT Broker successfully!")
        # Optional: subscribe to control topics from Home Assistant if they wish to command it via HA
        # E.g. client.subscribe(f"jackery/+/command/+")
    else:
        _LOGGER.error("Failed to connect to MQTT, return code %d", rc)

def setup_mqtt_discovery(device_id: str, device_name: str, product_type: str):
    """Publish MQTT configuration topics for Home Assistant Autodiscovery."""
    if not mqtt_client:
        return

    _LOGGER.info("Registering MQTT Home Assistant Autodiscovery topics for device %s...", device_id)
    
    device_info = {
        "identifiers": [f"jackery_{device_id}"],
        "name": device_name,
        "manufacturer": "Jackery",
        "model": product_type
    }
    
    # Helpers for discovery definitions
    # Format: (key, name, unit, device_class, state_class, is_binary)
    sensors = [
        ("rb", "Remaining Battery", "%", "battery", "measurement", False),
        ("bt", "Battery Temperature", "°C", "temperature", "measurement", False),
        ("op", "Output Power", "W", "power", "measurement", False),
        ("ip", "Input Power", "W", "power", "measurement", False),
        ("acip", "AC Input Power", "W", "power", "measurement", False),
        ("solar_input", "Solar Input Power", "W", "power", "measurement", False),
        ("it", "Time to Full", "h", "duration", "measurement", False),
        ("ot", "Remaining Output Time", "h", "duration", "measurement", False),
        ("acov", "AC Output Voltage", "V", "voltage", "measurement", False),
        # Diagnostics
        ("cs", "Charging Speed", None, None, None, False),
        ("ast", "Auto Shutdown Time", "h", None, None, False),
        ("sltb", "Screen Timeout Setting", None, None, None, False),
        ("lm", "Light Mode", None, None, None, False),
        # Binary Sensors
        ("oac", "AC Output Active", None, "power", None, True),
        ("odc", "DC Output Active", None, "power", None, True),
        ("ac_active", "AC Wall Input Active", None, "plug", None, True),
        ("sfc", "Emergency Fast Charge", None, None, None, True),
        ("pm", "Eco Mode", None, None, None, True),
    ]

    state_topic = f"jackery/sensor/jackery_{device_id}/state"

    for key, name, unit, dev_class, state_class, is_binary in sensors:
        component = "binary_sensor" if is_binary else "sensor"
        config_topic = f"{MQTT_DISCOVERY_PREFIX}/{component}/jackery_{device_id}/{key}/config"
        
        config_payload = {
            "name": f"{device_name} {name}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "unique_id": f"jackery_{device_id}_{key}",
            "device": device_info
        }
        
        if unit:
            config_payload["unit_of_measurement"] = unit
        if dev_class:
            config_payload["device_class"] = dev_class
        if state_class:
            config_payload["state_class"] = state_class

        # For binary sensors, we map our states 0/1 or False/True to ON/OFF
        if is_binary:
            if key in ["oac", "odc", "pm", "sfc"]:
                config_payload["value_template"] = f"{{{{ 'ON' if value_json.{key} == 1 else 'OFF' }}}}"
            elif key == "ac_active":
                config_payload["value_template"] = f"{{{{ 'ON' if value_json.ac_active == 'ON' else 'OFF' }}}}"

        mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)

def evaluate_alerts(device_id: str, states: Dict[str, Any]):
    """Evaluate metric thresholds and trigger Telegram alert notifications."""
    batt = states.get("rb", 100)
    temp = states.get("bt", 0)
    
    # Initialize alert states if not present
    if device_id not in alert_states:
        alert_states[device_id] = {"low": False, "critical": False, "temp": False}
        
    alerts = alert_states[device_id]
    
    # Critical Alert (<5%)
    if batt <= CRITICAL_BATTERY_THRESHOLD:
        if not alerts["critical"]:
            send_telegram_message(f"🚨 *CRITICAL ALERT:* Jackery battery is critically low: `{batt}%`!")
            alerts["critical"] = True
    else:
        # Reset with hysteresis
        if batt >= (CRITICAL_BATTERY_THRESHOLD + 2):
            alerts["critical"] = False

    # Low Alert (<20%)
    if batt <= LOW_BATTERY_THRESHOLD:
        if not alerts["low"] and not alerts["critical"]:
            send_telegram_message(f"⚠️ *LOW BATTERY WARNING:* Jackery battery level has dropped to `{batt}%`.")
            alerts["low"] = True
    else:
        # Reset with hysteresis
        if batt >= (LOW_BATTERY_THRESHOLD + 2):
            alerts["low"] = False
            
    # Temperature Alert (>45°C)
    if temp >= 45:
        if not alerts["temp"]:
            send_telegram_message(f"🔥 *TEMPERATURE WARNING:* Jackery battery temperature is high: `{temp}°C`!")
            alerts["temp"] = True
    else:
        if temp <= 40:
            alerts["temp"] = False

def poll_device(device_id: str, manual_refresh: bool = False):
    """Query Jackery API for specific device telemetry, process it, and publish."""
    if not api_client:
        return

    _LOGGER.info("Polling Jackery device: %s (manual_refresh=%s)", device_id, manual_refresh)
    try:
        detail = api_client.get_device_detail(device_id)
        data = detail.get("data", {})
        properties = data.get("properties", {})
        
        if not properties:
            _LOGGER.warning("No properties returned for device %s", device_id)
            return

        # Extract values
        rb = properties.get("rb")
        bt = properties.get("bt", 0) / 10.0 if "bt" in properties else 0
        op = properties.get("op", 0)
        ip = properties.get("ip", 0)
        acip = properties.get("acip", 0)
        it = properties.get("it", 0) / 10.0 if "it" in properties else 0
        ot = properties.get("ot", 0) / 10.0 if "ot" in properties else 0
        acov = properties.get("acov", 0) / 10.0 if "acov" in properties else 0

        # Calculations
        if acip > 0:
            ac_input = acip
            solar_input = 0
            ac_active = "ON"
        else:
            ac_input = 0
            solar_input = ip
            ac_active = "OFF"

        # Diagnostic battery settings
        sfc = properties.get("sfc", 0) # fast charge
        cs = properties.get("cs", "normal") # charge speed
        lps = properties.get("lps", 0) # performance setting
        pm = properties.get("pm", 0) # energy saving
        ast = properties.get("ast", 0) # auto saving duration
        sltb = properties.get("sltb", 0) # screen timeout
        lm = properties.get("lm", 0) # light mode

        # Pack states
        state_payload = {
            "rb": rb,
            "bt": bt,
            "op": op,
            "ip": ip,
            "acip": ac_input,
            "solar_input": solar_input,
            "it": it,
            "ot": ot,
            "acov": acov,
            "oac": properties.get("oac", 0),
            "odc": properties.get("odc", 0),
            "ac_active": ac_active,
            "sfc": sfc,
            "cs": cs,
            "lps": lps,
            "pm": pm,
            "ast": ast,
            "sltb": sltb,
            "lm": lm,
        }

        # Update in-memory state
        device_states[device_id] = state_payload

        # Publish state to MQTT
        if mqtt_client:
            state_topic = f"jackery/sensor/jackery_{device_id}/state"
            mqtt_client.publish(state_topic, json.dumps(state_payload), retain=True)
            _LOGGER.info("Published telemetry updates to MQTT for device %s", device_id)

        # Evaluate alerts
        evaluate_alerts(device_id, state_payload)
        
        # If manual refresh from Telegram, send confirmation status
        if manual_refresh:
            status_text = "🔄 *Status Refreshed:*\n\n" + format_status_message(device_id, state_payload)
            keyboard = make_control_keyboard(device_id)
            send_telegram_message(status_text, reply_markup=keyboard)

    except Exception as e:
        _LOGGER.error("Failed to query or process device states: %s", e)
        if manual_refresh:
            send_telegram_message(f"❌ Failed to refresh device status: `{e}`")

def main_loop():
    """Main program execution loop."""
    global api_client, mqtt_client
    
    _LOGGER.info("Starting Jackery Integration stack service...")
    
    # 1. Initialize Jackery API
    if not JACKERY_USERNAME or not JACKERY_PASSWORD:
        _LOGGER.error("JACKERY_USERNAME or JACKERY_PASSWORD is not set in environment!")
        sys.exit(1)
        
    api_client = JackeryAPI(JACKERY_USERNAME, JACKERY_PASSWORD)
    try:
        # Check authentication (loads cache, otherwise logins)
        if not api_client._token:
            api_client.login()
    except JackeryAuthenticationError as e:
        _LOGGER.error("Failed to authenticate with Jackery Cloud API: %s", e)
        sys.exit(1)

    # 2. Setup MQTT client
    if MQTT_BROKER:
        _LOGGER.info("Initializing MQTT client connecting to %s:%d...", MQTT_BROKER, MQTT_PORT)
        mqtt_client = mqtt.Client()
        if MQTT_USERNAME and MQTT_PASSWORD:
            mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        mqtt_client.on_connect = on_mqtt_connect
        try:
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            mqtt_client.loop_start()
        except Exception as e:
            _LOGGER.error("Failed to connect to MQTT broker: %s. Continuing without MQTT.", e)
            mqtt_client = None
    else:
        _LOGGER.warning("MQTT_BROKER not set in environment. Continuing in Telegram-only mode.")

    # 3. Discover devices and register topics
    devices = []
    try:
        res = api_client.get_device_list()
        devices = res.get("data", [])
    except Exception as e:
        _LOGGER.error("Failed to retrieve device list during startup: %s", e)
        sys.exit(1)

    if not devices:
        _LOGGER.error("No devices found bound to this Jackery account!")
        sys.exit(1)

    # Filter by user configuration
    monitored_devices = []
    for d in devices:
        dev_id = d.get("devId")
        if JACKERY_DEVICE_ID and dev_id != JACKERY_DEVICE_ID:
            continue
        monitored_devices.append(d)

    if not monitored_devices:
        _LOGGER.error("Monitored device list is empty! (Checked configured JACKERY_DEVICE_ID: %s)", JACKERY_DEVICE_ID)
        sys.exit(1)

    # Register autodiscovery configs
    for d in monitored_devices:
        dev_id = d.get("devId")
        dev_name = d.get("devName", f"Jackery Explorer {dev_id}")
        prod_type = d.get("productType", "Explorer 2000 Plus")
        setup_mqtt_discovery(dev_id, dev_name, prod_type)
        # Prepopulate state dict
        device_states[dev_id] = {}

    # Start Telegram Listener thread
    telegram_thread = threading.Thread(target=telegram_polling_loop)
    telegram_thread.daemon = True
    telegram_thread.start()

    # Initial poll
    for d in monitored_devices:
        poll_device(d["devId"])

    # Main Polling loop
    _LOGGER.info("Entering main poll loop. Interval: %d seconds.", POLL_INTERVAL_SEC)
    while running:
        try:
            for d in monitored_devices:
                poll_device(d["devId"])
        except Exception as e:
            _LOGGER.error("Error in main poll iteration: %s", e)
            
        # Sleep incrementally to allow graceful exit shutdown
        for _ in range(POLL_INTERVAL_SEC):
            if not running:
                break
            time.sleep(1)

    # Cleanup
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    _LOGGER.info("Service shutdown completed.")

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        _LOGGER.info("Received termination. Shutting down...")
        running = False
