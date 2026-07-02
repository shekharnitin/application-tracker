import os
import time
import secrets
import threading
import requests
from pydantic import BaseModel
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Mount static files for uploads
os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

# Global state
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

state = {
    "users": {},          # Maps lowercase username -> chat_id
    "last_update_id": 0,
    "uploads": [],        # list of dicts: {"url": ..., "filename": ..., "username": ...}
    "stage4_pending": {}, # username -> timestamp
    "user_tracks": {},    # username -> {"medical": str, "eta_days": int, "frozen": bool, "current_stage": str}
    "track_tokens": {},   # token -> username
    "user_tokens": {}     # username -> token
}

def send_mock_email(username):
    chat_id = state["users"].get(username)
    if chat_id:
        print(f"Sending fallback email to {username}")
        url = f"{TELEGRAM_API_URL}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": "📧 [SIMULATED EMAIL NOTIFICATION]\n\nSubject: URGENT: Missing Documents Required\n\nDear Applicant,\nWe noticed you haven't uploaded your income verification slip via WhatsApp. Please use our secure desktop portal to upload it immediately so we can proceed with underwriting."
        }
        requests.post(url, json=payload)

# --- Telegram Polling Thread ---
def poll_telegram():
    if not TELEGRAM_BOT_TOKEN:
        print("No TELEGRAM_BOT_TOKEN found. Polling disabled.")
        return
        
    print("Starting Telegram polling thread...")
    while True:
        try:
            # Check for expired Stage 4 timers
            current_time = time.time()
            for uname, trigger_time in list(state["stage4_pending"].items()):
                if current_time - trigger_time > 30: # 30 seconds delay for testing
                    send_mock_email(uname)
                    del state["stage4_pending"][uname]

            url = f"{TELEGRAM_API_URL}/getUpdates?offset={state['last_update_id'] + 1}&timeout=10"
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for result in data["result"]:
                        state["last_update_id"] = result["update_id"]
                        
                        message = result.get("message", {})
                        chat = message.get("chat", {})
                        from_user = message.get("from", {})
                        
                        if "id" in chat:
                            chat_id = chat["id"]
                            # Try to get username, fallback to first_name
                            username = from_user.get("username")
                            if not username:
                                username = from_user.get("first_name", f"user_{chat_id}")
                            
                            username_lower = username.lower()
                            state["users"][username_lower] = chat_id
                            
                        # Handle photo
                        if "photo" in message:
                            photo = message["photo"][-1]
                            file_id = photo["file_id"]
                            download_telegram_file(file_id, "photo.jpg", username)
                            if username_lower in state["stage4_pending"]:
                                print(f"{username} uploaded a photo. Canceling Stage 4 fallback timer.")
                                del state["stage4_pending"][username_lower]
                                unfreeze_eta(username_lower, chat_id)
                            
                        # Handle document
                        if "document" in message:
                            doc = message["document"]
                            file_id = doc["file_id"]
                            file_name = doc.get("file_name", "document.file")
                            download_telegram_file(file_id, file_name, username)
                            if username_lower in state["stage4_pending"]:
                                print(f"{username} uploaded a document. Canceling Stage 4 fallback timer.")
                                del state["stage4_pending"][username_lower]
                                unfreeze_eta(username_lower, chat_id)
                            
        except Exception as e:
            print(f"Polling error: {e}")
        time.sleep(1)

def download_telegram_file(file_id, filename, username="unknown"):
    try:
        url = f"{TELEGRAM_API_URL}/getFile?file_id={file_id}"
        resp = requests.get(url)
        if resp.status_code == 200:
            file_path_info = resp.json().get("result", {}).get("file_path")
            if file_path_info:
                dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path_info}"
                dl_resp = requests.get(dl_url)
                if dl_resp.status_code == 200:
                    safe_filename = f"{int(time.time())}_{filename}"
                    local_path = os.path.join("static", "uploads", safe_filename)
                    with open(local_path, "wb") as f:
                        f.write(dl_resp.content)
                    
                    state["uploads"].append({
                        "url": f"/static/uploads/{safe_filename}",
                        "filename": safe_filename,
                        "username": username
                    })
                    print(f"Downloaded file: {safe_filename} from {username}")
    except Exception as e:
        print(f"Error downloading file: {e}")

