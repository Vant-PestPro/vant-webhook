"""
Vant Phone Agent — Vapi Server URL Webhook
Fires on every inbound call. Injects current Eastern time + caller ID context.
Deploy: Railway (free tier), fly.io, or run locally with ngrok for testing.
"""

from flask import Flask, request, jsonify
from datetime import datetime
import pytz
import logging
import json

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

EASTERN = pytz.timezone("America/New_York")

# Known callers — grows over time (caller ID memory)
KNOWN_CALLERS = {
    "+19544106389": {
        "name": "Daniel Rumsey",
        "role": "Owner/CEO",
        "note": "Founder of Pest Pro. May be testing or calling with operational questions."
    },
    "+14079222276": {
        "name": "Anne Rumsey",
        "role": "Office Manager",
        "note": "Handles scheduling, billing, and admin. Internal team."
    },
    "+14078408852": {
        "name": "David Kell",
        "role": "Lead Field Technician",
        "note": "Lead technician in the field. May call with job questions, active service updates, or scheduling issues. Treat as trusted internal team."
    },
}

# Business hours (Eastern)
BUSINESS_HOURS = {
    0: None,          # Monday: not defined, use default
    1: None,
    2: None,
    3: None,
    4: None,
    5: None,
    6: None,
}

def is_business_hours(now_et: datetime) -> bool:
    """Returns True if current time is within business hours (Mon–Fri 8am–6pm, Sat 8am–2pm)."""
    weekday = now_et.weekday()  # 0=Mon, 6=Sun
    hour = now_et.hour
    minute = now_et.minute
    time_decimal = hour + minute / 60.0

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
    time_str = now_et.strftime("%I:%M %p").lstrip("0")  # "7:24 AM" not "07:24 AM"
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
        elif hour >= 22 or hour < 0:
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

@app.route("/context", methods=["POST"])
def context():
    """
    Vapi tool-call endpoint. Called by Vant at the start of every call.
    Returns current time, open/closed status, and caller ID info.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        # Extract caller number from tool call payload
        caller_number = None
        message = payload.get("message", {})
        call = message.get("call", {})
        customer = call.get("customer", {})
        caller_number = customer.get("number")

        now_et = datetime.now(EASTERN)
        time_context = get_time_context(now_et)
        routing_context = get_routing_context(now_et)

        caller_info = ""
        if caller_number and caller_number in KNOWN_CALLERS:
            info = KNOWN_CALLERS[caller_number]
            caller_info = f"Caller is {info['name']} ({info['role']}). {info['note']}"
        elif caller_number:
            caller_info = f"Unknown caller from {caller_number}."

        result = {
            "current_time": time_context,
            "business_status": routing_context,
            "caller_info": caller_info
        }

        app.logger.info(f"Context tool called for {caller_number}: {time_context}")

        return jsonify({
            "results": [{
                "toolCallId": message.get("toolCallList", [{}])[0].get("id", "unknown"),
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
    return jsonify({
        "status": "ok",
        "service": "Vant Phone Agent Webhook",
        "time_et": get_time_context(now_et),
        "business_hours": is_business_hours(now_et)
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Vapi calls this on every inbound call (assistant-request event).
    We return a system message injection with time context + caller ID.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        app.logger.info(f"Vapi webhook received: {payload.get('message', {}).get('type', 'unknown')}")

        # Extract caller number
        caller_number = None
        message = payload.get("message", {})
        call = message.get("call", {})
        customer = call.get("customer", {})
        caller_number = customer.get("number")

        # Build time context
        now_et = datetime.now(EASTERN)
        time_context = get_time_context(now_et)
        routing_context = get_routing_context(now_et)

        # Build caller ID context
        caller_context = ""
        if caller_number and caller_number in KNOWN_CALLERS:
            info = KNOWN_CALLERS[caller_number]
            caller_context = (
                f"\n\nCALLER IDENTIFICATION: This call is from {info['name']} ({info['role']}). "
                f"Note: {info['note']} "
                f"Greet them by name and adjust your tone accordingly — this is an internal team member, not a prospect."
            )
        elif caller_number:
            caller_context = f"\n\nCALLER: Unknown caller from {caller_number}. Treat as a new potential customer."

        # Compose the system message injection
        system_injection = (
            f"REAL-TIME CONTEXT (injected at call start):\n"
            f"Current time: {time_context}\n"
            f"Business status: {routing_context}"
            f"{caller_context}"
        )

        app.logger.info(f"Injecting context for {caller_number or 'unknown'}: {time_context}, open={is_business_hours(now_et)}")

        # Vapi assistant-request response format
        return jsonify({
            "assistant": {
                "firstMessage": None,  # Keep the configured first message
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
        # Return minimal valid response on error — don't break the call
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

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
