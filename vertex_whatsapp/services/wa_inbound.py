"""Background processing of stored WhatsApp webhook events (WA Event -> WA Contact / WA Message).

Safe to run more than once on the same event: messages are keyed by their unique wamid and
duplicates are skipped. Each event is processed all-or-nothing: on any error the whole event is
rolled back, the error is written onto the WA Event, and it stays unprocessed.

No AI, no sending here (Phase 1).
"""

import json
import re
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from vertex_whatsapp.services.internal_contacts import is_internal_contact
from vertex_whatsapp.services.phone import country_for_number, format_international, normalise_number

KNOWN_MSG_TYPES = {"text", "image", "audio", "document", "video", "interactive", "button", "reaction"}
MEDIA_TYPES = ("image", "audio", "document", "video", "sticker")

# Whole-message opt-out keywords (compared after lower-casing and trimming punctuation).
OPT_OUT_KEYWORDS = {
	# English
	"stop", "unsubscribe", "stop all", "opt out", "optout",
	# French
	"arrêt", "arret", "arrêter", "arreter", "désabonner", "desabonner", "se désabonner", "se desabonner",
	"désinscrire", "desinscrire", "désabonnement", "desabonnement",
	# Arabic
	"توقف", "توقّف", "إيقاف", "ايقاف", "أوقف", "اوقف", "إلغاء الاشتراك", "الغاء الاشتراك", "الغاء الإشتراك",
}

# Status updates never move backwards (webhooks can arrive out of order). "failed" always applies.
STATUS_RANK = {"received": 0, "sent": 1, "delivered": 2, "read": 3}


class UnsupportedPayload(Exception):
	"""The payload is valid but has a shape we can't store yet. The event stays unprocessed with this note."""


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def process_event(event_name: str) -> None:
	"""Background job: process one WA Event. Never raises; outcome is recorded on the event."""
	event = frappe.get_doc("WA Event", event_name)
	if event.processed:
		return

	try:
		payload = json.loads(event.payload) if isinstance(event.payload, str) else (event.payload or {})
		_process_payload(payload)
	except Exception as exc:
		frappe.db.rollback()
		if isinstance(exc, UnsupportedPayload):
			note = f"Not processed: {exc}"
		else:
			note = "Processing failed: " + traceback.format_exc()[-1500:]
			frappe.log_error(title="WhatsApp event processing failed", reference_doctype="WA Event", reference_name=event_name)
		frappe.db.set_value("WA Event", event_name, {"processed": 0, "error": note}, update_modified=False)
		frappe.db.commit()
		return

	frappe.db.set_value("WA Event", event_name, {"processed": 1, "error": None}, update_modified=False)
	frappe.db.commit()


def process_pending_events() -> None:
	"""Hourly safety net: process events that were stored but never processed (e.g. the queue was down).

	Events that already have an error are left alone so a broken payload doesn't loop forever.
	To retry one, a System Manager clears its Error field.
	"""
	cutoff = now_datetime() - timedelta(minutes=5)
	names = frappe.get_all(
		"WA Event",
		filters={"processed": 0, "creation": ["<", cutoff]},
		or_filters=[["error", "is", "not set"], ["error", "=", ""]],
		order_by="creation asc",
		limit=500,
		pluck="name",
	)
	for name in names:
		process_event(name)


# ---------------------------------------------------------------------------
# Payload walking
# ---------------------------------------------------------------------------


def _process_payload(payload: dict) -> None:
	if not isinstance(payload, dict):
		raise UnsupportedPayload("payload is not a JSON object")
	for entry in payload.get("entry") or []:
		for change in entry.get("changes") or []:
			field = change.get("field")
			value = change.get("value") or {}
			if field == "messages":
				profiles = _profile_names(value)
				for message in value.get("messages") or []:
					handle_inbound_message(message, profiles)
				for status in value.get("statuses") or []:
					handle_status(status)
			elif field == "smb_message_echoes":
				for echo in value.get("message_echoes") or []:
					handle_echo(echo)
			# Other fields (account updates, template status, etc.) are kept as WA Events only.


