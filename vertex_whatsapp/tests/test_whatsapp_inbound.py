"""Phase 1 tests: webhook verification + signature, and processing into WA Contact / WA Message.

Run: bench --site vertex.localhost run-tests --app vertex_whatsapp
Uses fake secrets set here and mocks the job queue. No network calls.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from vertex_whatsapp.api import webhook
from vertex_whatsapp.services import wa_inbound
from vertex_whatsapp.services.internal_contacts import is_internal_contact
from vertex_whatsapp.services.phone import country_for_number
from vertex_whatsapp.tests import wa_payloads as P

TEST_CONF = {"wa_verify_token": "test-verify-token-not-real", "wa_app_secret": "test-app-secret-not-real"}

UK = "447700900101"
UK_2 = "447700900102"
UK_INTERNAL = "447700900199"
UGANDA = "256772000101"


def sign(body: bytes, secret: str = TEST_CONF["wa_app_secret"]) -> str:
	return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def cleanup():
	frappe.db.delete("WA Message", {"wamid": ["like", "wamid.TEST%"]})
	frappe.db.delete("WA Contact", {"wa_id": ["like", "4477009001%"]})
	frappe.db.delete("WA Contact", {"wa_id": ["like", "2567720001%"]})
	frappe.db.delete("WA Internal Contact", {"phone": ["like", "4477009001%"]})
	frappe.db.delete("WA Event", {"payload": ["like", f"%{P.TEST_WABA_ID}%"]})
	frappe.db.commit()


def store_and_process(payload: dict) -> str:
	"""Insert a WA Event the way the webhook does, then run the background processor on it."""
	event = frappe.get_doc(
		{
			"doctype": "WA Event",
			"received_at": frappe.utils.now_datetime(),
			"event_type": webhook.classify_payload(json.dumps(payload).encode()),
			"payload": json.dumps(payload),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # the real webhook commits before the job runs
	wa_inbound.process_event(event.name)
	return event.name


def message_by_wamid(wamid: str):
	name = frappe.db.get_value("WA Message", {"wamid": wamid})
	return frappe.get_doc("WA Message", name) if name else None


class WebhookRequestTestCase(FrappeTestCase):
	"""Calls webhook.receive() with a real werkzeug request object, as Frappe would."""

	def setUp(self):
		cleanup()
		self._saved_request = getattr(frappe.local, "request", None)

	def tearDown(self):
		frappe.local.request = self._saved_request
		cleanup()

	def call(self, method="GET", query=None, body: bytes = b"", headers=None, conf=TEST_CONF):
		env = EnvironBuilder(
			path="/api/method/vertex_whatsapp.api.webhook.receive",
			method=method,
			query_string=query,
			data=body,
			headers=headers or {},
			content_type="application/json" if body else None,
		).get_environ()
		frappe.local.request = Request(env)
		with patch.dict(frappe.local.conf, conf, clear=False):
			for key in TEST_CONF:
				if key not in conf:
					frappe.local.conf.pop(key, None)
			return webhook.receive()


class TestVerifyHandshake(WebhookRequestTestCase):
	def test_good_handshake_returns_bare_challenge(self):
		resp = self.call(query={"hub.mode": "subscribe", "hub.verify_token": TEST_CONF["wa_verify_token"], "hub.challenge": "1158201444"})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.get_data(as_text=True), "1158201444")
		self.assertEqual(resp.mimetype, "text/plain")

	def test_wrong_token_is_forbidden(self):
		resp = self.call(query={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1158201444"})
		self.assertEqual(resp.status_code, 403)
		self.assertNotIn("1158201444", resp.get_data(as_text=True))

	def test_wrong_mode_is_forbidden(self):
		resp = self.call(query={"hub.mode": "unsubscribe", "hub.verify_token": TEST_CONF["wa_verify_token"], "hub.challenge": "1"})
		self.assertEqual(resp.status_code, 403)

	def test_missing_config_refuses_with_503(self):
		resp = self.call(query={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}, conf={})
		self.assertEqual(resp.status_code, 503)
		body = json.dumps(P.text_message("wamid.TEST_NOCONF", UK)).encode()
		resp = self.call("POST", body=body, headers={"X-Hub-Signature-256": sign(body)}, conf={"wa_verify_token": "only-one"})
		self.assertEqual(resp.status_code, 503)
		self.assertFalse(frappe.db.exists("WA Event", {"payload": ["like", "%wamid.TEST_NOCONF%"]}))


class TestSignedPost(WebhookRequestTestCase):
	def test_bad_signature_is_rejected_and_nothing_stored(self):
		body = json.dumps(P.text_message("wamid.TEST_BADSIG", UK)).encode()
		before = frappe.db.count("WA Event")
		with patch("frappe.enqueue") as enqueue:
			resp = self.call("POST", body=body, headers={"X-Hub-Signature-256": sign(body, "some-other-secret")})
		self.assertEqual(resp.status_code, 401)
		self.assertEqual(frappe.db.count("WA Event"), before)
		enqueue.assert_not_called()

	def test_missing_signature_is_rejected(self):
		body = json.dumps(P.text_message("wamid.TEST_NOSIG", UK)).encode()
		before = frappe.db.count("WA Event")
		resp = self.call("POST", body=body)
		self.assertEqual(resp.status_code, 401)
		self.assertEqual(frappe.db.count("WA Event"), before)

	def test_tampered_body_is_rejected(self):
		body = json.dumps(P.text_message("wamid.TEST_TAMPER", UK)).encode()
		resp = self.call("POST", body=body.replace(b"Hello", b"Hellx"), headers={"X-Hub-Signature-256": sign(body)})
		self.assertEqual(resp.status_code, 401)

	def test_good_signature_stores_event_and_queues_processing(self):
		body = json.dumps(P.text_message("wamid.TEST_GOODSIG", UK)).encode()
		with patch("frappe.enqueue") as enqueue:
			resp = self.call("POST", body=body, headers={"X-Hub-Signature-256": sign(body)})
		self.assertEqual(resp.status_code, 200)
		name = frappe.db.get_value("WA Event", {"payload": ["like", "%wamid.TEST_GOODSIG%"]})
		self.assertTrue(name)
		event = frappe.get_doc("WA Event", name)
		self.assertEqual(event.event_type, "message")
		self.assertEqual(event.processed, 0)
		self.assertEqual(json.loads(event.payload), json.loads(body))
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], webhook.PROCESS_METHOD)
		self.assertEqual(enqueue.call_args.kwargs["event_name"], name)
		# Nothing processed yet: the webhook only stores.
		self.assertIsNone(message_by_wamid("wamid.TEST_GOODSIG"))

	def test_queue_failure_still_returns_200_and_keeps_event(self):
		body = json.dumps(P.text_message("wamid.TEST_QDOWN", UK)).encode()
		with patch("frappe.enqueue", side_effect=ConnectionError("redis down")):
			resp = self.call("POST", body=body, headers={"X-Hub-Signature-256": sign(body)})
		self.assertEqual(resp.status_code, 200)
		self.assertTrue(frappe.db.exists("WA Event", {"payload": ["like", "%wamid.TEST_QDOWN%"]}))


class TestInboundProcessing(FrappeTestCase):
	def setUp(self):
		cleanup()

	def tearDown(self):
		cleanup()

	def test_text_message_creates_contact_and_message(self):
		event = store_and_process(P.text_message("wamid.TEST_TEXT1", UK, body="Hi, I am looking for work", name="Jane Doe"))
		self.assertEqual(frappe.db.get_value("WA Event", event, ["processed", "error"]), (1, None))

		msg = message_by_wamid("wamid.TEST_TEXT1")
		self.assertEqual((msg.direction, msg.sent_by, msg.msg_type, msg.status), ("In", "Customer", "text", "received"))
		self.assertEqual(msg.body, "Hi, I am looking for work")
		self.assertEqual(msg.contact, UK)
		self.assertIsNotNone(msg.sent_at)

		contact = frappe.get_doc("WA Contact", UK)
		self.assertEqual(contact.profile_name, "Jane Doe")
		self.assertEqual((contact.country_code, contact.country_name, contact.flagged_country), ("GB", "United Kingdom", 0))
		self.assertEqual((contact.category, contact.conversation_status), ("Unclear", "AI handling"))
		self.assertEqual(contact.is_internal, 0)
		self.assertIsNotNone(contact.first_seen)
		self.assertEqual(str(contact.last_inbound_at), str(msg.sent_at))

	def test_image_message(self):
		store_and_process(P.image_message("wamid.TEST_IMG1", UK, caption="My CV", media_id="9988776655"))
		msg = message_by_wamid("wamid.TEST_IMG1")
		self.assertEqual((msg.msg_type, msg.media_id, msg.body), ("image", "9988776655", "My CV"))

	def test_ad_referral_is_captured(self):
		store_and_process(P.referral_message("wamid.TEST_REF1", UK))
		msg = message_by_wamid("wamid.TEST_REF1")
		self.assertEqual(msg.referral_source_type, "ad")
		self.assertEqual(msg.referral_source_id, "120210000000000001")
		self.assertEqual(msg.referral_headline, "Become a Vertex partner agency")
		self.assertTrue(msg.referral_ctwa_clid.startswith("ARAkLkA8"))

	def _outgoing(self, wamid: str):
		wa_inbound.upsert_contact(UK)
		return frappe.get_doc(
			{"doctype": "WA Message", "wamid": wamid, "contact": UK, "direction": "Out", "sent_by": "AI", "msg_type": "text", "status": "sent"}
		).insert(ignore_permissions=True)

	def test_status_delivered_then_read(self):
		self._outgoing("wamid.TEST_OUT1")
		store_and_process(P.status_update("wamid.TEST_OUT1", UK, "delivered"))
		self.assertEqual(message_by_wamid("wamid.TEST_OUT1").status, "delivered")
		store_and_process(P.status_update("wamid.TEST_OUT1", UK, "read"))
		self.assertEqual(message_by_wamid("wamid.TEST_OUT1").status, "read")

	def test_late_delivered_does_not_undo_read(self):
		self._outgoing("wamid.TEST_OUT2")
		store_and_process(P.status_update("wamid.TEST_OUT2", UK, "read"))
		store_and_process(P.status_update("wamid.TEST_OUT2", UK, "delivered"))
		self.assertEqual(message_by_wamid("wamid.TEST_OUT2").status, "read")

	def test_status_failed_stores_error(self):
		self._outgoing("wamid.TEST_OUT3")
		event = store_and_process(P.status_update("wamid.TEST_OUT3", UK, "failed", 131047, "Re-engagement message"))
		msg = message_by_wamid("wamid.TEST_OUT3")
		self.assertEqual((msg.status, msg.error_code, msg.error_title), ("failed", "131047", "Re-engagement message"))
		self.assertEqual(frappe.db.get_value("WA Event", event, "event_type"), "status")

	def test_status_for_unknown_message_is_harmless(self):
		event = store_and_process(P.status_update("wamid.TEST_UNKNOWN", UK, "delivered"))
		self.assertEqual(frappe.db.get_value("WA Event", event, "processed"), 1)

	def test_message_echo_is_stored_as_staff_outgoing(self):
		event = store_and_process(P.message_echo("wamid.TEST_ECHO1", UK_2, body="Thanks, we will be in touch."))
		self.assertEqual(frappe.db.get_value("WA Event", event, ["event_type", "processed"]), ("echo", 1))
		msg = message_by_wamid("wamid.TEST_ECHO1")
		self.assertEqual((msg.direction, msg.sent_by, msg.status, msg.contact), ("Out", "Staff", "sent", UK_2))
		self.assertEqual(msg.body, "Thanks, we will be in touch.")
		# A staff message is not customer activity.
		self.assertIsNone(frappe.db.get_value("WA Contact", UK_2, "last_inbound_at"))

	def test_echo_without_number_is_kept_unprocessed_with_note(self):
		payload = P.message_echo("wamid.TEST_ECHO2", UK_2)
		del payload["entry"][0]["changes"][0]["value"]["message_echoes"][0]["to"]
		event = store_and_process(payload)
		processed, error = frappe.db.get_value("WA Event", event, ["processed", "error"])
		self.assertEqual(processed, 0)
		self.assertIn("username-only", error)
		self.assertIsNone(message_by_wamid("wamid.TEST_ECHO2"))

	def test_duplicate_delivery_is_skipped(self):
		payload = P.text_message("wamid.TEST_DUP1", UK, body="Hello twice")
		first = store_and_process(payload)
		second = store_and_process(payload)
		self.assertEqual(frappe.db.count("WA Message", {"wamid": "wamid.TEST_DUP1"}), 1)
		self.assertEqual(frappe.db.get_value("WA Event", first, "processed"), 1)
		self.assertEqual(frappe.db.get_value("WA Event", second, ["processed", "error"]), (1, None))

	def test_processing_same_event_twice_is_safe(self):
		event = store_and_process(P.text_message("wamid.TEST_DUP2", UK))
		frappe.db.set_value("WA Event", event, "processed", 0)
		wa_inbound.process_event(event)
		self.assertEqual(frappe.db.count("WA Message", {"wamid": "wamid.TEST_DUP2"}), 1)

	def test_flagged_country_uganda(self):
		store_and_process(P.text_message("wamid.TEST_UG1", UGANDA))
		contact = frappe.get_doc("WA Contact", UGANDA)
		self.assertEqual((contact.country_code, contact.country_name, contact.flagged_country), ("UG", "Uganda", 1))

	def test_flagged_country_lookup(self):
		for number, code in (("251911000000", "ET"), ("639171234567", "PH"), ("27821234567", "ZA"), ("256772123456", "UG")):
			self.assertEqual(country_for_number(number)["flagged_country"], 1, code)
			self.assertEqual(country_for_number(number)["country_code"], code)
		self.assertEqual(country_for_number("254712345678")["flagged_country"], 0)  # Kenya
		self.assertEqual(country_for_number("not a number")["country_code"], None)

	def test_opt_out_keywords(self):
		for i, word in enumerate(["STOP", "Unsubscribe.", "ARRÊT", "désabonner", "توقف", "إلغاء الاشتراك"]):
			wa_id = f"44770090011{i}"
			store_and_process(P.text_message(f"wamid.TEST_OPT{i}", wa_id, body=word))
			contact = frappe.get_doc("WA Contact", wa_id)
			self.assertEqual(contact.opted_out, 1, word)
			self.assertIsNotNone(contact.opted_out_at)

	def test_stop_inside_a_sentence_is_not_an_opt_out(self):
		store_and_process(P.text_message("wamid.TEST_OPTX", UK, body="Please don't stop helping me find work"))
		self.assertEqual(frappe.db.get_value("WA Contact", UK, "opted_out"), 0)

	def _unprocessed_event(self, wamid: str, age_minutes: int) -> str:
		event = frappe.get_doc(
			{"doctype": "WA Event", "received_at": frappe.utils.now_datetime(), "event_type": "message",
			 "payload": json.dumps(P.text_message(wamid, UK))}
		).insert(ignore_permissions=True)
		frappe.db.set_value("WA Event", event.name, "creation", frappe.utils.add_to_date(None, minutes=-age_minutes), update_modified=False)
		frappe.db.commit()
		return event.name

	def test_pending_sweeper_processes_events_older_than_10_minutes(self):
		event = self._unprocessed_event("wamid.TEST_SWEEP_OLD", age_minutes=11)
		wa_inbound.process_pending_events()
		self.assertEqual(frappe.db.get_value("WA Event", event, "processed"), 1)
		self.assertIsNotNone(message_by_wamid("wamid.TEST_SWEEP_OLD"))

	def test_pending_sweeper_leaves_recent_events_to_the_queue(self):
		self.assertEqual(wa_inbound.PENDING_EVENT_MIN_AGE_MINUTES, 10)
		fresh = self._unprocessed_event("wamid.TEST_SWEEP_NEW", age_minutes=0)
		nine = self._unprocessed_event("wamid.TEST_SWEEP_9MIN", age_minutes=9)
		wa_inbound.process_pending_events()
		for name in (fresh, nine):
			self.assertEqual(frappe.db.get_value("WA Event", name, "processed"), 0)
		self.assertIsNone(message_by_wamid("wamid.TEST_SWEEP_NEW"))
		self.assertIsNone(message_by_wamid("wamid.TEST_SWEEP_9MIN"))


class TestInternalContacts(FrappeTestCase):
	def setUp(self):
		cleanup()

	def tearDown(self):
		cleanup()

	def test_phone_is_normalised_and_detected(self):
		doc = frappe.get_doc({"doctype": "WA Internal Contact", "phone": "+44 7700 900-199", "contact_name": "IT Team"}).insert()
		self.assertEqual(doc.phone, UK_INTERNAL)
		self.assertTrue(is_internal_contact(UK_INTERNAL))
		self.assertTrue(is_internal_contact("+447700900199"))
		self.assertFalse(is_internal_contact(UK))
		self.assertFalse(is_internal_contact(None))

	def test_local_format_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "WA Internal Contact", "phone": "07700 900199", "contact_name": "No code"}).insert()

	def test_duplicate_internal_number_is_refused(self):
		frappe.get_doc({"doctype": "WA Internal Contact", "phone": "+447700900199", "contact_name": "A"}).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "WA Internal Contact", "phone": "00447700900199", "contact_name": "B"}).insert()

	def test_new_message_from_internal_number_marks_contact(self):
		frappe.get_doc({"doctype": "WA Internal Contact", "phone": "+447700900199", "contact_name": "Doctor"}).insert()
		store_and_process(P.text_message("wamid.TEST_INT1", UK_INTERNAL))
		self.assertEqual(frappe.db.get_value("WA Contact", UK_INTERNAL, "is_internal"), 1)

	def test_adding_and_removing_internal_contact_updates_existing_contact(self):
		store_and_process(P.text_message("wamid.TEST_INT2", UK_INTERNAL))
		self.assertEqual(frappe.db.get_value("WA Contact", UK_INTERNAL, "is_internal"), 0)

		doc = frappe.get_doc({"doctype": "WA Internal Contact", "phone": "+447700900199", "contact_name": "Colleague"}).insert()
		self.assertEqual(frappe.db.get_value("WA Contact", UK_INTERNAL, "is_internal"), 1)

		doc.delete()
		self.assertEqual(frappe.db.get_value("WA Contact", UK_INTERNAL, "is_internal"), 0)
