#!/usr/bin/env python3
"""Export and import the Hud ClickStack (HyperDX) dashboards.

Export the "Hud: 1..5" dashboards from one ClickStack to a JSON file, and
import that file into another ClickStack (or back into the same one).

The import rewrites everything that is specific to one instance:
  * data source and connection ids  (matched by name, or set with --source/--connection)
  * ClickHouse tables in SQL tiles   (taken from the target's sources, e.g. hud_otel.otel_logs)
  * row links between the dashboards (they point at the target's dashboard ids)

The dashboards read the SQL tables hud-migrate creates for the Hud collector. The
dropdowns read the "Hud endpoints" and "Hud functions" sources over its endpoints
and functions tables, which the import creates in the logs source's database (or
--sql-database); the metrics tables follow their database.

Importing is an upsert by dashboard name: a dashboard that already exists on
the target is updated in place and keeps its id, so running it again is safe
and links that people already shared keep working.

Only the Python standard library is needed (Python 3.10 or later).

Authentication:
  * Self-hosted ClickStack: a personal API key, from ClickStack Team Settings >
    API & Agents ("Personal API access key"). Pass it with --api-key or
    CLICKSTACK_API_KEY.
  * ClickStack on ClickHouse Cloud (--cloud): a ClickHouse Cloud API key with the
    Service Admin (or Org Admin) role, from the Cloud console's API keys page. Pass
    its ID and secret with --key-id/--key-secret or CLICKHOUSE_CLOUD_KEY_ID and
    CLICKHOUSE_CLOUD_KEY_SECRET, and name the organization and service:
    --organization ORG_ID --service SERVICE_ID (both are in the console's URLs).

Examples (from the repository root):
  export CLICKSTACK_API_KEY=...
  python3 clickstack-ui/scripts/dashboard-loader.py import --url http://localhost:8080 --dry-run
  python3 clickstack-ui/scripts/dashboard-loader.py import --url https://clickstack.example.com \\
      --source "Hud investigations=OTel logs" --sql-database hud_otel --default-environment production
  python3 clickstack-ui/scripts/dashboard-loader.py export --url http://localhost:8080 --out out/hud-all-dashboards.json
  python3 clickstack-ui/scripts/dashboard-loader.py import --cloud --organization ORG_ID --service SERVICE_ID --dry-run
  python3 clickstack-ui/scripts/dashboard-loader.py alerts import --url http://localhost:8080 \\
      --webhook "Hud alerts" --dry-run

By default "import" reads clickstack-ui/dashboards/hud-all-dashboards.json, and "export"
writes the dashboards whose name starts with "Hud:".
"alerts import" reads clickstack-ui/alerts/hud-alerts.json and upserts by alert name.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import pathlib
import urllib.error
import urllib.request

FORMAT = "hud-clickstack-dashboards/v1"
OBJECT_ID = re.compile(r"\b[0-9a-f]{24}\b")
# The UI serves the external API under /api/api/v2 (it proxies /api/* to the API
# server); the API server itself serves it under /api/v2.
API_PREFIXES = ("/api/api/v2", "/api/v2")
CLOUD_API = "https://api.clickhouse.cloud"
CLOUD_UI = "https://hyperdx.clickhouse.cloud"
DASHBOARD_FIELDS = ("name", "tiles", "tags", "filters", "savedQuery",
                    "savedQueryLanguage", "savedFilterValues")
# The sources the dropdowns read their values from: log sources over hud-migrate's
# endpoints and functions tables (their timestamp is "a minute ago").
LABELS_ROLE = "labels"
LABELS_SOURCE_FIELDS = ("kind", "timestampValueExpression", "defaultTableSelectExpression",
                        "bodyExpression", "implicitColumnExpression")
# The labels tables, and the Environment selector's expression
LABELS_TABLES = ("endpoints", "functions")
ENV_EXPRESSION = "env"
DEFAULT_FILE = pathlib.Path(__file__).resolve().parent.parent / "dashboards" / "hud-all-dashboards.json"
ALERT_FORMAT = "hud-clickstack-alerts/v2"
ALERT_FILE = pathlib.Path(__file__).resolve().parent.parent / "alerts" / "hud-alerts.json"
ALERT_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "6h", "12h", "1d"}
ALERT_THRESHOLD_TYPES = {
    "above", "below", "above_exclusive", "below_or_equal",
    "equal", "not_equal", "between", "not_between",
}
PAGE_SIZE = 100


def is_labels_source(s):
    table = (s.get("from") or {}).get("tableName") or s.get("table") or ""
    return table in LABELS_TABLES


class ApiError(RuntimeError):
    pass


class Client:
    """A self-hosted ClickStack (Bearer personal key, /api/v2), or ClickStack on
    ClickHouse Cloud (basic auth with a Cloud API key, /v1/.../clickstack)."""

    def __init__(self, url, api_key=None, timeout=60, cloud=None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.base = None
        self.cloud = cloud
        if cloud:
            token = base64.b64encode(f"{cloud['key_id']}:{cloud['key_secret']}".encode()).decode()
            self.auth = "Basic " + token
            self.prefixes = (f"/v1/organizations/{cloud['organization']}/services/{cloud['service']}/clickstack",)
        else:
            self.auth = "Bearer " + api_key
            self.prefixes = API_PREFIXES

    def dashboard_url(self, public_url, dashboard_id):
        url = f"{public_url}/dashboards/{dashboard_id}"
        return url + (f"?chcServiceId={self.cloud['service']}" if self.cloud else "")

    def _request(self, method, path, body=None, base=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            (base or self.base) + path, data=data, method=method,
            headers={"Authorization": self.auth,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ApiError(f"{method} {path}: HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {path}: cannot reach {self.url}: {e.reason}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise ApiError(f"{method} {path}: response is not JSON") from None

    def connect(self):
        errors = []
        for prefix in self.prefixes:
            base = self.url + prefix
            try:
                self._request("GET", "/dashboards", base=base)
                self.base = base
                return
            except ApiError as e:
                errors.append(f"{prefix}: {e}")
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    raise ApiError("the API key was rejected (" + str(e) + "). " + (
                        "Use a ClickHouse Cloud API key with the Service Admin role for this service, and "
                        "check --organization and --service." if self.cloud else
                        "Use the Personal API access key from Team Settings > API & Agents."))
        raise ApiError("no ClickStack API found at " + self.url + "\n  " + "\n  ".join(errors))

    @staticmethod
    def _payload(response):
        # Self-hosted wraps the payload in "data", ClickHouse Cloud in "result".
        return response.get("data", response.get("result"))

    def get(self, path):
        return self._payload(self._request("GET", path))

    def post(self, path, body):
        return self._payload(self._request("POST", path, body))

    def put(self, path, body):
        return self._payload(self._request("PUT", path, body))

    def connections(self, sources):
        """The ClickHouse connections. ClickHouse Cloud manages them itself and has no
        API for them, so there they are the connections the sources use."""
        try:
            return self.get("/connections")
        except ApiError as e:
            if not self.cloud or not any(f"HTTP {c}" in str(e) for c in (403, 404, 405)):
                raise
        ids = list(dict.fromkeys(s["connection"] for s in sources if s.get("connection")))
        return [{"id": i, "name": i} for i in ids]


def make_client(args):
    if args.cloud:
        return Client(args.url or CLOUD_API, cloud={"organization": args.organization, "service": args.service,
                                                    "key_id": args.key_id, "key_secret": args.key_secret})
    return Client(args.url, args.api_key)


def default_public_url(args, client):
    return (args.public_url or (CLOUD_UI if args.cloud else client.url)).rstrip("/")


# ---------------------------------------------------------------- export

def cmd_export(args):
    client = make_client(args)
    client.connect()
    dashboards = [d for d in client.get("/dashboards") if d["name"].startswith(args.name_prefix)]
    if not dashboards:
        sys.exit(f"no dashboards whose name starts with {args.name_prefix!r} at {client.base}")
    dashboards.sort(key=lambda d: d["name"])
    if args.default_environment:
        for d in dashboards:
            set_default_environment(d, args.default_environment)
    used = set(OBJECT_ID.findall(json.dumps(dashboards)))
    all_sources = client.get("/sources")
    sources = [s for s in all_sources if s["id"] in used]
    connections = [c for c in client.connections(all_sources) if c["id"] in used]
    origin = {
        "base_url": default_public_url(args, client),
        "sources": [dict({"id": s["id"], "name": s["name"], "kind": s.get("kind"),
                          "database": (s.get("from") or {}).get("databaseName"),
                          "table": (s.get("from") or {}).get("tableName")},
                         **({"role": LABELS_ROLE,
                             "spec": {k: s[k] for k in LABELS_SOURCE_FIELDS if s.get(k)}}
                            if is_labels_source(s) else {}))
                    for s in sources],
        "connections": [{"id": c["id"], "name": c["name"]} for c in connections],
    }
    write_doc(args.out, origin, dashboards)
    print(f"exported {len(dashboards)} dashboards to {args.out}")
    for d in dashboards:
        print(f"  {d['name']}  ({len(d.get('tiles', []))} tiles)")
    if args.split:
        for d in dashboards:
            path = os.path.join(args.split, dashboard_file_name(d["name"]))
            write_doc(path, origin, [d])
            print(f"  wrote {path}")


def make_doc(origin, dashboards):
    return {"format": FORMAT, "exported_from": origin, "dashboards": dashboards}


def write_doc(path, origin, dashboards):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(make_doc(origin, dashboards), f, indent=2, ensure_ascii=False)
        f.write("\n")


def dashboard_file_name(name):
    """'Hud: 2. Service Overview' -> 'hud-2-service-overview.json'"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".json"


