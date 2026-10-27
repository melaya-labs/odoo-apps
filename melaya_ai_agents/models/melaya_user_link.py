from odoo import api, fields, models


class MelayaUserLink(models.Model):
    """Odoo users whose Melaya account is linked, as last reported by Melaya.

    Only used to decide what identifies the user in calls to Melaya: before a
    user links, calls carry the Odoo user id only; once linked, also the login
    and name. Read and written with sudo by melaya.client only.
    """

    _name = "melaya.user.link"
    _description = "Odoo user linked to Melaya"

    user_id = fields.Many2one("res.users", string="User", required=True, ondelete="cascade", index=True)

    _user_uniq = models.Constraint("unique(user_id)", "This user is already marked as linked.")

    @api.model
    def _is_linked(self, user):
        return bool(self.sudo().search_count([("user_id", "=", user.id)], limit=1))

    @api.model
    def _set_linked(self, user, linked):
        """Write only when the state changes."""
        Links = self.sudo()
        current = Links.search([("user_id", "=", user.id)], limit=1)
        if linked and not current:
            Links.create({"user_id": user.id})
        elif not linked and current:
            current.unlink()
