# Recent operation history

Projects and Shared pool expose an Operation log beside their existing views.
The log identifies the operation's actual actor, time, affected record and
changed values. A project includes its expenses, settings and recurring rules;
Shared pool includes its expenses and recurring rules. Viewers can read the
history within their current ledger and target permissions.

The interval is a rolling 24 hours, enforced using Server time. Results sort
newest first and use bounded cursor pagination. Reload is an explicit action;
the browser does not poll. Time is displayed using the user's local timezone.
Opening history does not inherit expense date or category filters.

`GET /api/v1/activity` selects `target=shared`, or `target=project` with a
`projectId`. The default page has 50 entries, with a maximum of 100. An opaque
cursor preserves the first page's upper time boundary, while every page excludes
records older than the current Server time minus 24 hours.

Successful mutations publish immutable actor and record snapshots in their
original guarded transaction. Failed authorization, revision conflicts and
idempotent replays publish no extra activity. An API key is identified as an API
key, and scheduled work as system execution; neither claims that a person
manually performed the operation. Activity reads do not expose credentials,
private authentication state or protected GitHub provider metadata.

The recent projection starts with this release. Existing financial/audit history
is preserved; it is not rewritten to invent past setting or rule changes.
Expired activity is excluded immediately on reads and retired in bounded groups
through existing mutation and hourly maintenance paths. GET requests perform no
cleanup writes, and no additional schedule is introduced.

Schema 0009 extends the existing database. Preview publication uses the original
coordinator namespace, singleton journal and cumulative accounting after native
preservation, rollback and workflow checks. Production remains paused.
