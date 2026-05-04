"""
Vant Phone Agent — Vapi Server URL Webhook
Fires on every inbound call. Injects current Eastern time + caller ID context.
Also handles end-of-call-report events — saves caller history + sends Telegram notification.

Caller ID Memory System:
- SQLite DB persists caller history across calls
- Every call end: upserts caller record (name from transcript, call count, summary, last called)
- Every call start: injects full history into Vant's context
- Seed data: known team members pre-loaded on first run

Deploy: Railway (free tier). SQLite file lives at /tmp/callers.db on Railway (ephemeral —
for persistence, set DATABASE_PATH env var to a mounted volume path).
"""

from flask import Flask, request, jsonify
from datetime import datetime
import pytz
import logging
import json
import os
import sqlite3
import threading
import requests as http_requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# In-memory log of recent Pumble events (last 20)
RECENT_PUMBLE_EVENTS = []

EASTERN = pytz.timezone("America/New_York")

# Telegram notification config
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8653968146:AAHXHthQx3zPuqLjWH7m_W_BbR7j8aDwD28")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8738797908")

# DB path — override with env var for persistent volume
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/tmp/callers.db")

# Seed data — known team members (loaded on first run if not already in DB)
SEED_CALLERS = [
    {
        "phone": "+19544106389",
        "name": "Daniel Rumsey",
        "role": "Owner/CEO",
        "notes": "Founder of Pest Pro. May be testing or calling with operational questions."
    },
    {
        "phone": "+14079222276",
        "name": "Anne Rumsey",
        "role": "Office Manager",
        "notes": "Handles scheduling, billing, and admin. Internal team."
    },
    {
        "phone": "+14078408852",
        "name": "David Kell",
        "role": "Lead Field Technician",
        "notes": "Lead technician in the field. May call with job questions, active service updates, or scheduling issues. Treat as trusted internal team."
    },
]


# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema and seed known callers."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS callers (
                phone TEXT PRIMARY KEY,
                name TEXT,
                role TEXT,
                call_count INTEGER DEFAULT 0,
                first_called TEXT,
                last_called TEXT,
                notes TEXT,
                last_summary TEXT,
                history TEXT
            )
        """)
        conn.commit()

        # Seed known team members if not already present
        for seed in SEED_CALLERS:
            existing = conn.execute(
                "SELECT phone FROM callers WHERE phone = ?", (seed["phone"],)
            ).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO callers (phone, name, role, call_count, notes)
                    VALUES (?, ?, ?, 0, ?)
                """, (seed["phone"], seed["name"], seed["role"], seed["notes"]))
        # Memory cache table for push-based Vant memory context
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        app.logger.info("DB initialized and seed data loaded.")
    finally:
        conn.close()


def get_caller(phone: str) -> dict | None:
    """Look up a caller by phone number. Returns dict or None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM callers WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_caller(phone: str, name: str = None, role: str = None,
                  notes: str = None, summary: str = None):
    """
    Create or update a caller record after a call ends.
    - Increments call_count
    - Updates last_called timestamp
    - Saves latest summary
    - Appends to history (last 5 call summaries kept)
    - Only updates name/role/notes if provided (don't overwrite known data with None)
    """
    now_et = datetime.now(EASTERN).strftime("%Y-%m-%d %I:%M %p ET")
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT * FROM callers WHERE phone = ?", (phone,)
        ).fetchone()

        if existing:
            existing = dict(existing)
            # Merge: only overwrite if new value is provided
            new_name = name or existing.get("name") or phone
            new_role = role or existing.get("role") or "Unknown"
            new_notes = notes or existing.get("notes") or ""
            new_count = (existing.get("call_count") or 0) + 1
            first_called = existing.get("first_called") or now_et

            # Append to history
            history_raw = existing.get("history") or "[]"
            try:
                history = json.loads(history_raw)
            except Exception:
                history = []
            if summary:
                history.append({"date": now_et, "summary": summary[:500]})
                history = history[-5:]  # Keep last 5

            conn.execute("""
                UPDATE callers SET
                    name = ?, role = ?, call_count = ?, last_called = ?,
                    notes = ?, last_summary = ?, history = ?, first_called = ?
                WHERE phone = ?
            """, (
                new_name, new_role, new_count, now_et,
                new_notes, summary[:500] if summary else existing.get("last_summary"),
                json.dumps(history), first_called, phone
            ))
        else:
            # New caller — create record
            history = []
            if summary:
                history.append({"date": now_et, "summary": summary[:500]})
            conn.execute("""
                INSERT INTO callers
                    (phone, name, role, call_count, first_called, last_called, notes, last_summary, history)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            """, (
                phone,
                name or "Unknown",
                role or "Unknown",
                now_et, now_et,
                notes or "",
                summary[:500] if summary else "",
                json.dumps(history)
            ))

        conn.commit()
        app.logger.info(f"Caller record upserted for {phone}")
    finally:
        conn.close()


def build_caller_context(phone: str) -> str:
    """
    Build the caller context string to inject into Vant's system prompt.
    Returns a rich string if the caller is known, minimal string if unknown.
    """
    caller = get_caller(phone)

    if not caller:
        return f"Unknown caller from {phone}. Treat as a new potential customer."

    name = caller.get("name") or "Unknown"
    role = caller.get("role") or ""
    call_count = caller.get("call_count") or 0
    last_called = caller.get("last_called") or "first time"
    notes = caller.get("notes") or ""
    last_summary = caller.get("last_summary") or ""

    # Parse history
    history_raw = caller.get("history") or "[]"
    try:
        history = json.loads(history_raw)
    except Exception:
        history = []

    # Internal team vs customer
    internal_roles = {"Owner/CEO", "Office Manager", "Lead Field Technician", "Field Technician", "Admin"}
    is_internal = role in internal_roles

    lines = []
    if is_internal:
        lines.append(
            f"CALLER IDENTIFICATION: This is {name} ({role}) — internal Pest Pro team member. "
            f"Greet by name. They have called {call_count} time(s). Last call: {last_called}."
        )
    else:
        lines.append(
            f"CALLER IDENTIFICATION: {name}"
            + (f" ({role})" if role and role != "Unknown" else "")
            + f". This caller has contacted us {call_count} time(s) before. Last call: {last_called}."
        )

    if notes:
        lines.append(f"Notes: {notes}")

    if last_summary:
        lines.append(f"Last call summary: {last_summary}")

    if len(history) > 1:
        lines.append(f"Call history ({len(history)} recent calls):")
        for entry in history[-3:]:  # Show last 3 in context
            lines.append(f"  • {entry.get('date', '')}: {entry.get('summary', '')[:200]}")

    return "\n".join(lines)


# ── UTILITY ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    """Send a message to Daniel via Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = http_requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not resp.ok:
            app.logger.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        return resp.ok
    except Exception as e:
        app.logger.error(f"Telegram send error: {e}")
        return False


def send_telegram_audio(recording_url: str, caption: str = ""):
    """Download a call recording and send it as a voice note to Daniel."""
    try:
        audio_resp = http_requests.get(recording_url, timeout=30)
        if not audio_resp.ok:
            app.logger.error(f"Failed to download recording: {audio_resp.status_code}")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio"
        resp = http_requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption[:1024] if caption else "",
            "title": "Vant Call Recording",
        }, files={
            "audio": ("call-recording.wav", audio_resp.content, "audio/wav")
        }, timeout=60)
        if not resp.ok:
            app.logger.error(f"Telegram audio send failed: {resp.status_code} {resp.text[:200]}")
        return resp.ok
    except Exception as e:
        app.logger.error(f"Telegram audio send error: {e}")
        return False


