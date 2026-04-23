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
import requests as http_requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

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


def is_business_hours(now_et: datetime) -> bool:
    """Returns True if current time is within business hours (Mon–Fri 8am–6pm, Sat 8am–2pm)."""
    weekday = now_et.weekday()  # 0=Mon, 6=Sun
    time_decimal = now_et.hour + now_et.minute / 60.0

    if weekday < 5:   # Mon–Fri
        return 8.0 <= time_decimal < 18.0
    elif weekday == 5:  # Saturday
        return 8.0 <= time_decimal < 14.0
    else:  # Sunday
        return False


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
            "We are currently OPEN during normal business hours. "
            "You can schedule appointments, answer questions, and offer to connect callers with the team."
        )
    else:
        weekday = now_et.weekday()
        hour = now_et.hour
        if 0 <= hour < 7:
            period = "very early morning — most people are asleep"
        elif hour >= 22:
            period = "late night"
        else:
            period = "after business hours"

        if weekday == 6:
            next_open = "Monday morning at 8:00 AM"
        elif weekday == 5 and now_et.hour >= 14:
            next_open = "Monday morning at 8:00 AM"
        else:
            next_open = "tomorrow morning at 8:00 AM" if weekday < 4 else "Monday morning at 8:00 AM"

        return (
            f"We are currently CLOSED — it is {period}. "
            f"The office reopens {next_open}. "
            "For non-emergency pest issues, offer to take a message or schedule a callback. "
            "For genuine emergencies (active infestation causing health risk, commercial account crisis), "
            "you can offer to attempt reaching Daniel at 954-410-6389. "
            "Do NOT promise immediate response — just offer to pass the message."
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

                lines = [
                    f"\U0001f41f <b>Vant Call Complete</b>",
                    f"\U0001f4de {caller_label}{count_str} \u2022 {duration_str} \u2022 {reason_str}",
                ]

                if summary:
                    lines.append("")
                    lines.append(f"\U0001f4cb <b>Summary:</b>")
                    lines.append(summary[:800])
                elif transcript:
                    lines.append("")
                    lines.append(f"\U0001f4ac <b>Transcript (last 500 chars):</b>")
                    lines.append(transcript[-500:])

                send_telegram("\n".join(lines))
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
