"""v10: every endpoint behaves the same when the API connects as `synth_voice_api` instead of the schema owner.

One deterministic walk over the HTTP API (real `session()`, keys auth, admin → people → folders → meetings → transcript
→ minutes → glossary → questions → voice → integrations → MCP) runs twice on fresh schemas: as the owner, and with every
request connection doing SET ROLE synth_voice_api. Migrations, fixtures and the worker stay owner. Both runs must give
the same (method, path, status) sequence and the same bodies once ids, timestamps and tokens are normalized, and the
statuses must be today's API behaviour (cross-user reads, a reader's writes, a scoped integration key).
The one wording allowed to differ is which «… no está disponible.» a 404 names: under the role a row the actor may not
see is absent, so «La carpeta» (the meeting exists, its folder is not yours) becomes «La reunión»; the status is kept.
"""
import json
import os
import random
import re
import sys
import unittest
import uuid
from contextlib import ExitStack
from itertools import zip_longest
from pathlib import Path
from unittest.mock import patch

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "server/schema.sql"
ADMIN = "00000000-0000-4000-8000-000000000001"
ADMIN_TOKEN = "walk-admin-" + "x" * 32  # synthetic; never a real credential
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
MISSING = {"step": "(no such step in this run)", "key": None, "status": None, "body": None}
MCP = {"MCP-Protocol-Version": "2025-06-18", "Origin": "http://127.0.0.1:18280"}


