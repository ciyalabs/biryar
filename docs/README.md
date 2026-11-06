# Biryar documentation

| Guide | What it covers |
|---|---|
| [Install](setup/install.md) | Getting Biryar running on your own machine |
| [SP-API keys](setup/sp-api.md) | Registering your own Amazon application and authorising it against your seller account |
| [Keepa](setup/keepa.md) | Not needed for release 0.1. Covered because later releases use it |
| [How the fees are measured](methodology/fees.md) | Why the real cut is not the one on the fee schedule |
| [How tenants are kept apart](architecture/tenancy.md) | Why isolation is the database's job, not the query author's |

These are written for a seller, not a developer. Where a step is genuinely
technical, it is spelled out rather than assumed.

**These files are the source for the pages on
[biryar.ciyalabs.com](https://biryar.ciyalabs.com).** That site generates
them; it does not keep its own copies, and its deploy fails if they have
fallen behind. So edit the document here — a correction made on the website
would be overwritten, and a correction made only here still reaches readers.

Links between these documents are rewritten to site URLs during that build,
and a link to a file that is not a page becomes a link to this repository.
Both work in a checkout and on the web, so write them as ordinary relative
markdown links.
