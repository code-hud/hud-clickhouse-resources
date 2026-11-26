"""Tests for the ClickStack alert definitions and the alerts import/export command.

Run from the repository root:
    python3 -m unittest discover -s clickstack-ui/tests -v
"""

import contextlib
import importlib.util
import io
import itertools
import json
import pathlib
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = UI / "scripts" / "dashboard-loader.py"
ALERTS = UI / "alerts" / "hud-alerts.json"
DEFINITIONS = UI / "alerts" / "definitions.py"

_spec = importlib.util.spec_from_file_location("dashboard_loader", SCRIPT)
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

_def = importlib.util.spec_from_file_location("hud_alert_definitions", DEFINITIONS)
defs = importlib.util.module_from_spec(_def)
_def.loader.exec_module(defs)

KEY = "test-key"
NAMES = [
    "HudFlowErrors",
    "HudWeekOverWeekDegradation",
    "HudDayOverWeekDegradation",
    "HudLatencySpike",
    "HudErrorRateSpike",
    "HudFunctionDayOverWeekDegradation",
]


def load_alerts():
    with open(ALERTS, encoding="utf-8") as f:
        return json.load(f)


def by_name(doc):
    return {a["name"]: a for a in doc["alerts"]}


class AlertDefinitionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load_alerts()
        cls.alerts = by_name(cls.doc)

    def test_file_matches_the_generator(self):
        self.assertEqual(self.doc, defs.document())

    def test_names_match_the_opensearch_rules(self):
        self.assertEqual([a["name"] for a in self.doc["alerts"]], NAMES)
        self.assertEqual(self.doc["format"], dl.ALERT_FORMAT)

    def test_sql_is_a_grouped_line_chart_query(self):
        tables = {"endpoint_metrics_union", "endpoint_durations", "endpoint_metrics_lean_1h", "function_metrics_lean_1h"}
        for alert in self.doc["alerts"]:
            sql = alert["sql"]
            for macro in ("startDateMilliseconds", "endDateMilliseconds", "intervalSeconds"):
                self.assertIn(macro, sql, alert["name"])
            read = set(re.findall(r"\bhud_otel\.(\w+)", sql))
            self.assertTrue(read and read <= tables, (alert["name"], read))
            self.assertEqual(sql.startswith(defs.QUANTILE), "hud_duration_quantile(" in sql, alert["name"])
            self.assertTrue(sql.removeprefix(defs.QUANTILE).startswith("SELECT\n  toDateTime("), alert["name"])
            self.assertEqual(sql.count(" AS value"), 1, alert["name"])
            for alias, _expr in alert["columns"]:
                self.assertEqual(sql.count(" AS " + alias + ",\n"), 1, (alert["name"], alias))
            self.assertEqual(alert["thresholdType"], "above")
            self.assertIn(alert["interval"], dl.ALERT_INTERVALS)
            self.assertIn(f"{alert['threshold']}\n", sql, "the threshold is one of the SQL's conditions")

    def test_nothing_reads_the_timeseries_table(self):
        text = json.dumps(self.doc)
        for word in ("prometheusQuery", "target_info", "promql", "metricsTable", "_bucket", "increase("):
            self.assertNotIn(word, text)
        self.assertEqual(self.doc["database"], "hud_otel")

    def test_hourly_rules_read_complete_hours_of_the_lean_tables(self):
        for alert in self.doc["alerts"]:
            fine = "endpoint_durations" in alert["sql"] or "endpoint_metrics_union" in alert["sql"]
            self.assertEqual(fine, alert["interval"] == "5m", alert["name"])
            if not fine:
                self.assertIn("AND hour < toStartOfHour(", alert["sql"], alert["name"])

    def test_placeholders_name_the_alert_columns(self):
        for alert in self.doc["alerts"]:
            columns = {alias for alias, _tag in alert["columns"]}
            for text in (alert["summary"], alert["description"]):
                for placeholder in re.findall(r"\{\{\s*([^}]*?)\s*\}\}", text):
                    if placeholder == "value":
                        continue
                    self.assertTrue(placeholder.startswith("attributes."), (alert["name"], placeholder))
                    self.assertIn(placeholder.removeprefix("attributes."), columns, alert["name"])

    def test_sqs_split_and_4xx_exclusion(self):
        for name in ("HudFlowErrors", "HudErrorRateSpike"):
            sql = self.alerts[name]["sql"]
            self.assertIn("flow_type = 'sqs'", sql)
            self.assertIn("messages", sql)
            self.assertRegex(sql, r"toFloat64\((calls|sum\(duration_count\))\)")
            self.assertIn("NOT match(framework_result, '^4..$')", sql)
        flow = self.alerts["HudFlowErrors"]["sql"]
        self.assertIn("FROM hud_otel.endpoint_metrics_union\n", flow)  # issues and runs in one read
        self.assertIn("INTERVAL 20 MINUTE", flow)
        self.assertIn("INTERVAL 10 MINUTE", flow)
        self.assertIn("errors >= 1", flow)
        spike = self.alerts["HudErrorRateSpike"]
        self.assertIn("quantileExactInclusiveIf(0.99)", spike["sql"])
        self.assertIn("INTERVAL 193 HOUR", spike["sql"])  # the last hour and the 8 days before it
        for gate in ("runs >= 20", "rate > 2 * baseline", "last_runs >= 50", "baseline_n >= 1"):
            self.assertIn(gate, spike["sql"])
        self.assertEqual(spike["threshold"], 0.05)
        self.assertIn("alpha", spike["tags"])

    def test_latency_schedule_is_5m_and_the_window_stays_10m(self):
        alert = self.alerts["HudLatencySpike"]
        self.assertEqual(alert["interval"], "5m")
        self.assertIn(", 600) AS age", alert["sql"])
        self.assertIn("INTERVAL 24 HOUR", alert["sql"])
        for gate in ("cur_p90 >= 0.3", "ref_n >= 72", "cur_runs >= 50", "ref_runs >= 50", "2.58"):
            self.assertIn(gate, alert["sql"])
        self.assertTrue(any("5m" in gap or "10" in gap for gap in alert["gaps"]))

    def test_degradation_thresholds_stay(self):
        wow = self.alerts["HudWeekOverWeekDegradation"]["sql"]
        dow = self.alerts["HudDayOverWeekDegradation"]["sql"]
        self.assertIn("INTERVAL 336 HOUR", wow)  # 7 days against the 7 before
        self.assertIn("INTERVAL 168 HOUR AS cur", wow)
        for gate in ("cur_runs >= 350", "ref_runs >= 350", "cur_n >= 84", "ref_n >= 84"):
            self.assertIn(gate, wow)
        self.assertIn("INTERVAL 192 HOUR", dow)  # 1 day against the 7 before
        self.assertIn("INTERVAL 24 HOUR AS cur", dow)
        for gate in ("cur_runs >= 100", "ref_runs >= 100", "cur_n >= 12", "ref_n >= 84"):
            self.assertIn(gate, dow)
        for sql in (wow, dow):
            for gate in (">= 1.7", "2.58", "cur_p90 >= 0.15", "cur_sd / cur_avg <= 1", "ref_sd / ref_avg <= 1"):
                self.assertIn(gate, sql)
            self.assertIn("hud_duration_quantile(0.9, bk)", sql)
            self.assertIn("stddevPopIf", sql)

    def test_function_rule_is_not_installed(self):
        alert = self.alerts["HudFunctionDayOverWeekDegradation"]
        self.assertFalse(alert["install"])
        self.assertIn("match(function, '^(?:__NO_FUNCTION_SELECTED__)$')", alert["sql"])
        self.assertIn("hud_otel.function_metrics_lean_1h", alert["sql"])
        self.assertIn("cur_calls >= 100", alert["sql"])


