"""Webhook payloads in Meta's documented shapes (WhatsApp Cloud API), with fictional numbers and IDs.

UK numbers use Ofcom's reserved drama range (+44 7700 900xxx).
"""

import time

TEST_WABA_ID = "TEST_WABA_ID_000"
BUSINESS_NUMBER = "15550783881"
PHONE_NUMBER_ID = "106540352242922"


def _envelope(field: str, value: dict) -> dict:
	value = {
		"messaging_product": "whatsapp",
		"metadata": {"display_phone_number": BUSINESS_NUMBER, "phone_number_id": PHONE_NUMBER_ID},
		**value,
	}
	return {"object": "whatsapp_business_account", "entry": [{"id": TEST_WABA_ID, "changes": [{"value": value, "field": field}]}]}


def _contacts(wa_id: str, name: str) -> list:
	return [{"profile": {"name": name}, "wa_id": wa_id}]


def text_message(wamid: str, wa_id: str, body: str = "Hello", name: str = "Test Customer", ts: int | None = None) -> dict:
	return _envelope(
		"messages",
		{
			"contacts": _contacts(wa_id, name),
			"messages": [{"from": wa_id, "id": wamid, "timestamp": str(ts or int(time.time())), "type": "text", "text": {"body": body}}],
		},
	)


def image_message(wamid: str, wa_id: str, caption: str = "My CV", media_id: str = "1234567890123456") -> dict:
	return _envelope(
		"messages",
		{
			"contacts": _contacts(wa_id, "Image Sender"),
			"messages": [
				{
					"from": wa_id,
					"id": wamid,
					"timestamp": str(int(time.time())),
					"type": "image",
					"image": {"caption": caption, "mime_type": "image/jpeg", "sha256": "c2hhMjU2LXRlc3Q=", "id": media_id},
				}
			],
		},
	)


def referral_message(wamid: str, wa_id: str) -> dict:
	payload = text_message(wamid, wa_id, body="What benefits do I get from partnering with Vertex?", name="Ad Clicker")
	payload["entry"][0]["changes"][0]["value"]["messages"][0]["referral"] = {
		"source_url": "https://fb.me/test-ad",
		"source_id": "120210000000000001",
		"source_type": "ad",
		"body": "Partner with Vertex International Recruitment",
		"headline": "Become a Vertex partner agency",
		"media_type": "image",
		"image_url": "https://example.com/ad.jpg",
		"ctwa_clid": "ARAkLkA8rmlFeiCktEJQ-QTwRiyYHAFDLMNDBH0CD3qpjd0HR4irJ6LEkR7JwFF4XvnO2E4Nx0-eM-GABDLOPaOdRMv",
		"welcome_message": {"text": "Hi! Thanks for your interest."},
	}
	return payload


def status_update(wamid: str, recipient: str, status: str, error_code: int | None = None, error_title: str | None = None) -> dict:
	item = {"id": wamid, "status": status, "timestamp": str(int(time.time())), "recipient_id": recipient}
	if status == "failed":
		item["errors"] = [
			{
				"code": error_code,
				"title": error_title,
				"message": error_title,
				"error_data": {"details": "Message failed to send."},
				"href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/",
			}
		]
	return _envelope("messages", {"statuses": [item]})


def message_echo(wamid: str, to: str, body: str = "Thanks, we will be in touch.") -> dict:
	"""Shape from Meta's smb_message_echoes webhook reference."""
	return _envelope(
		"smb_message_echoes",
		{
			"contacts": [{"wa_id": to, "user_id": "GB.13491208655302741918"}],
			"message_echoes": [
				{
					"from": BUSINESS_NUMBER,
					"to": to,
					"to_user_id": "GB.13491208655302741918",
					"id": wamid,
					"timestamp": str(int(time.time())),
					"type": "text",
					"text": {"body": body},
				}
			],
		},
	)
