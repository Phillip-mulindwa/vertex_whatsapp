"""WhatsApp Cloud API webhook.

URL: /api/method/vertex_whatsapp.api.webhook.receive

GET  = Meta's one-time verification handshake.
POST = event delivery. We only verify the signature, store the raw event, and return 200 fast.
       All real work happens in the background job vertex_whatsapp.services.wa_inbound.process_event.
"""

import hashlib
import hmac
import json

import frappe
from frappe.utils import now_datetime
from werkzeug.wrappers import Response

PROCESS_METHOD = "vertex_whatsapp.services.wa_inbound.process_event"


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def receive():
	verify_token = frappe.conf.get("wa_verify_token")
	app_secret = frappe.conf.get("wa_app_secret")
	if not verify_token or not app_secret:
		_log_not_configured()
		return _text("Service not configured", 503)

	if frappe.request.method == "GET":
		return _verify_handshake(verify_token)
	return _receive_event(app_secret)


def _verify_handshake(verify_token: str) -> Response:
	args = frappe.request.args
	mode = args.get("hub.mode")
	token = args.get("hub.verify_token") or ""
	challenge = args.get("hub.challenge") or ""
	if mode == "subscribe" and hmac.compare_digest(token.encode(), verify_token.encode()):
		return _text(challenge, 200)
	return _text("Forbidden", 403)


def _receive_event(app_secret: str) -> Response:
	# The exact bytes Meta sent. Werkzeug caches them, so Frappe's own JSON parsing
	# of the body earlier in the request does not change what we verify here.
	raw_body = frappe.request.get_data(cache=True)

	if not is_valid_signature(raw_body, frappe.request.headers.get("X-Hub-Signature-256"), app_secret):
		return _text("Invalid signature", 401)

	event = frappe.get_doc(
		{
			"doctype": "WA Event",
			"received_at": now_datetime(),
			"event_type": classify_payload(raw_body),
			"payload": _payload_for_storage(raw_body),
			"processed": 0,
		}
	)
	event.insert(ignore_permissions=True)
	frappe.db.commit()  # the event is safely stored before anything else can fail

	try:
		frappe.enqueue(PROCESS_METHOD, queue="short", event_name=event.name)
	except Exception:
		# Stored but not queued (e.g. Redis down). The hourly sweeper will pick it up.
		frappe.log_error(title="WhatsApp webhook: could not queue event", reference_doctype="WA Event", reference_name=event.name)
		frappe.db.commit()

	return _text("OK", 200)


def is_valid_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
	"""Check Meta's X-Hub-Signature-256 header ("sha256=<hex>") against the raw body."""
	if not header or not header.startswith("sha256="):
		return False
	expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(header[len("sha256=") :].strip().lower(), expected)


def classify_payload(raw_body: bytes) -> str:
	"""Label the event (message / status / echo / other) for the WA Event list. Purely informational."""
	try:
		data = json.loads(raw_body)
	except ValueError:
		return "other"
	found = set()
	for entry in (data.get("entry") or []) if isinstance(data, dict) else []:
		for change in entry.get("changes") or []:
			value = change.get("value") or {}
			if change.get("field") == "smb_message_echoes":
				found.add("echo")
			elif change.get("field") == "messages":
				if value.get("messages"):
					found.add("message")
				if value.get("statuses"):
					found.add("status")
	for label in ("message", "echo", "status"):
		if label in found:
			return label
	return "other"


def _payload_for_storage(raw_body: bytes) -> str:
	text = raw_body.decode("utf-8", errors="replace")
	try:
		json.loads(text)
		return text
	except ValueError:
		# Signed by Meta but not JSON: keep it anyway, wrapped so the JSON column accepts it.
		return json.dumps({"_unparsed_body": text})


def _text(body: str, status: int) -> Response:
	return Response(body, status=status, mimetype="text/plain")


def _log_not_configured():
	# One Error Log entry per 10 minutes at most, so a misconfigured site doesn't flood the log.
	cache_key = "vertex_whatsapp:webhook_not_configured_logged"
	if frappe.cache.get_value(cache_key):
		return
	frappe.cache.set_value(cache_key, 1, expires_in_sec=600)
	frappe.log_error(
		title="WhatsApp webhook not configured",
		message="wa_verify_token and/or wa_app_secret is missing from site_config.json. "
		"All webhook requests are refused with HTTP 503 until both are set "
		"(bench --site <site> set-config wa_verify_token ... / wa_app_secret ...).",
	)
	frappe.db.commit()
