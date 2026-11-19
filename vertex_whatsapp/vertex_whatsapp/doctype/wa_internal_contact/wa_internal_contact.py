# Copyright (c) 2026, Vertex International Recruitment Ltd
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from vertex_whatsapp.services.internal_contacts import sync_contact_flag
from vertex_whatsapp.services.phone import normalise_number


class WAInternalContact(Document):
	def validate(self):
		raw = (self.phone or "").strip()
		self.phone = normalise_number(raw)
		if raw.startswith("0") and not raw.startswith("00"):
			frappe.throw(_("Enter the number in international format with the country code, e.g. +44 7700 900123."))
		if not 8 <= len(self.phone) <= 15:
			frappe.throw(_("Phone must be 8 to 15 digits including the country code."))

	def on_update(self):
		previous = self.get_doc_before_save()
		if previous and previous.phone != self.phone:
			sync_contact_flag(previous.phone)
		sync_contact_flag(self.phone)

	def on_trash(self):
		self.flags.phone_to_clear = self.phone

	def after_delete(self):
		sync_contact_flag(self.flags.phone_to_clear)
