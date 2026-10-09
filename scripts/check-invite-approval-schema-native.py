"""Four-request local native proof for the populated v7-to-v8 invitation-approval extension.

The generated fixture packages canonical modules without changing them. It has
two separate local D1 bindings and one local SQLite Durable Object namespace;
neither provider transports nor remote bindings exist. The second D1 tests a
deliberately failed atomic batch. Raw native attempts, including missing fields,
are preserved. This is migration evidence, not authenticated API acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
HTTP_PATHS = ("setup", "upgrade", "restart", "rollback")

ENTRY = r'''
import hashlib
import json
import time
from workers import WorkerEntrypoint, DurableObject, Response
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError, _field
from pullwise_server.cloudflare_preview_budget import upgrade_product_schema_v8
from pullwise_server.cloudflare_preview_schema import (
    V7_SCHEMA_SQL, V7_SCHEMA_OBJECTS, V7_SCHEMA_FINGERPRINT, V7_INDEX_COUNTS,
    SCHEMA_OBJECTS, SCHEMA_FINGERPRINT, SCHEMA_VERSION, UPGRADE_V8_SQL,
)
from pullwise_server.cloudflare_state_records import STATE_KINDS, record_name, encode_record

OWNER, MEMBER = "usr_github_740001", "usr_github_740002"
OLD_UPGRADE = {"from":4,"to":5,"request":23,"complete":True,
    "write_execution":{"native_attempts":[None]*10,
        "provenance":"d1-nonretryable-write-contract-v1"}}
OLD_V6_UPGRADE = {"from":5,"to":6,"request":63,"complete":True,
    "write_execution":{"native_attempts":[None]*9,
        "provenance":"d1-atomic-write-batch-with-fk-pragma-v1"}}
OLD_V7_UPGRADE = {"from":6,"to":7,"request":90,"complete":True,
    "write_execution":{"native_attempts":[None]*13,
        "provenance":"d1-nonretryable-write-contract-v1"}}
OLD_CUTOVER = {"version":1,"request":41,"complete":True,"copied_records":7,
    "write_execution":{"provenance":"local-synthetic-historical-record-cutover"}}
COUNTERS = ("requests","reserved_read","reserved_written","actual_read","actual_written")


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),
        ensure_ascii=False).encode()).hexdigest()


async def save_fixture(storage, key, value):
    # Use plain JSON across the Python/JavaScript DO-storage boundary. Fixture
    # snapshots contain numeric proofs, digests and schema text, never secrets.
    await storage.put(key,json.dumps(value,separators=(",",":")))


async def load_fixture(storage, key):
    return json.loads(await storage.get(key))


class ObservedStatement:
    def __init__(self, owner, sql, native):
        self.owner, self.sql, self.native = owner, sql, native
    def bind(self, *values):
        return ObservedStatement(self.owner,self.sql,self.native.bind(*values))


class ObservedD1(NativeD1):
    def __init__(self, binding, label, *, inject=False):
        super().__init__(binding)
        self.label, self.inject = label, inject
        self.groups, self.dispatches, self.failed = [], 0, []
    def prepare(self, sql):
        return ObservedStatement(self,sql,super().prepare(sql))
    async def batch(self, statements):
        statements = list(statements)
        assert statements and len(statements)<=64
        assert all(item.owner is self for item in statements)
        native = [item.native for item in statements]
        injected = self.inject and tuple(item.sql for item in statements)==UPGRADE_V8_SQL
        if injected:
            position = next(index for index,item in enumerate(statements)
                if item.sql.strip().upper().startswith("DROP TABLE WORKSPACE_INVITES"))
            # Only this isolated rollback fixture inserts the failure. The
            # canonical migration and every copied module remain unchanged.
            native.insert(position+1,super().prepare("INSERT INTO d1_command_guard(ok) VALUES(0)"))
        self.dispatches += 1
        try:
            results = await super().batch(native)
        except BaseException:
            failure = {"binding":self.label,"statements":len(native),
                "injectedAfterInviteDrop":injected,"nativeResultsUnavailable":True}
            self.failed.append(failure)
            print(json.dumps({"nativeInviteApprovalSchemaFailure":failure}))
            raise
        values = [{"rowsRead":_field(_field(item,"meta"),"rows_read"),
            "rowsWritten":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in results]
        assert all(type(item[key]) is int and item[key]>=0 for item in values
            for key in ("rowsRead","rowsWritten"))
        group = {"binding":self.label,"statements":len(values),
            "rowsRead":sum(item["rowsRead"] for item in values),
            "rowsWritten":sum(item["rowsWritten"] for item in values),
            "nativeAttempts":[item["attempts"] for item in values]}
        self.groups.append(group)
        print(json.dumps({"nativeInviteApprovalSchemaMeta":group}))
        return results


async def execute(native, commands):
    result = []
    for start in range(0,len(commands),64):
        result.extend(await native.batch([native.prepare(sql).bind(*values)
            for sql,values in commands[start:start+64]]))
    return result


async def rejected_probe(native, sql, values):
    before = native.dispatches
    try:
        await execute(native,[(sql,values)])
    except BaseException:
        assert native.dispatches==before+1
        assert native.failed[-1]["nativeResultsUnavailable"] is True
    else:
        raise AssertionError("Invalid local integrity probe unexpectedly committed")


def seed_commands():
    commands = []
    def add(sql,*values):
        commands.append((sql,values))
    projects = (("prj_live",101,"org/live","active",7,"Live project",77),
        ("prj_archived",202,"org/archive","archived",11,"Archived project",77),
        ("prj_other",303,"user/other","active",3,"",None))
    for identity,repo,full,status,revision,name,org in projects:
        add("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,
            description,status,revision,created_at,updated_at,name,github_organization_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",identity,OWNER,repo,full,"保留财务历史🙂 "+identity,
            status,revision,"2025-11-01T00:00:00Z","2026-10-01T01:02:03Z",name,org)
    for project,repo,full in (("prj_live",101,"org/live"),("prj_live",104,"org/linked"),
            ("prj_archived",202,"org/archive"),("prj_other",303,"user/other")):
        add("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
            github_full_name,installation_id,github_account_id,github_account_login,
            github_account_type,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            OWNER,project,repo,full,501,77 if full.startswith("org/") else 740001,
            full.split("/")[0],"Organization" if full.startswith("org/") else "User","2025-11-01")
    for identity,name,archived in (("cat_live","Hosting",None),("cat_archived","Old tools","2026-09-01")):
        add("""INSERT INTO expense_categories(id,owner_id,name,color,archived_at,revision,
            created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",identity,OWNER,name,"blue",archived,5,"old","updated")
    expenses = (("exp_live","project","prj_live","cat_live",1250,None),
        ("exp_deleted","project","prj_live","cat_live",2500,"2026-10-01"),
        ("exp_archived","project","prj_archived","cat_archived",799,None),
        ("exp_shared","shared",None,"cat_live",300,None))
    for identity,kind,project,category,amount,deleted in expenses:
        add("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,note,quantity_decimal,unit,revision,created_at,
            updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            identity,OWNER,kind,project,category,"2026-09-30",amount,"USD","Historical expense "+identity,
            "财务备注🙂","1.5","month",9,"created","updated",deleted)
    for identity,expense,action in (("evt_create","exp_live","create"),("evt_delete","exp_deleted","delete")):
        add("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,action,
            before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            identity,expense,OWNER,"session",OWNER,action,'{"retained":"before"}',
            '{"retained":"after"}',"2026-10-01")
    for identity,expense in (("idem_live","exp_live"),("idem_deleted","exp_deleted")):
        add("""INSERT INTO expense_create_idempotency(owner_id,idempotency_key,request_sha256,
            expense_id,response_json,created_at) VALUES(?,?,?,?,?,?)""",OWNER,identity,"a"*64,
            expense,json.dumps({"id":expense,"projectId":"prj_live","revision":9}),"old")
    add("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,joined_at,
        updated_at,invited_by_user_id) VALUES(?,?,?,?,?,?,?)""",OWNER,MEMBER,"editor",6,"joined","updated",OWNER)
    for identity,recipient,login,token,role,status,accepted,accepted_at in (
        ("inv_pending",740003,"local-third","b","viewer","pending",None,None),
        ("inv_accepted",740002,"local-member","e","editor","accepted",MEMBER,"2026-09-30"),
        ("inv_revoked",740004,"local-fourth","f","admin","revoked",None,None)):
        add("""INSERT INTO workspace_invites(id,workspace_id,github_recipient_id,github_login,
            token_hash,role,status,revision,expires_at,created_by_user_id,created_by_revision,
            created_at,updated_at,accepted_by_user_id,accepted_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",identity,OWNER,recipient,
            login,token*64,role,status,4,2000000000,OWNER,1,"created","updated",accepted,accepted_at)
    for identity,action,subject,actor,before,after in (
        ("ws_invite","invite","inv_pending",OWNER,None,'{"role":"viewer"}'),
        ("ws_accept","accept_invite","inv_accepted",MEMBER,'{"status":"pending"}','{"status":"accepted"}'),
        ("ws_revoke","revoke_invite","inv_revoked",OWNER,'{"status":"pending"}','{"status":"revoked"}')):
        add("""INSERT INTO workspace_events(id,workspace_id,actor_user_id,action,subject_id,
            before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",identity,OWNER,actor,
            action,subject,before,after,"2026-10-01")
    add("UPDATE ledger_projects SET development_url=?,product_url=? WHERE id=?",
        "https://github.com/local/preserved","https://example.com/product?version=7","prj_live")
    add("UPDATE ledger_projects SET product_url=? WHERE id=?",
        "https://example.com/archived","prj_archived")
    for identity,target,project,status,next_run,next_on,next_period,blocked in (
        ("rule_active","project","prj_live","active",1790812800,"2026-10-01","2026-10",None),
        ("rule_paused","shared",None,"paused",None,None,None,None)):
        add("""INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,target_kind,
            project_id,template_json,schedule_json,status,revision,next_run_at,next_occurrence_on,
            next_period_key,blocked_code,created_at,updated_at,create_key,create_sha256,create_response_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",identity,OWNER,MEMBER,target,project,
            '{"amountMinor":1250,"currency":"USD","note":"保留🙂"}',
            '{"frequency":"monthly","timezone":"Asia/Shanghai","anchorDay":31}',status,7,
            next_run,next_on,next_period,blocked,"created","updated",identity,"9"*64,
            json.dumps({"id":identity,"revision":7},separators=(",",":")))
    for rule,period,expense in (("rule_active","2026-09","exp_live"),
            ("rule_paused","2026-09","exp_deleted")):
        add("""INSERT INTO expense_recurring_occurrences(rule_id,owner_id,period_key,
            scheduled_on,expense_id,rule_revision,created_at) VALUES(?,?,?,?,?,?,?)""",
            rule,OWNER,period,"2026-09-30",expense,6,"created")
    add("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,action,
        before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        "evt_schedule","exp_shared",OWNER,"schedule",MEMBER,"create",None,
        '{"retained":"scheduled fact"}',"2026-10-01")
    add("""INSERT INTO ledger_plan_usage(owner_id,projects,records,month,writes,minute,
        minute_writes,jev_reserved_microusd,project_cap,record_cap,minute_cap,month_cap,
        jev_cap,project_delta,record_delta,jev_delta,previous_month,previous_minute)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",OWNER,3,4,"2026-10",17,1,2,0,100,20000,60,10000,0,0,0,0,"2026-10",1)
    for owner,revision in ((OWNER,8),(MEMBER,3)):
        add("""INSERT INTO account_entitlement_authority(owner_id,revision,plan,period,
            period_start,valid_until,dirty) VALUES(?,?,?,?,?,?,?)""",owner,revision,"free","2026-10",1,2000000000,0)
    add("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,scopes,expires_at,
        restrictions,created_at,last_used_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        "key_history",OWNER,"Historical restricted key","local","c"*64,'["projects:read"]',
        None,'{"projectIds":["prj_live"]}',1,2,None)
    add("INSERT INTO billing_webhook_receipts VALUES(?,?,?,?,?)","billing_history","d"*64,
        '{"retained":true}',3,"applied")
    add("INSERT INTO billing_public_catalog VALUES(?,?,?,?,?)",1,'{"retained":true}',2000000000,12,3)
    add("INSERT INTO expense_suggestion_budget VALUES(?,?,?)",OWNER,"2026-10-01",2)
    add("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,question_version,
        draft_target_kind,draft_project_id,outcome) VALUES(?,?,?,?,?,?,?)""",
        "suggestion_history",OWNER,"old","v1","project","prj_live","unavailable")
    records = (("users",OWNER,{"id":OWNER,"githubId":"740001","billing":{"plan":"free"},"retained":{"memo":"🙂"}}),
        ("users",MEMBER,{"id":MEMBER,"githubId":"740002","billing":{"plan":"free"}}),
        ("sessions","local-session-owner",{"id":"local-session-owner","userId":OWNER,"expiresAt":2000000000}),
        ("sessions","local-session-member",{"id":"local-session-member","userId":MEMBER,"expiresAt":2000000000}),
        ("githubStates","local-state",{"kind":"login","expiresAt":2000000000,"retained":True}),
        ("billingEvents","local-billing",{"retained":True,"applied":True}),
        ("billingPendingUpdates","local-pending",{"eventId":"local-pending","retained":True}))
    for kind,identity,value in records:
        add("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",record_name(kind,identity),encode_record(kind,identity,value),17)
    return commands


async def snapshot(native):
    tables = sorted(V7_INDEX_COUNTS)
    # All v7 columns are retained, including optional URLs and recurring facts.
    commands = [("PRAGMA table_info("+table+")",()) for table in tables]
    commands += [("SELECT * FROM "+table,()) for table in tables]
    commands += [("PRAGMA foreign_keys",()),("PRAGMA foreign_key_check",()),
        ("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT GLOB '_cf_*' "
         "AND (name NOT GLOB 'sqlite_*' OR name GLOB 'sqlite_autoindex_*') ORDER BY type,name",())]
    results = await execute(native,commands)
    data = {}
    for index,table in enumerate(tables):
        columns = [_field(row,"name") for row in _field(results[index],"results")]
        rows = [[_field(row,column) for column in columns]
            for row in _field(results[index+len(tables)],"results")]
        rows.sort(key=lambda row:json.dumps(row,ensure_ascii=False,separators=(",",":")))
        data[table] = {"rows":len(rows),"sha256":digest({"columns":columns,"rows":rows})}
    foreign = list(_field(results[-3],"results"))
    assert len(foreign)==1 and _field(foreign[0],"foreign_keys")==1
    assert not list(_field(results[-2],"results"))
    objects = tuple((_field(row,"type"),_field(row,"name"),_field(row,"tbl_name"),
        " ".join(_field(row,"sql").split()) if _field(row,"sql") else None)
        for row in _field(results[-1],"results"))
    return {"tables":data,"foreignKeysEnabled":True,"foreignKeyViolations":0,
        "schemaObjects":objects,"schemaObjectsSha256":digest(objects)}


def historical_journal(sql, baseline):
    journal = BudgetJournal(sql,preview_product=True,product_operations=True)
    state = journal.snapshot()
    assert state["requests"]==0
    rows = {table:item["rows"] for table,item in baseline["tables"].items()}
    state.update(schema_ready=True,schema_version=7,schema_fingerprint=V7_SCHEMA_FINGERPRINT,
        requests=90,cases={"product-schema":1,"product":89,"product-schema-v4-to-v5":1,
            "product-state-record-v1":1,"product-schema-v5-to-v6":1,"product-schema-v6-to-v7":1},
        reserved_read=30056,reserved_written=1053,actual_read=1530,actual_written=246,
        evidence=[{"request":1,"operation":1,"rows_read":800,"rows_written":200},
            {"request":90,"operation":2,"rows_read":730,"rows_written":46}],
        schema_upgrade=OLD_UPGRADE,schema_upgrade_v6=OLD_V6_UPGRADE,schema_upgrade_v7=OLD_V7_UPGRADE,
        state_record_migration=OLD_CUTOVER,state_storage_version=1,
        product_data={"rows":rows,"json":{},"arrays":262144,
            "records":{"users":2,"sessions":2,"githubStates":1,"billingEvents":1,"billingPendingUpdates":1}},
        product_data_verified=True)
    journal._save(state)
    return journal


def preserved(before, after):
    mutable = {"requests","reserved_read","reserved_written","actual_read","actual_written",
        "active","deadline","stopped","cases","evidence","product_evidence_rows",
        "schema_version","schema_fingerprint","product_data","product_data_verified"}
    for key in set(before)-mutable:
        assert after[key]==before[key],key
    assert after["evidence"][:len(before["evidence"])]==before["evidence"]
    assert all(after[key]>=before[key] for key in COUNTERS)
    assert after["requests"]==before["requests"]+1
    assert after["cases"]=={**before["cases"],"product":before["cases"]["product"]+1,
        "product-schema-v7-to-v8":1}
    assert {table:n for table,n in after["product_data"]["rows"].items()
        if table!="workspace_join_requests"}==before["product_data"]["rows"]


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if getattr(self.env,"PULLWISE_MODE","")!="local":
            return Response.json({"error":"LOCAL_ONLY"},status=503)
        path = str(request.url).rsplit("/",1)[-1]
        if path not in {"setup","upgrade","restart","rollback"} or request.method!="POST":
            return Response.json({"error":"NOT_FOUND"},status=404)
        name = "local-invite-approval-rollback-fixture" if path=="rollback" else "local-invite-approval-main-fixture"
        return await self.env.FIXTURE_JOURNAL.get(self.env.FIXTURE_JOURNAL.idFromName(name)).fetch(request)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx,self.env = ctx,env
    async def fetch(self, request):
        path = str(request.url).rsplit("/",1)[-1]
        native = ObservedD1(self.env.ROLLBACK_DB if path=="rollback" else self.env.DB,
            "rollback" if path=="rollback" else "main",inject=path=="rollback")
        if path=="setup":
            baselines = []
            for item in (native,ObservedD1(self.env.ROLLBACK_DB,"rollback")):
                await execute(item,[(sql,()) for sql in V7_SCHEMA_SQL])
                await execute(item,seed_commands())
                value = await snapshot(item)
                assert value["schemaObjects"]==V7_SCHEMA_OBJECTS
                baselines.append(value)
            assert baselines[0]["tables"]==baselines[1]["tables"]
            historical_journal(self.ctx.storage.sql,baselines[0])
            await save_fixture(self.ctx.storage,"fixtureBaseline",baselines[0])
            return Response.json({"passed":True,"v7Fingerprint":V7_SCHEMA_FINGERPRINT,
                "twoFreshLocalBindings":True,"syntheticPriorJournal":True,
                "foreignKeysEnabled":True,"tables":baselines[0]["tables"],
                "schemaObjects":len(V7_SCHEMA_OBJECTS),"fixtureSeedStatementsPerBinding":len(seed_commands()),
                "fixtureSeedOutsideMigrationJournal":True})
        if path=="upgrade":
            journal = BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            baseline = await load_fixture(self.ctx.storage,"fixtureBaseline")
            before = journal.snapshot()
            start = len(native.groups)
            await upgrade_product_schema_v8(native,journal)
            after = journal.snapshot()
            migration_groups = native.groups[start:]
            preserved(before,after)
            assert SCHEMA_VERSION==8 and after["schema_version"]==8
            assert after["schema_fingerprint"]==SCHEMA_FINGERPRINT
            assert after["schema_upgrade_v8"]["complete"] is True
            assert after["cases"]["product-schema-v7-to-v8"]==1
            assert after["active"] is None and after["stopped"] is None
            assert after["actual_read"]-before["actual_read"]==sum(item["rowsRead"] for item in migration_groups)
            assert after["actual_written"]-before["actual_written"]==sum(item["rowsWritten"] for item in migration_groups)
            verified = await snapshot(native)
            assert verified["tables"]==baseline["tables"]
            assert verified["schemaObjects"]==SCHEMA_OBJECTS
            assert after["product_data"]["rows"]["workspace_join_requests"]==0
            final = after
            await save_fixture(self.ctx.storage,"postUpgradeJournal",final)
            await save_fixture(self.ctx.storage,"postUpgradeTables",(await snapshot(native))["tables"])
            return Response.json({"passed":True,"schemaVersion":8,"schemaFingerprint":SCHEMA_FINGERPRINT,
                "all20HistoricalTablesPreserved":True,"joinRequestsInitiallyEmpty":True,"foreignKeyViolations":0,
                "stableIDsAndEveryStoredField":True,"legacyTargetedInvitesPreserved":True,"legacyMarkersAndEvidencePreserved":True,
                "oneShotCaseCount":1,"migrationNativeBatches":migration_groups,
                "migrationJournalActualDeltasMatchRawMeta":True,
                "migrationWriteExecution":after["schema_upgrade_v8"].get("write_execution"),
                "counterBefore":{key:before[key] for key in COUNTERS},
                "counterAfter":{key:after[key] for key in COUNTERS},
                "postUpgradeCounters":{key:final[key] for key in COUNTERS}})
        if path=="restart":
            journal = BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            before = journal.snapshot()
            assert before==await load_fixture(self.ctx.storage,"postUpgradeJournal")
            await upgrade_product_schema_v8(native,journal)
            assert native.dispatches==0 and journal.snapshot()==before
            value = await snapshot(native)
            assert value["tables"]==await load_fixture(self.ctx.storage,"postUpgradeTables")
            assert value["schemaObjects"]==SCHEMA_OBJECTS
            # These are bounded local schema probes, deliberately outside the
            # migration journal. They never invoke application routes/providers.
            await execute(native,[("""INSERT INTO workspace_invites(id,workspace_id,
                github_recipient_id,github_login,token_hash,role,status,revision,expires_at,
                created_by_user_id,created_by_revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",("inv_open",OWNER,None,None,"0"*64,
                "viewer","pending",1,2000000000,OWNER,1,"local","local"))])
            request_sql = """INSERT INTO workspace_join_requests(id,workspace_id,invite_id,
                applicant_user_id,status,revision,created_at,updated_at,reviewed_by_user_id,
                reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?)"""
            await execute(native,[(request_sql,("join_pending",OWNER,"inv_open",
                "usr_github_740003","pending",1,"local","local",None,None)),
                (request_sql,("join_approved",OWNER,"inv_open","usr_github_740004",
                    "approved",2,"local","local",OWNER,"reviewed")),
                (request_sql,("join_rejected",OWNER,"inv_open","usr_github_740005",
                    "rejected",2,"local","local",OWNER,"reviewed"))])
            for identity,action in (("event_request","request_join"),
                    ("event_approve","approve_join"),("event_reject","reject_join")):
                await execute(native,[("""INSERT INTO workspace_events(id,workspace_id,
                    actor_user_id,action,subject_id,before_json,after_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",(identity,OWNER,OWNER,action,"inv_open",
                    None,'{"local":true}',"local"))])
            probes = [
                ("UPDATE workspace_invites SET github_recipient_id=? WHERE id=?",
                    (740099,"inv_open")),
                (request_sql,("join_duplicate",OWNER,"inv_open","usr_github_740003",
                    "pending",1,"local","local",None,None)),
                ("UPDATE workspace_join_requests SET reviewed_at=? WHERE id=?",
                    ("invalid","join_pending")),
                ("UPDATE workspace_join_requests SET status=? WHERE id=?",
                    ("approved","join_pending")),
                (request_sql,("join_bad_fk",OWNER,"inv_missing","usr_github_740006",
                    "pending",1,"local","local",None,None)),
                ("""INSERT INTO ledger_project_repositories(owner_id,project_id,
                    github_repo_id,github_full_name,created_at) VALUES(?,?,?,?,?)""",
                    (OWNER,"prj_missing",9090,"local/invalid","local")),
            ]
            for sql,values in probes:
                await rejected_probe(native,sql,values)
            local = await snapshot(native)
            assert local["tables"]["workspace_members"]==value["tables"]["workspace_members"]
            await execute(native,[("DELETE FROM workspace_events WHERE id=?",(identity,))
                for identity in ("event_request","event_approve","event_reject")]
                + [("DELETE FROM workspace_join_requests WHERE invite_id=?",("inv_open",)),
                   ("DELETE FROM workspace_invites WHERE id=?",("inv_open",))])
            empty = await execute(native,[("SELECT COUNT(*) AS n FROM workspace_join_requests",())])
            assert _field(list(_field(empty[0],"results"))[0],"n")==0
            assert (await snapshot(native))["tables"]==value["tables"]
            assert journal.snapshot()==before
            return Response.json({"passed":True,"actualProcessRestart":True,
                "canonicalNoReplayNativeDispatches":0,"journalUnchanged":True,
                "foreignKeysStillEnforced":True,"failedIntegrityProbesNativeMetaUnavailable":True,
                "integrityProbesOutsideMigrationJournal":True,"schemaObjectsMatch":True,
                "nullRecipientPairAccepted":True,"halfNullRecipientPairRejected":True,
                "pendingApprovedRejectedRequestsAccepted":True,"duplicateApplicantRejected":True,
                "reviewStatusConsistencyEnforced":True,"newAuditActionsAccepted":True,
                "schemaProbesGrantNoMembership":True,"invalidProbeCount":len(probes),
                "allProbeRowsRemoved":True,"allHistoricalTableDigestsPreserved":True})
        if path=="rollback":
            baseline = await snapshot(native)
            assert baseline["schemaObjects"]==V7_SCHEMA_OBJECTS
            journal = historical_journal(self.ctx.storage.sql,baseline)
            before = journal.snapshot()
            try:
                await upgrade_product_schema_v8(native,journal)
            except BudgetError as error:
                assert str(error)=="D1_OUTCOME_UNKNOWN"
            else:
                raise AssertionError("Injected native failure unexpectedly completed")
            after = journal.snapshot()
            assert after["stopped"]=="D1_OUTCOME_UNKNOWN" and after["schema_version"]==7
            assert after["schema_upgrade_v8"]["complete"] is False
            assert after["reserved_read"]>before["reserved_read"]
            assert after["reserved_written"]>before["reserved_written"]
            preserved(before,after)
            value = await snapshot(native)
            assert value["tables"]==baseline["tables"] and value["schemaObjects"]==V7_SCHEMA_OBJECTS
            assert any(item["injectedAfterInviteDrop"] for item in native.failed)
            reconstructed = BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            stopped = reconstructed.snapshot()
            before_dispatches = native.dispatches
            try:
                await upgrade_product_schema_v8(native,reconstructed)
            except BudgetError:
                pass
            else:
                raise AssertionError("Unknown native outcome was admitted for retry")
            assert native.dispatches==before_dispatches and reconstructed.snapshot()==stopped
            assert stopped["stopped"]=="D1_OUTCOME_UNKNOWN"
            return Response.json({"passed":True,"injectedAfterInviteDrop":True,
                "entireAtomicBatchRolledBack":True,"all20TableDigestsAndIndexesPreserved":True,
                "foreignKeysEnabled":True,"foreignKeyViolations":0,"schemaVersionRetained":7,
                "unknownOutcomeStopRetained":True,"reconstructedJournalNoRetryDispatches":0,
                "fullReadWriteReservationRetained":True,"failedBatchNativeResultsUnavailable":True,
                "counterBefore":{key:before[key] for key in COUNTERS},
                "counterAfter":{key:after[key] for key in COUNTERS}})
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8908)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024 <= args.port <= 64535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "src"
    source.mkdir()
    (source / "entry.py").write_text(ENTRY, encoding="utf-8")
    shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKER / "python_modules", directory / "python_modules",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Keep the fixture's Worker dependencies and lock identical to the release,
    # including the pinned IANA tzdata package used by recurring execution.
    for name in ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json"):
        shutil.copy2(WORKER / name, directory / name)
    # Resolve npx to the same already-installed Wrangler and Workerd release;
    # do not install a different tooling version inside this generated fixture.
    (directory / "node_modules").symlink_to(WORKER / "node_modules", target_is_directory=True)
    config = {"name": "pullwise-invite-approval-schema-native-local-only", "main": "src/entry.py",
        "compatibility_date": "2026-09-23", "compatibility_flags": ["python_workers"],
        "workers_dev": False, "preview_urls": False, "routes": [],
        "vars": {"PULLWISE_MODE": "local", "PULLWISE_D1_ACCESS_ENABLED": "1"},
        "d1_databases": [{"binding": binding, "database_name": name,
            "database_id": identity, "remote": False} for binding, name, identity in (
                ("DB", "invite-approval-schema-main-local-only", "00000000-0000-0000-0000-000000000028"),
                ("ROLLBACK_DB", "invite-approval-schema-rollback-local-only", "00000000-0000-0000-0000-000000000029"))],
        "durable_objects": {"bindings": [{"name": "FIXTURE_JOURNAL", "class_name": "FixtureJournal"}]},
        "migrations": [{"tag": "local-fixture-v1", "new_sqlite_classes": ["FixtureJournal"]}]}
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    bundle = Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle) != BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache is missing or invalid")
    binary = WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(binary)) +
        ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env = {**os.environ, "XDG_CACHE_HOME": "/workspace/.cache", "XDG_CONFIG_HOME": "/workspace/.config",
        "UV_CACHE_DIR": "/workspace/.cache/uv", "UV_PYTHON_INSTALL_DIR": "/workspace/.python",
        "UV_SYSTEM_CERTS": "true", "NPM_CONFIG_CACHE": str(directory / "npm-cache"),
        "WRANGLER_SEND_METRICS": "false", "MINIFLARE_WORKERD_PATH": str(wrapper),
        "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log")}
    files = sorted((source / "pullwise_server").glob("*.py"))
    manifest = [(str(path.relative_to(source)), sha256(path)) for path in files]
    d1_source = WORKER / "node_modules/miniflare/dist/src/workers/d1/database.worker.js"
    evidence = {"passed": False, "date": "2026-10-09", "localOnly": True,
        "nativePythonFFI": True, "nativeDurableObjectSQLite": True, "syntheticPriorJournal": True,
        "remoteRequests": 0, "remoteD1Operations": 0, "realProviderRequests": 0,
        "realAccounts": 0, "realPayments": 0, "clientRetries": 0, "localHttpCap": len(HTTP_PATHS),
        "nativeAttemptsFabricated": False, "fixtureBindingCount": 2,
        "canonicalSourceTreeSha256": hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest(),
        "canonicalSourceFileCount": len(files), "fixtureEntrySha256": sha256(source / "entry.py"),
        "dependencySha256": {name: sha256(directory / name) for name in
            ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json")},
        "sourceSha256": {str(path.relative_to(source)): sha256(path) for path in files
            if path.name in {"cloudflare_preview_schema.py", "cloudflare_preview_budget.py",
                "cloudflare_validation_budget.py", "cloudflare_native_d1.py", "cloudflare_state_records.py"}},
        "migrationsSha256": {path.name: sha256(path) for path in sorted((WORKER / "migrations").glob("*.sql"))},
        "runtimeProvenance": {"workerdSha256": sha256(binary), "runtimeBundleSha256": BUNDLE_SHA256,
            "miniflareD1SourceSha256": sha256(d1_source),
            "localTransaction": "state.storage.transactionSync(() => queries.map(query))",
            "totalAttempts": "Raw native fields retained; no fixture metadata substitution"},
        "scope": "Populated schema migration, storage integrity and journal preservation; authenticated API flow separate"}
    process, attempted, processes_started, results = None, 0, 0, []
    log_path = directory / "runtime.log"
    try:
        with log_path.open("w") as log:
            command = [str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config_path), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(directory / "state")]
            def start_runtime():
                nonlocal process, processes_started
                offset = log_path.stat().st_size
                process = subprocess.Popen(command, cwd=directory, env=env, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                processes_started += 1
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("Native schema Worker startup failed")
                    recent = log_path.read_bytes()[offset:].decode(errors="replace")
                    if "Ready on http://127.0.0.1:" + str(args.port) in recent:
                        try:
                            with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                                return
                        except OSError:
                            pass
                    time.sleep(.2)
                raise AssertionError("Native schema Worker startup timed out")
            def stop_runtime():
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
            start_runtime()
            opener = build_opener(ProxyHandler({}), NoRedirect())
            for path in HTTP_PATHS:
                if path == "restart":
                    stop_runtime()
                    start_runtime()
                attempted += 1
                assert attempted <= len(HTTP_PATHS)
                request = Request(f"http://127.0.0.1:{args.port}/_fixture/" + path,
                    method="POST", data=b"", headers={"Content-Type": "application/json"})
                try:
                    response = opener.open(request, timeout=60)
                except HTTPError as error:
                    response = error
                with response:
                    raw = response.read()
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        payload = {"nonJsonResponse": True, "bodyBytes": len(raw)}
                    results.append({"path": path, "status": response.status, "result": payload})
                    assert response.status == 200 and payload.get("passed") is True, (path, response.status, payload)
            evidence["passed"] = True
    except Exception as error:
        evidence["failure"] = str(error)
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        groups, failures = [], []
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeInviteApprovalSchemaMeta":'):
                groups.append(json.loads(line)["nativeInviteApprovalSchemaMeta"])
            elif line.startswith('{"nativeInviteApprovalSchemaFailure":'):
                failures.append(json.loads(line)["nativeInviteApprovalSchemaFailure"])
        evidence.update(localHttpRequestsAttempted=attempted, localHttpRequestsCompleted=len(results),
            runtimeProcessesStarted=processes_started, actualProcessRestarts=max(0, processes_started - 1),
            nativeResultStatements=sum(group["statements"] for group in groups),
            nativeRowsRead=sum(group["rowsRead"] for group in groups),
            nativeRowsWritten=sum(group["rowsWritten"] for group in groups),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]}, key=str),
            failedNativeDispatches=failures, nativeFailedBatchTotalsUnavailable=bool(failures),
            cases=results, runtimeStopped=process is None or process.poll() is not None,
            setupAndIntegritySnapshotsOutsideMigrationJournal=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({key: evidence.get(key) for key in ("passed", "failure",
            "localHttpRequestsCompleted", "nativeResultStatements", "nativeRowsRead", "nativeRowsWritten",
            "actualProcessRestarts", "runtimeStopped")}, ensure_ascii=False))
    if not evidence["passed"]:
        raise SystemExit("Native schema proof failed; preserve fixture and evidence without retry")


if __name__ == "__main__":
    main()