def _profile_names(value: dict) -> dict:
	names = {}
	for contact in value.get("contacts") or []:
		wa_id = normalise_number(contact.get("wa_id"))
		name = (contact.get("profile") or {}).get("name")
		if wa_id and name:
			names[wa_id] = name
	return names


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_inbound_message(message: dict, profiles: dict | None = None) -> str | None:
	"""Store one customer message. Returns the WA Message name, or None if it was a duplicate."""
	wamid = message.get("id")
	wa_id = normalise_number(message.get("from"))
	if not wamid:
		raise UnsupportedPayload("inbound message has no id (wamid)")
	if not wa_id:
		raise UnsupportedPayload(f"inbound message {wamid} has no 'from' number (username-only senders are not supported yet)")
	if _message_exists(wamid):
		return None

	sent_at = _from_unix(message.get("timestamp"))
	contact = upsert_contact(wa_id, profile_name=(profiles or {}).get(wa_id), seen_at=sent_at, inbound=True)

	msg_type, body, media_id = _extract_content(message)
	referral = message.get("referral") or {}
	doc = _insert_message(
		{
			"wamid": wamid,
			"contact": contact.name,
			"direction": "In",
			"sent_by": "Customer",
			"msg_type": msg_type,
			"body": body,
			"media_id": media_id,
			"sent_at": sent_at,
			"status": "received",
			"referral_source_type": referral.get("source_type"),
			"referral_source_id": referral.get("source_id"),
			"referral_headline": referral.get("headline"),
			"referral_ctwa_clid": referral.get("ctwa_clid"),
		}
	)
	if doc and msg_type in ("text", "button") and is_opt_out(body):
		frappe.db.set_value("WA Contact", contact.name, {"opted_out": 1, "opted_out_at": sent_at or now_datetime()})
	return doc.name if doc else None


def handle_status(status: dict) -> None:
	"""Apply a sent / delivered / read / failed update to the matching outgoing WA Message."""
	wamid = status.get("id")
	new_status = status.get("status")
	if not wamid or new_status not in ("sent", "delivered", "read", "failed"):
		return
	name = frappe.db.get_value("WA Message", {"wamid": wamid}, "name")
	if not name:
		return  # a status for a message we never recorded (e.g. sent from Meta's dashboard): nothing to update

	current = frappe.db.get_value("WA Message", name, "status")
	updates = {}
	if new_status == "failed":
		errors = status.get("errors") or [{}]
		updates = {
			"status": "failed",
			"error_code": str(errors[0].get("code") or "") or None,
			"error_title": errors[0].get("title") or errors[0].get("message"),
		}
	elif current != "failed" and STATUS_RANK[new_status] > STATUS_RANK.get(current or "received", 0):
		updates = {"status": new_status}
	if updates:
		frappe.db.set_value("WA Message", name, updates)


