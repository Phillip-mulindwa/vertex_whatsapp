"""Phone number helpers: normalising numbers and working out the country.

Uses the `phonenumbers` library that already ships with Frappe.
"""

import re

import phonenumbers
from phonenumbers import geocoder

# Countries whose candidates get a polite decline (agencies are still saved, with a marker).
FLAGGED_COUNTRY_CODES = frozenset({"ET", "PH", "UG", "ZA"})  # Ethiopia, Philippines, Uganda, South Africa


def normalise_number(value: str | None) -> str:
	"""Return the number as digits only, with country code and no leading + or 00.

	"+44 7700 900-123" -> "447700900123", "00256 772 123456" -> "256772123456".
	"""
	digits = re.sub(r"\D", "", value or "")
	if digits.startswith("00"):
		digits = digits[2:]
	return digits


def country_for_number(wa_id: str | None) -> dict:
	"""Work out the country for a WhatsApp ID (international digits).

	Returns {"country_code": "UG", "country_name": "Uganda", "flagged_country": 1}
	with empty values when the number cannot be parsed.
	"""
	result = {"country_code": None, "country_name": None, "flagged_country": 0}
	digits = normalise_number(wa_id)
	if not digits:
		return result
	try:
		parsed = phonenumbers.parse("+" + digits, None)
	except phonenumbers.NumberParseException:
		return result

	# Exact match for valid numbers; otherwise fall back to the calling code (+44 -> GB), which also
	# covers number ranges newer than the library. Shared codes (+1, +7) fall back to the main country.
	region = phonenumbers.region_code_for_number(parsed)
	if not region or region == "ZZ":
		region = phonenumbers.region_code_for_country_code(parsed.country_code)
	if not region or region in ("ZZ", "001"):
		return result

	result["country_code"] = region
	result["country_name"] = geocoder.country_name_for_number(parsed, "en") or _country_name(region)
	result["flagged_country"] = 1 if region in FLAGGED_COUNTRY_CODES else 0
	return result


def format_international(wa_id: str | None) -> str | None:
	"""Human-readable international format, e.g. "+256 772 123456"."""
	digits = normalise_number(wa_id)
	if not digits:
		return None
	try:
		parsed = phonenumbers.parse("+" + digits, None)
		return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
	except phonenumbers.NumberParseException:
		return "+" + digits


def _country_name(region: str) -> str:
	"""Country name from Frappe's built-in Country list, e.g. "GB" -> "United Kingdom"."""
	import frappe

	return frappe.db.get_value("Country", {"code": region.lower()}, "name") or region
