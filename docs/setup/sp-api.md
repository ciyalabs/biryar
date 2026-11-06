# Connecting Biryar to your Amazon account

Biryar reads your Amazon financial transactions. To do that it needs an
Amazon *application* — an identity Amazon recognises — and your permission,
as a seller, for that application to read your account.

This guide is the step most likely to lose someone, so it is written to be
read in order, and it says what goes wrong as well as what to do.

> Every Amazon-side detail below is cited at the bottom. Amazon's developer
> console is renamed and reorganised more often than its documentation is
> updated, so where this guide and the screen in front of you disagree,
> the screen is right — and please open an issue so this page gets fixed.

## What Biryar actually asks for

Less than you probably expect. Release 0.1 calls exactly one Amazon
operation:

| | |
|---|---|
| Operation | `listTransactions`, Finances API `v2024-06-19` |
| Role needed | **Finance and Accounting** — and nothing else |
| Restricted-data (PII) approval | **Not needed** |
| Write access | **None.** Biryar cannot change a price, a listing or an order |

That matters for two reasons. Finance and Accounting is a standard role, not
one of the restricted roles that gate buyer names and shipping addresses, so
you are not applying for access to personal data and not waiting on a
separate approval for it. And because Biryar asks for no write role, no
mistake in it — or in this guide — can alter anything in your Amazon
account. The worst case is that it reads nothing.

If a later release needs another role, it will say so and ask again. It
cannot quietly widen what it already has: the roles are fixed on the
application, and adding one requires you to re-authorise.

## The two routes, and which one you are on

There are two ways an application gets permission from a seller. **Which one
is available to you is decided by what kind of application you have, not by
what you would prefer**, and the guide below was checked against a live
German seller account in November 2026 rather than taken from the
documentation.

**Self-authorisation** is the route for a **private application** — one your
organisation built to manage its own business. You authorise your own
application from the Solution Provider Portal and Amazon hands you a refresh
token. Amazon caps it at a *"maximum of 10 authorizations per
application."* [[1]](#sources)

**The website (OAuth) workflow** is the route for an application that other
sellers use. Your application sends a seller to a consent page, they
approve, and your application exchanges a code for a refresh token.

**If you are self-hosting Biryar, you are on the first route, and the second
one is not merely unnecessary — it does not work.** A private application in
Draft has no OAuth configuration to speak of: the registration form has no
redirect URI field anywhere on it, and the portal says so plainly.

![The portal's authorisations dialog: ten self-authorisations, and
OAuthorizations "No authorization allowed"](img/authorizations-remaining.jpg)

Sending a seller to the consent URL for such an application answers this,
with or without `version=beta`:

