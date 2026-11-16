import asyncio, random
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import PeerFloodError, FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
from supabase_db import supabase
from telegram_connect import API_ID, API_HASH

SOURCE_HUMAN = {
    "mamba": "мамбы", "lovelaz": "ловелаза", "badanga": "баданги",
    "twinby": "твинби", "vznakomstve": "взнакомстве",
}

def _opener(source: str) -> str:
    place = SOURCE_HUMAN.get((source or "").lower(), "сайта")
    hello = random.choice(["привет", "приветик", "хай", "привет)"])
    return f"{hello}, с {place} 🙂"

def _mark(row, status, error=None, session=None):
    upd = {"status": status, "error": error}
    if status == "sent":
        upd["sent_at"] = datetime.now(timezone.utc).isoformat()
        if session:
            upd["assigned_session"] = session
    try:
        supabase.table("tg_outreach").update(upd).eq("id", row["id"]).execute()
    except Exception as e:
        print(f"[OUTREACH] mark error: {e}", flush=True)

async def _send_one(row):
    lead = (row.get("lead_tg") or "").lstrip("@")
    source = row.get("source") or ""
    want = (row.get("sender_username") or "").lstrip("@")
    q = (supabase.table("telegram_accounts")
         .select("session_name, session_string, username")
         .eq("status", "active").eq("active", True)
         .not_.is_("session_string", "null"))
    if want:
        q = q.ilike("username", want)   # писать именно с тг этой анкеты
    acc = q.limit(1).execute()
    if not acc.data:
        _mark(row, "failed", f"нет сессии для @{want}" if want else "нет активной TG-сессии")
        return
    sess = acc.data[0]
    client = TelegramClient(StringSession(sess["session_string"]), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            _mark(row, "failed", f"сессия @{sess.get('username')} не авторизована")
            return
        try:
            entity = await client.get_entity(lead)
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
            _mark(row, "skipped", "ник не найден")
            return
        # не писать, если с этим лидом уже был чат в телеге
        prior = await client.get_messages(entity, limit=1)
        if prior:
            _mark(row, "skipped", "уже был чат в телеге")
            print(f"[OUTREACH] — @{lead}: уже был чат, пропускаю", flush=True)
            return
        await client.send_message(entity, _opener(source))
        _mark(row, "sent", None, sess["session_name"])
        print(f"[OUTREACH] ✓ @{lead} ← @{sess.get('username')} (source={source})", flush=True)
    except PeerFloodError:
        _mark(row, "failed", "PEER_FLOOD — аккаунт ограничен")
        print("[OUTREACH] ⚠ PEER_FLOOD — аккаунт режут, притормози", flush=True)
    except FloodWaitError as e:
        _mark(row, "pending", f"flood_wait {e.seconds}s")
    except Exception as e:
        _mark(row, "failed", str(e)[:200])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

async def outreach_loop():
    print("✅ outreach_sender запущен", flush=True)
    while True:
        try:
            res = (supabase.table("tg_outreach").select("*")
                   .eq("status", "pending").order("created_at").limit(5).execute())
            for row in (res.data or []):
                await _send_one(row)
                await asyncio.sleep(random.uniform(40, 90))
        except Exception as e:
            print(f"[OUTREACH] loop error: {e}", flush=True)
        await asyncio.sleep(30)
