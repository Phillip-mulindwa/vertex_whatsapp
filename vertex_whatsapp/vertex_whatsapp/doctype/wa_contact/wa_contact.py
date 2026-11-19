# Copyright (c) 2026, Vertex International Recruitment Ltd
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import now_datetime


class WAContact(Document):
	def validate(self):
		# Keep opted_out_at in step when staff tick or untick "Opted Out" by hand.
		if self.opted_out and not self.opted_out_at:
			self.opted_out_at = now_datetime()
		elif not self.opted_out:
			self.opted_out_at = None
