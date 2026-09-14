import os
import sys
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
import time
import json
import logging
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

# Timezone Configuration (Defaults to America/Los_Angeles)
TIMEZONE_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Los_Angeles"))
try:
    APP_TZ = zoneinfo.ZoneInfo(TIMEZONE_NAME)
    _LOGGER.info("Configured application timezone: %s", TIMEZONE_NAME)
except Exception as e:
    _LOGGER.warning("Could not load timezone '%s' (%s). Falling back to America/Los_Angeles", TIMEZONE_NAME, e)
    APP_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

def get_current_time_str() -> str:
    """Return current timestamp formatted in local timezone (e.g. '5:26:15 PM PDT')."""
    now = datetime.now(APP_TZ)
    formatted = now.strftime("%I:%M:%S %p %Z")
    if formatted.startswith("0"):
        formatted = formatted[1:]
    return formatted

# Global states
running = True
api_client: Optional[JackeryAPI] = None
mqtt_client: Optional[mqtt.Client] = None
monitored_devices: List[Dict[str, Any]] = []
device_states: Dict[str, Dict[str, Any]] = {}  # device_id -> properties
alert_states: Dict[str, Dict[str, bool]] = {}   # device_id -> alert_name -> triggered

def create_telegram_api_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "JackeryBridge-API/1.0"})
    return s

telegram_api_session = create_telegram_api_session()

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

    for attempt in range(2):
        try:
            res = telegram_api_session.post(url, json=payload, timeout=10)
            if res.status_code != 200:
                _LOGGER.error("Failed to send Telegram message (%s): %s", res.status_code, res.text)
                if "parse_mode" in payload:
                    del payload["parse_mode"]
                    res2 = telegram_api_session.post(url, json=payload, timeout=10)
                    if res2.status_code == 200:
                        _LOGGER.info("Delivered plain text fallback Telegram message.")
                        return True
                return False
            return True
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
                continue
            _LOGGER.error("Failed to send Telegram message: %s", e)
            return False
    return False

