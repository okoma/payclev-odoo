# PayClev Odoo 19 bridge (unreleased)

Current verification: implementation and static/offline PayClev adapter checks
are complete; the addon `TransactionCase` suite and real-instance qualification
are pending because no Odoo 19 runtime is available in this workspace.

This addon targets Odoo 19 on Odoo.sh or a self-hosted Odoo server. It is not
usable on Odoo Online. PayClev's Odoo catalog entry and merchant checkout stay
disabled until the full payment lifecycle has passed qualification; installing
this addon alone does not enable either.

Install `payclev_bridge` alongside Odoo's `account` addon. Create a dedicated
Odoo integration user with access only to the chosen company, invoice/payment
accounting permissions, and a short-lived JSON-2 API key. The Odoo deployment
must have Custom-plan external API access. Configure an incoming bank/cash
journal and manual payment method with an outstanding receipts account. Do not
use a personal administrator key or an Odoo payment-provider method for
external Paystack/Stripe or verified-bank write-back.

PayClev connection setup accepts the HTTPS base URL, optional database name,
deployment type, key and its expiry. The key is encrypted in PayClev and is
never returned in API responses. Rotation verifies the replacement key before
switching and requires a successful new-key sync before the administrator can
confirm that the old key was revoked in Odoo.

The bridge exposes only health, eligible payment methods, payment snapshots,
stable-UUID lookup, and one atomic confirmed-payment application. It does not
provide a generic write API. The payment method enforces company, invoice,
customer, currency, journal, outstanding-account, exact amount and unique
PayClev UUID. A failed method call rolls back the entire Odoo transaction.

Before changing the provider release gate, run the PayClev unit suite and an
Odoo 19 instance qualification covering:

1. Custom-plan JSON-2 authentication, bot permissions and multi-company denial.
2. Initial, incremental and safety sync of customers, invoices and payments.
3. Native Odoo payment sync, partial payment, and exact allocation snapshots.
4. Confirmed PayClev processor and verified bank payment write-back.
5. Same UUID retry and lost-response retry without a duplicate Odoo payment.
6. Full and partial residual changes after Odoo re-fetch; `in_payment` is not
   displayed as settled cash.
7. Payment cancellation, unreconciliation, reallocation and missing-payment
   review holds.
8. Wrong company/currency, changed journal/outstanding account, expired or
   revoked key, and Odoo Online denial.

The operational release decision must be recorded separately. Until those
checks pass, `release_ready=False` is intentional.

## Bridge contract and configuration

The addon exposes only five JSON-2 model methods on `payclev.payment.bridge`:
`payclev_health`, `payclev_payment_methods`, `payclev_find_payment`,
`payclev_payment_snapshot`, and `payclev_apply_confirmed_payment`. It does not
expose a generic accounting mutation method. Odoo's ordinary JSON-2 API remains
available to the bot according to its normal Odoo access rights, so restrict
its permissions and protect its expiring API key.

The bridge returns Odoo/bridge version, database UUID, bot identity, selected
company/currency, and the count/readiness of enabled payment methods. An
Accounting Manager must opt in each selected bank/cash journal under its
**Incoming Payments** tab using **Allow PayClev payment write-back**. It is
off by default. The journal must have a manual incoming payment method with
an outstanding receipts account. PayClev's payment-account setting selects
one of these eligible journal/method pairs. A bot cannot change the journal
opt-in flag.

Each confirmed payment is one Odoo JSON-2 transaction. A transaction-scoped
UUID lock serializes retries, an invoice row lock serializes different
payments against one residual, and a database unique constraint on
`(company_id, payment_uuid)` is the final duplicate guard. The bridge claims
the identity before checking residual, registers/posts/reconciles via Odoo
19's payment wizard, verifies exact allocation to the selected invoice, and
stores the Odoo payment ID. Any failure rolls the transaction back. A retry
with the same UUID returns the committed result only if the immutable invoice,
customer, amount, currency, journal, method and payment date also match.
After a lost HTTP response, PayClev looks up that UUID before attempting
creation. The bridge never identifies duplicates by amount, date or memo.

