import os
import sys
import time
import json
import logging
import asyncio
import traceback
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

import aiohttp
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

import socketry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
_LOGGER = logging.getLogger("jackery_bridge")

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

TIMEZONE_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Los_Angeles"))
try:
    APP_TZ = zoneinfo.ZoneInfo(TIMEZONE_NAME)
except Exception as e:
    APP_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

def get_current_time_str() -> str:
    now = datetime.now(APP_TZ)
    formatted = now.strftime("%I:%M:%S %p %Z")
    if formatted.startswith("0"):
        formatted = formatted[1:]
    return formatted

# Global states
running = True
mqtt_client: Optional[mqtt.Client] = None
jackery_client: Optional[socketry.Client] = None
monitored_devices: List[Dict[str, Any]] = []
device_states: Dict[str, Dict[str, Any]] = {}
alert_states: Dict[str, Dict[str, bool]] = {}

telegram_session: Optional[aiohttp.ClientSession] = None

async def send_telegram_message(text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not telegram_session:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(2):
        try:
            async with telegram_session.post(url, json=payload, timeout=10) as res:
                if res.status != 200:
                    if "parse_mode" in payload:
                        del payload["parse_mode"]
                        async with telegram_session.post(url, json=payload, timeout=10) as res2:
                            if res2.status == 200:
                                return True
                    return False
                return True
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            _LOGGER.error("Failed to send Telegram message: %s", e)
    return False

async def edit_telegram_message(chat_id: Any, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not telegram_session:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(2):
        try:
            async with telegram_session.post(url, json=payload, timeout=10) as res:
                if res.status == 200:
                    return True
                text_resp = await res.text()
                if "message is not modified" in text_resp:
                    return True
                if "parse_mode" in payload:
                    del payload["parse_mode"]
                    async with telegram_session.post(url, json=payload, timeout=10) as res2:
                        if res2.status == 200 or "message is not modified" in await res2.text():
                            return True
                return False
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
    return False

async def answer_callback_query(query_id: str, text: str = "", show_alert: bool = False) -> bool:
    if not TELEGRAM_BOT_TOKEN or not query_id or not telegram_session:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": query_id, "text": text, "show_alert": show_alert}
    try:
        async with telegram_session.post(url, json=payload, timeout=5) as res:
            return res.status == 200
    except Exception:
        return False

def make_control_keyboard(device_id: str) -> Dict[str, Any]:
    states = device_states.get(device_id, {})
    ac_label = "🔴 Turn AC OFF" if states.get("oac") == 1 else "🟢 Turn AC ON"
    dc_label = "🔴 Turn DC OFF" if states.get("odc") == 1 else "🟢 Turn DC ON"
    return {"inline_keyboard": [
        [{"text": ac_label, "callback_data": f"toggle_ac_{device_id}"},
         {"text": dc_label, "callback_data": f"toggle_dc_{device_id}"}],
        [{"text": "🔄 Refresh", "callback_data": f"refresh_{device_id}"},
         {"text": "⚙️ Settings", "callback_data": f"settings_{device_id}"}]
    ]}

def make_settings_keyboard(device_id: str) -> Dict[str, Any]:
    states = device_states.get(device_id, {})
    eco_label = "🔴 Disable Eco (PM)" if states.get("pm") == 1 else "🟢 Enable Eco (PM)"
    charge_speed = states.get("cs", "Unknown")
    cs_label = "⚡ Set Fast Charge" if charge_speed == "mute" else "🤫 Set Quiet Charge"
    cs_target = "fast" if charge_speed == "mute" else "mute"
    
    bp = states.get("bp", 0) # battery-protection is usually 0/1
    bp_label = "🔴 Disable Battery Protect" if bp == 1 else "🟢 Enable Battery Protect"
    
    return {"inline_keyboard": [
        [{"text": eco_label, "callback_data": f"toggle_eco_{device_id}"}],
        [{"text": bp_label, "callback_data": f"toggle_bp_{device_id}"}],
        [{"text": cs_label, "callback_data": f"set_cs_{cs_target}_{device_id}"}],
        [{"text": "◀️ Back to Controls", "callback_data": f"back_{device_id}"}]
    ]}

def format_status_message(device_id: str, states: Dict[str, Any]) -> str:
    batt = states.get("rb", 0)
    temp = states.get("bt", 0)
    op = states.get("op", 0)
    ip = states.get("ip", 0)
    acip = states.get("acip", 0)
    
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
    
    eco_mode = "ON 🟢" if states.get("pm") == 1 else "OFF 🔴"
    bp_mode = "ON 🟢" if states.get("bp") == 1 else "OFF 🔴"
    charge_speed = states.get("cs", "Unknown")
    now_str = get_current_time_str()
    
    online_status = states.get("online_status", 1)
    last_poll_epoch = states.get("last_poll_epoch", 0)
    cloud_status_str = "ONLINE 🟢" if online_status == 1 else "OFFLINE 🔴"

    stale_warning = ""
    if online_status == 0:
        stale_warning = "\n⚠️ *WARNING: DEVICE REPORTED OFFLINE BY CLOUD*"
    elif last_poll_epoch > 0:
        age_sec = time.time() - last_poll_epoch
        if age_sec > 300:
            stale_warning = f"\n⚠️ *WARNING: TELEMETRY STALE*\n_(Last sync was {int(age_sec // 60)} minutes ago)_"

    return (
        f"🔋 *Jackery Status* (ID: `{device_id}`){stale_warning}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• *Status*: `{cloud_status_str}`\n"
        f"• *Battery level*: `{batt}%`\n"
        f"• *Temperature*: `{temp}°C`\n"
        f"• *Output Power*: `{op} W` (AC Output: {ac_out} | DC Output: {dc_out})\n"
        f"• *Input Power*: `{ip} W` (Source: {charging_source})\n"
        f"  - AC Wall input: `{ac_input} W`\n"
        f"  - Solar Harvest: `{solar_input} W`\n"
        f"• *Eco-mode (PM)*: {eco_mode}\n"
        f"• *Battery Protect*: {bp_mode}\n"
        f"• *Charging speed*: `{charge_speed}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ *Updated*: `{now_str}`"
    )

async def refresh_and_update_telegram(device_id: str, chat_id: Any, message_id: Optional[int] = None, show_settings: bool = False):
    _LOGGER.info("Executing telemetry refresh for %s", device_id)
    try:
        await poll_device(device_id)
        states = device_states.get(device_id)
        if not states:
            return

        status_text = f"🔋 *Live Battery Status:*\n\n" + format_status_message(device_id, states)
        keyboard = make_settings_keyboard(device_id) if show_settings else make_control_keyboard(device_id)

        if message_id:
            updated = await edit_telegram_message(chat_id, message_id, status_text, reply_markup=keyboard)
            if not updated:
                await send_telegram_message(status_text, reply_markup=keyboard)
        else:
            await send_telegram_message(status_text, reply_markup=keyboard)
    except Exception as e:
        _LOGGER.error("Error refreshing device %s: %s", device_id, e)

async def handle_callback_query(callback_query: Dict[str, Any]):
    query_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    message_id = message.get("message_id")
    chat_id = message.get("chat", {}).get("id") or TELEGRAM_CHAT_ID

    _LOGGER.info("Received callback: %s", data)

    # Parsing action
    if data.startswith("refresh_"):
        device_id = data.replace("refresh_", "")
        await answer_callback_query(query_id, text="🔄 Fetching latest telemetry...")
        asyncio.create_task(refresh_and_update_telegram(device_id, chat_id, message_id, False))
        return

    if data.startswith("settings_"):
        device_id = data.replace("settings_", "")
        await answer_callback_query(query_id)
        states = device_states.get(device_id)
        if states:
            status_text = f"🔋 *Live Battery Status:*\n\n" + format_status_message(device_id, states)
            await edit_telegram_message(chat_id, message_id, status_text, reply_markup=make_settings_keyboard(device_id))
        return

    if data.startswith("back_"):
        device_id = data.replace("back_", "")
        await answer_callback_query(query_id)
        states = device_states.get(device_id)
        if states:
            status_text = f"🔋 *Live Battery Status:*\n\n" + format_status_message(device_id, states)
            await edit_telegram_message(chat_id, message_id, status_text, reply_markup=make_control_keyboard(device_id))
        return

    # Actions that require socketry control
    action = None
    device_id = ""
    target_val = None
    
    if data.startswith("toggle_ac_"):
        device_id = data.replace("toggle_ac_", "")
        current = device_states.get(device_id, {}).get("oac", 0)
        action = ("oac", 0 if current == 1 else 1)
    elif data.startswith("toggle_dc_"):
        device_id = data.replace("toggle_dc_", "")
        current = device_states.get(device_id, {}).get("odc", 0)
        action = ("odc", 0 if current == 1 else 1)
    elif data.startswith("toggle_eco_"):
        device_id = data.replace("toggle_eco_", "")
        current = device_states.get(device_id, {}).get("pm", 0)
        action = ("energy-saving", 0 if current == 1 else 1)
    elif data.startswith("toggle_bp_"):
        device_id = data.replace("toggle_bp_", "")
        current = device_states.get(device_id, {}).get("bp", 0)
        action = ("battery-protection", 0 if current == 1 else 1)
    elif data.startswith("set_cs_"):
        # set_cs_<fast/mute>_<device_id>
        parts = data.split("_")
        target_val = parts[2]
        device_id = parts[3]
        action = ("charge-speed", target_val)

    if not action or not device_id:
        return

    await answer_callback_query(query_id, text=f"⚡ Sending {action[0]} command to Jackery...")
    try:
        dev = jackery_client.device(device_id)
        await dev.set_property(action[0], action[1], wait=False)
        # Refresh UI after a short delay
        await asyncio.sleep(2)
        show_settings = "cs" in action[0] or "energy-saving" in action[0]
        await refresh_and_update_telegram(device_id, chat_id, message_id, show_settings)
    except Exception as e:
        _LOGGER.error("Failed to send command via Socketry: %s", e)
        await answer_callback_query(query_id, text=f"❌ Command failed: {e}", show_alert=True)

async def handle_telegram_message(message: Dict[str, Any]):
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id") or TELEGRAM_CHAT_ID

    if text in ("/start", "/help"):
        await send_telegram_message(
            "🤖 *Jackery Telegram Bridge Bot*\n\n"
            "Commands:\n"
            "• `/status` - View current state & interactive card\n"
            "• `/refresh` - Force an immediate Jackery cloud telemetry pull\n"
            "• `/help` - Show this guidance message\n\n"
            "The bridge automatically polls Jackery every 60 seconds and sends low-battery alerts."
        )
    elif text == "/status":
        if not device_states:
            await send_telegram_message("⏳ Collecting first telemetry reading. Please wait...")
            return
        for dev_id, states in device_states.items():
            status_text = f"🔋 *Live Battery Status:*\n\n" + format_status_message(dev_id, states)
            await send_telegram_message(status_text, reply_markup=make_control_keyboard(dev_id))
    elif text == "/refresh":
        if not monitored_devices:
            await send_telegram_message(f"⚠️ No devices found.")
            return
        await send_telegram_message("🔄 Telemetry refresh requested...")
        for d in monitored_devices:
            dev_id = str(d.get("devSn") or d.get("devId"))
            asyncio.create_task(refresh_and_update_telegram(dev_id, chat_id, None, False))

async def telegram_polling_loop():
    _LOGGER.info("Starting Telegram Bot listener...")
    offset = 0
    while running:
        if not TELEGRAM_BOT_TOKEN or not telegram_session:
            await asyncio.sleep(5)
            continue
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 15, "allowed_updates": json.dumps(["message", "callback_query", "edited_message"])}
            async with telegram_session.get(url, params=params, timeout=25) as res:
                if res.status == 200:
                    data = await res.json()
                    if data.get("ok"):
                        updates = data.get("result", [])
                        for update in updates:
                            offset = update["update_id"] + 1
                            if "callback_query" in update:
                                await handle_callback_query(update["callback_query"])
                            elif "message" in update:
                                await handle_telegram_message(update["message"])
                elif res.status == 409:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(2)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            _LOGGER.warning("Telegram polling error: %s", e)
            await asyncio.sleep(2)

def evaluate_alerts(device_id: str, states: Dict[str, Any]):
    batt = states.get("rb", 100)
    temp = states.get("bt", 0)
    
    if device_id not in alert_states:
        alert_states[device_id] = {"low": False, "critical": False, "temp": False}
        
    alerts = alert_states[device_id]
    
    if batt <= CRITICAL_BATTERY_THRESHOLD:
        if not alerts["critical"]:
            asyncio.create_task(send_telegram_message(f"🚨 *CRITICAL ALERT:* Jackery battery is critically low: `{batt}%`!"))
            alerts["critical"] = True
    else:
        if batt >= (CRITICAL_BATTERY_THRESHOLD + 2):
            alerts["critical"] = False

    if batt <= LOW_BATTERY_THRESHOLD:
        if not alerts["low"] and not alerts["critical"]:
            asyncio.create_task(send_telegram_message(f"⚠️ *LOW BATTERY WARNING:* Jackery battery level has dropped to `{batt}%`."))
            alerts["low"] = True
    else:
        if batt >= (LOW_BATTERY_THRESHOLD + 2):
            alerts["low"] = False
            
    if temp >= 45:
        if not alerts["temp"]:
            asyncio.create_task(send_telegram_message(f"🔥 *TEMPERATURE WARNING:* Jackery battery temperature is high: `{temp}°C`!"))
            alerts["temp"] = True
    else:
        if temp <= 40:
            alerts["temp"] = False

def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        _LOGGER.info("Connected to MQTT Broker successfully!")
    else:
        _LOGGER.error("Failed to connect to MQTT, return code %d", rc)

def setup_mqtt_discovery(device_id: str, device_name: str, product_type: str):
    if not mqtt_client:
        return
    _LOGGER.info("Registering MQTT Home Assistant Autodiscovery topics for device %s...", device_id)
    device_info = {
        "identifiers": [f"jackery_{device_id}"],
        "name": device_name,
        "manufacturer": "Jackery",
        "model": product_type
    }
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
        ("cs", "Charging Speed", None, None, None, False),
        ("ast", "Auto Shutdown Time", "h", None, None, False),
        ("sltb", "Screen Timeout Setting", None, None, None, False),
        ("lm", "Light Mode", None, None, None, False),
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
        if unit: config_payload["unit_of_measurement"] = unit
        if dev_class: config_payload["device_class"] = dev_class
        if state_class: config_payload["state_class"] = state_class

        if is_binary:
            if key in ["oac", "odc", "pm", "sfc"]:
                config_payload["value_template"] = f"{{{{ 'ON' if value_json.{key} == 1 else 'OFF' }}}}"
            elif key == "ac_active":
                config_payload["value_template"] = f"{{{{ 'ON' if value_json.ac_active == 'ON' else 'OFF' }}}}"

        mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)