def unfreeze_eta(username: str, chat_id: int):
    """Called when user uploads a doc during Stage 4. Drops ETA by 1 day and sends an update."""
    track = state["user_tracks"].get(username, {})
    current_eta = track.get("eta_days", 2)
    new_eta = max(1, current_eta - 1)
    state["user_tracks"].setdefault(username, {})["eta_days"] = new_eta
    state["user_tracks"][username]["frozen"] = False

    msg = (
        f"✅ Document received! Your timeline has been unfrozen.\n\n"
        f"---\n"
        f"📊 *Progress:* Back on track!\n"
        f"⏱️ *ETA:* {new_eta} Day{'s' if new_eta != 1 else ''} Remaining\n"
        f"💡 Thanks for acting quickly! Your application is back in the review queue."
    )
    url = f"{TELEGRAM_API_URL}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})

# Start polling thread
thread = threading.Thread(target=poll_telegram, daemon=True)
thread.start()

# --- API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/status")
async def get_status():
    return {
        "users": list(state["users"].keys()),
        "uploads": state["uploads"]
    }

# SLA node weights in hours (from spec)
SLA_PIQA    = 12  # Node A: Ingestion & PIQA
SLA_MEDICAL = 48  # Node B: Medical scheduling & report sync
SLA_UW      = 24  # Node C: Senior underwriting queue

def get_total_track_hours(medical: str) -> int:
    if medical == "none":
        return SLA_PIQA + SLA_UW          # Express: 36 hrs
    return SLA_PIQA + SLA_MEDICAL + SLA_UW # Tele/Physical: 84 hrs

def calculate_eta(stage_str: str, medical: str, username: str = None):
    """
    Returns (eta_text, eta_days) based on SLA-weighted progress.
    Stores eta_days in user_tracks so Stage 4 can freeze it.
    """
    total_hours = get_total_track_hours(medical)
    completed_hours = 0
    eta_days = 12
    highlight = ""
    frozen = False

    if stage_str == "0":
        completed_hours = 0
        eta_days = 12
        highlight = "Application successfully received. Initial checks starting now."

    elif stage_str == "1":
        completed_hours = 0
        eta_days = 12
        highlight = "Our team is performing initial document checks right now."

    elif stage_str == "2a":
        completed_hours = SLA_PIQA  # 12 hrs done
        if medical == "none":
            eta_days = 3
            highlight = "⚡ Fast-tracked! No medical required — 9-day buffer removed."
        elif medical == "tele":
            eta_days = 6
            highlight = "📞 Tele-medical required. It takes less than 30 minutes!"
        else:
            eta_days = 10
            highlight = "🩺 Physical medical required. Please schedule your appointment."

    elif stage_str == "2b":
        completed_hours = SLA_PIQA + SLA_MEDICAL  # 60 hrs done
        if medical == "tele":
            eta_days = 3
            highlight = "✅ Tele-medical complete! Reports synced and verified."
        else:
            eta_days = 5
            highlight = "✅ Physical exam complete! Awaiting lab processing."

    elif stage_str == "3":
        completed_hours = total_hours  # all pre-UW done
        eta_days = 2
        highlight = "No action needed from you! Senior underwriting team has your file."

    elif stage_str == "4":
        # Freeze: pull the last stored ETA for this user
        frozen = True
        if username and username in state["user_tracks"]:
            eta_days = state["user_tracks"][username].get("eta_days", 2)
        else:
            eta_days = 2
        completed_hours = total_hours  # pre-UW is fully done
        highlight = "Reply directly to this chat with a photo of your document to unfreeze your timeline!"

    elif stage_str.startswith("5"):
        completed_hours = total_hours
        eta_days = 0
        if stage_str == "5a":
            highlight = "🎉 Your family is now secured. We've emailed your digital policy document!"
        elif stage_str == "5c":
            highlight = "Please check your email for the detailed breakdown and to accept the updated terms."
        else:
            highlight = "Our underwriting team was unable to proceed with your current profile."

    # Calculate progress %
    if stage_str == "0" or stage_str == "1":
        progress_pct = 0
    elif stage_str.startswith("5"):
        progress_pct = 100
    else:
        progress_pct = round((completed_hours / total_hours) * 100)

    # Persist ETA for this user (so Stage 4 can freeze it)
    if username and not frozen:
        state["user_tracks"].setdefault(username, {})["eta_days"] = eta_days
        state["user_tracks"][username]["frozen"] = False
    elif username and frozen:
        state["user_tracks"].setdefault(username, {})["frozen"] = True

    # Build the footer block
    eta_text = f"\n\n---\n📊 *Progress:* {progress_pct}%\n"
    if frozen:
        eta_text += f"⏱️ *ETA:* {eta_days} Days _(Clock paused until upload)_\n"
    elif eta_days == 0:
        eta_text += f"⏱️ *ETA:* ✅ Completed\n"
    elif eta_days <= 2:
        eta_text += f"⏱️ *ETA:* 1–2 Days Remaining\n"
    else:
        eta_text += f"⏱️ *ETA:* {eta_days} Days Remaining\n"

    if highlight:
        eta_text += f"💡 {highlight}"

    return eta_text, eta_days

