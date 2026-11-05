# Getting your own SP-API keys

**Status: outline. This guide is the single biggest determinant of whether
anyone successfully installs Biryar, and it needs to be written
screenshot-by-screenshot before release.**

Biryar never holds your Amazon credentials. You register an application
against your own seller account and keep the keys on your own machine. This
is the step that makes the rest of the privacy promise true, and it is also
the step most likely to lose a user, so it gets the most careful writing in
this repository.

## What you end up with

Five values, which go into Biryar's setup screen and nowhere else:

| Value | Where it comes from |
|---|---|
| `LWA_APP_ID` | Your application, in Seller Central's developer console |
| `LWA_CLIENT_SECRET` | The same application |
| `SP_API_REFRESH_TOKEN` | Produced when you authorise the application against your own seller account |
| `SP_API_SELLER_ID` | Your merchant token |
| `SP_API_MARKETPLACE_ID` | The marketplace you sell in |

## The route, in outline

1. Register as a developer in Seller Central, under Apps and Services →
   Develop Apps. You are registering a **private** application for your own
   account, not a public one for distribution, which is a far shorter path.
2. Create the application and request **read-only** roles. Biryar 0.1 asks
   for nothing that writes.
3. Self-authorise it against your own seller account. This produces the
   refresh token.
4. Paste the five values into Biryar's setup screen.

## To be written

- Screenshots of every screen in the developer console, which changes often
  enough that prose alone goes stale.
- The exact set of roles to request, and what each one unlocks in Biryar.
- What to do when the developer profile is rejected, which happens.
- Whether a seller on a non-EU marketplace needs anything different.
- How to rotate the refresh token, and what breaks while it is rotating.
