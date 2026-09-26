import frappe
from frappe import _

no_cache = 1


def get_context(context):
	"""Serve Domino Stream Console with DCMS-aligned session auth."""
	if frappe.session.user in (None, "Guest"):
		frappe.local.flags.redirect_location = "/login?redirect-to=/domino-stream"
		raise frappe.Redirect

	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(("System Manager", "Domino Manager")):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()  # nosemgrep
	context.csrf_token = csrf_token
	context.user = frappe.session.user
	return context