def is_business_hours(now_et: datetime) -> bool:
    """Returns True if current time is within business hours (7 days/week, 8am–6pm ET)."""
    time_decimal = now_et.hour + now_et.minute / 60.0
    return 8.0 <= time_decimal < 18.0


def get_time_context(now_et: datetime) -> str:
    """Returns a human-readable time/date string for Vant's awareness."""
    day_name = now_et.strftime("%A")
    date_str = now_et.strftime("%B %d, %Y")
    time_str = now_et.strftime("%I:%M %p").lstrip("0")
    return f"{day_name}, {date_str} at {time_str} Eastern Time"


def get_routing_context(now_et: datetime) -> str:
    """Returns after-hours vs business-hours routing guidance."""
    if is_business_hours(now_et):
        return (
            "We are currently open — normal business hours, 8 AM to 6 PM, seven days a week. "
            "You can schedule appointments, answer questions, and offer to connect callers with the team."
        )
    else:
        hour = now_et.hour
        if 0 <= hour < 6:
            period = "the middle of the night"
        elif hour >= 21:
            period = "late evening"
        else:
            period = "outside our normal office hours"

        return (
            f"It is currently {period} — our office hours are 8 AM to 6 PM, seven days a week. "
            "We are always available for urgent situations, 24 hours a day, 365 days a year. "
            "IMPORTANT — how to handle this call naturally: "
            "Do NOT open by announcing we are closed. Greet the caller warmly and ask what is going on. Listen first. "
            "For most callers: say something like 'Of course — our team is available every day from 8 AM to 6 PM. "
            "I want to make sure someone gets back to you first thing. Can I get your name and best callback number?' "
            "Then collect: (1) full name, (2) best callback number, (3) pest issue, (4) best time to call back. "
            "Confirm each piece before ending the call. "
            "Emergency transfer — ONLY if the caller explicitly uses words like 'emergency' or 'urgent,' "
            "or sounds genuinely panicked or distressed. Even then, ASK first: "
            "'It sounds like this may not be able to wait — would you like me to connect you with someone on our team right now?' "
            "If they say yes, use the transferCall tool. If they say no or are unsure, take their message instead. "
            "Do NOT offer the emergency line based on pest type or severity — that is their call to make, not ours. "
            "Always close warmly. Every caller should feel heard and taken care of, not turned away."
        )


