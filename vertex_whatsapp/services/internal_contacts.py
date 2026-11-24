"""Internal contacts (IT team, Doctor, colleagues) are never answered by the AI.

Phase 2 must call `is_internal_contact(wa_id)` FIRST, before anything reaches the AI.
"""

import frappe

from vertex_whatsapp.services.phone import normalise_number


def is_internal_contact(wa_id: str | None) -> bool:
	"""True if this WhatsApp number is on the WA Internal Contact list."""
	digits = normalise_number(wa_id)
	if not digits:
		return False
	return bool(frappe.db.exists("WA Internal Contact", {"phone": digits}))


def sync_contact_flag(phone: str | None) -> None:
	"""Set or clear `is_internal` on the WA Contact with this number (if one exists)."""
	digits = normalise_number(phone)
	if not digits or not frappe.db.exists("WA Contact", digits):
		return
	frappe.db.set_value("WA Contact", digits, "is_internal", 1 if is_internal_contact(digits) else 0)