class MockAlerts:
    def __init__(self):
        ids = (f"{n:024x}" for n in itertools.count(1))
        self.next_id = lambda: next(ids)
        self.state = {
            "sources": [
                {"id": "b" * 24, "name": "Hud endpoints", "kind": "log", "connection": "c" * 24,
                 "from": {"databaseName": "otel", "tableName": "endpoints"}},
            ],
            "webhooks": [],
            "alerts": [],
            "dashboards": [],
        }
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self):
                if self.headers.get("Authorization") != "Bearer " + KEY:
                    self._send(401, {"message": "Unauthorized"})
                    return None
                path = self.path.split("?", 1)[0]
                prefix = "/api/v2/"
                if not path.startswith(prefix):
                    self._send(404, {"message": "no"})
                    return None
                return path[len(prefix):].strip("/").split("/")

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                parts = self._route()
                if parts is None:
                    return
                if len(parts) == 1 and parts[0] in mock.state:
                    return self._send(200, {"data": mock.state[parts[0]]})
                if len(parts) == 2 and parts[0] == "alerts":
                    for item in mock.state["alerts"]:
                        if item["id"] == parts[1]:
                            return self._send(200, {"data": item})
                self._send(404, {"message": "not found"})

            def do_POST(self):
                parts = self._route()
                if parts is None:
                    return
                body = self._body()
                if parts == ["webhooks"]:
                    for key in ("name", "service", "url"):
                        if key not in body:
                            return self._send(400, {"message": "missing " + key})
                    item = {"id": mock.next_id(), **body}
                    mock.state["webhooks"].append(item)
                    return self._send(200, {"data": item})
                if parts == ["alerts"]:
                    for key in ("displayName", "interval", "threshold", "thresholdType", "source", "chartConfig"):
                        if key not in body:
                            return self._send(400, {"message": "missing " + key})
                    if body["source"] != "inline":
                        return self._send(400, {"message": "source"})
                    cfg = body["chartConfig"]
                    if cfg.get("configType") != "sql" or cfg.get("displayType") != "line":
                        return self._send(400, {"message": "chart"})
                    if not body.get("channels") or body.get("channel") != body["channels"][0]:
                        return self._send(400, {"message": "channel"})
                    item = {"id": mock.next_id(), **body}
                    mock.state["alerts"].append(item)
                    return self._send(200, {"data": item})
                self._send(404, {"message": "not found"})

            def do_PUT(self):
                parts = self._route()
                if parts is None:
                    return
                body = self._body()
                collection = {"alerts": mock.state["alerts"], "webhooks": mock.state["webhooks"]}.get(parts[0])
                if collection is None or len(parts) != 2:
                    return self._send(404, {"message": "not found"})
                for item in collection:
                    if item["id"] == parts[1]:
                        item.clear()
                        item.update({"id": parts[1], **body})
                        return self._send(200, {"data": item})
                self._send(404, {"message": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            dl.main(list(argv))
        except SystemExit as e:
            return str(e.code), out.getvalue()
    return None, out.getvalue()


class AlertImportTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with MockAlerts() as mock:
            mock.state["webhooks"].append(
                {"id": "d" * 24, "name": "Hud alerts", "service": "generic", "url": "http://127.0.0.1:9/hook"}
            )
            err, out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                           "--webhook", "Hud alerts", "--dry-run")
            self.assertIsNone(err, out)
            self.assertEqual(mock.state["alerts"], [])
            self.assertIn("would create  HudFlowErrors", out)
            self.assertIn("skip       HudFunctionDayOverWeekDegradation", out)
            self.assertIn("dry run: nothing was changed", out)

    def test_import_creates_then_updates(self):
        with MockAlerts() as mock:
            err, out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                           "--webhook", "Hud alerts", "--webhook-url", "http://127.0.0.1:9/hook")
            self.assertIsNone(err, out)
            names = [a["displayName"] for a in mock.state["alerts"]]
            self.assertEqual(names, NAMES[:-1])
            self.assertEqual(mock.state["webhooks"][0]["service"], "generic")
            flow = next(a for a in mock.state["alerts"] if a["displayName"] == "HudFlowErrors")
            self.assertEqual(flow["interval"], "5m")
            self.assertEqual(flow["threshold"], 0.01)
            self.assertEqual(flow["channel"]["webhookId"], mock.state["webhooks"][0]["id"])
            sql = flow["chartConfig"]["sqlTemplate"]
            self.assertIn("FROM otel.endpoint_metrics_union\n", sql)
            self.assertNotIn("hud_otel", sql)
            self.assertEqual(flow["chartConfig"]["connectionId"], "c" * 24)
            self.assertEqual(flow["chartConfig"]["sourceId"], "b" * 24)
            created_id = flow["id"]

            err, out = run("alerts", "import", "--url", mock.url, "--api-key", KEY, "--webhook", "Hud alerts")
            self.assertIsNone(err, out)
            self.assertIn("updated  HudFlowErrors", out)
            self.assertEqual(len(mock.state["alerts"]), 5)
            self.assertEqual(mock.state["alerts"][0]["id"], created_id)

    def test_include_inert_installs_the_function_rule(self):
        with MockAlerts() as mock:
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                            "--webhook", "Hud alerts", "--webhook-url", "http://127.0.0.1:9/hook",
                            "--include-inert")
            self.assertIsNone(err)
            names = [a["displayName"] for a in mock.state["alerts"]]
            self.assertIn("HudFunctionDayOverWeekDegradation", names)
            fn = next(a for a in mock.state["alerts"] if a["displayName"] == "HudFunctionDayOverWeekDegradation")
            self.assertIn("__NO_FUNCTION_SELECTED__", fn["chartConfig"]["sqlTemplate"])

    def test_missing_webhook_is_an_error(self):
        with MockAlerts() as mock:
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY, "--webhook", "missing")
            self.assertIn("no webhook named", err)
            self.assertEqual(mock.state["alerts"], [])

    def test_webhook_url_update_keeps_its_fields(self):
        with MockAlerts() as mock:
            mock.state["webhooks"].append(
                {"id": "d" * 24, "name": "Hud alerts", "service": "generic", "url": "http://127.0.0.1:9/old",
                 "description": "On-call", "body": '{"text": "{{title}}"}'}
            )
            err, out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                           "--webhook", "Hud alerts", "--webhook-url", "http://127.0.0.1:9/new")
            self.assertIsNone(err, out)
            self.assertEqual(mock.state["webhooks"], [
                {"id": "d" * 24, "name": "Hud alerts", "service": "generic", "url": "http://127.0.0.1:9/new",
                 "description": "On-call", "body": '{"text": "{{title}}"}'}
            ])

    def test_webhook_url_update_refuses_other_services(self):
        with MockAlerts() as mock:
            slack = {"id": "d" * 24, "name": "Hud alerts", "service": "slack",
                     "url": "https://hooks.slack.com/services/T/B/X"}
            mock.state["webhooks"].append(dict(slack))
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                            "--webhook", "Hud alerts", "--webhook-url", "http://127.0.0.1:9/new")
            self.assertIn("is a slack webhook", err)
            self.assertEqual(mock.state["webhooks"], [slack])
            self.assertEqual(mock.state["alerts"], [])

    def test_missing_source_is_an_error(self):
        with MockAlerts() as mock:
            mock.state["webhooks"].append(
                {"id": "d" * 24, "name": "Hud alerts", "service": "generic", "url": "http://127.0.0.1:9/hook"}
            )
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                            "--webhook", "Hud alerts", "--source-name", "Other")
            self.assertIn("no source named", err)
            self.assertEqual(mock.state["alerts"], [])

    def test_sql_database_overrides_the_source(self):
        with MockAlerts() as mock:
            err, out = run("alerts", "import", "--url", mock.url, "--api-key", KEY, "--webhook", "Hud alerts",
                           "--webhook-url", "http://127.0.0.1:9/hook", "--sql-database", "hud_otel")
            self.assertIsNone(err, out)
            for alert in mock.state["alerts"]:
                self.assertIn("hud_otel.endpoint_", alert["chartConfig"]["sqlTemplate"])
                self.assertNotIn("otel.endpoint_", alert["chartConfig"]["sqlTemplate"].replace("hud_otel.", ""))
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY, "--webhook", "Hud alerts",
                            "--sql-database", "bad name;")
            self.assertIn("not a plain identifier", err)

    def test_export_round_trip_keeps_the_sql(self):
        with MockAlerts() as mock:
            err, _out = run("alerts", "import", "--url", mock.url, "--api-key", KEY,
                            "--webhook", "Hud alerts", "--webhook-url", "http://127.0.0.1:9/hook")
            self.assertIsNone(err)
            with tempfile.TemporaryDirectory() as tmp:
                dest = pathlib.Path(tmp) / "hud-alerts.json"
                err, out = run("alerts", "export", "--url", mock.url, "--api-key", KEY, "--out", str(dest))
                self.assertIsNone(err, out)
                exported = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(exported["format"], dl.ALERT_FORMAT)
        self.assertEqual(exported["database"], "otel")
        self.assertEqual([a["name"] for a in exported["alerts"]], NAMES[:-1])
        self.assertIn("otel.endpoint_metrics_union", exported["alerts"][0]["sql"])


if __name__ == "__main__":
    unittest.main()
