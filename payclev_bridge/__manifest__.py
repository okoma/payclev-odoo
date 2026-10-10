{  # noqa: B018 - Odoo reads this manifest as a Python dict literal.
    "name": "Payclev",
    "version": "19.0.1.0.9",
    "summary": "Connect Odoo accounting to Payclev",
    "author": "Payclev",
    "category": "Accounting",
    "license": "LGPL-3",
    "depends": ["account", "account_payment", "auth_signup"],
    "data": [
        "security/payclev_bridge_security.xml",
        "security/ir.model.access.csv",
        "data/invoice_notification_cron.xml",
        "views/account_journal_views.xml",
        "views/payclev_connection_views.xml",
        "views/payclev_payment_option_views.xml",
        "views/account_move_views.xml",
    ],
    "assets": {
        "web.assets_backend": ["payclev_bridge/static/src/css/invoice_context.css"],
    },
    "installable": True,
    "application": True,
}
