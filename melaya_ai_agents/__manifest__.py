{
    "name": "Melaya AI Agents for Odoo",
    "version": "20.0.1.0.0",
    "category": "Productivity",
    "summary": "Ask Melaya about a record and run your Melaya pipelines from Odoo",
    "description": """
Connect this Odoo database to a Melaya project. Users in the Melaya / User
group get a Melaya action on the records the administrator exposes: ask the
Melaya Assistant about the record, run a Melaya pipeline with a short brief,
and follow the run status. Changes Melaya makes in Odoo need an approval in
Melaya. A free Melaya account is required.

Data sent to Melaya (api.melaya.org, over HTTPS):
when an administrator connects, the database name, URL and Odoo version, the
login and API key of the Melaya Agent user the module creates, its groups,
and the administrator's login and name; on each call, the model name, the
record id, the Odoo user id (login and name once the user links a Melaya
account) and the brief the user types. Never the record content: Melaya
reads records as the Melaya Agent user, with the groups the administrator
picked, for every user of the project. Ask Melaya opens only after Odoo
checks the user's read access to the record, and works on that record's own
fields.

Requirements: Odoo 20 at a public https address with the JSON-2 external
API (the Odoo plan must include external API access). The Melaya Agent is an
internal user: on Odoo plans billed per user it counts as one paid user.
""",
    "author": "Melaya",
    "website": "https://melaya.org",
    "support": "info@melaya.org",
    "license": "OPL-1",
    "depends": ["base_setup", "web"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/melaya_security.xml",
        "security/ir.access.csv",
        "data/ir_cron_data.xml",
        "views/res_config_settings_views.xml",
        "views/melaya_exposed_model_views.xml",
        "views/melaya_connect_wizard_views.xml",
        "views/melaya_diagnostics_views.xml",
        "views/melaya_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "melaya_ai_agents/static/src/**/*",
        ],
    },
    # Add static/description/main_screenshot.png (first) and the other real
    # screenshots here once captured on a running Odoo 20 (see README).
    "images": ["static/description/icon.png"],
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "application": True,
}