The bridge does not perform FX, split one payment across invoices, or settle
bank transactions. Odoo remains the financial authority; PayClev re-fetches
its invoice/payment snapshots after write-back. `in_payment` is not `paid`.

## Installation on Odoo 19

1. Back up the Odoo database and filestore. Confirm Odoo 19 and JSON-2 external
   API access are available on the target deployment/plan.
2. Put `payclev_bridge` on the Odoo addons path. On Odoo.sh, commit it to the
   branch's custom addons directory. On self-hosted Odoo, add this repository
   root (the directory containing `payclev_bridge`) to `addons_path`.
3. Update the Apps list and install **PayClev Accounting Bridge**. It depends
   on Odoo Accounting and loads a bridge-only group, read-only identity access,
   a company record rule and the journal configuration field.
4. Create a dedicated internal integration user. Assign **PayClev Accounting
   Bridge** plus the smallest Accounting role that permits invoice/payment
   registration (the bridge also requires Odoo's invoice permission). Limit
   allowed companies to the one connected to PayClev. Do not use a personal
   administrator key. Verify the effective permissions in the test suite.
5. In Accounting → Configuration → Journals, configure the selected incoming
   bank/cash journal and its manual method's Outstanding Receipts account.
   An Accounting Manager then enables **Allow PayClev payment write-back**.
6. Create an expiring API key for the bot. Supply PayClev's unreleased test
   connection flow with its HTTPS base URL, key, selected company and database
   name only if the host does not identify a database. Rotate before expiry.

Installing the addon alone never enables the PayClev provider or checkout.

## Upgrade

1. Back up the Odoo database and filestore. Pause PayClev write-back attempts
   and retain all in-flight PayClev payment UUIDs.
2. Deploy the new addon code to the same addons path. Review model/constraint
   changes before upgrading.
3. Upgrade the module on a test database, for example:

   ```bash
   odoo-bin -d TEST_DATABASE -u payclev_bridge --stop-after-init
   ```

   Use the Odoo.sh upgrade workflow instead of a shell command there.
4. Run the addon tests and query `payclev_health` with the bot API key. Check
   bridge version, database UUID, company, currency and eligible journal
   count. Verify old `(company_id, payment_uuid)` identities still resolve
   to the same Odoo payment and allocation.
5. Resume attempts only after verification. On failure, restore the backup;
   never delete identities or change a payment UUID to force a retry.

Exact minor units are stored as decimal digits because Odoo 19
`fields.Integer` is PostgreSQL `int4`. If upgrading an earlier local copy
which used an integer `amount_minor`, verify the Odoo schema upgrade preserves
all identities before any payment processing. No production backfill is
assumed while this provider remains unreleased.

## Tests and live qualification

Run the real Odoo accounting tests in a disposable Odoo 19 database:

```bash
odoo-bin -d TEST_DATABASE -i payclev_bridge \
  --test-enable --test-tags /payclev_bridge --stop-after-init
```

Use `-u` instead of `-i` for an already installed addon. The Odoo
`TransactionCase` suite tests partial registration/application, allocation
identity, same-UUID retries, lost-response lookup, changed-payload conflict,
overpayment rollback, opt-in journal, wrong currency and access boundaries.
PayClev adapter tests run with
`apps/api/.venv/bin/pytest apps/api/tests/test_odoo_units.py`.

Offline passing tests are not live qualification. On a real Odoo 19 Odoo.sh or
supported self-hosted test instance, record Odoo build/modules/localization,
test date, provider IDs and results for JSON-2 access, least-privilege bot,
multi-company denial, concurrent same/different UUID requests, a dropped
response after commit, full/partial payments, authoritative re-fetch, native
Odoo payments, reversals/unreconciliation, key expiry/rotation and degraded
journal/outstanding-account configuration. Only a passing qualification may
justify changing the PayClev release gate.

The implementation follows Odoo 19's [JSON-2 API and transaction contract](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html),
[payment registration behavior](https://www.odoo.com/documentation/19.0/applications/finance/accounting/payments.html),
[security rules](https://www.odoo.com/documentation/19.0/developer/reference/backend/security.html),
and [official account payment wizard](https://github.com/odoo/odoo/blob/19.0/addons/account/wizard/account_payment_register.py).