# ---------------------------------------------------------------- import

def _parse_pairs(pairs, what):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"--{what} takes OLD=NEW (names or ids), got {p!r}")
        old, new = p.split("=", 1)
        out[old.strip()] = new.strip()
    return out


def _find(items, key, label):
    """Find a target source/connection by id or by name."""
    hits = [i for i in items if i["id"] == key] or [i for i in items if i["name"] == key]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ApiError(f"the target has no {label} named {key!r}; it has: "
                       + ", ".join(repr(i["name"]) for i in items))
    raise ApiError(f"the target has several {label}s named {key!r}; pass its id instead")


def map_sources(exported, target, overrides):
    """Map the data sources the tiles query. The labels source is mapped separately."""
    mapping = {}
    for s in exported:
        if s.get("role") == LABELS_ROLE:
            continue
        key = overrides.get(s["name"]) or overrides.get(s["id"])
        if key:
            new = _find(target, key, "source")
        else:
            same_name = [t for t in target if t["name"] == s["name"]]
            same_kind = [t for t in target if t.get("kind") == s.get("kind")]
            if len(same_name) == 1:
                new = same_name[0]
            elif not same_name and len(same_kind) == 1:
                new = same_kind[0]
            else:
                raise ApiError(
                    f"cannot tell which target source replaces {s['name']!r} ({s.get('kind')}). "
                    f"Pass --source \"{s['name']}=<target source name>\"; the target has: "
                    + ", ".join(f"{t['name']!r} ({t.get('kind')})" for t in target))
        mapping[s["id"]] = new
    return mapping