async def poll_device(device_id: str):
    if not jackery_client:
        return
    _LOGGER.info("Polling Jackery device (Socketry): %s", device_id)
    try:
        dev = jackery_client.device(device_id)
        data = await dev.get_all_properties()
        properties = data.get("properties", {})
        
        if not properties:
            return

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

        if acip > 0:
            ac_input = acip; solar_input = 0; ac_active = "ON"
        else:
            ac_input = 0; solar_input = ip; ac_active = "OFF"

        sfc = properties.get("sfc", 0)
        cs = "mute" if properties.get("cs", 0) == 1 else "fast"
        lps = properties.get("lps", 0)
        pm = properties.get("pm", 0)
        bp = properties.get("bp", 0)
        ast = properties.get("ast", 0)
        sltb = properties.get("sltb", 0)
        lm = properties.get("lm", 0)

        state_payload = {
            "rb": rb, "bt": bt, "op": op, "ip": ip, "acip": ac_input,
            "solar_input": solar_input, "it": it, "ot": ot, "acov": acov,
            "oac": properties.get("oac", 0), "odc": properties.get("odc", 0),
            "ac_active": ac_active, "sfc": sfc, "cs": cs, "lps": lps,
            "pm": pm, "bp": bp, "ast": ast, "sltb": sltb, "lm": lm,
            "update_time_ms": update_time_ms, "online_status": online_status,
            "last_poll_epoch": time.time(),
        }

        device_states[device_id] = state_payload

        _LOGGER.info(
            "Telemetry for %s: SoC=%s%%, PV=%sW, AC_in=%sW, Total_out=%sW, Temp=%s°C, AC_out=%s, DC_out=%s",
            device_id, rb, solar_input, ac_input, op, bt,
            "ON" if properties.get("oac") == 1 else "OFF",
            "ON" if properties.get("odc") == 1 else "OFF",
        )

        if mqtt_client:
            state_topic = f"jackery/sensor/jackery_{device_id}/state"
            mqtt_client.publish(state_topic, json.dumps(state_payload), retain=True)

        evaluate_alerts(device_id, state_payload)
        
    except Exception as e:
        _LOGGER.error("Failed to poll device %s: %s", device_id, traceback.format_exc())

