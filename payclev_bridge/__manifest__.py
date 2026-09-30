{  # noqa: B018 - Odoo reads this manifest as a Python dict literal.
    "name": "PayClev Accounting Bridge",
    "version": "19.0.1.0.0",
    "summary": "Atomic, duplicate-safe PayClev invoice payment registration",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/payclev_bridge_security.xml",
        "security/ir.model.access.csv",
        "views/account_journal_views.xml",
    ],
    "installable": True,
    "application": False,
}