def map_connections(exported, target, source_map, overrides):
    mapping = {}
    via_sources = {t.get("connection") for t in source_map.values() if t.get("connection")}
    for c in exported:
        key = overrides.get(c["name"]) or overrides.get(c["id"])
        if key:
            new = _find(target, key, "connection")
        elif len(via_sources) == 1:
            new = _find(target, next(iter(via_sources)), "connection")
        else:
            same_name = [t for t in target if t["name"] == c["name"]]
            if len(same_name) == 1:
                new = same_name[0]
            elif len(target) == 1:
                new = target[0]
            else:
                raise ApiError(
                    f"cannot tell which target connection replaces {c['name']!r}. "
                    f"Pass --connection \"{c['name']}=<target connection name>\"; the target has: "
                    + ", ".join(repr(t["name"]) for t in target))
        mapping[c["id"]] = new
    return mapping


def map_labels_sources(exported, target, overrides, sql_database, connection_id, client, dry_run,
                       logs_database=None):
    """Find each labels source on the target, or create it in the logs database (or --sql-database)."""
    mapping = {}
    for s in exported:
        if s.get("role") != LABELS_ROLE:
            continue
        table = f"{sql_database}.{s['table']}" if sql_database else None
        mapping[s["id"]] = map_labels_source(s, target, overrides, table, connection_id, client, dry_run,
                                             logs_database)
    return mapping


def map_labels_source(s, target, overrides, labels_table, connection_id, client, dry_run, logs_database):
    key = overrides.get(s["name"]) or overrides.get(s["id"])
    if key:
        return _find(target, key, "source")
    same_name = [t for t in target if t["name"] == s["name"]]
    if len(same_name) > 1:
        raise ApiError(f"the target has several sources named {s['name']!r}; pass --source with its id")
    if same_name:
        current = (same_name[0].get("from") or {})
        current = f"{current.get('databaseName')}.{current.get('tableName')}"
        if labels_table and labels_table != current:
            print(f"  note: the target already has {s['name']!r} on {current}; --sql-database is ignored "
                  "(edit or delete that source in ClickStack to change it)")
        return same_name[0]
    if not labels_table:
        labels_table = f"{logs_database or s.get('database') or 'hud_otel'}.{s['table']}"
    database, _, table = labels_table.partition(".")
    spec = dict(s.get("spec") or {"kind": "log", "timestampValueExpression": "now64(3) - INTERVAL 1 MINUTE",
                                  "defaultTableSelectExpression": ("env, service, flow, function"
                                                                   if s["table"] == "functions" else "env, service, flow")})
    spec.update({"name": s["name"], "connection": connection_id,
                 "from": {"databaseName": database, "tableName": table}})
    if dry_run:
        return {"id": s["id"], "name": s["name"], "from": spec["from"], "_action": "create"}
    new = client.post("/sources", spec)
    return dict(new, _action="created")