async def main_async():
    global jackery_client, mqtt_client, telegram_session

    _LOGGER.info("Starting Jackery Integration (Socketry Engine)...")
    
    if not JACKERY_USERNAME or not JACKERY_PASSWORD:
        _LOGGER.error("JACKERY_USERNAME or JACKERY_PASSWORD is not set!")
        sys.exit(1)

    try:
        jackery_client = await socketry.Client.login(JACKERY_USERNAME, JACKERY_PASSWORD)
    except Exception as e:
        _LOGGER.error("Failed to authenticate with Jackery via Socketry: %s", e)
        sys.exit(1)

    if MQTT_BROKER:
        mqtt_client = mqtt.Client()
        if MQTT_USERNAME and MQTT_PASSWORD:
            mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        mqtt_client.on_connect = on_mqtt_connect
        try:
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
        except Exception as e:
            _LOGGER.error("Failed to connect to MQTT: %s", e)

    telegram_session = aiohttp.ClientSession()
    asyncio.create_task(telegram_polling_loop())

    _LOGGER.info("Fetching bound Jackery devices...")
    try:
        await jackery_client.fetch_devices()
        for d in jackery_client.devices:
            dev_sn = d.get("devSn")
            if JACKERY_DEVICE_ID and dev_sn != JACKERY_DEVICE_ID and d.get("devId") != JACKERY_DEVICE_ID:
                continue
            monitored_devices.append(d)
            device_name = d.get("devName", "Jackery Explorer")
            setup_mqtt_discovery(dev_sn, device_name, "Explorer 2000 Plus")
    except Exception as e:
        _LOGGER.error("Failed to fetch devices: %s", e)

    if not monitored_devices:
        _LOGGER.warning("No devices found in Jackery account! Waiting for device binding...")

    while running:
        if monitored_devices:
            for d in monitored_devices:
                await poll_device(str(d.get("devSn")))
        else:
            try:
                await jackery_client.fetch_devices()
                if jackery_client.devices:
                    for d in jackery_client.devices:
                        dev_sn = d.get("devSn")
                        monitored_devices.append(d)
                        setup_mqtt_discovery(dev_sn, d.get("devName", "Jackery"), "Explorer")
            except Exception:
                pass
        await asyncio.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down...")
        running = False
        if telegram_session and not telegram_session.closed:
            asyncio.run(telegram_session.close())
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
