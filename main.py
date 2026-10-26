"""Mock backend for the Swiggy rider-onboarding voice agents (Sarvam Samvaad).

Agent 1 (qualifier) tools : GET /leads/{id}, GET /centres, POST /slots/book, POST /crm/outcome (on_end)
Agent 2 (onboarding) tools: GET /onboarding/status   (verifies each upload step)
Simulated driver app      : GET /app  (page with buttons) -> POST /app/upload

State is in memory: it resets whenever the server restarts.
Optional auth: set env API_KEY and send header  X-API-Key: <value>.
"""
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

API_KEY = os.getenv("API_KEY")
VERIFY_DELAY_SECONDS = float(os.getenv("VERIFY_DELAY_SECONDS", "4"))  # pending -> verified
STEPS = ("aadhaar", "driving_license", "selfie")


OPEN_PATHS = {"/app", "/app/upload", "/health"}  # the mock driver app can't send headers


def auth(request: Request, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and request.url.path not in OPEN_PATHS and x_api_key != API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


app = FastAPI(title="Swiggy Rider Onboarding - Mock API", dependencies=[Depends(auth)])

# ---------------------------------------------------------------- seed data
LEADS = {
    "L001": {"lead_id": "L001", "name": "Ramesh Kumar", "phone": "9990000001", "city": "Lucknow", "language": "hi"},
    "L002": {"lead_id": "L002", "name": "Sana Khan", "phone": "9990000002", "city": "Delhi", "language": "en"},
    "L003": {"lead_id": "L003", "name": "Vijay Patil", "phone": "9990000003", "city": "Pune", "language": "hi"},
    "L004": {"lead_id": "L004", "name": "Anil Yadav", "phone": "9990000004", "city": "Kanpur", "language": "hi"},
}
CENTRES = {
    "Lucknow": [{"centre_id": "LKO-01", "name": "Swiggy Hub Hazratganj", "address": "Hazratganj, Lucknow"}],
    "Delhi": [
        {"centre_id": "DEL-01", "name": "Swiggy Hub Saket", "address": "Saket, New Delhi"},
        {"centre_id": "DEL-02", "name": "Swiggy Hub Rohini", "address": "Rohini, New Delhi"},
    ],
    "Pune": [{"centre_id": "PNQ-01", "name": "Swiggy Hub Kothrud", "address": "Kothrud, Pune"}],
    "Bengaluru": [{"centre_id": "BLR-01", "name": "Swiggy Hub Koramangala", "address": "Koramangala, Bengaluru"}],
}
SLOT_TIMES = ["10:00", "12:00", "15:00", "17:00"]
BOOKINGS: dict[str, dict] = {}
CRM: list[dict] = []
UPLOADS: dict[str, dict[str, dict]] = {}  # phone -> step -> {state, uploaded_at, result, reason}


def norm_phone(p: str) -> str:
    digits = "".join(c for c in p if c.isdigit())
    return digits[-10:]


def next_days(n: int = 3) -> list[str]:
    from datetime import date, timedelta
    return [(date.today() + timedelta(days=i)).isoformat() for i in range(1, n + 1)]


# ---------------------------------------------------------------- leads
@app.get("/leads/{lead_id}")
def get_lead(lead_id: str):
    lead = LEADS.get(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    return lead


@app.get("/leads")
def list_leads():
    return list(LEADS.values())


# ---------------------------------------------------------------- centres & slots
@app.get("/centres")
def centres(city: str = Query(..., description="e.g. Lucknow")):
    key = next((c for c in CENTRES if c.lower() == city.strip().lower()), None)
    if not key:
        return {"city": city, "serviceable": False, "centres": [],
                "message": "No onboarding centre in this city yet."}
    out = []
    for c in CENTRES[key]:
        slots = [f"{d} {t}" for d in next_days() for t in SLOT_TIMES
                 if not any(b["centre_id"] == c["centre_id"] and b["slot"] == f"{d} {t}" for b in BOOKINGS.values())]
        out.append({**c, "available_slots": slots[:6]})
    return {"city": key, "serviceable": True, "centres": out}


class BookReq(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    phone: str
    city: str
    centre_id: str
    slot: str  # "YYYY-MM-DD HH:MM" exactly as returned by /centres


@app.post("/slots/book")
def book(req: BookReq):
    valid = {c["centre_id"] for cs in CENTRES.values() for c in cs}
    if req.centre_id not in valid:
        raise HTTPException(422, "unknown centre_id")
    if any(b["centre_id"] == req.centre_id and b["slot"] == req.slot for b in BOOKINGS.values()):
        raise HTTPException(409, "slot already taken, pick another from /centres")
    phone = norm_phone(req.phone)
    for b in BOOKINGS.values():  # one active booking per phone, idempotent re-book
        if b["phone"] == phone:
            raise HTTPException(409, f"lead already has booking {b['booking_id']} at {b['slot']}")
    bid = "BK" + uuid.uuid4().hex[:6].upper()
    BOOKINGS[bid] = {"booking_id": bid, "status": "confirmed", "name": req.name, "phone": phone,
                     "city": req.city, "centre_id": req.centre_id, "slot": req.slot}
    return BOOKINGS[bid]


@app.get("/slots/bookings")
def bookings():
    return list(BOOKINGS.values())


# ---------------------------------------------------------------- CRM (on_end push)
class CrmReq(BaseModel):
    model_config = ConfigDict(extra="allow")  # accept whatever variables the agent sends
    phone: Optional[str] = None
    outcome: Optional[str] = None  # qualified_and_booked | not_qualified | not_interested | follow_up_needed
    reason: Optional[str] = None


@app.post("/crm/outcome")
def crm_outcome(req: CrmReq):
    rec = {"crm_id": "CRM" + uuid.uuid4().hex[:6].upper(),
           "received_at": datetime.now(timezone.utc).isoformat(), **req.model_dump()}
    CRM.append(rec)
    return {"status": "recorded", "crm_id": rec["crm_id"]}


@app.get("/crm/outcomes")
def crm_list():
    return CRM


# ---------------------------------------------------------------- onboarding (Agent 2)
class UploadReq(BaseModel):
    phone: str
    step: str  # aadhaar | driving_license | selfie
    result: str = "verified"  # simulate "failed" to test the retry path
    reason: Optional[str] = None


def _step_state(rec: Optional[dict]) -> dict:
    if not rec:
        return {"status": "not_uploaded"}
    if rec["result"] == "failed":
        return {"status": "failed", "reason": rec.get("reason") or "document unreadable, please re-upload"}
    if time.time() - rec["uploaded_at"] < VERIFY_DELAY_SECONDS:
        return {"status": "pending"}
    return {"status": "verified"}


@app.post("/app/upload")
def app_upload(req: UploadReq):
    """What the Swiggy Delivery Partner app would call when the rider uploads a document."""
    if req.step not in STEPS:
        raise HTTPException(422, f"step must be one of {STEPS}")
    UPLOADS.setdefault(norm_phone(req.phone), {})[req.step] = {
        "uploaded_at": time.time(), "result": req.result, "reason": req.reason}
    return {"received": True, "step": req.step}


@app.get("/onboarding/status")
def onboarding_status(phone: str, step: Optional[str] = None):
    """Tool for Agent 2. With ?step= returns one step; without it returns all + the next step to ask for."""
    rec = UPLOADS.get(norm_phone(phone), {})
    if step:
        if step not in STEPS:
            raise HTTPException(422, f"step must be one of {STEPS}")
        return {"step": step, **_step_state(rec.get(step))}
    states = {s: _step_state(rec.get(s)) for s in STEPS}
    nxt = next((s for s in STEPS if states[s]["status"] != "verified"), None)
    return {"steps": states, "next_step": nxt, "onboarding_complete": nxt is None}


@app.post("/onboarding/reset")
def reset(phone: str):
    UPLOADS.pop(norm_phone(phone), None)
    return {"reset": True}


# ---------------------------------------------------------------- simulated driver app
@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def driver_app():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Driver App (mock)</title>
<style>body{font:16px system-ui;max-width:420px;margin:24px auto;padding:0 16px}
button{display:block;width:100%;padding:14px;margin:8px 0;font-size:16px;border-radius:8px;border:1px solid #bbb;background:#fff}
input{width:100%;padding:12px;font-size:16px;box-sizing:border-box}small{color:#666}</style>
<h2>Swiggy Delivery Partner (mock)</h2>
<label>Rider phone<input id=p value=9990000001></label>
<p><small>Tap to simulate the rider uploading, then ask the agent to check.</small></p>
<button onclick=up('aadhaar')>Upload Aadhaar</button>
<button onclick=up('driving_license')>Upload Driving Licence</button>
<button onclick=up('selfie')>Upload Selfie</button>
<button onclick="up('aadhaar','failed')">Upload Aadhaar (blurry, will FAIL)</button>
<pre id=o></pre>
<script>
async function up(step,result='verified'){
 const r=await fetch('/app/upload',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({phone:p.value,step,result,reason:result=='failed'?'image blurry':null})});
 o.textContent=step+' ('+result+') -> '+r.status+' '+await r.text();}
</script>"""


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