def detect_environment(client, labels_source_id, preferred):
    """Pick the environment the dashboards open with: `preferred` if the target has
    sessions in it, otherwise the environment with the most sessions. None if unknown."""
    # The labels source's timestamp is "a minute ago", so the window has to include now.
    # The charts API returns one group per call, so environments are found one at a time.
    now = int(time.time() * 1000)
    # One endpoints row per environment and service with sessions, last_seen being the service's latest
    # process metrics.
    base = "flow = '' AND last_seen >= now() - INTERVAL 1 HOUR"
    env_column = ENV_EXPRESSION
    quote = lambda v: "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def series(where, group_by):
        return client.post("/charts/series", {
            "startTime": now - 3600_000, "endTime": now + 300_000,
            "series": [{"sourceId": labels_source_id, "aggFn": "count", "where": where,
                        "whereLanguage": "sql", "groupBy": group_by}]}) or []

    def sessions(env):
        rows = series(f"{base} AND {env_column} = {quote(env)}", [])
        return sum(float(r.get("series_0.data") or 0) for r in rows)

    try:
        if sessions(preferred) > 0:
            return preferred
        found = []
        for _ in range(50):
            where = base + (f" AND {env_column} NOT IN ({', '.join(map(quote, found))})" if found else "")
            rows = series(where, [ENV_EXPRESSION])
            new = [g for r in rows for g in (r.get("group") or []) if isinstance(g, str)]
            new = [e for e in new if e]
            new = [e for e in dict.fromkeys(new) if e not in found]
            if not new:
                break
            found.extend(new)
        counts = {env: sessions(env) for env in found}
    except ApiError as e:
        print(f"  note: could not list the target's environments ({e}); keeping the exported default")
        return None
    if not counts:
        return None
    return max(counts, key=lambda e: (counts[e], e))


def set_default_environment(body, environment):
    """Point the saved Environment selection of a dashboard at another environment."""
    for f in body.get("savedFilterValues") or []:
        if f.get("type") == "sql" and f.get("condition", "").startswith(ENV_EXPRESSION + " IN "):
            quoted = environment.replace("\\", "\\\\").replace("'", "\\'")
            f["condition"] = f"{ENV_EXPRESSION} IN ('{quoted}')"
    return body


def table_rewrites(exported_sources, source_map):
    """old 'db.table' -> new 'db.table', from the exported and the target sources."""
    rewrites = {}
    for s in exported_sources:
        if s.get("role") == LABELS_ROLE:
            continue
        new_from = source_map[s["id"]].get("from") or {}
        if s.get("database") and s.get("table") and new_from.get("databaseName") and new_from.get("tableName"):
            old = f"{s['database']}.{s['table']}"
            new = f"{new_from['databaseName']}.{new_from['tableName']}"
            if old != new:
                rewrites[old] = new
    return rewrites


def database_rewrites(exported_sources, source_map):
    """Old database -> new database for the tables that are not sources (the metrics tables). They live next to
    the labels tables, so they follow their database."""
    s = next((x for x in exported_sources if x.get("role") == LABELS_ROLE), None)
    new = ((source_map.get(s["id"]) or {}).get("from") or {}).get("databaseName") if s else None
    return {s["database"]: new} if s and new and s.get("database") and new != s["database"] else {}


def rewrite(value, ids, base_urls, tables, databases=None):
    """Deep-rewrite ids, base URLs, table names and then databases in every string of a JSON value."""
    if isinstance(value, dict):
        return {k: rewrite(v, ids, base_urls, tables, databases) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(v, ids, base_urls, tables, databases) for v in value]
    if not isinstance(value, str):
        return value
    s = OBJECT_ID.sub(lambda m: ids.get(m.group(0), m.group(0)), value)
    for old, new in base_urls.items():
        s = s.replace(old, new)
    for old, new in tables.items():
        s = re.sub(r"(?<![\w.`])" + re.escape(old) + r"(?![\w])", new, s)
    for old, new in (databases or {}).items():
        s = re.sub(r"(?<![\w.`])" + re.escape(old) + r"\.(?=[\w`])", new + ".", s)
    return s


def dashboard_body(d):
    body = {k: d[k] for k in DASHBOARD_FIELDS if k in d and d[k] is not None}
    body.setdefault("tiles", [])
    body.setdefault("tags", [])
    return body


