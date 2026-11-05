# Tenancy

Biryar is one codebase in two deployment shapes. A hosted install serves many
selling businesses. A self-hosted install serves one. Nothing in the code
branches on which it is, because a second storage path is a second set of
bugs, and the one that gets less use is the one that breaks.

That decision has a consequence worth stating plainly: **a self-hosted
install runs the same isolation machinery as the hosted one, for a single
tenant who will never benefit from it.** The cost is one extra column and one
policy per table. The benefit is that the hosted product is not a different
program, so a bug fixed in one is fixed in both.

## The question that decides the design

What happens when someone writes a query and forgets to filter by tenant?

Under application-level scoping, the query succeeds and returns every
tenant's rows. The test suite passes, because tests usually have one tenant.
Review does not catch it, because `SELECT * FROM users` looks like every
other line of code. It is found when a customer sees someone else's revenue.

Under row-level security, the same query returns that tenant's rows and
nothing else. The mistake costs nothing.

For a product whose entire value is that the numbers can be trusted, those
two answers are not close, so the boundary lives in PostgreSQL.

## How it works

Every tenant-scoped table carries a `tenant_id` and a policy:

```sql
CREATE POLICY tenant_isolation ON users
    USING       (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK  (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
```

`tenant_session()` sets `app.tenant_id` for the transaction. Everything else
follows from the policy.

Four details do the real work:

**`USING` and `WITH CHECK` both.** `USING` filters reads. `WITH CHECK`
filters writes. With only the first, a caller can insert rows attributed to
another tenant and then be unable to see what they did — worse than either
failure on its own.

**`FORCE ROW LEVEL SECURITY`.** PostgreSQL exempts a table's owner from its
own policies. Migrations run as the owner. Without `FORCE`, any deployment
where the application reuses that role has isolation that silently does
nothing.

**An unprivileged application role.** It owns no tables and holds no
`BYPASSRLS`. `tests/test_tenancy.py` asserts all three, because this is the
kind of property that is true on the day it is written and quietly false
after someone debugs a permissions problem at midnight.

**Transaction-local, never session-wide.** A setting that outlives its
transaction rides the pooled connection into the next request. The engine
rejects both spellings of that mistake — a bare `SET`, and `set_config` with
`false` as its third argument — rather than relying on anyone remembering.

The setting is made with `set_config(name, value, true)` rather than
`SET LOCAL`. `SET` is a utility statement, so PostgreSQL will not plan it and
it cannot take a bind parameter: `SET LOCAL app.tenant_id = $1` is a syntax
error. Interpolating the uuid into the string would work, and would also be
the only place in the codebase assembling SQL by concatenation.

## Default-deny

`current_setting('app.tenant_id', true)` returns NULL when nothing has set
it, and NULL never equals a uuid. A connection with no tenant in scope
therefore sees no rows at all.

The `NULLIF(..., '')` is load-bearing. A transaction-local setting is
restored at commit to whatever it was beforehand, and for a custom GUC the
session never set, that previous value is the **empty string**, not NULL. On
a pooled connection that has already served one tenant transaction, the next
query with no tenant therefore reads `''`, and `''::uuid` raises rather than
comparing. Isolation holds either way — nothing leaks — but a query that
should quietly return nothing would instead fail with a cast error that says
nothing about tenancy, which is a bad afternoon for whoever hits it.

This is why `admin_session()` cannot read tenant data even though it is
"admin". Provisioning a tenant, listing tenants, and running migrations have
no business reading anyone's sales. If a query through an admin session
returns nothing where you expected rows, the table is tenant-scoped and you
wanted `tenant_session()`.

## Adding a tenant-scoped table

1. Inherit `TenantScoped` and declare `tenant_id: Mapped[uuid.UUID] = tenant_fk()`.
2. Import the model in `biryar/db/models/__init__.py`, or nothing will see it.
3. Write the migration, including `ENABLE`/`FORCE ROW LEVEL SECURITY` and the
   policy.

Step 3 is the one that gets forgotten, so it is not left to memory:
`test_every_tenant_scoped_model_has_a_live_policy` compares the live policies
against every model marked `TenantScoped` and fails if a table was added
without one.

Migrations freeze their own table lists rather than reading the models. A
migration is a description of one step in history; if it read current state
it would change meaning every time a model changed, and replaying history
would stop reproducing it.

## Why not a schema or a database per tenant

Both isolate better. Both make a thousand tenants a thousand schemas to
migrate, and turn every cross-tenant question — platform metrics, a support
query, a bug affecting a subset — into a loop over connections. Row-level
security gives one schema, one migration, one connection pool, and an
isolation guarantee the database enforces rather than the application.

The trade is that a policy bug affects everyone at once. That is why the
tests in `test_tenancy.py` are the ones that must never be allowed to rot.