def transcript():
    """The minutes fixture, with each segment carrying its turn's words so search and questions find them."""
    from synth.tests.test_minutes import TRANSCRIPT
    content = json.loads(json.dumps({k: v for k, v in TRANSCRIPT.items() if k != "speakers"}))
    for segment, turn in zip(content["segments"], content["turns"]):
        segment["text"] = turn["text"]
    return content


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class ApiRoleWalkTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in self.url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.admin = psycopg.connect(self.url, autocommit=True)
        self.admin.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN CREATE ROLE synth_voice_api NOLOGIN; END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN CREATE ROLE synth_voice_worker NOLOGIN; END IF;
            END $$""")
        self.schemas = []

    def tearDown(self):
        for schema in self.schemas:
            self.admin.execute(f"DROP SCHEMA {schema} CASCADE")
        self.admin.close()

    # -- plumbing -------------------------------------------------------------------------------------------------
    def connector(self, schema, role=None):
        import psycopg
        from psycopg.rows import dict_row

        def connect(*_, **__):
            db = psycopg.connect(self.url, row_factory=dict_row, options="-c search_path=" + schema)
            if role:
                db.execute("SET ROLE " + role)
                db.commit()  # a later rollback must not undo the role
            return db
        return connect

    def fresh_schema(self):
        from synth.server.db import digest
        schema = "walk_" + uuid.uuid4().hex
        self.schemas.append(schema)
        self.admin.execute("CREATE SCHEMA " + schema)
        with self.connector(schema)() as db:
            db.execute(SCHEMA_SQL.read_text())
            db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,'Administrador local','user')", (ADMIN,))
            db.execute("INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)",
                       (uuid.uuid4(), ADMIN, digest(ADMIN_TOKEN)))
        return schema

    def run_walk(self, role):
        """Run the walk on a fresh schema; `role` is set on every request connection. Returns the records."""
        from fastapi.testclient import TestClient
        from synth.server import app as api
        from synth.worker import main  # noqa: F401  (patched below: the worker seeds minutes as the owner)
        schema = self.fresh_schema()
        rng = random.Random(10)
        seeded = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)  # noqa: E731
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"AUTH_MODE": "keys", "VOICE_PROFILE_ENCRYPTION_KEY":
                                                        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}))
            stack.enter_context(patch.object(api, "connect", self.connector(schema, role)))
            stack.enter_context(patch.object(uuid, "uuid4", seeded))
            for module in [m for name, m in list(sys.modules.items()) if name.startswith("synth.") and m]:
                if getattr(module, "uuid4", None) is not None and module is not uuid:
                    stack.enter_context(patch.object(module, "uuid4", seeded))
            with api.connect() as db:  # the request connections really run as `role`
                self.assertEqual(db.execute("SELECT current_user AS u").fetchone()["u"], role or "postgres")
            walk = Walk(self, TestClient(api.app), schema)
            walk.run()
        return walk.records

    def test_the_api_role_answers_every_endpoint_like_the_owner(self):
        baseline, restricted = self.run_walk(None), self.run_walk("synth_voice_api")
        unexpected = [(r["step"], r["status"], r["expect"]) for r in baseline if r["status"] != r["expect"]]
        self.assertEqual(unexpected, [], "the baseline walk no longer matches today's API behaviour")
        differences = [{"step": b["step"], "owner": (b["status"], b["body"]), "synth_voice_api": (r["status"], r["body"])}
                       for b, r in zip_longest(baseline, restricted, fillvalue=MISSING)
                       if (b["key"], b["body"]) != (r["key"], r["body"])]
        if differences:
            self.fail("%d steps differ under synth_voice_api:\n%s" % (len(differences), json.dumps(
                differences, ensure_ascii=False, indent=1, default=str)[:20000]))
        self.assertEqual([(r["step"], r["status"]) for r in restricted], [(r["step"], r["expect"]) for r in restricted])


class Walk:
    """The scripted walk. Each `call` records (method, path template, status, normalized body)."""

    def __init__(self, test, client, schema):
        self.test, self.client, self.schema = test, client, schema
        self.records, self.ids, self.tokens = [], {}, {"admin": ADMIN_TOKEN}

    def norm(self, value):
        if isinstance(value, dict):
            return {k: ("<token>" if k == "token" else self.norm(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.norm(v) for v in value]
        if isinstance(value, str):
            value = TS_RE.sub("<ts>", value)
            return UUID_RE.sub(lambda m: "<id%d>" % self.ids.setdefault(m.group(0), len(self.ids)), value)
        return value

    def call(self, step, who, method, template, expect, body=None, headers=None, **path):
        url = template.format(**path)
        auth = {"Authorization": "Bearer " + self.tokens[who]} if who else {}
        try:
            response = self.client.request(method, url, json=body, headers={**auth, **(headers or {})})
            status, payload = response.status_code, (response.json() if response.content else None)
        except Exception as error:  # noqa: BLE001  (the unhandled database error is the finding)
            status, payload = 500, {"exception": type(error).__module__ + "." + type(error).__name__,
                                    "message": str(error).splitlines()[0]}
        hidden = status == 404 and isinstance(payload, dict) and str(payload.get("detail", "")).endswith("no está disponible.")
        self.records.append({"step": step, "key": (method, template, status), "status": status, "expect": expect,
                             "body": {"detail": "<no disponible>"} if hidden else self.norm(payload)})
        return payload if isinstance(payload, (dict, list)) else {}

    def mcp(self, step, who, rpc_id, method, params=None, expect=200):
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
        return self.call(step, who, "POST", "/mcp", expect, body, MCP)

    def tool(self, step, who, name, arguments, is_error):
        result = self.mcp(step, who, step, "tools/call", {"name": name, "arguments": arguments}).get("result") or {}
        self.records[-1]["expect"] = 200 if result.get("isError") is is_error else "isError=%s" % is_error
        return json.loads(result["content"][0]["text"]) if result and not result.get("isError") else None

    def owner_db(self):
        return self.test.connector(self.schema)()

    def publish_minutes(self):
        """The worker (owner connection, scripted model) turns the queued document job into minutes, tasks, decisions."""
        from unittest.mock import patch
        from synth.tests.test_minutes import GOOD_TOPIC_1, GOOD_TOPIC_2, FakeModel
        from synth.worker import main, queue
        with self.owner_db() as db:
            claimed = queue.claim(db, main.WORKER, 300)
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", self.test.connector(self.schema)), \
                patch.object(main, "completion", side_effect=lambda messages, max_tokens: model(messages, max_tokens)):
            main.run(claimed)

    # -- the walk ---------------------------------------------------------------------------------------------------
    def run(self):
        try:
            self.people()
            shared, private = self.folders_and_meetings()
            job = self.transcripts(shared, private)
            self.minutes(shared, job)
            self.glossary(shared)
            self.questions_voice_people(shared, private)
            self.integration_and_mcp(shared, private)
            self.cleanup(shared)
        except (KeyError, IndexError, TypeError) as error:  # a failed step left no id for the next ones
            self.records.append({"step": "walk stopped after: " + self.records[-1]["step"], "key": ("-", "-", "stopped"),
                                 "status": "stopped", "expect": "completed", "body": repr(error)})

    def people(self):
        c = self.call
        c("admin me", "admin", "GET", "/v1/me", 200)
        c("no key", None, "GET", "/v1/me", 401)
        for name in ("alice", "bob", "carol"):
            pid = c(f"admin creates {name}", "admin", "POST", "/v1/principals", 201, {"name": name, "kind": "user"})["id"]
            setattr(self, name, pid)
            self.tokens[name] = c(f"admin issues {name} key", "admin", "POST", "/v1/keys", 201, {"principal_id": pid})["token"]
        c("bob cannot create principals", "bob", "POST", "/v1/principals", 403, {"name": "x", "kind": "user"})
        c("alice me", "alice", "GET", "/v1/me", 200)
        spare = c("alice issues her own key", "alice", "POST", "/v1/keys", 201, {"principal_id": self.alice})
        self.tokens["spare"] = spare["token"]
        c("alice revokes it", "alice", "DELETE", "/v1/keys/{key}", 200, key=spare["id"])
        c("revoked key", "spare", "GET", "/v1/me", 401)
        c("bob revokes an unknown key", "bob", "DELETE", "/v1/keys/{key}", 404, key=uuid.UUID(int=1))
        with self.owner_db() as db:  # a Supabase-linked identity, so the corporate voice registry lists alice
            db.execute("INSERT INTO auth_identities(provider,subject,principal_id) VALUES ('supabase',%s,%s)",
                       (uuid.UUID(int=7), self.alice))

    def folders_and_meetings(self):
        c = self.call
        shared = c("alice creates Clientes", "alice", "POST", "/v1/folders", 201, {"name": "Clientes"})["id"]
        self.team = c("alice creates Equipo", "alice", "POST", "/v1/folders", 201, {"name": "Equipo"})["id"]
        c("alice renames Clientes", "alice", "PATCH", "/v1/folders/{f}", 200, {"name": "Clientes 2026"}, f=shared)
        c("alice shares Clientes with bob (reader)", "alice", "PUT", "/v1/folders/{f}/members", 200,
          {"principal_id": self.bob, "role": "reader"}, f=shared)
        c("alice shares Clientes with carol", "alice", "PUT", "/v1/folders/{f}/members", 200,
          {"principal_id": self.carol, "role": "editor"}, f=shared)
        c("alice unshares carol", "alice", "DELETE", "/v1/folders/{f}/members/{p}", 200, f=shared, p=self.carol)
        c("alice shares Equipo with bob (editor)", "alice", "PUT", "/v1/folders/{f}/members", 200,
          {"principal_id": self.bob, "role": "editor"}, f=self.team)
        c("alice lists members", "alice", "GET", "/v1/folders/{f}/members", 200, f=shared)
        c("bob cannot list members", "bob", "GET", "/v1/folders/{f}/members", 403, f=shared)
        c("bob cannot rename Clientes", "bob", "PATCH", "/v1/folders/{f}", 404, {"name": "x"}, f=shared)
        for who in ("alice", "bob", "carol"):
            c(f"{who} lists folders", who, "GET", "/v1/folders", 200)
        meeting = {"title": "Comité Valdivar", "folder_id": shared, "idempotency_key": "cap-1"}
        shared_m = self.shared_m = c("alice creates shared meeting", "alice", "POST", "/v1/meetings", 201, meeting)["id"]
        c("same capture again", "alice", "POST", "/v1/meetings", 201, meeting)
        c("same key, other data", "alice", "POST", "/v1/meetings", 409, {**meeting, "title": "Otro"})
        private = c("alice creates private meeting", "alice", "POST", "/v1/meetings", 201, {"title": "Uno a uno"})["id"]
        moved = c("alice creates a meeting to move", "alice", "POST", "/v1/meetings", 201, {"title": "Ventas"})["id"]
        c("alice moves it into Clientes", "alice", "PUT", "/v1/meetings/{m}/folder", 200, {"folder_id": shared}, m=moved)
        c("bob cannot move it", "bob", "PUT", "/v1/meetings/{m}/folder", 404, {"folder_id": None}, m=moved)
        self.bob_meeting = c("bob creates a meeting in Equipo", "bob", "POST", "/v1/meetings", 201,
                             {"title": "Retro", "folder_id": self.team})["id"]
        c("bob cannot create in Clientes", "bob", "POST", "/v1/meetings", 404, {"title": "x", "folder_id": shared})
        c("alice writes private notes", "alice", "PUT", "/v1/meetings/{m}/notes", 200,
          {"notes": "Hablar de salario", "expected_revision": 1}, m=private)
        c("stale revision", "alice", "PUT", "/v1/meetings/{m}/notes", 409, {"notes": "x", "expected_revision": 1}, m=private)
        c("alice reads notes", "alice", "GET", "/v1/meetings/{m}/notes", 200, m=private)
        c("alice gets shared", "alice", "GET", "/v1/meetings/{m}", 200, m=shared_m)
        c("bob reads shared meeting", "bob", "GET", "/v1/meetings/{m}", 200, m=shared_m)
        c("bob cannot read private meeting", "bob", "GET", "/v1/meetings/{m}", 404, m=private)
        c("bob cannot read private notes", "bob", "GET", "/v1/meetings/{m}/notes", 404, m=private)
        c("bob cannot patch private notes", "bob", "PUT", "/v1/meetings/{m}/notes", 404,
          {"notes": "x", "expected_revision": 2}, m=private)
        c("bob cannot patch shared notes", "bob", "PUT", "/v1/meetings/{m}/notes", 404,
          {"notes": "x", "expected_revision": 1}, m=shared_m)
        c("carol cannot read shared meeting", "carol", "GET", "/v1/meetings/{m}", 404, m=shared_m)
        return shared, (shared_m, private)

    def transcripts(self, shared, meetings):
        c, (shared_m, private) = self.call, meetings
        upload = {"transcript": transcript(), "idempotency_key": "up-1"}
        job = c("alice uploads shared transcript", "alice", "POST", "/v1/meetings/{m}/transcript", 201, upload, m=shared_m)["job_id"]
        c("same upload again", "alice", "POST", "/v1/meetings/{m}/transcript", 201, upload, m=shared_m)
        c("bob cannot upload", "bob", "POST", "/v1/meetings/{m}/transcript", 404, {**upload, "idempotency_key": "b"}, m=shared_m)
        self.publish_minutes()
        small = {"transcript": {**transcript(), "turns": None}, "idempotency_key": "up-2"}
        small["transcript"]["segments"] = small["transcript"]["segments"][:1]
        queued = c("alice uploads private transcript", "alice", "POST", "/v1/meetings/{m}/transcript", 201, small,
                   m=private)["job_id"]
        c("alice cancels the queued job", "alice", "POST", "/v1/jobs/{j}/cancel", 200, j=queued)
        c("retry a cancelled job", "alice", "POST", "/v1/jobs/{j}/retry", 409, j=queued)
        c("cancel a finished job", "alice", "POST", "/v1/jobs/{j}/cancel", 200, j=job)
        c("bob reads the shared job", "bob", "GET", "/v1/jobs/{j}", 200, j=job)
        c("bob cannot read the private job", "bob", "GET", "/v1/jobs/{j}", 404, j=queued)
        c("bob cannot cancel the shared job", "bob", "POST", "/v1/jobs/{j}/cancel", 404, j=job)
        for who, m, status in (("alice", shared_m, 200), ("alice", private, 200), ("bob", shared_m, 200), ("bob", private, 404)):
            c(f"{who} meeting jobs", who, "GET", "/v1/meetings/{m}/jobs", status, m=m)
            c(f"{who} transcript", who, "GET", "/v1/meetings/{m}/transcript", status, m=m)
            c(f"{who} versions", who, "GET", "/v1/meetings/{m}/versions", status, m=m)
        for who in ("alice", "bob", "carol"):
            c(f"{who} library", who, "GET", "/v1/library", 200)
            c(f"{who} searches", who, "GET", "/v1/search?q=piloto", 200)
        c("bob library of Clientes", "bob", "GET", "/v1/library?folder_id={f}", 200, f=shared)
        c("carol library of Clientes", "carol", "GET", "/v1/library?folder_id={f}", 404, f=shared)
        return job

    def minutes(self, shared, job):
        c, m = self.call, self.shared_m
        for path in ("document", "summary", "minutes", "follow-up-email", "tasks", "decisions"):
            c(f"alice {path}", "alice", "GET", "/v1/meetings/{m}/" + path, 200, m=m)
            c(f"bob {path}", "bob", "GET", "/v1/meetings/{m}/" + path, 200, m=m)
            c(f"carol {path}", "carol", "GET", "/v1/meetings/{m}/" + path, 404, m=m)
        tasks = c("alice tasks again", "alice", "GET", "/v1/meetings/{m}/tasks", 200, m=m)["items"]
        decisions = c("alice decisions again", "alice", "GET", "/v1/meetings/{m}/decisions", 200, m=m)["items"]
        c("alice my tasks", "alice", "GET", "/v1/tasks", 200)
        c("alice closes a task", "alice", "PATCH", "/v1/tasks/{t}", 200, {"status": "hecha", "owner_principal_id": self.alice},
          t=tasks[0]["id"])
        c("bob cannot edit a task", "bob", "PATCH", "/v1/tasks/{t}", 404, {"status": "abierta"}, t=tasks[0]["id"])
        c("alice edits a decision", "alice", "PATCH", "/v1/decisions/{d}", 200, {"status": "aparcada"}, d=decisions[0]["id"])
        c("bob cannot edit a decision", "bob", "PATCH", "/v1/decisions/{d}", 404, {"status": "acordada"}, d=decisions[0]["id"])
        c("alice my done tasks", "alice", "GET", "/v1/tasks?status=hecha", 200)
        c("follow-up after edits", "alice", "GET", "/v1/meetings/{m}/follow-up-email", 200, m=m)
        c("bob cannot regenerate", "bob", "POST", "/v1/meetings/{m}/regenerate", 404, m=m)
        c("alice regenerates", "alice", "POST", "/v1/meetings/{m}/regenerate", 202, m=m)
        c("alice regenerates twice", "alice", "POST", "/v1/meetings/{m}/regenerate", 409, m=m)
        action = {"action": "rename", "speaker_id": "system:S2", "name": "María", "expected_version": 1}
        c("bob cannot name speakers", "bob", "POST", "/v1/meetings/{m}/speakers", 404, action, m=m)
        c("alice names a speaker", "alice", "POST", "/v1/meetings/{m}/speakers", 201, action, m=m)
        c("alice stale speaker action", "alice", "POST", "/v1/meetings/{m}/speakers", 409, action, m=m)
        c("undo a missing correction", "alice", "POST", "/v1/meetings/{m}/corrections/{x}/undo", 404, m=m, x="c0001")
        c("minutes of the new version", "alice", "GET", "/v1/meetings/{m}/minutes", 404, m=m)
        c("bob job of the old version", "bob", "GET", "/v1/jobs/{j}", 200, j=job)

    def glossary(self, shared):
        c = self.call
        term = c("alice adds a workspace term", "alice", "POST", "/v1/glossary", 201,
                 {"term": "Lucerno", "kind": "organizacion", "definition": "cliente"})["id"]
        folder_term = c("alice adds a folder term", "alice", "POST", "/v1/glossary", 201,
                        {"term": "Valdivar", "folder_id": shared})["id"]
        proposal = c("alice proposes a term", "alice", "POST", "/v1/glossary/candidates", 201,
                     {"term": "Baldibar", "origin": "fonetico", "examples": ["dijo baldibar"]})["id"]
        variant = c("alice proposes a variant", "alice", "POST", "/v1/glossary/candidates", 201,
                    {"term": "Lucernos", "origin": "llm", "alias_of": term})["id"]
        c("alice approves", "alice", "POST", "/v1/glossary/{t}/approve", 200, t=proposal)
        c("alice merges the variant", "alice", "POST", "/v1/glossary/{t}/merge", 200, {"into": term}, t=variant)
        c("alice edits a term", "alice", "PATCH", "/v1/glossary/{t}", 200, {"definition": "cliente industrial"}, t=term)
        c("alice discards", "alice", "POST", "/v1/glossary/{t}/discard", 200, t=proposal)
        c("bob cannot approve a folder term", "bob", "POST", "/v1/glossary/{t}/approve", 404, t=folder_term)
        c("bob cannot edit a folder term", "bob", "PATCH", "/v1/glossary/{t}", 404, {"definition": "x"}, t=folder_term)
        c("carol proposes in Clientes", "carol", "POST", "/v1/glossary/candidates", 404,
          {"term": "Zeta", "origin": "llm", "folder_id": shared})
        for who in ("alice", "bob", "carol"):
            c(f"{who} glossary", who, "GET", "/v1/glossary", 200)
        c("bob glossary of Clientes", "bob", "GET", "/v1/glossary?folder_id={f}", 200, f=shared)
        c("carol glossary of Clientes", "carol", "GET", "/v1/glossary?folder_id={f}", 404, f=shared)
        c("alice discarded terms", "alice", "GET", "/v1/glossary?status=descartado", 200)

    def questions_voice_people(self, shared, meetings):
        c, (_, private) = self.call, meetings
        asked = c("alice asks", "alice", "POST", "/v1/questions", 201, {"question": "¿Cuándo es el piloto de Valdivar?"})
        c("alice reads her question", "alice", "GET", "/v1/questions/{q}", 200, q=asked["id"])
        c("bob cannot read it", "bob", "GET", "/v1/questions/{q}", 404, q=asked["id"])
        c("bob asks about Clientes", "bob", "POST", "/v1/questions", 201, {"question": "¿Qué presupuesto?", "folder_id": shared})
        c("carol asks a private meeting", "carol", "POST", "/v1/questions", 404, {"question": "¿Salario?", "meeting_id": private})
        carol = c("carol asks everything", "carol", "POST", "/v1/questions", 201, {"question": "¿Cuándo es el piloto?"})
        c("carol reads her answer", "carol", "GET", "/v1/questions/{q}", 200, q=carol["id"])
        profile = {"id": str(uuid.UUID(int=99)), "model": "ecapa-test", "consent_confirmed": True,
                   "embedding": [((i * 37) % 11) / 10 + 0.01 for i in range(256)]}
        c("alice enrolls her voice", "alice", "PUT", "/v1/voice/profiles", 200, profile)
        c("alice re-enrolls", "alice", "PUT", "/v1/voice/profiles", 200, {**profile, "model": "ecapa-test-2"})
        c("bob cannot overwrite it", "bob", "PUT", "/v1/voice/profiles", 404, profile)
        c("bob syncs voices", "bob", "GET", "/v1/voice/profiles", 200)
        c("bob cannot delete alice's voice", "bob", "DELETE", "/v1/voice/profiles/{v}", 404, v=profile["id"])
        c("alice deletes her voice", "alice", "DELETE", "/v1/voice/profiles/{v}", 200, v=profile["id"])
        c("alice syncs voices", "alice", "GET", "/v1/voice/profiles", 200)
        c("alice people", "alice", "GET", "/v1/people", 200)

    def integration_and_mcp(self, shared, meetings):
        c, (shared_m, private) = self.call, meetings
        c("bob cannot integrate Clientes", "bob", "POST", "/v1/integrations", 404, {"name": "x", "folder_ids": [shared]})
        robot = c("alice integrates Clientes", "alice", "POST", "/v1/integrations", 201, {"name": "Claude", "folder_ids": [shared]})
        self.tokens["robot"], self.robot = robot["token"], robot["id"]
        c("alice lists integrations", "alice", "GET", "/v1/integrations", 200)
        c("alice lists members with the integration", "alice", "GET", "/v1/folders/{f}/members", 200, f=shared)
        c("robot me", "robot", "GET", "/v1/me", 200)
        c("robot folders", "robot", "GET", "/v1/folders", 200)
        c("robot library", "robot", "GET", "/v1/library", 200)
        c("robot reads shared meeting", "robot", "GET", "/v1/meetings/{m}", 200, m=shared_m)
        c("robot cannot read private meeting", "robot", "GET", "/v1/meetings/{m}", 404, m=private)
        c("robot glossary", "robot", "GET", "/v1/glossary", 200)
        c("robot cannot write meetings", "robot", "POST", "/v1/meetings", 403, {"title": "x"})
        c("robot cannot read voices", "robot", "GET", "/v1/voice/profiles", 403)
        c("robot cannot edit notes", "robot", "PUT", "/v1/meetings/{m}/notes", 403, {"notes": "x", "expected_revision": 1}, m=shared_m)
        self.mcp("mcp initialize", "robot", 1, "initialize", {"protocolVersion": "2025-06-18"})
        self.mcp("mcp tools/list", "robot", 2, "tools/list")
        self.tool("mcp list_folders", "robot", "list_folders", {}, False)
        self.tool("mcp get_meeting shared", "robot", "get_meeting", {"meeting_id": str(shared_m)}, False)
        self.tool("mcp get_meeting private", "robot", "get_meeting", {"meeting_id": str(private)}, True)
        self.tool("mcp search_transcripts", "robot", "search_transcripts", {"q": "piloto"}, False)
        self.tool("mcp list_meetings", "robot", "list_meetings", {}, False)
        self.tool("mcp get_answer unknown question", "robot", "get_answer", {"question_id": str(uuid.UUID(int=5))}, True)
        self.mcp("mcp as bob", "bob", 3, "tools/call", {"name": "get_meeting", "arguments": {"meeting_id": str(private)}})

    def cleanup(self, shared):
        c = self.call
        c("bob cannot revoke the integration", "bob", "DELETE", "/v1/integrations/{k}", 404, k=self.robot)
        c("alice revokes the integration", "alice", "DELETE", "/v1/integrations/{k}", 200, k=self.robot)
        c("revoked integration", "robot", "GET", "/v1/me", 401)
        c("bob cannot delete Equipo", "bob", "DELETE", "/v1/folders/{f}", 403, f=self.team)
        c("alice deletes Equipo (bob's meeting inside)", "alice", "DELETE", "/v1/folders/{f}", 200, f=self.team)
        c("bob still reads his meeting", "bob", "GET", "/v1/meetings/{m}", 200, m=self.bob_meeting)
        c("alice no longer reads bob's meeting", "alice", "GET", "/v1/meetings/{m}", 404, m=self.bob_meeting)
        c("alice deletes Clientes", "alice", "DELETE", "/v1/folders/{f}", 200, f=shared)
        c("bob no longer reads the shared meeting", "bob", "GET", "/v1/meetings/{m}", 404, m=self.shared_m)
        for who in ("alice", "bob"):
            c(f"{who} folders at the end", who, "GET", "/v1/folders", 200)
            c(f"{who} library at the end", who, "GET", "/v1/library", 200)


if __name__ == "__main__":
    unittest.main()