def edit_telegram_message(chat_id: Any, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    """Edit an existing Telegram message in-place."""
    if not TELEGRAM_BOT_TOKEN:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(2):
        try:
            res = telegram_api_session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                _LOGGER.info("Successfully updated Telegram status card (message_id=%s)", message_id)
                return True
            elif "message is not modified" in res.text:
                _LOGGER.info("Telegram message %s is already up to date.", message_id)
                return True
            else:
                if "parse_mode" in payload:
                    del payload["parse_mode"]
                    res2 = telegram_api_session.post(url, json=payload, timeout=10)
                    if res2.status_code == 200 or "message is not modified" in res2.text:
                        return True
                _LOGGER.warning("Failed to edit Telegram message (%s): %s", res.status_code, res.text)
                return False
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
                continue
            _LOGGER.warning("Error editing Telegram message %s: %s", message_id, e)
            return False
    return False

def answer_callback_query(query_id: str, text: str = "", show_alert: bool = False) -> bool:
    """Acknowledge a Telegram button click and optionally display a toast notification or modal alert."""
    if not TELEGRAM_BOT_TOKEN or not query_id:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {
        "callback_query_id": query_id,
        "text": text,
        "show_alert": show_alert
    }
    try:
        res = telegram_api_session.post(url, json=payload, timeout=5)
        return res.status_code == 200
    except Exception as e:
        _LOGGER.warning("Could not answer callback query %s: %s", query_id, e)
        return False

def make_control_keyboard(device_id: str) -> Dict[str, Any]:
    """Generate the inline keyboard with toggle buttons."""
    device_id = str(device_id)
    states = device_states.get(device_id, {})
    ac_label = "🔴 Turn AC OFF" if states.get("oac") == 1 else "🟢 Turn AC ON"
    dc_label = "🔴 Turn DC OFF" if states.get("odc") == 1 else "🟢 Turn DC ON"
    
    keyboard = []
    # If MQTT is connected, expose remote switching buttons
    if mqtt_client:
        keyboard.append([
            {"text": ac_label, "callback_data": f"toggle_ac_{device_id}"},
            {"text": dc_label, "callback_data": f"toggle_dc_{device_id}"}
        ])
    # Always include the instant refresh button
    keyboard.append([
        {"text": "🔄 Refresh Status", "callback_data": f"refresh_{device_id}"}
    ])
    
    return {"inline_keyboard": keyboard}

def refresh_and_update_telegram(device_id: str, chat_id: Any, message_id: Optional[int] = None):
    """Fetch latest telemetry from Jackery API and update status card in Telegram."""
    device_id = str(device_id)
    _LOGGER.info("Executing on-demand telemetry refresh for device %s (message_id=%s)", device_id, message_id)
    try:
        poll_device(device_id)
        states = device_states.get(device_id)
        if not states:
            _LOGGER.warning("No state available after poll for %s", device_id)
            return

        status_text = (
            f"🔋 *Live Battery Status:*\n\n"
            + format_status_message(device_id, states)
        )
        keyboard = make_control_keyboard(device_id)

        if message_id:
            updated = edit_telegram_message(chat_id, message_id, status_text, reply_markup=keyboard)
            if not updated:
                _LOGGER.debug("Edit message returned False, sending fresh card as fallback")
                send_telegram_message(status_text, reply_markup=keyboard)
        else:
            send_telegram_message(status_text, reply_markup=keyboard)

    except Exception as e:
        _LOGGER.error("Error refreshing device status for %s: %s", device_id, e)

def handle_callback_query(callback_query: Dict[str, Any]):
    """Process incoming button clicks from Telegram."""
    query_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    message_id = message.get("message_id")
    chat_id = message.get("chat", {}).get("id") or TELEGRAM_CHAT_ID

    _LOGGER.info(
        "Received Telegram button callback: data='%s', query_id='%s', message_id=%s",
        data, query_id, message_id
    )

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
        if query_id:
            answer_callback_query(query_id, text="Unknown device action.")
        return

    states = device_states.get(device_id, {})
    
    if action == "ac":
        if not mqtt_client:
            answer_callback_query(
                query_id,
                text="⚠️ Remote AC/DC switching requires Home Assistant/MQTT. In standalone cloud mode, Jackery API is read-only telemetry.",
                show_alert=True
            )
        else:
            answer_callback_query(query_id, text="⚡ Sending AC toggle command...")
            current_state = states.get("oac", 0)
            target = "OFF" if current_state == 1 else "ON"
            topic = f"jackery/{device_id}/command/oac"
            mqtt_client.publish(topic, target, retain=False)
            reply = f"✉️ Published command `{target}` to MQTT topic `{topic}`. (AC output toggle requested)"
            send_telegram_message(reply)
        
    elif action == "dc":
        if not mqtt_client:
            answer_callback_query(
                query_id,
                text="⚠️ Remote AC/DC switching requires Home Assistant/MQTT. In standalone cloud mode, Jackery API is read-only telemetry.",
                show_alert=True
            )
        else:
            answer_callback_query(query_id, text="⚡ Sending DC toggle command...")
            current_state = states.get("odc", 0)
            target = "OFF" if current_state == 1 else "ON"
            topic = f"jackery/{device_id}/command/odc"
            mqtt_client.publish(topic, target, retain=False)
            reply = f"✉️ Published command `{target}` to MQTT topic `{topic}`. (DC output toggle requested)"
            send_telegram_message(reply)
        
    elif action == "refresh":
        # Immediate toast in Telegram UI
        answer_callback_query(query_id, text="🔄 Fetching latest Jackery telemetry...")
        threading.Thread(
            target=refresh_and_update_telegram,
            args=(device_id, chat_id, message_id)
        ).start()

def handle_telegram_message(message: Dict[str, Any]):
    """Process incoming text messages to the Telegram Bot."""
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id") or TELEGRAM_CHAT_ID

    _LOGGER.info("Received Telegram text message from %s: '%s'", chat_id, text)

    if text in ("/start", "/help"):
        help_text = (
            "🤖 *Jackery Telegram Bridge Bot*\n\n"
            "Commands:\n"
            "• `/status` - View current state & interactive card\n"
            "• `/refresh` - Force an immediate Jackery cloud telemetry pull\n"
            "• `/help` - Show this guidance message\n\n"
            "The bridge automatically polls Jackery every 60 seconds and sends low-battery alerts below 20% and 5%."
        )
        send_telegram_message(help_text)
        
    elif text == "/status":
        if not device_states:
            if not monitored_devices:
                send_telegram_message(
                    f"⚠️ *Jackery Bridge Online*\n\n"
                    f"• Account: `{JACKERY_USERNAME}`\n"
                    f"• Status: Authenticated to Jackery Cloud, waiting for battery to appear in account.\n\n"
                    f"👉 Please ensure your Explorer 2000 Plus is bound or shared in the Jackery app. Telemetry will appear automatically once detected."
                )
            else:
                send_telegram_message("⏳ Device detected! Collecting first telemetry reading. Please wait...")
            return
            
        for dev_id, states in device_states.items():
            status_text = (
                f"🔋 *Live Battery Status:*\n\n"
                + format_status_message(dev_id, states)
            )
            keyboard = make_control_keyboard(dev_id)
            send_telegram_message(status_text, reply_markup=keyboard)
            
    elif text == "/refresh":
        if not monitored_devices:
            send_telegram_message(f"⚠️ No devices bound to account `{JACKERY_USERNAME}` yet.")
            return
        send_telegram_message("🔄 Telemetry refresh requested. Fetching from Jackery cloud...")
        for d in monitored_devices:
            dev_id = str(d.get("devId") or d.get("devSn"))
            if dev_id:
                threading.Thread(
                    target=refresh_and_update_telegram,
                    args=(dev_id, chat_id, None)
                ).start()

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
    now_str = get_current_time_str()
    
    update_time_ms = states.get("update_time_ms", 0)
    stale_warning = ""
    if update_time_ms > 0:
        update_time = datetime.fromtimestamp(update_time_ms / 1000.0, timezone.utc)
        age = datetime.now(timezone.utc) - update_time
        if age.total_seconds() > 300: # 5 minutes
            stale_warning = f"\n⚠️ *WARNING: DEVICE OFFLINE*\n_(Data is {int(age.total_seconds() // 60)} minutes old)_"

    return (
        f"🔋 *Jackery Status* (ID: `{device_id}`){stale_warning}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• *Battery level*: `{batt}%`\n"
        f"• *Temperature*: `{temp}°C`\n"
        f"• *Output Power*: `{op} W` (AC Output: {ac_out} | DC Output: {dc_out})\n"
        f"• *Input Power*: `{ip} W` (Source: {charging_source})\n"
        f"  - AC Wall input: `{ac_input} W`\n"
        f"  - Solar Harvest: `{solar_input} W`\n"
        f"• *Eco-mode (PM)*: {eco_mode}\n"
        f"• *Charging speed*: `{charge_speed}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ *Updated*: `{now_str}`"
    )

def telegram_polling_loop():
    """Loop to poll Telegram Bot API for messages & button callbacks using dedicated Keep-Alive session."""
    _LOGGER.info("Starting Telegram Bot listener thread...")
    offset = 0

    poll_session = requests.Session()
    poll_session.headers.update({"User-Agent": "JackeryBridge-Poller/1.0"})

    while running:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(5)
            continue
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {
                "offset": offset,
                "timeout": 15,
                "allowed_updates": json.dumps(["message", "callback_query", "edited_message"])
            }
            res = poll_session.get(url, params=params, timeout=25)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    updates = data.get("result", [])
                    if updates:
                        _LOGGER.info("Received %d update(s) from Telegram", len(updates))
                    for update in updates:
                        offset = update["update_id"] + 1
                        
                        if "callback_query" in update:
                            handle_callback_query(update["callback_query"])
                        elif "message" in update:
                            handle_telegram_message(update["message"])
            elif res.status_code == 409:
                _LOGGER.warning("Telegram getUpdates conflict (409). Another poller instance may be running. Waiting 5s...")
                time.sleep(5)
            else:
                _LOGGER.warning("Telegram getUpdates returned status %d: %s", res.status_code, res.text)
                time.sleep(2)
                            
        except requests.exceptions.Timeout:
            # Normal long poll timeout when no updates occurred; continue immediately
            continue
        except requests.exceptions.RequestException as e:
            _LOGGER.warning("Telegram polling network issue (retry in 2s): %s", e)
            time.sleep(2)
        except Exception as e:
            _LOGGER.error("Unexpected error in telegram_polling_loop: %s", e)
            time.sleep(2)
            
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

def poll_device(device_id: Any):
    """Query Jackery API for specific device telemetry, process it, and publish."""
    if not api_client:
        return

    device_id = str(device_id)
    _LOGGER.info("Polling Jackery device: %s", device_id)
    try:
        detail = api_client.get_device_detail(device_id)
        data = detail.get("data", {})
        properties = data.get("properties", {})
        
        if not properties:
            _LOGGER.warning("No properties returned for device %s (detail: %s)", device_id, detail)
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
        
        device_meta = data.get("device", {})
        update_time_ms = device_meta.get("updateTime", 0)
        online_status = device_meta.get("onlineStatus", 0)

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
            "update_time_ms": update_time_ms,
            "online_status": online_status,
        }

        # Update in-memory state
        device_states[device_id] = state_payload

        _LOGGER.info(
            "Telemetry for device %s: SoC=%s%%, PV=%sW, AC_in=%sW, Total_out=%sW, Temp=%s°C, AC_out=%s, DC_out=%s",
            device_id,
            rb,
            solar_input,
            ac_input,
            op,
            bt,
            "ON" if properties.get("oac") == 1 else "OFF",
            "ON" if properties.get("odc") == 1 else "OFF",
        )

        # Publish state to MQTT
        if mqtt_client:
            state_topic = f"jackery/sensor/jackery_{device_id}/state"
            mqtt_client.publish(state_topic, json.dumps(state_payload), retain=True)
            _LOGGER.info("Published telemetry updates to MQTT for device %s", device_id)

        # Evaluate alerts
        evaluate_alerts(device_id, state_payload)
        
    except Exception as e:
        _LOGGER.error("Failed to query or process device states for %s: %s", device_id, e)

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

    # Start Telegram Listener thread early so user can interact with the bot immediately
    telegram_thread = threading.Thread(target=telegram_polling_loop)
    telegram_thread.daemon = True
    telegram_thread.start()

    # 3. Discover devices and register topics (with retry loop to prevent crashlooping)
    global monitored_devices
    notified_waiting = False

    while running and not monitored_devices:
        try:
            res = api_client.get_device_list()
            _LOGGER.info("Jackery get_device_list response: %s", res)
            raw_devices = res.get("data", [])
            devices = []
            if isinstance(raw_devices, list):
                devices = raw_devices
            elif isinstance(raw_devices, dict):
                devices = raw_devices.get("list", [raw_devices])

            for d in devices:
                dev_id = d.get("devId") or d.get("devSn")
                if JACKERY_DEVICE_ID and dev_id != JACKERY_DEVICE_ID:
                    continue
                if d not in monitored_devices:
                    monitored_devices.append(d)
        except Exception as e:
            _LOGGER.error("Failed to retrieve device list: %s", e)

        if not monitored_devices:
            if not notified_waiting:
                msg = (
                    f"⚠️ *Jackery Bridge Connected to Cloud*\n\n"
                    f"• Account: `{JACKERY_USERNAME}`\n"
                    f"• Status: Authenticated successfully, but 0 batteries were found bound to this account.\n\n"
                    f"👉 If this is a secondary account, please ensure your Explorer 2000 Plus "
                    f"is shared or bound in the Jackery mobile app.\n\n"
                    f"The bridge is listening and will connect automatically as soon as it appears."
                )
                send_telegram_message(msg)
                notified_waiting = True

            _LOGGER.warning(
                "No devices found bound to Jackery account '%s'. Waiting 30s before retrying...",
                JACKERY_USERNAME
            )
            for _ in range(30):
                if not running:
                    break
                time.sleep(1)

    if not running:
        return

    # Register autodiscovery configs
    for d in monitored_devices:
        dev_id = str(d.get("devId") or d.get("devSn"))
        dev_name = d.get("devName", f"Jackery Explorer {dev_id}")
        prod_type = d.get("productType", "Explorer 2000 Plus")
        setup_mqtt_discovery(dev_id, dev_name, prod_type)
        # Prepopulate state dict
        device_states[dev_id] = {}

    device_summary = ", ".join([d.get("devName", str(d.get("devId") or d.get("devSn"))) for d in monitored_devices])
    send_telegram_message(f"✅ *Jackery Battery Connected!*\n\nDiscovered: *{device_summary}*\nStarting telemetry monitoring...")

    # Initial poll and send status card
    for d in monitored_devices:
        dev_id = str(d.get("devId") or d.get("devSn"))
        if dev_id:
            poll_device(dev_id)
            if dev_id in device_states:
                status_text = (
                    f"🔋 *Live Battery Status:*\n\n"
                    + format_status_message(dev_id, device_states[dev_id])
                )
                keyboard = make_control_keyboard(dev_id)
                send_telegram_message(status_text, reply_markup=keyboard)

    # Main Polling loop
    _LOGGER.info("Entering main poll loop. Interval: %d seconds.", POLL_INTERVAL_SEC)
    while running:
        try:
            for d in monitored_devices:
                dev_id = str(d.get("devId") or d.get("devSn"))
                if dev_id:
                    poll_device(dev_id)
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