def handle_echo(echo: dict) -> str | None:
	"""Store a message our staff sent from the WhatsApp Business app (coexistence echo)."""
	wamid = echo.get("id")
	wa_id = normalise_number(echo.get("to"))
	if not wamid:
		raise UnsupportedPayload("message echo has no id (wamid)")
	if not wa_id:
		raise UnsupportedPayload(
			f"message echo {wamid} has no 'to' number (only to_user_id); username-only recipients are not supported yet"
		)
	if _message_exists(wamid):
		return None

	sent_at = _from_unix(echo.get("timestamp"))
	contact = upsert_contact(wa_id, seen_at=sent_at, inbound=False)
	msg_type, body, media_id = _extract_content(echo)
	doc = _insert_message(
		{
			"wamid": wamid,
			"contact": contact.name,
			"direction": "Out",
			"sent_by": "Staff",
			"msg_type": msg_type,
			"body": body,
			"media_id": media_id,
			"sent_at": sent_at,
			"status": "sent",
		}
	)
	return doc.name if doc else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def upsert_contact(wa_id: str, profile_name: str | None = None, seen_at=None, inbound: bool = False):
	"""Create or update the WA Contact for this number and return it."""
	seen_at = seen_at or now_datetime()
	internal = 1 if is_internal_contact(wa_id) else 0

	if frappe.db.exists("WA Contact", wa_id):
		contact = frappe.get_doc("WA Contact", wa_id)
	else:
		country = country_for_number(wa_id)
		contact = frappe.get_doc(
			{
				"doctype": "WA Contact",
				"wa_id": wa_id,
				"phone": format_international(wa_id),
				"first_seen": seen_at,
				**country,
			}
		)
		try:
			contact.insert(ignore_permissions=True)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			# Another worker created it a moment ago.
			contact = frappe.get_doc("WA Contact", wa_id)

	if profile_name:
		contact.profile_name = profile_name
	contact.is_internal = internal
	if not contact.country_code:
		contact.update(country_for_number(wa_id))
	if inbound and (not contact.last_inbound_at or get_datetime(seen_at) > get_datetime(contact.last_inbound_at)):
		contact.last_inbound_at = seen_at
	if not contact.first_seen or get_datetime(seen_at) < get_datetime(contact.first_seen):
		contact.first_seen = seen_at
	contact.save(ignore_permissions=True)
	return contact


def is_opt_out(body: str | None) -> bool:
	"""True if the whole message is an opt-out keyword (STOP, UNSUBSCRIBE, ARRÊT, توقف ...)."""
	if not body:
		return False
	cleaned = re.sub(r"[\s\.\!\?\,\;\:\"'«»؟،؛]+", " ", body).strip().casefold()
	return cleaned in OPT_OUT_KEYWORDS


def _extract_content(message: dict) -> tuple[str, str | None, str | None]:
	"""Return (msg_type, body, media_id) for a message or echo."""
	raw_type = message.get("type") or "other"
	msg_type = raw_type if raw_type in KNOWN_MSG_TYPES else "other"
	block = message.get(raw_type) or {}
	body = None
	media_id = None

	if raw_type == "text":
		body = block.get("body")
	elif raw_type in MEDIA_TYPES:
		body = block.get("caption") or block.get("filename")
		media_id = block.get("id")
	elif raw_type == "interactive":
		reply = block.get("button_reply") or block.get("list_reply") or {}
		body = reply.get("title")
	elif raw_type == "button":
		body = block.get("text") or block.get("payload")
	elif raw_type == "reaction":
		body = block.get("emoji")
	elif raw_type == "location":
		body = " ".join(str(block.get(k)) for k in ("name", "address", "latitude", "longitude") if block.get(k))
	elif raw_type == "edit":
		inner = block.get("message") or {}
		_t, inner_body, media_id = _extract_content(inner)
		body = f"[edited {block.get('original_message_id')}] {inner_body or ''}".strip()
	elif raw_type == "revoke":
		body = f"[deleted {block.get('original_message_id')}]"
	elif raw_type == "unsupported":
		body = "[unsupported message type]"
	return msg_type, body, media_id


def _insert_message(values: dict):
	try:
		return frappe.get_doc({"doctype": "WA Message", **values}).insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		return None  # same wamid stored by a parallel worker: skip silently


def _message_exists(wamid: str) -> bool:
	return bool(frappe.db.exists("WA Message", {"wamid": wamid}))


def _from_unix(timestamp) -> datetime | None:
	"""Meta timestamps are Unix seconds (UTC). Convert to the site's timezone, stored naive like Frappe does."""
	try:
		ts = int(timestamp)
	except (TypeError, ValueError):
		return None
	return datetime.fromtimestamp(ts, ZoneInfo(get_system_timezone())).replace(tzinfo=None)
