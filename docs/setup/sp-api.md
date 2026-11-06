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

There are two ways an application gets permission from a seller, and they
have very different limits.

**Self-authorisation** is the short path. You authorise your own application
against your own account from inside Seller Central, and Amazon hands you a
refresh token. It is meant for private applications — an application your
organisation built to manage its own business. It is capped: Amazon's docs
state a *"maximum of 10 authorizations per application."* [[1]](#sources)

**The website (OAuth) workflow** is the long path. Your application sends a
seller to a consent page on Amazon, the seller approves, and Amazon sends
them back to your application with a code it exchanges for a refresh token.
This is what a hosted service has to use, because the 10-authorisation cap
would stop the eleventh customer.

**Biryar implements the website workflow for both**, which is why the
self-hosted install asks you to register a redirect URI even though you are
the only seller who will ever use it. One code path, exercised by everyone,
is worth more than a shortcut that only the author's install ever runs.

## What you end up with

Four values, which are properties of the **application** — not of you as a
seller:

| Setting | What it is |
|---|---|
| `BIRYAR_AMAZON_APPLICATION_ID` | Public. It identifies your application in the consent URL |
| `BIRYAR_LWA_CLIENT_ID` | The Login with Amazon client id |
| `BIRYAR_LWA_CLIENT_SECRET` | The matching secret. This one is a credential |
| `BIRYAR_AMAZON_REDIRECT_URI` | Where Amazon sends a seller back, e.g. `http://localhost:8000/v1/amazon/callback` |

Your **refresh token is not in that list, and you never paste one.** It is
produced per seller by the authorisation round trip, sealed with your
`BIRYAR_SECRET_KEY` before it is stored, and never shown again — not in the UI,
not in a log, not in an API response. Your selling-partner id arrives the
same way. If a guide or a screenshot anywhere tells you to copy a refresh
token into a configuration file, it is describing an older design.

## Step 1 — register as a developer

Amazon has to know who is building the application before it will let you
create one.

Sign in to Seller Central, open the main menu, and go to **Apps and
Services → Develop Apps**. [[2]](#sources)

**If that menu is not there**, your account has been migrated to the
**Solution Provider Portal**. Sign in to the portal instead and go to
**Settings → Developer Profile**. [[2]](#sources) This is the single most
common reason people get stuck on step 1: the path in most tutorials, and
the path in older Amazon documentation, is the pre-migration one.

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

Once you are a registered developer, create an application and give it:

- **The Finance and Accounting role.** Just that one. `listTransactions`
  accepts it and requires at least one role from its list; calling without
  it returns `403`. [[3]](#sources)
- **An OAuth redirect URI** exactly equal to what Biryar will send. For a
  local install that is `http://localhost:8000/v1/amazon/callback`; for a
  server, the same path on your own domain over HTTPS. It has to match
  character for character, including the scheme and any trailing slash. When
  it does not match, Amazon refuses the exchange with a message that does
  not tell you which end is wrong — so check this one twice. It is the most
  expensive typo available on this page.

Leave the application in **Draft**. There is no reason to publish a private
application. [[1]](#sources)

Copy the application id, the LWA client id and the LWA client secret into
your `.env` (see [install.md](install.md)), and because the application is
in Draft, leave:

```
BIRYAR_AMAZON_APPLICATION_IS_DRAFT=true
```

That flag adds `version=beta` to the consent URL, which Amazon **requires**
while an application is in Draft and **rejects** once it is published.
[[4]](#sources) It is a setting rather than a constant for exactly that
reason: if you ever publish the application, flip it to `false` in the same
change.

`version=beta` also has a second effect worth knowing: a Draft application
and its published version can carry different role sets, and only
`version=beta` picks up the roles you most recently added. [[4]](#sources)
So if you add a role later and the new permission does not seem to take
effect, this flag is the first thing to check.

## Step 3 — authorise

Start Biryar, sign in, and use **Connect Amazon**. Biryar builds the consent
URL, sends you to Seller Central, you approve, and Amazon returns you to the
redirect URI with the authorisation code. Biryar exchanges it for a refresh
token, seals it, and stores it.

Three things about that round trip are worth knowing because they explain
most of its failures:

- **The code is short-lived.** Amazon's authorisation code is good for about
  five minutes. Biryar's `state` lasts ten, deliberately longer, so that an
  expired flow reports an expired *code* rather than a mysterious state
  mismatch.
- **`state` is signed, not stored.** It is bound to your tenant and to a
  ten-minute window, so a link someone else hands you cannot complete an
  authorisation into your account, and a used link cannot be replayed later.
- **The region is derived, not configured.** A refresh token works against
  one regional endpoint only. Biryar picks the host from the marketplace you
  chose, because sending a European seller's token to the North American
  host fails in a way that reads exactly like a bad credential.

After that, the settlement fetch is a queued job. The first run backfills;
later runs re-fetch an overlapping window, which is free because rows are
keyed on Amazon's own transaction id.

## When it goes wrong

| What you see | What it means |
|---|---|
| `MD1000` | A Draft application was authorised through the production OAuth flow. Set `BIRYAR_AMAZON_APPLICATION_IS_DRAFT=true` so the consent URL carries `version=beta`. [[4]](#sources) |
| `403` from the Finances call | The application does not have the Finance and Accounting role, or it has it in the published version but not the Draft one. [[3]](#sources)[[4]](#sources) |
| Amazon refuses the exchange, vaguely | The redirect URI Biryar sent is not character-for-character one of the registered ones. |
| `SPSA2043` | A **vendor** account whose group lacks the `SPDS-VC-MYA` role, which is required to reach the self-authorisation page. Your vendor manager has to add it. [[1]](#sources) |
| Biryar says the authorisation needs renewing | The refresh token was revoked — a password change or a withdrawn authorisation on Amazon's side. Biryar distinguishes this from a bad day at Amazon and stops retrying instead of hammering the endpoint. Re-run **Connect Amazon**. |

## Rotating and revoking

You can withdraw the authorisation from Seller Central at any time; Biryar's
next job marks the connection as needing re-authorisation and stops. Nothing
in Biryar can prevent you revoking it, which is the property you want from
software that holds a key to your finances.

Rotating the **sealing** key is separate and does not involve Amazon:
`BIRYAR_SECRET_KEY` takes a comma-separated list, newest first, so you add a
key and let stored tokens re-seal as they are read, rather than in one
transaction that rewrites every row.

## Still missing from this page

**Screenshots.** The console changes often enough that prose alone goes
stale, and this guide would be better with a picture of every screen. They
have to come from an account created for the purpose: the maintainer's own
developer console contains their business, and this is a public repository.
Until then, the words above are written to be followable without pictures,
and the error table is the fallback when a screen does not match.

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