@app.post("/api/send/{stage}")
async def send_stage_message(stage: str, username: str = None, medical: str = "physical"):
    if not username:
        return {"error": "Please specify a target username."}
        
    username = username.lower().strip().replace('@', '')
    chat_id = state["users"].get(username)
    
    if not chat_id:
        return {"error": f"User '{username}' not found. They need to send /start to the bot first."}
        
    # Build illustration paths relative to this file so they work on any OS/host
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    def ill(rel): return os.path.join(BASE_DIR, "illustration", rel)

    stage_data = {
        "0": {
            "text": "👋 Hi! We have successfully received your term insurance application (Ref: #TRK-12345). Our team is performing the initial document checks right now.",
            "photo": None
        },
        "1": {
            "text": "📄 Action Required! Please upload your identity and income documents below to get started. Your Tracking ID is #TRK-12345.",
            "photo": ill(r"a_friendly_2d_minimalist_character_smiling_while_tapping_a_giant_smartphone/document upload.png")
        },
        "2a": {
            "text": "🩺 To customise your term plan cover, let's get your complimentary medical check scheduled. It takes less than 30 minutes! Tap below to choose a time that works for you.\n\n[Book Free Medical Test]",
            "photo": ill(r"a_charming_minimalist_2d_character_standing_next_to_a_friendly_health/medical test.png")
        },
        "2b": {
            "text": "✅ Great news! Your medical reports have been received and synced to your file. We're now preparing your application for the underwriting desk.",
            "photo": ill(r"a_charming_minimalist_2d_character_standing_next_to_a_friendly_health/medical test.png")
        },
        "3": {
            "text": "🔍 Great news! Your files and health checks are fully verified. Your application is now on the desk of our senior underwriting team for final review. No action needed from you!",
            "photo": ill(r"modify_this_illustration_based_on_the_annotation_in_data_image_image_8._the/underwriting.png")
        },
        "4": {
            "text": "📑 We've hit a minor speed bump. Our underwriting team needs a quick clarification on your income document to finalise your approval. Reply directly to this chat with a photo of your document to unfreeze your timeline!\n\n[Upload Document]",
            "photo": ill(r"a_friendly_2d_character_looking_at_a_giant_document_icon_that_has_a_soft_yellow/missing-additional documents.png")
        },
        "5a": {
            "text": "🎉 Awesome news! Your Term Life Insurance cover is officially APPROVED. Your family is now secured. We've emailed your digital policy document (#POL-9999) right now. Welcome aboard!",
            "photo": ill(r"a_joyous_2d_character_tossing_a_few_pieces_of_confetti_into_the_air_under_a/approved.png")
        },
        "5b": {
            "text": "❌ Our underwriting team has finalised their review. Unfortunately we are unable to proceed with your current profile. Please check your email for the detailed explanation.",
            "photo": ill(r"modify_this_illustration_to_remove_all_text._the_label_final_update_application/rejected.png")
        },
        "5c": {
            "text": "⚠️ Our underwriters have finalised their review and shared a revised premium proposal based on your health checks. Please check your email for the detailed breakdown and to accept the updated terms.",
            "photo": ill(r"modify_this_illustration_to_remove_all_text._the_labels_stage_5b_final_update/counter-offer.png")
        }
    }
    
    data = stage_data.get(stage)
    if not data:
        return {"error": "Unknown stage."}

    # Generate or reuse a unique tracking token for this user
    if username not in state["user_tokens"]:
        token = secrets.token_urlsafe(12)
        state["user_tokens"][username] = token
        state["track_tokens"][token] = username
    else:
        token = state["user_tokens"][username]

    # Persist current stage and medical track
    state["user_tracks"].setdefault(username, {})["current_stage"] = stage
    state["user_tracks"][username]["medical"] = medical

    eta_text, _ = calculate_eta(stage, medical, username)

    # Append live tracking link
    track_url = f"{BASE_URL}/track/{token}"
    eta_text += f"\n\n🔗 [Track your application live]({track_url})"

    try:
        if data["photo"]:
            url = f"{TELEGRAM_API_URL}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "caption": data["text"] + eta_text,
                "parse_mode": "Markdown"
            }
            with open(data["photo"], 'rb') as photo_file:
                resp = requests.post(url, data=payload, files={"photo": photo_file})
        else:
            url = f"{TELEGRAM_API_URL}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": data["text"] + eta_text,
                "parse_mode": "Markdown"
            }
            resp = requests.post(url, json=payload)
            
        if resp.status_code == 200:
            if stage == "4":
                state["stage4_pending"][username] = time.time()
                print(f"Started 30-second Stage 4 fallback timer for {username}")
                
            return {"success": True, "message": f"Sent stage {stage} message."}
        else:
            return {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

class CustomMessage(BaseModel):
    username: str
    text: str

@app.post("/api/send_custom")
async def send_custom_message(payload: CustomMessage):
    if not payload.username:
        return {"error": "Please specify a target username."}
        
    username = payload.username.lower().strip().replace('@', '')
    chat_id = state["users"].get(username)
    
    if not chat_id:
        return {"error": f"User '{username}' not found. They need to send /start to the bot first."}
        
    url = f"{TELEGRAM_API_URL}/sendMessage"
    tg_payload = {
        "chat_id": chat_id,
        "text": payload.text
    }
    
    try:
        resp = requests.post(url, json=tg_payload)
        if resp.status_code == 200:
            return {"success": True, "message": "Sent custom message."}
        else:
            return {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/track/{token}", response_class=HTMLResponse)
async def tracker_page(request: Request, token: str):
    username = state["track_tokens"].get(token)
    if not username:
        return HTMLResponse(
            content="<h2 style='font-family:sans-serif;padding:40px;color:#555'>Invalid or expired tracking link.</h2>",
            status_code=404
        )
    return templates.TemplateResponse(request=request, name="track.html", context={"token": token})

@app.get("/api/track_status/{token}")
async def get_track_status(token: str):
    username = state["track_tokens"].get(token)
    if not username:
        return {"error": "Invalid tracking token"}
    track = state["user_tracks"].get(username, {})
    return {
        "current_stage": track.get("current_stage", None),
        "medical": track.get("medical", "physical"),
        "eta_days": track.get("eta_days", 12),
        "frozen": track.get("frozen", False),
        "ref": "#TRK-12345"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
