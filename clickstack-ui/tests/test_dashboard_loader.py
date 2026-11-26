"""Tests for clickstack-ui/scripts/dashboard-loader.py and the dashboards in clickstack-ui/dashboards/.

Run from the repository root:
    python3 -m unittest discover -s clickstack-ui/tests -v

Standard library only. The import/export tests run the loader against an in-memory
mock of the ClickStack external API (see MockClickStack), which follows the rules
of the real one that the loader depends on.
"""

import contextlib
import copy
import importlib.util
import io
import itertools
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = UI / "scripts" / "dashboard-loader.py"
ALL_FILE = UI / "dashboards" / "hud-all-dashboards.json"
INDIVIDUAL_DIR = UI / "dashboards" / "individual-dashboards"

_spec = importlib.util.spec_from_file_location("dashboard_loader", SCRIPT)
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

EXPECTED_NAMES = [
    "Hud: 1. Services",
    "Hud: 2. Service Overview",
    "Hud: 3. Flow",
    "Hud: 4. Function",
    "Hud: 5. Forensic Details",
]


def load_all(path=ALL_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def walk(value):
    """Yield every dict in a JSON value."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def links(dashboard):
    """(tile, onClick) for every row link of a dashboard."""
    for tile in dashboard.get("tiles", []):
        click = (tile.get("config") or {}).get("onClick")
        if isinstance(click, dict) and click.get("type") == "dashboard":
            yield tile, click


# ------------------------------------------------------------------ the dashboard files


class DashboardFilesTest(unittest.TestCase):
    """Checks of the dashboards file and the individual files."""
    FILE, INDIVIDUAL, NAMES, ENV, LABELS_TABLES, FLOW = ALL_FILE, INDIVIDUAL_DIR, EXPECTED_NAMES, dl.ENV_EXPRESSION, \
        dl.LABELS_TABLES, "flow"

    @classmethod
    def setUpClass(cls):
        cls.doc = load_all(cls.FILE)
        cls.dashboards = cls.doc["dashboards"]
        cls.origin = cls.doc["exported_from"]

    def test_format_and_names(self):
        self.assertEqual(self.doc["format"], dl.FORMAT)
        self.assertEqual([d["name"] for d in self.dashboards], self.NAMES)
        for d in self.dashboards:
            self.assertRegex(d["id"], r"^[0-9a-f]{24}$")
            self.assertTrue(d["tiles"], d["name"])

    def test_tiles_fit_the_grid(self):
        for d in self.dashboards:
            ids = [t["id"] for t in d["tiles"]]
            self.assertEqual(len(ids), len(set(ids)), d["name"])
            for t in d["tiles"]:
                self.assertGreaterEqual(t["x"], 0)
                self.assertLessEqual(t["x"] + t["w"], 24, f"{d['name']}: {t['id']}")

    def test_sources_and_connections_are_declared(self):
        declared = {s["id"] for s in self.origin["sources"]} | {c["id"] for c in self.origin["connections"]}
        for d in self.dashboards:
            for node in walk(d):
                for key in ("sourceId", "source", "connectionId", "connection"):
                    if isinstance(node.get(key), str) and dl.OBJECT_ID.fullmatch(node[key]):
                        self.assertIn(node[key], declared, f"{d['name']}: {key} {node[key]}")

    def test_no_unknown_object_ids(self):
        known = ({s["id"] for s in self.origin["sources"]} | {c["id"] for c in self.origin["connections"]}
                 | {d["id"] for d in self.dashboards})
        for d in self.dashboards:
            unknown = set(dl.OBJECT_ID.findall(json.dumps(d))) - known
            self.assertFalse(unknown, f"{d['name']}: ids the import cannot map: {sorted(unknown)}")

    def test_labels_source(self):
        labels = [s for s in self.origin["sources"] if s.get("role") == dl.LABELS_ROLE]
        self.assertEqual(tuple(s["table"] for s in labels), self.LABELS_TABLES)
        for s in labels:
            self.assertEqual(s["spec"]["kind"], "log")
            self.assertIn("timestampValueExpression", s["spec"])
        # Every dropdown reads its values from the labels source or a declared log source.
        log_sources = {s["id"] for s in self.origin["sources"] if s.get("kind") == "log"}
        for d in self.dashboards:
            for f in d["filters"]:
                self.assertIn(f["sourceId"], log_sources, f"{d['name']}: {f['name']}")

    def test_environment_selector(self):
        for d in self.dashboards:
            env = [f for f in d["filters"] if f["name"] == "Environment"]
            self.assertEqual(len(env), 1, d["name"])
            env = env[0]
            self.assertEqual(env["expression"], self.ENV)
            self.assertEqual(env["variableName"], "env")
            self.assertEqual((env.get("minSelections"), env.get("maxSelections")), (1, 1))
            self.assertTrue(env["isBroadcastEnabled"] and env["isVariableEnabled"])
            self.assertEqual(d["savedFilterValues"],
                             [{"type": "sql", "condition": f"{self.ENV} IN ('production')"}])

    def test_dropdown_options_ignore_the_time_range(self):
        # The options query is time-filtered; "X) OR (X" keeps the labels-source dropdowns
        # filled whatever range is selected.
        labels_ids = {s["id"] for s in self.origin["sources"] if s.get("role") == dl.LABELS_ROLE}
        for d in self.dashboards:
            for f in d["filters"]:
                if f["sourceId"] in labels_ids:
                    left, sep, right = f["where"].partition(") OR (")
                    self.assertTrue(sep and left == right, f"{d['name']}: {f['name']}: {f['where']}")

    def test_selectors_are_layered(self):
        # Environment, then Service, then Flow, then Function: each selector lists only the
        # values that go with the selectors before it.
        depends_on = {"env": [], "service": ["env"], "endpoint": ["service"], "flow": ["service"],
                      "function": ["service"], "investigation": ["env"]}
        rank = ["env", "service", "endpoint", "flow", "function", "investigation"]
        for d in self.dashboards:
            names = [f["variableName"] for f in d["filters"]]
            self.assertEqual(names, sorted(names, key=rank.index), d["name"])
            for f in d["filters"]:
                used = set(re.findall(r"\$__filter\([^,]+, \$(\w+)\)", f["where"]))
                self.assertTrue(set(depends_on[f["variableName"]]) <= used, f"{d['name']}: {f['name']}")
                # A selector only refers to selectors that come before it on the dashboard.
                before = set(names[:names.index(f["variableName"])])
                self.assertTrue(used <= before, f"{d['name']}: {f['name']} uses {sorted(used - before)}")
        function = next(d for d in self.dashboards if d["name"] == self.NAMES[3])
        by_name = {f["variableName"]: f for f in function["filters"]}
        self.assertIn(f"$__filter({self.FLOW}, $flow)", by_name["function"]["where"])

    def test_links_point_at_the_dashboards_and_their_filters(self):
        by_id = {d["id"]: d for d in self.dashboards}
        count = 0
        for d in self.dashboards:
            for tile, click in links(d):
                count += 1
                self.assertEqual(click["target"]["mode"], "id")
                target = by_id.get(click["target"]["id"])
                self.assertIsNotNone(target, f"{d['name']}: link to {click['target']['id']}")
                expressions = {f["expression"] for f in target["filters"]}
                sql = tile["config"].get("sqlTemplate", "")
                for lf in click["filters"]:
                    self.assertIn(lf["expression"], expressions,
                                  f"{d['name']} -> {target['name']}: {lf['expression']}")
                    for column in re.findall(r"\{\{(\w+)\}\}", lf["template"]):
                        self.assertRegex(sql, rf"AS `?{column}`?\b", f"{d['name']}: column {column}")
                self.assertIn(self.ENV, [lf["expression"] for lf in click["filters"]])
        self.assertGreater(count, 0)

    def test_portable_content(self):
        text = self.FILE.read_text(encoding="utf-8")
        self.assertEqual(self.origin["base_url"], "http://localhost:8080")
        self.assertNotIn("exported_at", self.doc)
        self.assertIsNone(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text),
                          "a ClickHouse table UUID")
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an email address")
        urls = set(re.findall(r"https?://[^\s\"'`]+", text))
        self.assertEqual(urls, {"http://localhost:8080"})

    def test_individual_files_match(self):
        files = sorted(p.name for p in self.INDIVIDUAL.glob("*.json"))
        self.assertEqual(files, sorted(dl.dashboard_file_name(n) for n in self.NAMES))
        for d in self.dashboards:
            path = self.INDIVIDUAL / dl.dashboard_file_name(d["name"])
            with open(path, encoding="utf-8") as f:
                one = json.load(f)
            self.assertEqual(one, dl.make_doc(self.origin, [d]), path.name)

    def test_sql_tiles_use_the_hud_database(self):
        # The logs table comes from its source; every other table is one of hud-migrate's, in the labels tables'
        # database, which the import rewrites as a whole.
        tables = {f"{s['database']}.{s['table']}" for s in self.origin["sources"]}
        database = next(s["database"] for s in self.origin["sources"] if s.get("role") == dl.LABELS_ROLE)
        for d in self.dashboards:
            for t in d["tiles"]:
                for ref in re.findall(r"(?:FROM|JOIN)\s*([\w]+\.[\w.]+)", t["config"].get("sqlTemplate") or ""):
                    self.assertTrue(ref in tables or ref.startswith(database + "."), f"{d['name']}: {t['id']}: {ref}")

    def test_tiles_are_sql(self):
        # Every tile is a SQL query of the Hud tables: no PromQL, no TimeSeries table.
        for d in self.dashboards:
            for t in d["tiles"]:
                c = t["config"]
                self.assertIn(c.get("configType"), ("sql", None) if c["displayType"] == "markdown" else ("sql",),
                              f"{d['name']}: {t['name']}")
                text = json.dumps(c, ensure_ascii=False)
                for word in ("prometheusQuery", "promqlExpression", "expressions", "tags['", "target_info"):
                    self.assertNotIn(word, text, f"{d['name']}: {t['name']}")

    def test_line_tiles_fill_their_own_gaps(self):
        # HyperDX fills empty buckets with 0 at its own granularity, which can be finer than the table's (1 or 5
        # minutes, an hour): the queries fill rates and counts themselves and leave durations and ratios with gaps.
        for d in self.dashboards:
            for t in d["tiles"]:
                if t["config"].get("displayType") == "line":
                    self.assertIs(t["config"].get("fillNulls"), False, f"{d['name']}: {t['name']}")


# ------------------------------------------------------------------ helpers


class HelpersTest(unittest.TestCase):
    def test_dashboard_file_name(self):
        self.assertEqual(dl.dashboard_file_name("Hud: 2. Service Overview"), "hud-2-service-overview.json")
        self.assertEqual(dl.dashboard_file_name("Hud: 5. Forensic Details"), "hud-5-forensic-details.json")

    def test_rewrite(self):
        old, new = "a" * 24, "b" * 24
        value = {"x": [f"id {old}", "SELECT * FROM db.t, db.t2, xdb.t, `db.t`"], "n": 3,
                 "u": "http://old:1/dashboards/" + old}
        got = dl.rewrite(value, {old: new}, {"http://old:1": "https://new"}, {"db.t": "other.logs"})
        self.assertEqual(got["x"][0], f"id {new}")
        self.assertEqual(got["x"][1], "SELECT * FROM other.logs, db.t2, xdb.t, `db.t`")
        self.assertEqual(got["u"], "https://new/dashboards/" + new)
        self.assertEqual(got["n"], 3)
        # Databases after tables: the metrics tables follow the labels tables' database.
        got = dl.rewrite("FROM db.t JOIN db.flow JOIN xdb.flow JOIN db.t2", {}, {}, {"db.t": "other.logs"}, {"db": "hud"})
        self.assertEqual(got, "FROM other.logs JOIN hud.flow JOIN xdb.flow JOIN hud.t2")

    def test_set_default_environment(self):
        body = {"savedFilterValues": [{"type": "sql", "condition": "env IN ('production')"},
                                      {"type": "sql", "condition": "service IN ('a')"}]}
        dl.set_default_environment(body, "o'neil")
        self.assertEqual(body["savedFilterValues"][0]["condition"], "env IN ('o\\'neil')")
        self.assertEqual(body["savedFilterValues"][1]["condition"], "service IN ('a')")

    def test_parse_pairs(self):
        self.assertEqual(dl._parse_pairs(["a = b", "c=d=e"], "source"), {"a": "b", "c": "d=e"})
        with self.assertRaises(SystemExit):
            dl._parse_pairs(["nope"], "source")

    def test_dashboard_body_keeps_only_api_fields(self):
        body = dl.dashboard_body({"id": "x", "name": "n", "tiles": [1], "createdAt": "t", "savedQuery": None})
        self.assertEqual(body, {"name": "n", "tiles": [1], "tags": []})


# ------------------------------------------------------------------ mock ClickStack


KEY = "test-key"
ORG = "11111111-2222-3333-4444-555555555555"
SERVICE = "66666666-7777-8888-9999-000000000000"
CLOUD_KEY_ID, CLOUD_KEY_SECRET = "cloud-id", "cloud-secret"
CLOUD_AUTH = "Basic " + __import__("base64").b64encode(f"{CLOUD_KEY_ID}:{CLOUD_KEY_SECRET}".encode()).decode()


class MockClickStack:
    """In-memory ClickStack external API (v2), enough for the loader.

    Like the real API: POST /dashboards rejects filter ids and PUT requires them;
    /charts/series returns no row for a zero count and a single group per call.
    """

    def __init__(self, prefix="/api/v2", sources=None, connections=None, envs=None, dashboards=None,
                 cloud=False):
        # cloud=True: like ClickStack on ClickHouse Cloud: basic auth with a Cloud API key,
        # /v1/organizations/<org>/services/<service>/clickstack, payloads in "result",
        # and no /connections or /charts endpoints.
        self.cloud = cloud
        self.prefix = f"/v1/organizations/{ORG}/services/{SERVICE}/clickstack" if cloud else prefix
        ids = (f"{n:024x}" for n in itertools.count(0xabc000000000000000000001))
        self.next_id = lambda: next(ids)
        self.state = {
            "sources": sources if sources is not None else [
                {"id": "d" * 24, "name": "OTel logs", "kind": "log", "connection": "c" * 24,
                 "from": {"databaseName": "otel", "tableName": "logs"}},
                {"id": "e" * 24, "name": "Traces", "kind": "trace", "connection": "c" * 24,
                 "from": {"databaseName": "otel", "tableName": "traces"}},
            ],
            "connections": connections if connections is not None else [
                {"id": "c" * 24, "name": "ClickHouse"}, {"id": "f" * 24, "name": "Other"}],
            "dashboards": dashboards if dashboards is not None else [],
        }
        self.envs = envs if envs is not None else {}
        self.requests = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                if mock.cloud and "data" in obj:
                    obj = {"status": code, "requestId": "r", "result": obj["data"]}
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self):
                mock.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != (CLOUD_AUTH if mock.cloud else "Bearer " + KEY):
                    self._send(401, {"message": "Unauthorized"})
                    return None
                if not self.path.startswith(mock.prefix + "/"):
                    self._send(404, {"message": "Cannot " + self.command})
                    return None
                return self.path[len(mock.prefix):].strip("/").split("/")

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                parts = self._route()
                if parts is None:
                    return
                if mock.cloud and parts == ["connections"]:
                    return self._send(404, {"status": 404, "error": "Not Found"})
                if len(parts) == 1 and parts[0] in mock.state:
                    return self._send(200, {"data": mock.state[parts[0]]})
                if len(parts) == 2 and parts[0] == "dashboards":
                    for d in mock.state["dashboards"]:
                        if d["id"] == parts[1]:
                            return self._send(200, {"data": d})
                self._send(404, {"message": "not found"})

            def do_POST(self):
                parts = self._route()
                if parts is None:
                    return
                body = self._body()
                if parts == ["charts", "series"] and not mock.cloud:
                    return self._send(*mock.series(body))
                if parts == ["sources"]:
                    missing = [k for k in ("name", "kind", "connection", "from", "timestampValueExpression")
                               if k not in body]
                    if missing:
                        return self._send(400, {"message": f"Body validation failed: {missing}"})
                    s = {"id": mock.next_id(), **body}
                    mock.state["sources"].append(s)
                    return self._send(200, {"data": s})
                if parts == ["dashboards"]:
                    bad = [i for i, f in enumerate(body.get("filters", [])) if "id" in f]
                    if bad:
                        return self._send(400, {"message": f"filters.{bad[0]}: Unrecognized key(s) in object: 'id'"})
                    d = {"id": mock.next_id(), **body}
                    d["filters"] = [{**f, "id": mock.next_id()} for f in body.get("filters", [])]
                    mock.state["dashboards"].append(d)
                    return self._send(200, {"data": d})
                self._send(404, {"message": "not found"})

            def do_PUT(self):
                parts = self._route()
                if parts is None:
                    return
                body = self._body()
                missing = [i for i, f in enumerate(body.get("filters", [])) if "id" not in f]
                if missing:
                    return self._send(400, {"message": f"filters.{missing[0]}.id: Required"})
                for d in mock.state["dashboards"]:
                    if len(parts) == 2 and d["id"] == parts[1]:
                        d.clear()
                        d.update({"id": parts[1], **copy.deepcopy(body)})
                        return self._send(200, {"data": d})
                self._send(404, {"message": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def series(self, body):
        s = body["series"][0]
        where = s.get("where", "")
        if "flow = ''" in where:  # the endpoints table: env is a column
            eq = re.search(r"\benv = '([^']*)'", where)
            notin = re.search(r"NOT IN \(([^)]*)\)", where)
            excluded = re.findall(r"'([^']*)'", notin.group(1)) if notin else []
            if s.get("groupBy") == []:
                n = self.envs.get(eq.group(1), 0) if eq else sum(self.envs.values())
                return 200, {"data": [{"group": [], "series_0.data": str(n)}] if n else []}
            left = [e for e in self.envs if e not in excluded and self.envs[e]]
            if s.get("groupBy") == ["env"]:
                return 200, {"data": [{"group": [left[0]], "series_0.data": "1"}] if left else []}
            return 400, {"message": "unexpected groupBy"}
        return 400, {"message": "unexpected where"}

    def source(self, name):
        return next(s for s in self.state["sources"] if s["name"] == name)

    def dashboard(self, name):
        return next(d for d in self.state["dashboards"] if d["name"] == name)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def run(*argv):
    """Run the loader; return (exit message or None, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            dl.main(list(argv))
        except SystemExit as e:
            return str(e.code), out.getvalue()
    return None, out.getvalue()


IMPORT = ["--source", "Hud investigations=OTel logs"]


class ImportTest(unittest.TestCase):
    """The labels sources are created next to the logs table, and the metrics tables follow their database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def import_(self, mock, *extra):
        return run("import", "--url", mock.url, "--api-key", KEY, *extra)

    def test_import_into_a_new_target(self):
        with MockClickStack(envs={"production": 3}) as mock:
            err, out = self.import_(mock, *IMPORT)
            self.assertIsNone(err, out)
            self.assertEqual(sorted(d["name"] for d in mock.state["dashboards"]), EXPECTED_NAMES)
            # Created next to the logs table, where hud-migrate puts them
            endpoints, functions = mock.source("Hud endpoints"), mock.source("Hud functions")
            self.assertEqual(endpoints["from"], {"databaseName": "otel", "tableName": "endpoints"})
            self.assertEqual(functions["from"], {"databaseName": "otel", "tableName": "functions"})
            new_ids = {d["id"] for d in mock.state["dashboards"]}
            for d in mock.state["dashboards"]:
                text = json.dumps(d)
                self.assertNotIn("hud_otel.", text, d["name"])
                for _, click in links(d):
                    self.assertIn(click["target"]["id"], new_ids)
                for f in d["filters"]:
                    self.assertIn(f["sourceId"], {endpoints["id"], functions["id"], "d" * 24}, d["name"])
                self.assertEqual(d["savedFilterValues"], [{"type": "sql", "condition": "env IN ('production')"}])
            self.assertIn("FROM otel.process_metrics", json.dumps(mock.dashboard("Hud: 1. Services")))
            self.assertIn("FROM otel.logs", json.dumps(mock.dashboard("Hud: 2. Service Overview")))
            self.assertIn("database    hud_otel -> otel (the metrics tables)", out)
            self.assertIn("default     Environment = production", out)

    def test_sql_database_option(self):
        with MockClickStack() as mock:
            err, out = self.import_(mock, *IMPORT, "--sql-database", "hud")
            self.assertIsNone(err, out)
            self.assertEqual(mock.source("Hud endpoints")["from"], {"databaseName": "hud", "tableName": "endpoints"})
            self.assertEqual(mock.source("Hud functions")["from"], {"databaseName": "hud", "tableName": "functions"})
            self.assertIn("FROM hud.endpoint_durations", json.dumps(mock.dashboard("Hud: 3. Flow")))
            self.assertIn("FROM otel.logs", json.dumps(mock.dashboard("Hud: 3. Flow")))

    def test_existing_labels_source_is_kept(self):
        with MockClickStack() as mock:
            for i, table in enumerate(dl.LABELS_TABLES):
                mock.state["sources"].append({"id": str(i) * 24, "name": f"Hud {table}", "kind": "log",
                                              "connection": "c" * 24,
                                              "from": {"databaseName": "elsewhere", "tableName": table}})
            err, out = self.import_(mock, *IMPORT)
            self.assertIsNone(err, out)
            self.assertNotIn("is ignored", out)
            self.assertNotIn("would create", out)
            self.assertIn("FROM elsewhere.process_metrics", json.dumps(mock.dashboard("Hud: 1. Services")))

    def test_dry_run_changes_nothing(self):
        with MockClickStack() as mock:
            sources = copy.deepcopy(mock.state["sources"])
            err, out = self.import_(mock, *IMPORT, "--dry-run")
            self.assertIsNone(err, out)
            self.assertEqual((mock.state["dashboards"], mock.state["sources"]), ([], sources))
            self.assertIn("would create on otel.endpoints", out)
            self.assertIn("would create on otel.functions", out)

    def test_environment_detection(self):
        for envs, expected in (({"production": 2, "qa": 9}, "production"), ({"qa": 2, "dev": 7}, "dev"), ({}, "production")):
            with self.subTest(envs=envs), MockClickStack(envs=envs) as mock:
                self.assertIsNone(self.import_(mock, *IMPORT)[0])
                err, out = self.import_(mock, *IMPORT)
                self.assertIsNone(err, out)
                for d in mock.state["dashboards"]:
                    self.assertEqual(d["savedFilterValues"], [{"type": "sql", "condition": f"env IN ('{expected}')"}])

    def test_round_trip(self):
        """Import into a target with other names, export, and import back into one like the original."""
        out_file = os.path.join(self.tmp.name, "out", "all.json")
        split = os.path.join(self.tmp.name, "split")
        with MockClickStack(envs={"qa": 1}) as mock:
            self.assertIsNone(self.import_(mock, *IMPORT)[0])
            err, out = run("export", "--url", mock.url, "--api-key", KEY, "--out", out_file, "--split", split,
                           "--default-environment", "production")
            self.assertIsNone(err, out)
        with open(out_file, encoding="utf-8") as f:
            exported = json.load(f)
        self.assertEqual([d["name"] for d in exported["dashboards"]], EXPECTED_NAMES)
        self.assertEqual(sorted(os.listdir(split)), sorted(dl.dashboard_file_name(n) for n in EXPECTED_NAMES))
        labels = [s for s in exported["exported_from"]["sources"] if s.get("role") == dl.LABELS_ROLE]
        self.assertEqual([(s["database"], s["table"]) for s in labels], [("otel", "endpoints"), ("otel", "functions")])
        original = load_all()
        sources = [{"id": "2" * 24, "name": "Hud investigations", "kind": "log", "connection": "c" * 24,
                    "from": {"databaseName": "hud_otel", "tableName": "otel_logs"}}]
        with MockClickStack(sources=sources, connections=[{"id": "c" * 24, "name": "Hud ClickHouse"}]) as mock:
            err, out = run("import", "--url", mock.url, "--api-key", KEY, "--in", out_file,
                           "--source", "OTel logs=Hud investigations")
            self.assertIsNone(err, out)
            got = {d["name"]: d for d in mock.state["dashboards"]}
            id_map = {d["id"]: got[d["name"]]["id"] for d in original["dashboards"]}
            for s in original["exported_from"]["sources"]:
                id_map[s["id"]] = ("2" * 24 if s["name"] == "Hud investigations" else mock.source(s["name"])["id"])
        id_map[original["exported_from"]["connections"][0]["id"]] = "c" * 24
        for d in original["dashboards"]:
            self.assertEqual(dl.dashboard_body(got[d["name"]]), dl.rewrite(dl.dashboard_body(d), id_map, {}, {}), d["name"])

    def test_import_again_updates_in_place(self):
        with MockClickStack(envs={"production": 1}) as mock:
            self.assertIsNone(self.import_(mock, *IMPORT)[0])
            before = {d["name"]: d["id"] for d in mock.state["dashboards"]}
            sources = len(mock.state["sources"])
            err, out = self.import_(mock, *IMPORT)
            self.assertIsNone(err, out)
            self.assertEqual({d["name"]: d["id"] for d in mock.state["dashboards"]}, before)
            self.assertEqual(len(mock.state["sources"]), sources)
            self.assertIn("updated  Hud: 1. Services", out)

    def test_written_in_reverse_order(self):
        with MockClickStack() as mock:
            self.assertIsNone(self.import_(mock, *IMPORT)[0])
            puts = [p.rsplit("/", 1)[1] for m, p in mock.requests if m == "PUT"]
            order = {d["id"]: d["name"] for d in mock.state["dashboards"]}
            self.assertEqual([order[i] for i in puts], list(reversed(EXPECTED_NAMES)))

    def test_matching_names_need_no_mapping(self):
        sources = [{"id": "2" * 24, "name": "Hud investigations", "kind": "log", "connection": "c" * 24,
                    "from": {"databaseName": "hud_otel", "tableName": "otel_logs"}}]
        with MockClickStack(sources=sources, connections=[{"id": "c" * 24, "name": "Hud ClickHouse"}]) as mock:
            err, out = self.import_(mock)
            self.assertIsNone(err, out)
            self.assertEqual(len(mock.state["sources"]), 3)
            self.assertNotIn("table       ", out)
            self.assertNotIn("database    ", out)

    def test_ambiguous_source_asks_for_a_mapping(self):
        with MockClickStack() as mock:
            mock.state["sources"].append({"id": "9" * 24, "name": "More logs", "kind": "log",
                                          "connection": "c" * 24, "from": {}})
            err, _ = self.import_(mock)
            self.assertIn("--source \"Hud investigations=", err)
            self.assertEqual(mock.state["dashboards"], [])

    def test_public_url_rewrites_urls(self):
        with MockClickStack() as mock:
            err, out = self.import_(mock, *IMPORT, "--public-url", "https://clickstack.example.com/")
            self.assertIsNone(err, out)
            self.assertIn("urls        http://localhost:8080 -> https://clickstack.example.com", out)
            self.assertIn("https://clickstack.example.com/dashboards/", out)

    def test_ui_api_prefix(self):
        with MockClickStack(prefix="/api/api/v2") as mock:
            err, out = self.import_(mock, *IMPORT)
            self.assertIsNone(err, out)
            self.assertIn("/api/api/v2", out)

    def test_bad_api_key(self):
        with MockClickStack() as mock:
            err, _ = run("import", "--url", mock.url, "--api-key", "wrong")
            self.assertIn("API key was rejected", err)

    def test_unreachable_host(self):
        with MockClickStack() as mock:
            url = mock.url
        err, _ = run("import", "--url", url, "--api-key", KEY)
        self.assertIn("no ClickStack API found", err)

    def test_not_an_export(self):
        path = os.path.join(self.tmp.name, "x.json")
        with open(path, "w") as f:
            json.dump({"format": "something else"}, f)
        err, _ = run("import", "--url", "http://127.0.0.1:9", "--api-key", KEY, "--in", path)
        self.assertIn("is not a", err)

    def test_export_with_nothing_to_export(self):
        with MockClickStack() as mock:
            err, _ = run("export", "--url", mock.url, "--api-key", KEY,
                         "--out", os.path.join(self.tmp.name, "x.json"))
            self.assertIn("no dashboards whose name starts with 'Hud:'", err)


class CloudTest(unittest.TestCase):
    """ClickStack on ClickHouse Cloud: the same import and export through the Cloud API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cloud(self, mock, *extra, key_secret=CLOUD_KEY_SECRET):
        return ("--cloud", "--url", mock.url, "--organization", ORG, "--service", SERVICE,
                "--key-id", CLOUD_KEY_ID, "--key-secret", key_secret, *extra)

    def test_import(self):
        with MockClickStack(cloud=True) as mock:
            err, out = run("import", *self.cloud(mock, *IMPORT))
            self.assertIsNone(err, out)
            self.assertEqual(sorted(d["name"] for d in mock.state["dashboards"]), EXPECTED_NAMES)
            # The connection comes from the sources: Cloud has no /connections endpoint.
            self.assertEqual(mock.source("Hud endpoints")["connection"], "c" * 24)
            self.assertIn(f"connection  'Hud ClickHouse' -> '{'c' * 24}'", out)
            # No /charts endpoint either: the exported default environment is kept.
            for d in mock.state["dashboards"]:
                self.assertEqual(d["savedFilterValues"][0]["condition"], f"{dl.ENV_EXPRESSION} IN ('production')")
            self.assertIn(f"https://hyperdx.clickhouse.cloud/dashboards/", out)
            self.assertIn(f"?chcServiceId={SERVICE}", out)
            # Run again: updated in place.
            before = {d["name"]: d["id"] for d in mock.state["dashboards"]}
            err, out = run("import", *self.cloud(mock, *IMPORT, "--default-environment", "staging"))
            self.assertIsNone(err, out)
            self.assertEqual({d["name"]: d["id"] for d in mock.state["dashboards"]}, before)
            for d in mock.state["dashboards"]:
                self.assertEqual(d["savedFilterValues"][0]["condition"], f"{dl.ENV_EXPRESSION} IN ('staging')")

    def test_export(self):
        out_file = os.path.join(self.tmp.name, "all.json")
        with MockClickStack(cloud=True) as mock:
            self.assertIsNone(run("import", *self.cloud(mock, *IMPORT))[0])
            err, out = run("export", *self.cloud(mock, "--out", out_file))
            self.assertIsNone(err, out)
        with open(out_file, encoding="utf-8") as f:
            exported = json.load(f)
        self.assertEqual([d["name"] for d in exported["dashboards"]], EXPECTED_NAMES)
        self.assertEqual(exported["exported_from"]["base_url"], "https://hyperdx.clickhouse.cloud")
        self.assertEqual(exported["exported_from"]["connections"], [{"id": "c" * 24, "name": "c" * 24}])

    def test_bad_key(self):
        with MockClickStack(cloud=True) as mock:
            err, _ = run("import", *self.cloud(mock, key_secret="wrong"))
            self.assertIn("ClickHouse Cloud API key", err)
            self.assertEqual(mock.state["dashboards"], [])

    def test_missing_arguments(self):
        def usage_error(*argv):
            stderr = io.StringIO()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("CLICKSTACK_", "CLICKHOUSE_CLOUD_"))}
            with contextlib.redirect_stderr(stderr), unittest.mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    dl.main(list(argv))
            return stderr.getvalue()
        err = usage_error("import", "--cloud", "--organization", ORG)
        self.assertIn("--service", err)
        self.assertIn("CLICKHOUSE_CLOUD_KEY_ID", err)
        self.assertIn("--url is required", usage_error("import", "--api-key", KEY))


if __name__ == "__main__":
    unittest.main()