![Seller Central answering MD9100, "this app does not have the right
application details"](img/consent-md9100.jpg)

So Biryar accepts a self-authorisation token directly. Both routes end in
the same sealed row, so nothing downstream behaves differently.

## What you end up with

**Self-hosting.** Two values, and you get them in step 3. Neither goes in a
configuration file — you paste them into Biryar once and it seals them:

| Value | What it is |
|---|---|
| Merchant token | Your selling-partner id, shown with the authorisation |
| Refresh token | The `Atzr|...` string self-authorisation produces |

Biryar seals the refresh token with your `BIRYAR_SECRET_KEY` before storing
it and never shows it again — not in the interface, not in a log, not in an
API response. There is a test that holds it to that.

**Hosting Biryar for other sellers.** Then you need an application others
can authorise, and four settings describe it:

| Setting | What it is |
|---|---|
| `BIRYAR_AMAZON_APPLICATION_ID` | Public. It identifies your application in the consent URL |
| `BIRYAR_LWA_CLIENT_ID` | The Login with Amazon client id |
| `BIRYAR_LWA_CLIENT_SECRET` | The matching secret. This one is a credential |
| `BIRYAR_AMAZON_REDIRECT_URI` | Where Amazon sends a seller back |

Leave all four empty for a self-hosted install. Biryar then says the OAuth
button is unavailable instead of offering a flow that cannot complete.

## Step 1 — register as a developer

Amazon has to know who is building the application before it will let you
create one.

Amazon's documentation says to sign in to Seller Central, open the main
menu, and go to **Apps and Services → Develop Apps**, and to use the
**Solution Provider Portal** instead if your account has been migrated to
it. [[2]](#sources)

**On a current account, expect the second path.** Checked against a live
German seller account in November 2026: there is no "Apps and Services" in
the menu at all — the section is called **Programs and apps**, and it
contains Explore programmes, Selling partner appstore and Explore services.
No Develop Apps. Going to the old Developer Central URL
(`/sellingpartner/developerconsole`) does not 404 either; it shows this and
then forwards you:

![Seller Central announcing that Developer Central now forwards to the
Solution Provider Portal](img/developer-console-redirect.jpg)

So the practical route is: **go to
<https://solutionproviderportal.amazon.com> and sign in there.** It is a
separate sign-in even though it is the same Amazon account, and if you have
more than one selling account it asks which to use first.

Once you are through, the portal's front page is the whole working area —
your developer profile's status at the top, and your applications below it:

![The Solution Provider Portal front page, with the developer profile
reviewed and one application in Draft](img/solution-provider-portal-apps.jpg)

If you have never registered, this is where you complete the developer
profile first. [[2]](#sources)

This is the single most common reason people get stuck on step 1, and it is
worth knowing that the tutorials are not wrong so much as old: they describe
the menu as it was before the migration.

The registration form asks for contact information, data access details,
your use cases, and your security controls, and ends with a checkbox
accepting the Solution Provider Portal Agreement, the Acceptable Use Policy
and the Data Protection Policy. [[2]](#sources)

In the **Data Access** section, choose the option for an organisation that
sells on Amazon and only wants to integrate to manage its own business.
Amazon's docs give the wording as *"My organization sells on Amazon, and I
only want to integrate to manage my own business only."* [[2]](#sources)
Choosing a public/third-party option here puts you on a much longer review
path for no benefit.

Then wait. Amazon reviews the request and sends updates to the admin email
on the account. **If Amazon asks you for more information, answer within
five days or the case is closed** and you start again. [[2]](#sources)

This step is the one with a human on the other end, and the one that can be
rejected. Nothing in Biryar can shorten it.

## Step 2 — create the application

In the portal, **Add new app client**. The whole form is four questions:

![The app registration form: name, API type, business entities and
roles](img/app-roles.jpg)

- **App name** — anything; it is shown to you, not to buyers.
- **API Type** — SP API.
- **Business entities** — Sellers.
- **Roles** — **Finance and Accounting**, and for Biryar 0.1 nothing else.
  `listTransactions` requires at least one role from its list and accepts
  that one; calling without it returns `403`. [[3]](#sources)

> The screenshot is from an account whose application also drives a
> repricer, so it has Pricing, Inventory, Fulfilment and Listing ticked too.
> You do not need them. Every extra role widens what a leaked credential
> reaches, and Biryar 0.1 calls exactly one operation.

Answer **no** to delegating PII access to another developer's application.

Note what is *not* on this form: there is no OAuth redirect URI field, no
login URI, no OAuth section at all. That is not an oversight in this guide —
it is why the consent flow cannot work for a private application, and why
step 3 does not use it.

Leave the application in **Draft**. There is no reason to publish a private
application [[1]](#sources), and publishing is a review process you do not
need.

## Step 3 — authorise, and bring the token to Biryar

In the portal, open the app's **Action** menu. For a private application it
has exactly one entry:

![The app row's action menu, offering only Authorize](img/app-actions-authorize.jpg)

Choose **Authorize**. Amazon authorises the application against your own
account and shows you a **refresh token** — a long string beginning
`Atzr|`. It is shown once.

> Each of these uses one of ten. The dialog in the earlier screenshot is
> where you check how many you have left, and re-authorising to get a fresh
> token spends another. There is no way to raise the limit, so do not spend
> them exploring.

Then, in Biryar: sign in, open **Self-hosting? Paste a refresh token
instead** under the Amazon card, and enter your merchant token and the
refresh token. Biryar seals it immediately. You will not see it again, and
nor will anyone reading your logs.

Two things worth knowing, because they explain most later confusion:

- **The region is derived, not configured.** A refresh token works against
  one regional endpoint only. Biryar picks the host from the marketplace,
  because sending a European seller's token to the North American host
  fails in a way that reads exactly like a bad credential.
- **Revoking is yours.** Withdraw the authorisation in the portal and
  Biryar's next job marks the connection as needing re-authorisation and
  stops, rather than retrying against a dead token.

After that the settlement fetch is a queued job. The first run backfills;
later runs re-fetch an overlapping window, which costs nothing because rows
are keyed on Amazon's own transaction id.

## When it goes wrong

| What you see | What it means |
|---|---|
| `MD9100` | You sent a seller to the consent URL for a **private** application. It has no OAuth configuration, so there is nothing to fix on the application — use self-authorisation instead (step 3). Verified on a live account; `version=beta` does not help. |
| `MD1000` | A Draft application authorised through the production OAuth flow, on an application that *does* have OAuth configured. Set `BIRYAR_AMAZON_APPLICATION_IS_DRAFT=true` so the consent URL carries `version=beta`. [[4]](#sources) |
| `403` from the Finances call | The application does not have the Finance and Accounting role, or it has it in the published version but not the Draft one. [[3]](#sources)[[4]](#sources) |
| Amazon refuses the exchange, vaguely | The redirect URI Biryar sent is not character-for-character one of the registered ones. |
| `SPSA2043` | A **vendor** account whose group lacks the `SPDS-VC-MYA` role, which is required to reach the self-authorisation page. Your vendor manager has to add it. [[1]](#sources) |
| Biryar says the authorisation needs renewing | The refresh token was revoked — a password change, or an authorisation withdrawn in the portal. Biryar tells this apart from a bad day at Amazon and stops retrying rather than hammering the endpoint. Authorise again and paste the new token. |
| The portal shows fewer authorisations than you expected | Each self-authorisation spends one of ten, including the ones you did while finding your way around. The count is in the Authorizations remaining dialog. |

## Rotating and revoking

You can withdraw the authorisation from Seller Central at any time; Biryar's
next job marks the connection as needing re-authorisation and stops. Nothing
in Biryar can prevent you revoking it, which is the property you want from
software that holds a key to your finances.

Rotating the **sealing** key is separate and does not involve Amazon:
`BIRYAR_SECRET_KEY` takes a comma-separated list, newest first, so you add a
key and let stored tokens re-seal as they are read, rather than in one
transaction that rewrites every row.

## About the screenshots

They are from a real German seller account, taken in November 2026. Account
names are blurred and the application id is replaced with a placeholder of
the same shape; both were done in the page before the image was captured,
so no unblurred copy exists anywhere.

Amazon reorganises this area often, so treat them as evidence that a screen
existed in this shape on that date rather than as a promise about what you
will see. Where they and your screen disagree, your screen is right — and
an issue saying so is welcome, because that is the only way this page stays
true.

## Still missing from this page

**A published-application walkthrough.** Everything above is the private
application path. Publishing — which a hosted deployment needs, and which
is what makes the OAuth flow available — is a review process this guide has
not been through and will not describe from documentation alone.

**A non-EU/NA marketplace walkthrough.** Biryar's regional hosts cover NA
and EU today. A seller in `FE` would need the endpoint and timezone maps
extended; the structure is there and it is a small change.

## Sources

Amazon's own documentation, which is canonical and which this page may lag:

1. [Self authorization](https://developer-docs.amazon.com/sp-api/docs/self-authorization) — private applications, the 10-authorisation cap, `SPSA2043`
2. [Register as a Private SP-API Developer](https://developer-docs.amazon.com/sp-api/docs/register-as-a-private-developer) — the registration path, the Solution Provider Portal alternative, the Data Access wording, the five-day reply window
3. [Role Mappings for SP-API Operations](https://developer-docs.amazon.com/sp-api/docs/role-mappings) and [Selling Partner API Roles](https://developer-docs.amazon.com/sp-api/docs/finance-and-accounting-role) — Finance and Accounting for `listTransactions`, and `403` without it
4. [Website Authorization Workflow](https://developer-docs.amazon.com/sp-api/docs/website-authorization-workflow) and [Resolve Common HTTP and Authorization Error Codes](https://developer-docs.amazon.com/sp-api-blog/docs/troubleshooting-selling-partner-api-authorization-errors) — `version=beta` in Draft, Draft vs published role sets, `MD1000`

For anything this page does not answer, the Solution Provider Portal
Agreement and the SP-API Developer Agreement are the authoritative sources,
not this repository.