def cmd_import(args):
    with open(args.infile, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("format") != FORMAT:
        sys.exit(f"{args.infile} is not a {FORMAT} file")
    origin = doc["exported_from"]
    dashboards = sorted(doc["dashboards"], key=lambda d: d["name"])

    client = make_client(args)
    client.connect()
    public_url = default_public_url(args, client)

    target_sources = client.get("/sources")
    target_connections = client.connections(target_sources)
    source_map = map_sources(origin["sources"], target_sources, _parse_pairs(args.source, "source"))
    conn_map = map_connections(origin["connections"], target_connections, source_map,
                               _parse_pairs(args.connection, "connection"))
    main_connection = next(iter(conn_map.values()))["id"] if conn_map else target_connections[0]["id"]
    database_of = lambda kind: next(((source_map[s["id"]].get("from") or {}).get("databaseName")
                                     for s in origin["sources"] if s.get("kind") == kind and s["id"] in source_map), None)
    source_map.update(map_labels_sources(origin["sources"], target_sources, _parse_pairs(args.source, "source"),
                                         args.sql_database, main_connection, client, args.dry_run,
                                         database_of("log")))
    tables = table_rewrites(origin["sources"], source_map)
    databases = database_rewrites(origin["sources"], source_map)
    if not args.default_environment:
        # The source the Environment selector reads: the endpoints table
        exported_labels = next((s for s in origin["sources"] if s.get("role") == LABELS_ROLE
                                and s.get("table") != "functions"), None)
        labels = source_map.get(exported_labels["id"]) if exported_labels else None
        if labels and labels.get("_action") != "create":
            detected = detect_environment(client, labels["id"], "production")
            if detected:
                args.default_environment = detected
    base_urls = {} if origin["base_url"].rstrip("/") == public_url else {origin["base_url"].rstrip("/"): public_url}

    existing = {}
    for d in client.get("/dashboards"):
        existing.setdefault(d["name"], []).append(d)
    dupes = [n for n, ds in existing.items() if len(ds) > 1 and any(x["name"] == n for x in dashboards)]
    if dupes:
        sys.exit("the target has more than one dashboard named " + ", ".join(map(repr, dupes))
                 + "; delete the extra copies first")

    print(f"target: {client.base}")
    for s in origin["sources"]:
        t = source_map[s["id"]]
        where = (t.get("from") or {})
        action = {"create": "would create", "created": "created"}.get(t.get("_action"), "")
        print(f"  source      {s['name']!r} -> {t['name']!r} ({t['id']})"
              + (f"  [{action} on {where.get('databaseName')}.{where.get('tableName')}]" if action else ""))
    if args.default_environment:
        print(f"  default     Environment = {args.default_environment}")
    for c in origin["connections"]:
        t = conn_map[c["id"]]
        print(f"  connection  {c['name']!r} -> {t['name']!r} ({t['id']})")
    for old, new in tables.items():
        print(f"  table       {old} -> {new}")
    for old, new in databases.items():
        print(f"  database    {old} -> {new} (the metrics tables)")
    for old, new in base_urls.items():
        print(f"  urls        {old} -> {new}")

    # Pass 1: every dashboard needs its target id before links can point at it.
    ids = {sid: t["id"] for sid, t in source_map.items()}
    ids.update({cid: t["id"] for cid, t in conn_map.items()})
    created = []
    for d in dashboards:
        if d["name"] in existing:
            ids[d["id"]] = existing[d["name"]][0]["id"]
        elif args.dry_run:
            ids[d["id"]] = d["id"]
            created.append(d["name"])
        else:
            new = client.post("/dashboards", {"name": d["name"], "tiles": [], "tags": d.get("tags") or []})
            ids[d["id"]] = new["id"]
            created.append(d["name"])

    # Pass 2: write the full content. Done from the last dashboard to the first, so a
    # list sorted by "Last updated" shows them in order 1..N.
    plan = []
    for d in reversed(dashboards):
        body = rewrite(dashboard_body(d), ids, base_urls, tables, databases)
        if args.default_environment:
            set_default_environment(body, args.default_environment)
        leftovers = (set(OBJECT_ID.findall(json.dumps(body))) & {x["id"] for x in dashboards}) - set(ids.values())
        if leftovers:
            raise ApiError(f"{d['name']}: ids left unmapped: {sorted(leftovers)}")
        target_id = ids[d["id"]]
        plan.append({"id": target_id, "body": body})
        action = "create" if d["name"] in created else "update"
        if args.dry_run:
            print(f"  would {action}  {d['name']}  ({len(body['tiles'])} tiles)")
        else:
            client.put(f"/dashboards/{target_id}", body)
            got = client.get(f"/dashboards/{target_id}")
            if len(got.get("tiles", [])) != len(body["tiles"]):
                raise ApiError(f"{d['name']}: wrote {len(body['tiles'])} tiles, the target has {len(got.get('tiles', []))}")
            print(f"  {action}d  {d['name']}  ({len(body['tiles'])} tiles)  {client.dashboard_url(public_url, target_id)}")

    if args.plan_out:
        with open(args.plan_out, "w", encoding="utf-8") as f:
            json.dump(list(reversed(plan)), f, indent=2, ensure_ascii=False)
        print(f"wrote the request bodies to {args.plan_out}")
    if args.dry_run:
        print("dry run: nothing was changed")


def list_pages(client, path):
    """Every item of a paginated list. The client unwraps the data array."""
    out = []
    offset = 0
    while True:
        page = client.get(f"{path}?limit={PAGE_SIZE}&offset={offset}")
        if page is None:
            return out
        if not isinstance(page, list):
            raise ApiError(f"{path}: expected a list, got {type(page).__name__}")
        out.extend(page)
        if len(page) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def alert_target(sources, name, database_override):
    """The source the alerts are attached to: its id, its connection and the database of the Hud SQL tables."""
    matches = [s for s in sources if s.get("name") == name]
    if not matches:
        known = ", ".join(sorted(repr(s.get("name")) for s in sources)) or "(none)"
        raise ApiError(f"no source named {name!r}. Sources on the target: {known}")
    if len(matches) > 1:
        raise ApiError(f"the target has more than one source named {name!r}")
    source = matches[0]
    connection = source.get("connection") or source.get("connectionId")
    if not connection:
        raise ApiError(f"source {name!r} has no connection id")
    database = database_override or (source.get("from") or {}).get("databaseName")
    if not database:
        raise ApiError(f"source {name!r} has no database. Pass --sql-database DATABASE")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ApiError(f"database {database!r} is not a plain identifier")
    return source["id"], connection, database


def rewrite_alert_database(sql, old, new):
    """Point the Hud tables of an alert's SQL (old.endpoint_durations, ...) at the target's database."""
    pattern = re.compile(r"\b" + re.escape(old) + r"\.(?=[a-z_0-9]+\b)")
    if not pattern.search(sql):
        raise ApiError(f"the SQL does not query the {old} database")
    return sql if old == new else pattern.sub(new + ".", sql)


def alert_sql(alert, database, target_database):
    sql = alert.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ApiError(f"{alert.get('name')}: missing sql")
    for macro in ("startDateMilliseconds", "endDateMilliseconds", "intervalSeconds"):
        if macro not in sql:
            raise ApiError(f"{alert.get('name')}: SQL is missing {macro}, so the alert would never see a window")
    return rewrite_alert_database(sql, database, target_database)


def alert_body(alert, sql, connection_id, source_id, webhook_id):
    if alert.get("interval") not in ALERT_INTERVALS:
        raise ApiError(f"{alert.get('name')}: interval {alert.get('interval')!r} is not one ClickStack evaluates")
    if alert.get("thresholdType", "above") not in ALERT_THRESHOLD_TYPES:
        raise ApiError(f"{alert.get('name')}: unknown threshold type {alert.get('thresholdType')!r}")
    channel = {"type": "webhook", "webhookId": webhook_id}
    body = {
        "source": "inline",
        "displayName": alert["name"],
        "name": alert.get("summary") or alert["name"],
        "message": alert.get("description") or "",
        "interval": alert["interval"],
        "threshold": alert["threshold"],
        "thresholdType": alert.get("thresholdType", "above"),
        "tags": alert.get("tags") or [],
        "channel": channel,
        "channels": [channel],
        "chartConfig": {
            "configType": "sql",
            "displayType": "line",
            "name": alert["name"],
            "connectionId": connection_id,
            "sourceId": source_id,
            "sqlTemplate": sql,
        },
    }
    if alert.get("note"):
        body["note"] = alert["note"]
    return body


def resolve_webhook(client, name, url, dry_run):
    matches = [w for w in list_pages(client, "/webhooks") if w.get("name") == name]
    if len(matches) > 1:
        raise ApiError(f"more than one webhook is named {name!r}")
    if matches:
        webhook = matches[0]
        if url and webhook.get("url") != url and webhook.get("service") != "generic":
            raise ApiError(
                f"webhook {name!r} is a {webhook.get('service')} webhook; change its URL in ClickStack, "
                "or pass another --webhook name to create a generic one."
            )
        if url and webhook.get("url") != url and not dry_run:
            # PUT replaces the webhook: omitted description and body are cleared.
            update = {"name": name, "service": "generic", "url": url}
            update.update({k: webhook[k] for k in ("description", "body") if webhook.get(k)})
            client.put(f"/webhooks/{webhook['id']}", update)
            print(f"  webhook    {name!r} url updated")
        elif url and webhook.get("url") != url:
            print(f"  webhook    {name!r} would update url")
        else:
            print(f"  webhook    {name!r} ({webhook['id']})")
        return webhook["id"]
    if not url:
        raise ApiError(
            f"no webhook named {name!r}. Create one in ClickStack, or pass --webhook-url to create a generic webhook."
        )
    if dry_run:
        print(f"  webhook    would create {name!r}")
        return "0" * 24
    created = client.post("/webhooks", {"name": name, "service": "generic", "url": url})
    print(f"  webhook    created {name!r} ({created['id']})")
    return created["id"]


def load_alert_doc(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("format") != ALERT_FORMAT:
        sys.exit(f"{path} is not a {ALERT_FORMAT} file")
    if not doc.get("database") or not isinstance(doc.get("alerts"), list):
        sys.exit(f"{path} is missing database or alerts")
    names = [a.get("name") for a in doc["alerts"]]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit("duplicate alert names: " + ", ".join(dupes))
    return doc


def cmd_alerts_import(args):
    doc = load_alert_doc(args.infile)
    chosen = [a for a in doc["alerts"] if a.get("install", True) or args.include_inert]
    client = make_client(args)
    client.connect()
    source_id, connection_id, database = alert_target(
        client.get("/sources"), args.source_name, args.sql_database
    )
    webhook_id = resolve_webhook(client, args.webhook, args.webhook_url, args.dry_run)
    existing = {}
    for item in list_pages(client, "/alerts"):
        existing.setdefault(item.get("displayName"), []).append(item)
    dupes = [n for n, items in existing.items() if len(items) > 1 and any(a["name"] == n for a in chosen)]
    if dupes:
        raise ApiError("the target has more than one alert named " + ", ".join(map(repr, dupes)))

    print(f"target: {client.base}")
    print(f"  source     {args.source_name!r} ({source_id}), database {database}")
    for alert in chosen:
        sql = alert_sql(alert, doc["database"], database)
        body = alert_body(alert, sql, connection_id, source_id, webhook_id)
        found = existing.get(alert["name"], [])
        action = "update" if found else "create"
        if args.dry_run:
            print(f"  would {action}  {alert['name']}  ({alert['interval']}, {alert['thresholdType']} {alert['threshold']})")
            continue
        if found:
            client.put(f"/alerts/{found[0]['id']}", body)
        else:
            client.post("/alerts", body)
        print(f"  {action}d  {alert['name']}")
    skipped = [a["name"] for a in doc["alerts"] if not a.get("install", True) and not args.include_inert]
    for name in skipped:
        print(f"  skip       {name}  (install is false; pass --include-inert)")
    if args.dry_run:
        print("dry run: nothing was changed")


def cmd_alerts_export(args):
    doc = load_alert_doc(args.infile)
    wanted = [a["name"] for a in doc["alerts"]]
    client = make_client(args)
    client.connect()
    by_name = {}
    for item in list_pages(client, "/alerts"):
        if item.get("displayName") in wanted:
            by_name.setdefault(item["displayName"], []).append(item)
    dupes = [n for n, items in by_name.items() if len(items) > 1]
    if dupes:
        raise ApiError("the target has more than one alert named " + ", ".join(map(repr, dupes)))
    exported = []
    database = None
    for name in wanted:
        found = by_name.get(name)
        if not found:
            print(f"  missing    {name}")
            continue
        full = client.get(f"/alerts/{found[0]['id']}")
        cfg = full.get("chartConfig") or {}
        sql = cfg.get("sqlTemplate") or ""
        match = re.search(r"\b(\w+)\.(?:endpoint|function)_\w+", sql)
        if match and database is None:
            database = match.group(1)
        channel = (full.get("channels") or [None])[0] or full.get("channel") or {}
        exported.append({
            "name": name,
            "install": True,
            "interval": full.get("interval"),
            "threshold": full.get("threshold"),
            "thresholdType": full.get("thresholdType"),
            "tags": full.get("tags") or [],
            "summary": full.get("name") or name,
            "description": full.get("message") or "",
            "note": full.get("note") or "",
            "sql": sql,
            "webhookId": channel.get("webhookId"),
        })
        print(f"  export     {name}")
    out = {
        "format": ALERT_FORMAT,
        "database": database or doc["database"],
        "alerts": exported,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {len(exported)} alerts to {args.out}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--url", help="ClickStack UI URL, e.g. http://localhost:8080 (with --cloud: the "
                        f"ClickHouse Cloud API, default {CLOUD_API})")
        sp.add_argument("--api-key", default=os.environ.get("CLICKSTACK_API_KEY"),
                        help="personal API key (default: $CLICKSTACK_API_KEY)")
        sp.add_argument("--public-url", help="URL people open the dashboards with, used in the links the "
                        f"loader prints (default: --url; with --cloud {CLOUD_UI})")
        c = sp.add_argument_group("ClickStack on ClickHouse Cloud")
        c.add_argument("--cloud", action="store_true", help="talk to ClickStack on ClickHouse Cloud")
        c.add_argument("--organization", metavar="ORG_ID", help="ClickHouse Cloud organization ID")
        c.add_argument("--service", metavar="SERVICE_ID", help="ClickHouse Cloud service ID")
        c.add_argument("--key-id", default=os.environ.get("CLICKHOUSE_CLOUD_KEY_ID"),
                       help="Cloud API key ID (default: $CLICKHOUSE_CLOUD_KEY_ID)")
        c.add_argument("--key-secret", default=os.environ.get("CLICKHOUSE_CLOUD_KEY_SECRET"),
                       help="Cloud API key secret (default: $CLICKHOUSE_CLOUD_KEY_SECRET)")

    e = sub.add_parser("export", help="export dashboards to a JSON file")
    common(e)
    e.add_argument("--out", default=os.path.join("out", "hud-all-dashboards.json"),
                   help="file to write (default: out/hud-all-dashboards.json)")
    e.add_argument("--split", metavar="DIR", help="also write one file per dashboard to DIR")
    e.add_argument("--name-prefix", default="Hud:", help='export dashboards whose name starts with this (default "Hud:")')
    e.add_argument("--default-environment", metavar="NAME",
                   help="save the dashboards with this environment selected (default: as on the source)")

    i = sub.add_parser("import", help="create or update dashboards from a JSON file")
    common(i)
    i.add_argument("--in", dest="infile", default=str(DEFAULT_FILE),
                   help="file to import (default: clickstack-ui/dashboards/hud-all-dashboards.json)")
    i.add_argument("--source", action="append", metavar="OLD=NEW",
                   help='map an exported source to a target source by name or id, e.g. "Hud investigations=OTel logs"')
    i.add_argument("--connection", action="append", metavar="OLD=NEW",
                   help="map an exported connection to a target connection by name or id")
    i.add_argument("--sql-database", metavar="DATABASE",
                   help="the database of hud-migrate's tables, when it is not the logs source's")
    i.add_argument("--default-environment", metavar="NAME",
                   help="environment the dashboards open with (default: production if the target has "
                        "sessions in it, otherwise the environment with the most sessions)")
    i.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    i.add_argument("--plan-out", help="also write the request bodies to this file")

    a = sub.add_parser("alerts", help="import or export the Hud alert rules")
    alerts = a.add_subparsers(dest="alerts_cmd", required=True)

    ai = alerts.add_parser("import", help="create or update alerts from a JSON file")
    common(ai)
    ai.add_argument("--in", dest="infile", default=str(ALERT_FILE),
                    help="file to import (default: clickstack-ui/alerts/hud-alerts.json)")
    ai.add_argument("--webhook", required=True, metavar="NAME",
                    help="webhook that receives the alerts, matched by name")
    ai.add_argument("--webhook-url", metavar="URL",
                    help="create that webhook (service generic) when it does not exist, or update its url")
    ai.add_argument("--source-name", default="Hud endpoints",
                    help='source the alerts are attached to; its connection runs them and its database '
                         'holds the Hud SQL tables (default: "Hud endpoints")')
    ai.add_argument("--sql-database", metavar="DATABASE",
                    help="database of the Hud SQL tables (default: the source's database)")
    ai.add_argument("--include-inert", action="store_true",
                    help="also import alerts marked install false (the function rule, until its regex is set)")
    ai.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")

    ae = alerts.add_parser("export", help="save the Hud alerts from a ClickStack")
    common(ae)
    ae.add_argument("--in", dest="infile", default=str(ALERT_FILE),
                    help="names to export come from this file (default: clickstack-ui/alerts/hud-alerts.json)")
    ae.add_argument("--out", default=os.path.join("out", "hud-alerts.json"),
                    help="file to write (default: out/hud-alerts.json)")

    args = p.parse_args(argv)
    if args.cloud:
        missing = [flag for flag, value in (("--organization", args.organization), ("--service", args.service),
                                            ("--key-id or CLICKHOUSE_CLOUD_KEY_ID", args.key_id),
                                            ("--key-secret or CLICKHOUSE_CLOUD_KEY_SECRET", args.key_secret))
                   if not value]
        if missing:
            p.error("--cloud needs " + ", ".join(missing))
    else:
        if not args.url:
            p.error("--url is required (or --cloud for ClickStack on ClickHouse Cloud)")
        if not args.api_key:
            p.error("an API key is required: --api-key or CLICKSTACK_API_KEY")
    try:
        if args.cmd == "export":
            cmd_export(args)
        elif args.cmd == "import":
            cmd_import(args)
        elif args.alerts_cmd == "export":
            cmd_alerts_export(args)
        else:
            cmd_alerts_import(args)
    except ApiError as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    main()
