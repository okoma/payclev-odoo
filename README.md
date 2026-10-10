# Payclev Odoo 19 app (unreleased)

The local Odoo 19 test instance is available in the Payclev development
workspace. This does not qualify the module for production release.

This addon targets Odoo 19 on Odoo.sh or a self-hosted Odoo server. It is not
usable on Odoo Online. Payclev's Odoo catalog entry and merchant checkout stay
disabled until the full payment lifecycle has passed qualification; installing
this addon alone does not enable either.

Install `payclev_bridge` alongside Odoo's `account` addon. The module appears
as **Payclev** in Odoo's app launcher, under Accounting, with the Payclev icon
and a dedicated management screen. An Odoo administrator selects the company
and clicks **Connect to Payclev**. Payclev asks the administrator to sign in,
select a business and approve the accounting access. On return, the module
creates a dedicated company-scoped service user and short-lived JSON-2 key,
then securely submits it to Payclev for verification. No key is copied through
the browser. This API-only user has no browser password or human signup email,
is shown as a service account rather than a pending invitation, and is excluded
from Odoo's unregistered-user reminder. An addon upgrade migrates existing
paired service users without rotating or revoking their JSON-2 keys. The Odoo
deployment must have Custom-plan external API access.
In **Odoo → Payclev → Manage Payclev payment options**, an Accounting Manager
creates one option per invoice currency and selects an existing, reconcilable
Outstanding Receipts account approved by the accountant. The add-on creates a
Payclev-only journal and incoming method, then protects their accounting
configuration from ordinary edits. Check pending Payclev reconciliations
before pausing an option: pausing blocks unposted write-backs in that currency,
but does not delete historical payments. Previously opted-in
bank/cash journals remain available for existing connections. Once an option
is saved, Payclev detects it on the next sync and verifies it for open invoices
in that currency; there is no second selection step for newly managed options.
Existing manually selected journals are not replaced automatically. This
option is an internal write-back destination, not a customer checkout link or
an Odoo payment-provider method for Paystack/Stripe or verified-bank receipts.

Odoo-hosted invoice payment is separate. With **Invoice Online Payment** and a
published compatible Odoo payment provider enabled, the add-on returns Odoo's
own portal payment link for each posted, payable customer invoice. Payclev can
show that official link alongside independently eligible Payclev payment
methods. Odoo handles the hosted checkout and accounting payment; Payclev
observes the result through its normal sync. The link is not created from a
Payclev payment option, and enabling an Odoo provider does not enable Payclev
merchant write-back.

The Odoo admin screen shows connection status, selected company, Payclev
workspace, payment-journal setup and key expiry. The administrator can enable
**Renew service key automatically** before expiry. This is recommended: during
the 14-day renewal window, Payclev verifies a new company-scoped key, durably
switches to it, and only then asks Odoo to revoke the exact old key. If the
current key has already expired, a new administrator-approved pairing is
required. Set the Payclev app/API origins on that screen only
when they differ from the production defaults; local HTTP requires an explicit
local-testing option and a localhost Odoo base URL. Payclev stores the key
encrypted and never returns it in browser API responses. There is no manual
key entry or replacement in Payclev. If automatic renewal is off, disconnect
the Odoo integration in Payclev before expiry, then choose
**Reset for reconnection** in the Odoo Payclev app. This revokes the previous
bridge key and re-enables **Connect to Payclev**. Complete pairing again and
re-check sync and payment-journal eligibility. Sync and invoice checkout may
be unavailable during the change.

## Optional invoice panel

Version `19.0.1.0.3` adds an Odoo customer-invoice **Payclev** tab. In
**Odoo → Payclev**, an Odoo administrator can enable **Show Payclev on
invoices** and, separately, **Show Payclev activity timeline**. Both switches
are off by default. After enabling one, run an Odoo sync in Payclev; the
paired company-scoped service key delivers read-only collection snapshots.
The tab shows collection status, Payclev payment/write-back status, next step,
last contact and last refresh, with a link to the full Payclev invoice. The
timeline contains at most 20 labeled recent events and no message bodies.
Snapshots are stored separately from `account.move`; they do not change an
Odoo invoice balance, posted payment or invoice `write_date`. If syncing
stops, the old snapshot remains with its last-refresh time. Odoo remains the
accounting source of truth. Disabling a switch hides that feature without
disconnecting Payclev.

The bridge exposes only health, eligible payment methods, payment snapshots,
stable-UUID lookup, and one atomic confirmed-payment application. It does not
provide a generic write API. The payment method enforces company, invoice,
customer, currency, journal, outstanding-account, exact amount and unique
Payclev UUID. A failed method call rolls back the entire Odoo transaction.

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

The addon exposes five financial JSON-2 model methods on `payclev.payment.bridge`:
`payclev_health`, `payclev_payment_methods`, `payclev_find_payment`,
`payclev_payment_snapshot`, and `payclev_apply_confirmed_payment`. The same
model also exposes company-scoped connection-status, payment-setup reporting,
key-renewal and optional `payclev_update_invoice_contexts` methods. The
invoice-context method is limited to paired company customer invoices and
100 snapshots per call; it writes only separate display records. It does not
expose a generic accounting mutation method. Odoo's ordinary JSON-2 API remains
available to the bot according to its normal Odoo access rights, so restrict
its permissions and protect its expiring API key.

The bridge returns Odoo/bridge version, database UUID, bot identity, selected
company/currency, and the count/readiness of enabled payment methods. An
Accounting Manager creates a Payclev payment option for each needed,
already-active Odoo currency and chooses a reconcilable Outstanding Receipts
account. The add-on provisions the managed journal and incoming method. Legacy
bank/cash journals can still be opted in under **Incoming Payments** with
**Allow Payclev payment write-back**, and selected manually in Payclev; they
are never adopted automatically. A bot cannot change the journal opt-in flag.

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
3. Update the Apps list and install **Payclev**. It depends
   on Odoo Accounting and loads a bridge-only group, read-only identity access,
   a company record rule and the journal configuration field.
4. Open **Payclev** in the Odoo app menu as a Settings administrator, review
   the chosen company and click **Connect to Payclev**. Sign into Payclev and
   approve the correct workspace. The app creates a restricted service user;
   verify its effective permissions and single-company scope in the test suite.
5. In Accounting → Configuration → Journals, configure the selected incoming
   bank/cash journal and its manual method's Outstanding Receipts account.
   An Accounting Manager then enables **Allow PayClev payment write-back**.
6. Return to Payclev, enable integration sync if desired, and select the
   verified payment journal. Connecting alone does not enable checkout.

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