def extract_caller_phone(payload: dict) -> str | None:
    """Extract caller phone number from various Vapi payload structures."""
    message = payload.get("message", {})
    call = message.get("call", {}) or payload.get("call", {})
    customer = call.get("customer", {})
    return customer.get("number") or payload.get("customerPhoneNumber")


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/context", methods=["POST"])
def context():
    """
    Vapi tool-call endpoint. Called by Vant at the start of every call.
    Returns current time, open/closed status, and full caller history.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        caller_number = extract_caller_phone(payload)
        message = payload.get("message", {})

        now_et = datetime.now(EASTERN)
        time_context = get_time_context(now_et)
        routing_context = get_routing_context(now_et)

        caller_info = build_caller_context(caller_number) if caller_number else "Caller number not available."

        result = {
            "current_time": time_context,
            "business_status": routing_context,
            "caller_info": caller_info
        }

        app.logger.info(f"Context tool called for {caller_number}: {time_context}")

        tool_call_id = "unknown"
        tool_call_list = message.get("toolCallList", [])
        if tool_call_list:
            tool_call_id = tool_call_list[0].get("id", "unknown")

        return jsonify({
            "results": [{
                "toolCallId": tool_call_id,
                "result": json.dumps(result)
            }]
        })

    except Exception as e:
        app.logger.error(f"Context tool error: {e}", exc_info=True)
        now_et = datetime.now(EASTERN)
        return jsonify({
            "results": [{
                "toolCallId": "unknown",
                "result": json.dumps({"current_time": get_time_context(now_et)})
            }]
        })


@app.route("/", methods=["GET"])
def health():
    now_et = datetime.now(EASTERN)
    conn = get_db()
    try:
        caller_count = conn.execute("SELECT COUNT(*) FROM callers").fetchone()[0]
    except Exception:
        caller_count = "DB error"
    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "service": "Vant Phone Agent Webhook",
        "time_et": get_time_context(now_et),
        "business_hours": is_business_hours(now_et),
        "known_callers": caller_count
    })


@app.route("/form-lead", methods=["POST"])
def form_lead():
    """Receives Formspree webhook submissions and forwards to Telegram Pest Pro Leads group."""
    try:
        data = request.get_json(force=True, silent=True) or request.form.to_dict()
        app.logger.info(f"Form lead received: {data}")

        name        = data.get("name", "").strip() or "Unknown"
        phone       = data.get("phone", "").strip() or "Not provided"
        email       = data.get("email", "").strip() or "Not provided"
        pest        = data.get("pest_problem", "").strip() or "Not specified"
        message     = data.get("message", "").strip()
        source_page = data.get("_next", data.get("referrer", "")).strip()

        lines = [
            "\ud83d\udcec *New Website Lead*",
            f"\ud83d\udc64 Name: {name}",
            f"\ud83d\udcf1 Phone: {phone}",
            f"\ud83d\udce7 Email: {email}",
            f"\ud83d\udc1b Pest: {pest}",
        ]
        if message:
            lines.append(f"\ud83d\udcac Message: {message}")

        send_telegram("\n".join(lines))
        return jsonify({"ok": True}), 200

    except Exception as e:
        app.logger.error(f"Form lead error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/callers", methods=["GET"])
def list_callers():
    """Debug endpoint — list all caller records."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT phone, name, role, call_count, last_called FROM callers ORDER BY call_count DESC"
        ).fetchall()
        return jsonify({"callers": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Vapi calls this on every event (assistant-request, end-of-call-report, etc).
    - assistant-request: inject time context + caller history into system prompt
    - end-of-call-report: save caller record + send Telegram notification
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        message = payload.get("message", {})
        event_type = message.get("type") or payload.get("type", "unknown")

        app.logger.info(f"Vapi webhook received: {event_type}")

        # ── END-OF-CALL: save record + notify ────────────────────────────────
        if event_type == "end-of-call-report":
            try:
                src = message if (message.get("endedReason") or message.get("call")) else payload
                call = src.get("call", {})
                customer = call.get("customer", {})
                caller_number = customer.get("number", "Unknown")
                ended_reason = src.get("endedReason", "unknown")
                duration_s = src.get("durationSeconds", 0)
                summary = src.get("summary", "").strip()
                transcript = src.get("transcript", "").strip()

                # Save / update caller record
                if caller_number and caller_number != "Unknown":
                    upsert_caller(
                        phone=caller_number,
                        summary=summary or (transcript[-300:] if transcript else None)
                    )

                # Build Telegram notification
                caller = get_caller(caller_number) if caller_number != "Unknown" else None
                caller_label = (caller.get("name") if caller else None) or caller_number

                if duration_s:
                    mins = int(duration_s) // 60
                    secs = int(duration_s) % 60
                    duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                else:
                    duration_str = "unknown"

                reason_labels = {
                    "customer-ended-call": "caller hung up",
                    "assistant-forwarded-call": "transferred to live",
                    "assistant-ended-call": "Vant ended call",
                    "customer-did-not-answer": "no answer",
                    "voicemail": "went to voicemail",
                    "max-duration-exceeded": "max duration hit",
                    "silence-timed-out": "silence timeout",
                }
                reason_str = reason_labels.get(ended_reason, ended_reason)

                call_count = caller.get("call_count") if caller else None
                count_str = f" (call #{call_count})" if call_count else ""

                now_et = datetime.now(EASTERN)
                after_hours = not is_business_hours(now_et)

                if after_hours:
                    header = "\U0001f319 <b>After-Hours Message</b>"
                else:
                    header = "\U0001f41f <b>Vant Call Complete</b>"

                lines = [
                    header,
                    f"\U0001f4de {caller_label}{count_str} \u2022 {duration_str} \u2022 {reason_str}",
                ]

                if after_hours:
                    lines.insert(1, "\u26a0\ufe0f <b>Needs follow-up when open</b>")

                if summary:
                    lines.append("")
                    lines.append(f"\U0001f4cb <b>Summary:</b>")
                    lines.append(summary[:1200])
                elif transcript:
                    lines.append("")
                    if after_hours:
                        lines.append(f"\U0001f4ac <b>Full Transcript:</b>")
                        lines.append(transcript[:2000])
                    else:
                        lines.append(f"\U0001f4ac <b>Transcript (last 500 chars):</b>")
                        lines.append(transcript[-500:])

                send_telegram("\n".join(lines))

                # Send call recording audio
                artifact = src.get("artifact", {}) or payload.get("artifact", {})
                recording_url = artifact.get("recordingUrl") or src.get("recordingUrl")
                if recording_url:
                    send_telegram_audio(recording_url)
                    app.logger.info(f"Recording sent for {caller_number}")
                else:
                    app.logger.info(f"No recording URL in payload for {caller_number}")

                app.logger.info(f"End-of-call handled for {caller_number}")

            except Exception as e:
                app.logger.error(f"End-of-call handler error: {e}", exc_info=True)

            return jsonify({"status": "ok"})

        # ── ASSISTANT-REQUEST: inject context into system prompt ──────────────
        caller_number = extract_caller_phone(payload)

        now_et = datetime.now(EASTERN)
        time_context = get_time_context(now_et)
        routing_context = get_routing_context(now_et)
        caller_context = build_caller_context(caller_number) if caller_number else ""

        system_injection = (
            f"REAL-TIME CONTEXT (injected at call start):\n"
            f"Current time: {time_context}\n"
            f"Business status: {routing_context}"
            + (f"\n\n{caller_context}" if caller_context else "")
        )

        app.logger.info(f"Injecting context for {caller_number or 'unknown'}: {time_context}")

        return jsonify({
            "assistant": {
                "firstMessage": None,
                "model": {
                    "messages": [
                        {
                            "role": "system",
                            "content": system_injection
                        }
                    ]
                }
            }
        })

    except Exception as e:
        app.logger.error(f"Webhook error: {e}", exc_info=True)
        now_et = datetime.now(EASTERN)
        return jsonify({
            "assistant": {
                "model": {
                    "messages": [
                        {
                            "role": "system",
                            "content": f"Current time: {get_time_context(now_et)}"
                        }
                    ]
                }
            }
        })


# ── STARTUP ───────────────────────────────────────────────────────────────────

# Initialize DB on import (works with gunicorn workers too)
try:
    init_db()
except Exception as e:
    logging.error(f"DB init failed: {e}")

# ── MEMORY CONTEXT FETCH ─────────────────────────────────────────────────────

MEMORY_SERVER_URL = os.environ.get("MEMORY_SERVER_URL", "http://100.104.191.118:18790")
MEMORY_SERVER_TOKEN = os.environ.get("MEMORY_SERVER_TOKEN", "061eed4d69ee5b4485b4cba6f10b5a4d6e40671baefa2067")
TS_PROXY = os.environ.get("TS_PROXY", "socks5://localhost:1080")

# Surface files to fetch for Pumble context
CONTEXT_FILES = ["clients.json", "pricing.json", "team.json", "company.json", "pending.json"]


def _tailscale_curl_fetch(url: str, token: str, timeout: int = 15) -> dict | None:
    """
    Use system curl with SOCKS5 proxy to fetch via Tailscale userspace networking.
    Returns parsed JSON dict on success, None on failure.
    """
    import subprocess as _sp
    try:
        result = _sp.run(
            ["curl", "--silent", "--max-time", str(timeout),
             "--socks5", "127.0.0.1:1080",
             "--header", f"Authorization: Bearer {token}", url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        else:
            logging.warning(f"curl socks5 failed (code {result.returncode}): {result.stderr[:200]}")
            return None
    except Exception as e:
        logging.warning(f"curl socks5 exception: {e}")
        return None


def fetch_memory_context(timeout_per_file: int = 15) -> dict:
    """
    Fetch live memory surface files.
    Primary: read from SQLite cache (push-based from Mac mini via /memory/push).
    Fallback: Tailscale SOCKS5 proxy (legacy, may time out in userspace mode).
    Returns dict of filename -> parsed content.
    """
    context = {}

    # Primary: read from DB cache (push-based)
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT key, value FROM memory_cache WHERE key IN ({})".format(
                ",".join(["?" for _ in CONTEXT_FILES])
            ),
            CONTEXT_FILES
        ).fetchall()
        conn.close()
        for row in rows:
            try:
                context[row["key"]] = json.loads(row["value"])
            except Exception:
                context[row["key"]] = row["value"]
        if context:
            logging.info(f"Memory context loaded from DB cache: {list(context.keys())}")
            return context
    except Exception as e:
        logging.warning(f"Memory DB read failed: {e}")

    # Fallback: Tailscale SOCKS5
    logging.warning("Memory DB empty — trying Tailscale SOCKS5 fallback")
    proxies = {"http": TS_PROXY, "https": TS_PROXY}
    headers = {"Authorization": f"Bearer {MEMORY_SERVER_TOKEN}"}
    for fname in CONTEXT_FILES:
        url = f"{MEMORY_SERVER_URL}/memory/{fname}"
        data = _tailscale_curl_fetch(url, MEMORY_SERVER_TOKEN, timeout=timeout_per_file)
        if data is None:
            try:
                resp = http_requests.get(url, headers=headers, proxies=proxies, timeout=timeout_per_file)
                if resp.status_code == 200:
                    data = resp.json()
            except Exception as e:
                logging.warning(f"SOCKS5 fallback failed for {fname}: {e}")
        if data and data.get("ok"):
            try:
                context[fname] = json.loads(data["content"])
            except Exception:
                context[fname] = data.get("content", "")

    return context


def build_context_block(context: dict) -> str:
    """
    Format fetched memory surfaces into a compact context string for Claude.
    """
    if not context:
        return ""

    lines = ["\n\n[LIVE MEMORY CONTEXT — fetched from Mac mini at query time]"]

    if "clients.json" in context:
        clients = context["clients.json"]
        if isinstance(clients, list):
            active = [c for c in clients if c.get("status") not in ("inactive", "archived", None)]
            lines.append(f"\nACTIVE CLIENTS ({len(active)} of {len(clients)} total):")
            for c in active[:15]:  # Cap at 15 to keep context size sane
                name = c.get("name", "Unknown")
                addr = c.get("address", "")
                contact = c.get("contact_name", "") or c.get("contact", "")
                phone = c.get("phone", "")
                notes = c.get("notes", "") or c.get("status_notes", "")
                lines.append(f"  • {name} | {addr} | {contact} {phone} | {notes}"[:120])

    if "team.json" in context:
        team = context["team.json"]
        if isinstance(team, list):
            lines.append("\nTEAM:")
            for m in team:
                lines.append(f"  • {m.get('name','')} ({m.get('role','')}) {m.get('phone','')}")

    if "pending.json" in context:
        pending = context["pending.json"]
        if isinstance(pending, dict):
            items = pending.get("items", []) or pending.get("pending", [])
            if items:
                urgent = [i for i in items if i.get("priority") in ("high", "urgent", "immediate")]
                if urgent:
                    lines.append("\nURGENT PENDING:")
                    for item in urgent[:5]:
                        lines.append(f"  • {item.get('text', item.get('description', ''))}"[:120])

    return "\n".join(lines)


# ── CLAUDE AI BRIDGE ─────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

VANT_PUMBLE_SYSTEM_PROMPT = """You are Vant, the AI assistant and operations backbone for Pest Pro LLC, a pest control company in Central Florida.

ABOUT PEST PRO:
- Owner: Daniel Rumsey, CPO (954-410-6389)
- Office line: (407) 922-2276
- Website: pestprollc.com
- Address: 3211 Vineland Rd #107, Kissimmee FL 34746
- Hours: Mon-Sun 8AM-6PM, 24/7 emergency line
- FDACS License: JB304313

TEAM:
- Daniel Rumsey — Owner/CEO (final approval on all external, financial, client-facing decisions)
- Anne Rumsey — Office Manager (scheduling, billing, admin, client communication)
- David Kell — Lead Field Technician (407-840-8852)
- Brandon Rumsey — Field Technician

SERVICES: GHP (General Home Protection), German Roach cleanouts (Code R), mosquito treatment, rodent control, ant control, bed bug treatment, commercial IPM, healthcare IPM, hospitality training. NO termite control.

PRICING (general):
- Monthly GHP: $49-$80/mo depending on property
- Bi-monthly: $79/visit
- Quarterly: $99/visit
- German Roach cleanout (Code R): $120+ based on severity
- One-time service: from $80 (no guarantee)

TOOLS PEST PRO USES:
- FieldworkHQ: scheduling, work orders, field calendar (source of truth for jobs)
- Pumble: team communication (this channel)
- GoHighLevel (GHL): CRM, contacts, website hosting
- Telegram: Daniel's direct AI command channel
- Railway: hosts the AI phone system and Pumble bot
- Google Business Profile: online presence, reviews
- Google Search Console + Ads: SEO and paid traffic
- Facebook: social media (Pest Pro LLC page)
- Formspree: website contact form
- Anthropic Claude: AI model powering Vant
- Twilio + Vapi: AI phone receptionist on (689) 334-2276

YOUR ROLE IN PUMBLE:
You are a full team member, not a passive tool. You have deep context about Pest Pro's operations, clients, pricing, and systems. When @Vant is mentioned, give a direct, useful answer. You do not need to defer to Daniel or Anne for questions you can answer yourself.

You help with:
- Answering questions about services, pricing, tools, scheduling, and operations
- Logging and confirming client or lead information
- Flagging urgent items that need Daniel's attention
- Supporting Anne and David directly with day-to-day operational questions
- Providing status updates and checking in on open jobs

IMPORTANT RULES:
- Professional, direct, warm tone. Write like a real team member who knows the business, not a hedging chatbot.
- Never say "I'm not sure" or defer unnecessarily. If you know it, answer it.
- Never use em dashes (--), en dashes, or double hyphens in any output.
- No AI-sounding phrases: 'it is worth noting', 'I need to be straight with you', 'furthermore', 'robust', 'seamlessly', 'I appreciate the question'.
- Team channel replies: concise. 1-3 short paragraphs max. No long preambles.
- Treat Anne and David as peers. You are support staff, not their manager.
- You do NOT need to say "ask Daniel or Anne" for questions about tools, pricing, services, or operations. You know these things. Just answer.
- ALWAYS include full property address AND contact info (name + phone) in any client update or check-in. Never post client info without both.
- If something genuinely requires Daniel's decision (financial, external-facing, structural), flag it clearly and concisely."""


def get_ai_response(user_message: str, sender_id: str = "", live_context: str = "") -> str:
    """Call Claude API to generate a real Vant response for a Pumble message.
    live_context is an optional pre-fetched memory context block.
    """
    if not ANTHROPIC_API_KEY:
        logging.error("ANTHROPIC_API_KEY not set")
        return None
    try:
        from datetime import datetime
        import pytz as _pytz
        eastern = _pytz.timezone("America/New_York")
        now_str = datetime.now(eastern).strftime("%A, %B %d, %Y %I:%M %p EDT")
        prefix = f"[Current time: {now_str}]\n"
        if sender_id:
            prefix += f"[Pumble message from user {sender_id}]\n"
        if live_context:
            prefix += live_context + "\n\n"
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 2048,
                "system": VANT_PUMBLE_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": f"{prefix}{user_message}"}
                ]
            },
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["content"][0]["text"].strip()
        else:
            logging.error(f"Claude API error: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        logging.error(f"Claude API call failed: {e}")
        return None


# ── PUMBLE BOT ────────────────────────────────────────────────────────────────

PUMBLE_APP_ID = os.environ.get("PUMBLE_APP_ID", "69f0a644a524654b0ff4a7f9")
PUMBLE_APP_KEY = os.environ.get("PUMBLE_APP_KEY", "xpat-8c5dfea06cf475a9e93d4d863f1b51bc")
PUMBLE_CLIENT_SECRET = os.environ.get("PUMBLE_CLIENT_SECRET", "xpcls-eabd7a251a49ec6f1715fd42898f0e76")
PUMBLE_SIGNING_SECRET = os.environ.get("PUMBLE_SIGNING_SECRET", "xpss-4825801c137f2138d3c90f86e7036ab4")
PUMBLE_WORKSPACE_ID = os.environ.get("PUMBLE_WORKSPACE_ID", "69f088d8bafb15ecbe65900c")
PUMBLE_BOT_TOKEN_PATH = os.environ.get("PUMBLE_BOT_TOKEN_PATH", "/data/pumble_bot_token.json")
PUMBLE_API = "https://api-ga.pumble.com"

def get_pumble_bot_token():
    """Load stored bot token from file."""
    try:
        with open(PUMBLE_BOT_TOKEN_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None

def save_pumble_bot_token(token_data):
    """Save bot token to persistent file."""
    try:
        os.makedirs(os.path.dirname(PUMBLE_BOT_TOKEN_PATH), exist_ok=True)
        with open(PUMBLE_BOT_TOKEN_PATH, "w") as f:
            json.dump(token_data, f)
    except Exception as e:
        logging.error(f"Failed to save Pumble bot token: {e}")

def pumble_send_message(channel_id, text, bot_token):
    """Send a message to a Pumble channel via bot API."""
    try:
        resp = http_requests.post(
            f"{PUMBLE_API}/v1/channels/{channel_id}/messages",
            headers={
                "Content-Type": "application/json",
                "token": bot_token,
                "x-app-token": PUMBLE_APP_KEY
            },
            json={"text": text},
            timeout=10
        )
        logging.info(f"Pumble send to {channel_id}: HTTP {resp.status_code} | {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"Pumble send error: {e}")
        return False

@app.route("/redirect", methods=["GET"])
def pumble_redirect():
    """Handle Pumble OAuth redirect — exchange code for bot token."""
    code = request.args.get("code")
    if not code:
        return "Missing code", 400

    try:
        # Correct Pumble SDK OAuth2 token exchange endpoint
        # SDK source confirms: POST /oauth2/access with multipart form-data
        resp = http_requests.post(
            f"{PUMBLE_API}/oauth2/access",
            data={
                'client-id': PUMBLE_APP_ID,
                'client-secret': PUMBLE_CLIENT_SECRET,
                'code': code
            },
            timeout=10
        )
        if resp.status_code == 200:
            token_data = resp.json()
            save_pumble_bot_token(token_data)
            logging.info(f"Pumble bot token saved. Keys: {list(token_data.keys())}")
            return "Vant bot installed successfully! You can close this tab.", 200
        else:
            logging.error(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")
            return f"Token exchange failed: {resp.status_code}", 400
    except Exception as e:
        logging.error(f"Redirect error: {e}")
        return f"Error: {e}", 500

@app.route("/manifest", methods=["GET"])
def pumble_manifest():
    """Serve app manifest for Pumble SDK."""
    return jsonify({
        "name": "vant-bot",
        "displayName": "Vant",
        "botTitle": "Vant — Pest Pro AI",
        "bot": True,
        "scopes": {
            "botScopes": ["messages:read", "messages:write"],
            "userScopes": ["messages:read"]
        },
        "eventSubscriptions": {
            "url": "https://vant-webhook-production.up.railway.app/pumble/events",
            "events": ["NEW_MESSAGE", "APP_UNAUTHORIZED", "APP_UNINSTALLED"]
        },
        "redirectUrls": ["https://vant-webhook-production.up.railway.app/redirect"],
        "welcomeMessage": "Vant is online. Type @Vant to talk to me.",
        "offlineMessage": "Vant is temporarily offline."
    })


# Pumble channel ID -> name + incoming webhook URL
PUMBLE_CHANNEL_WEBHOOKS = {
    "69f089baa524654b0ff3f92a": {"name": "general", "webhook": "https://api.pumble.com/workspaces/69f088d8bafb15ecbe65900c/incomingWebhooks/postMessage/qQnulTeYZMwGTI6EnwkS9cPz"},
    "69f088d8bafb15ecbe659014": {"name": "all-active-customers", "webhook": "https://api.pumble.com/workspaces/69f088d8bafb15ecbe65900c/incomingWebhooks/postMessage/A5fIPdAKCwqYqqadUn9KhRPm"},
    "69f0895d4606315d10330fd0": {"name": "excelsior", "webhook": "https://api.pumble.com/workspaces/69f088d8bafb15ecbe65900c/incomingWebhooks/postMessage/52jhkWFyTfcTyEQu8cAHc6Rm"},
    "69f08913bafb15ecbe659231": {"name": "parkway", "webhook": "https://api.pumble.com/workspaces/69f088d8bafb15ecbe65900c/incomingWebhooks/postMessage/zCX7Hst8hJ3Hg1zW39mPIwZ3"},
    "69f089f24606315d103312fc": {"name": "sales-leads-new", "webhook": "https://api.pumble.com/workspaces/69f088d8bafb15ecbe65900c/incomingWebhooks/postMessage/Wwhza8TCtX9GTDLaU7fuA95R"},
}

# Telegram DELTA relay config (Cleo bot -> DELTA group)
DELTA_CHAT_ID = "-1003787895414"
CLEO_BOT_TOKEN = "7741554651:AAFyhoAFM9Vvp5vm_x-fHRmfo4_7ptZAvIM"

def forward_to_delta(channel_id, sender_name, message_text):
    """Forward @Vant Pumble mention to DELTA Telegram group via Cleo bot."""
    ch_info = PUMBLE_CHANNEL_WEBHOOKS.get(channel_id, {})
    ch_name = ch_info.get("name", channel_id)
    webhook_url = ch_info.get("webhook", "")
    relay_msg = "🔔 PUMBLE | #" + ch_name + " | " + sender_name + ": " + message_text
    if webhook_url:
        relay_msg += "\n\nreply-to: " + webhook_url
    try:
        tg_url = "https://api.telegram.org/bot" + CLEO_BOT_TOKEN + "/sendMessage"
        http_requests.post(tg_url, json={"chat_id": DELTA_CHAT_ID, "text": relay_msg}, timeout=10)
        logging.info(f"Forwarded Pumble msg from {sender_name} in #{ch_name} to DELTA")
    except Exception as e:
        logging.error(f"DELTA forward error: {e}")

# Vant's bot user ID in Pumble (used to detect @Vant mentions in rich text blocks)
VANT_BOT_USER_ID = "69f1a3164606315d1038e292"


def extract_pumble_message(data: dict):
    """
    Parse Pumble's actual event format.
    Events arrive as: {"body": "{JSON string}"}
    Inner JSON: aId (author), cId (channel), bl (rich text blocks)
    Returns (channel_id, sender_id, plain_text, has_vant_mention)
    """
    try:
        body_str = data.get("body", "")
        if not body_str:
            return None, None, "", False

        msg = json.loads(body_str)
        channel_id = msg.get("cId", "")
        sender_id = msg.get("aId", "")

        plain_text = ""
        has_vant_mention = False

        # Format 1: rich text blocks (bl)
        blocks = msg.get("bl", [])
        for block in blocks:
            for section in block.get("elements", []):
                for item in section.get("elements", []):
                    if item.get("type") == "text":
                        plain_text += item.get("text", "")
                    elif item.get("type") == "user":
                        uid = item.get("user_id", "")
                        if uid == VANT_BOT_USER_ID:
                            has_vant_mention = True
                        else:
                            plain_text += f"@{uid[:8]}"

        # Format 2: flat text + mentions array (fallback)
        if not plain_text:
            plain_text = msg.get("text", "")
        if not has_vant_mention:
            for m in msg.get("mentions", []):
                if m.get("userId") == VANT_BOT_USER_ID or m.get("user_id") == VANT_BOT_USER_ID:
                    has_vant_mention = True

        # Format 3: last resort — plain @Vant text scan
        if not has_vant_mention and "@vant" in plain_text.lower():
            has_vant_mention = True

        return channel_id, sender_id, plain_text.strip(), has_vant_mention

    except Exception as e:
        logging.error(f"Error parsing Pumble message body: {e}")
        return None, None, "", False


@app.route("/pumble/events", methods=["POST"])
def pumble_events():
    """Handle incoming Pumble events."""
    try:
        data = request.get_json(force=True)
        logging.info(f"Pumble event raw: {str(data)[:400]}")

        # Store in debug log
        RECENT_PUMBLE_EVENTS.append({
            'time': datetime.now(EASTERN).strftime('%H:%M:%S'),
            'data': str(data)[:400]
        })
        if len(RECENT_PUMBLE_EVENTS) > 20:
            RECENT_PUMBLE_EVENTS.pop(0)

        # URL verification challenge
        challenge = data.get("challenge")
        if challenge:
            logging.info(f"URL verification challenge: {challenge}")
            return jsonify({"challenge": challenge})

        # Legacy event type handling
        event_type = data.get("event", {}).get("type") or data.get("type", "")
        if event_type in ("APP_UNAUTHORIZED", "APP_UNINSTALLED", "URL_VERIFICATION"):
            return jsonify({"ok": True})

        # Parse Pumble's actual format: {"body": "{JSON}"}
        channel_id, sender_id, clean_text, has_vant_mention = extract_pumble_message(data)

        if not has_vant_mention:
            logging.info(f"No @Vant mention detected, ignoring. channel={channel_id}")
            return jsonify({"ok": True})

        if not channel_id:
            logging.warning("No channel_id in Pumble event")
            return jsonify({"ok": True})

        logging.info(f"@Vant mention! channel={channel_id} text={clean_text[:100]}")

        # Load bot token
        token_data = get_pumble_bot_token()
        if not token_data:
            logging.warning("No Pumble bot token available")
            return jsonify({"ok": True})

        bot_token = token_data.get("botToken") or token_data.get("access_token", "")

        # Resolve sender display name from Pumble user ID
        sender_name = sender_id

        # Ack immediately so Pumble doesn't time out
        if bot_token:
            pumble_send_message(channel_id, "On it", bot_token)

        # Spawn background thread: fetch memory -> build context -> get AI response -> reply
        def handle_in_background(ch_id, s_name, msg_text, b_token):
            try:
                logging.info(f"Background thread started for channel={ch_id}")
                live_ctx = fetch_memory_context()
                context_block = build_context_block(live_ctx)
                ai_reply = get_ai_response(msg_text, sender_id=s_name, live_context=context_block)
                if ai_reply:
                    pumble_send_message(ch_id, ai_reply, b_token)
                else:
                    logging.error(f"AI response was None for channel={ch_id} — skipping Pumble send")
                logging.info(f"Background thread completed for channel={ch_id}")
            except Exception as bg_err:
                logging.error(f"Background thread error: {bg_err}", exc_info=True)

        import threading
        t = threading.Thread(
            target=handle_in_background,
            args=(channel_id, sender_name, clean_text, bot_token),
            daemon=True
        )
        t.start()
        logging.info(f"Pumble msg from {sender_name} in {channel_id} — background thread spawned")

        return jsonify({"ok": True})

    except Exception as e:
        logging.error(f"Pumble event error: {e}", exc_info=True)
        return jsonify({"ok": True})


@app.route("/version", methods=["GET"])
def version():
    """Version check endpoint."""
    return jsonify({"version": "2026-05-03-delta-relay-v12", "pumble_api": "v1/channels", "claude_bridge": "direct", "url_verification": "handled"})


@app.route("/memory/push", methods=["POST"])
def memory_push():
    """Receive pushed memory surfaces from Mac mini and cache in SQLite.
    Auth: Authorization: Bearer <MEMORY_SERVER_TOKEN>
    Body: {"surfaces": {"clients.json": "...", "pricing.json": "...", ...}}
    """
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {MEMORY_SERVER_TOKEN}":
        return jsonify({"error": "unauthorized"}), 401
    try:
        data = request.get_json(force=True)
        surfaces = data.get("surfaces", {})
        if not surfaces:
            return jsonify({"error": "no surfaces provided"}), 400
        conn = get_db()
        try:
            now = datetime.now(EASTERN).isoformat()
            for key, value in surfaces.items():
                conn.execute(
                    "INSERT OR REPLACE INTO memory_cache (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value if isinstance(value, str) else json.dumps(value), now)
                )
            conn.commit()
        finally:
            conn.close()
        logging.info(f"Memory push received: {list(surfaces.keys())}")
        return jsonify({"ok": True, "updated": list(surfaces.keys())})
    except Exception as e:
        logging.error(f"Memory push error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/memory/status", methods=["GET"])
def memory_status():
    """Check what memory surfaces are cached and when they were last pushed."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT key, updated_at FROM memory_cache ORDER BY updated_at DESC").fetchall()
        conn.close()
        return jsonify({"ok": True, "cached": [{"key": r["key"], "updated_at": r["updated_at"]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/pumble/debug", methods=["GET"])
def pumble_debug():
    """Debug: check if bot token is saved and show recent events."""
    token_data = get_pumble_bot_token()
    result = {
        "token_saved": bool(token_data),
        "keys": list(token_data.keys()) if token_data else [],
        "token_preview": None,
        "recent_events_count": len(RECENT_PUMBLE_EVENTS),
        "recent_events": RECENT_PUMBLE_EVENTS[-5:]
    }
    if token_data:
        bot_token = token_data.get("botToken") or token_data.get("token") or token_data.get("access_token", "")
        result["token_preview"] = bot_token[:20] + "..." if bot_token else None
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
