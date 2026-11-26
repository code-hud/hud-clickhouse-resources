# ClickStack dashboards

Ready-made ClickStack (HyperDX) dashboards for data produced by the Hud SDK, and a loader that imports them into your ClickStack.

Commands below run from the repository root.

Hud investigations arrive as OTLP logs (events named `hud.forensic`) and Hud metrics as OTLP delta metrics, stored as a row per SDK export: see [How Hud data arrives in ClickHouse](../README.md#how-hud-data-arrives-in-clickhouse).

| Path | Contents |
| --- | --- |
| `clickstack-ui/dashboards/hud-all-dashboards.json` | All five Hud dashboards ("Hud: 1. Services" to "Hud: 5. Forensic Details") in one export, with the data sources and connection they use. **This is the file to import**: the dashboards link to one another, and importing them together keeps those links working. |
| `clickstack-ui/dashboards/individual-dashboards/` | The same dashboards, one file per dashboard, for reference (reading one dashboard's tiles and queries, or reviewing a change). Not for importing on their own: a dashboard imported alone has links to dashboards that are not there. |
| `clickstack-ui/scripts/dashboard-loader.py` | Imports the dashboards into a ClickStack, adapting them to its data sources, connection and tables, and exports them back to a file. The `alerts` command does the same for the alert rules. |
| `clickstack-ui/alerts/hud-alerts.json` | The Hud alert rules, one inline SQL alert per OpenSearch rule name. **This is the file to import.** `clickstack-ui/alerts/definitions.py` generates it. |
| `clickstack-ui/tests/test_dashboard_loader.py` | Tests for the loader and the dashboard files. |
| `clickstack-ui/tests/test_alert_loader.py` | Tests for the alert file and the alerts import. |

## The dashboards

Every tile is a SQL query of the tables the [Hud collector](https://github.com/code-hud/hud-otel-collector) writes: its ClickStack config inserts the SDK's metrics into ClickHouse, and the tables `hud-migrate` creates sum them per endpoint (flow), machine (session) and function (`hud-migrate/schema/0002_sql.sql`). Duration percentiles define `hud_duration_quantile` in the query's `WITH` clause, so they need no function on the server.

| Dashboard | Shows |
| --- | --- |
| **Hud: 1. Services** | Every service with a session in the selected range, with the columns of the Hud services list: status, live sessions, functions invoked, flows invoked and last seen, plus the error rate. A live session is one that reported in the last 30 minutes; a service is Running while it has at least one. Click a service for its overview. |
| **Hud: 2. Service Overview** | One service, in sections: Metrics (KPIs, live alerts, runs, error rate and duration, then CPU, memory and event loop utilization), Flows, Functions, Forensics and Sessions (with their pod). Every table is full width. Click a flow, function or forensic for its page. |
| **Hud: 3. Flow** | One flow (HTTP endpoint, queue consumer or custom flow): runs, error rate, latency percentiles, status codes, queue latency, the functions it runs, and its exceptions, forensics and full forensic JSON. |
| **Hud: 4. Function** | One function: runs, errors by type, latency, runs by version, its callers and callees, the flows it runs in, and its forensics. |
| **Hud: 5. Forensic Details** | The latest Hud forensic of one error fingerprint, with all its fields. |

Each dashboard has an **Environment** selector, and Service, Flow and Function selectors where they apply, in that order. Each selector lists only the values that go with the ones before it: the services of the selected environment, the flows of the selected services, and the functions of the selected services and flows. Clicking a row opens the next dashboard in the same tab with the clicked item, the selected environment and the time range already selected.

## Prerequisites

- Python 3.10 or later, to run `clickstack-ui/scripts/dashboard-loader.py`. It uses only the standard library.
- ClickStack 2.43.1 or later.
- The Hud collector writing the SDK's metrics and forensics to ClickHouse, and `hud-migrate` run on its database: the `endpoint_*`, `function_*`, `process_metrics` tables, their hourly tables, and the `endpoints` and `functions` tables.
- A ClickStack log source on the logs table (`otel_logs`). The forensics tiles query these logs (`EventName = 'hud.forensic'`).
- Each Hud SDK session reports its environment as the `deployment.environment.name` resource attribute.
- An API key:
  - **Self-hosted ClickStack:** a **personal API key**. In ClickStack, open **Team Settings** > **API & Agents** and copy the **Personal API access key** (not the ingestion key). Pass it with `--api-key` or the `CLICKSTACK_API_KEY` environment variable.
  - **ClickStack on ClickHouse Cloud:** a **ClickHouse Cloud API key**; see [ClickStack on ClickHouse Cloud](#clickstack-on-clickhouse-cloud).

In the export the data sources are named `Hud investigations` (logs, `hud_otel.otel_logs`), `Hud endpoints` (`hud_otel.endpoints`) and `Hud functions` (`hud_otel.functions`), on a connection named `Hud ClickHouse`. Yours can have other names and tables: see below.

## Install the dashboards

### Import

Check what the import would do first:

```bash
export CLICKSTACK_API_KEY=<personal API key>
python3 clickstack-ui/scripts/dashboard-loader.py import --url https://<your clickstack host> --dry-run
```

The loader prints how it maps every source and connection in the file to one of yours, the tables and database it rewrites in the SQL tiles, the environment the dashboards will open with, and whether each dashboard is created or updated. Then run the same command without `--dry-run`.

The selectors read the `Hud endpoints` and `Hud functions` sources over the `endpoints` table (every environment, service and flow seen) and the `functions` table (every function seen, with its flow). The first import creates them in the database of your logs source, where `hud-migrate` creates them next to `otel_logs`; pass `--sql-database DATABASE` if yours are elsewhere. The other Hud tables in the queries follow their database.

| Option | Value |
| --- | --- |
| `--url` | The ClickStack URL you open in the browser. The loader finds the API under it (`/api/api/v2`), or give it the API server's URL (`/api/v2`). |
| `--api-key` | The personal API key (default: `$CLICKSTACK_API_KEY`). |
| `--source OLD=NEW` | Which of your sources replaces a source of the export, by name or id, e.g. `--source "Hud investigations=OTel logs"`. Repeat for each source. Not needed when a source of the same name exists, or when you have only one source of that kind. |
| `--connection OLD=NEW` | The same for the ClickHouse connection. Not needed when the sources you map to share one connection. |
| `--sql-database DATABASE` | The database of `hud-migrate`'s tables, when it is not your logs source's. |
| `--default-environment NAME` | The environment the dashboards open with. Default: `production` if it has sessions reporting, otherwise the environment with the most sessions. |
| `--public-url URL` | The URL people open ClickStack with, if it differs from `--url`. Used in the links the loader prints. |
| `--in FILE` | The file to import (default: `clickstack-ui/dashboards/hud-all-dashboards.json`). |
| `--dry-run` | Show what would change, and change nothing. |
| `--plan-out FILE` | Also write the request bodies to a file, to review them. |

The import is an upsert by dashboard name: a dashboard that already exists is updated in place and keeps its id, so you can run it again, and links people already shared keep working. To upgrade, check out the newer release and run the same command.

When the import is done, open **Dashboards** in ClickStack and open **Hud: 1. Services**.

### ClickStack on ClickHouse Cloud

ClickStack on ClickHouse Cloud (**Observability** in the Cloud console's app switcher) has the same dashboards API under the ClickHouse Cloud API, with a ClickHouse Cloud API key instead of a personal key. Add `--cloud` to the commands above, without `--url` and `--api-key`:

1. **Create the API key.** In ClickStack, open **Team settings** > **API & Agents** and click **Manage API Keys**, or in the Cloud console open your organization's **API keys**. Create a key with the **Service Admin** role (or Org Admin). Copy the **key ID** and the **key secret**; the secret is shown only once.
2. **Find the IDs.** The organization ID is in the API keys page's URL (`console.clickhouse.cloud/organizations/<organization id>/keys`); the service ID is in the service's URL (`console.clickhouse.cloud/services/<service id>/...`), and under **Team settings** > **API & Agents** in ClickStack.
3. **Import:**

   ```bash
   export CLICKHOUSE_CLOUD_KEY_ID=<key id>
   export CLICKHOUSE_CLOUD_KEY_SECRET=<key secret>
   python3 clickstack-ui/scripts/dashboard-loader.py import --cloud \
     --organization <organization id> --service <service id> --dry-run
   ```

| Option | Value |
| --- | --- |
| `--cloud` | Use the ClickHouse Cloud API (`https://api.clickhouse.cloud`). |
| `--organization`, `--service` | The organization and service IDs. |
| `--key-id`, `--key-secret` | The Cloud API key (default: `$CLICKHOUSE_CLOUD_KEY_ID`, `$CLICKHOUSE_CLOUD_KEY_SECRET`). |

The other options are the same. Differences from self-hosted:

- The service's logs source must exist before the import, as on self-hosted. ClickHouse Cloud manages the ClickHouse connection itself, so the loader takes it from those sources.
- The Cloud API has no endpoint the loader can use to see which environments report, so the dashboards open on `production` unless you pass `--default-environment`.
- `export --cloud` works the same way.

### Export

To save dashboards you changed, for example to propose a change to this repository:

```bash
python3 clickstack-ui/scripts/dashboard-loader.py export --url https://<your clickstack host> \
  --public-url http://localhost:8080 --default-environment production \
  --out clickstack-ui/dashboards/hud-all-dashboards.json \
  --split clickstack-ui/dashboards/individual-dashboards
```

The export writes every dashboard whose name starts with `Hud:` (`--name-prefix`), and the sources and connection they use. `--public-url` and `--default-environment` keep the URL and environment of your ClickStack out of the file. Without `--out` it writes `out/hud-all-dashboards.json`.

## Install the alert rules

`clickstack-ui/alerts/hud-alerts.json` is the ClickStack form of the OpenSearch rules in `hud-opensearch-resources` (`HudFlowErrors`, the three latency degradations, `HudErrorRateSpike`, and the inert function rule). The loader upserts by alert name.

The rules read the SQL tables of the [Hud collector](https://github.com/code-hud/hud-otel-collector) (`hud-migrate/schema/0002_sql.sql`), the ones the [dashboards](#the-dashboards) read, and define `hud_duration_quantile` for percentiles in their `WITH` clause. They need no function on the server and no recording rules: the hourly tables are already the hourly aggregates.

Each rule is an inline SQL alert that fires once per flow or fingerprint. Its columns keep the OpenSearch rules' label names (`job`, `code_flow_name`, `issue_fingerprint`, ...), so a webhook body written for those still works.

```bash
export CLICKSTACK_API_KEY=<personal API key>
python3 clickstack-ui/scripts/dashboard-loader.py alerts import --url https://<your clickstack host> \
  --webhook "Hud alerts" --webhook-url https://<where the alerts should go> --dry-run
```

Then run the same command without `--dry-run`. On ClickHouse Cloud, replace `--url` with `--cloud --organization <organization id> --service <service id>`, as for [the dashboards](#clickstack-on-clickhouse-cloud).

### The webhook the alerts are sent to

ClickStack alerts notify through a webhook, so the import needs one to route the alerts to:

- `--webhook NAME --webhook-url URL` creates a Generic webhook with that URL, or updates the URL of an existing Generic one.
- `--webhook NAME` alone uses a webhook you already created in ClickStack: Slack, PagerDuty, incident.io, or a Generic one with headers or its own body.

See [Adding a webhook](https://clickhouse.com/docs/clickstack/features/alerts#add-webhook) and [Alert webhook template variables](https://github.com/ClickHouse/ClickStack/blob/main/docs/alert-webhook-template-variables.md) in the ClickStack docs.

### The rules

The alerts are attached to the `Hud endpoints` source (`--source-name`), which the dashboards' import creates, and read the Hud tables in its database (`--sql-database` if they are elsewhere). `--include-inert` also imports `HudFunctionDayOverWeekDegradation`, once you set its function regex in the file. `alerts export` writes the installed alerts to `out/hud-alerts.json`.

| Alert | Schedule | Threshold (`above`, so `>=`) | Reads | OpenSearch equivalent |
| --- | --- | --- | --- | --- |
| `HudFlowErrors` | 5m | error rate 0.01 | `endpoint_metrics_union` (the view of both fine endpoint tables) | Both `HudFlowErrors` rules (non-SQS and SQS) as one alert. Window [now-20m, now-10m], at least 1 error. |
| `HudWeekOverWeekDegradation` | 1h | p90 ratio 1.7 | `endpoint_metrics_lean_1h` | Same gates: 84 hourly buckets, 350 invocations, p90 >= 150ms. |
| `HudDayOverWeekDegradation` | 1h | p90 ratio 1.7 | `endpoint_metrics_lean_1h` | 12 and 84 hourly buckets, 100 invocations, p90 >= 150ms. |
| `HudLatencySpike` | 5m | p90 ratio 1.7 | `endpoint_durations` | The window stays 10m against the previous 23h50m. ClickStack has no 10m schedule. |
| `HudErrorRateSpike` | 1h | error rate 0.05 | `endpoint_metrics_lean_1h` | Critical, alpha. Twice the p99 of the previous 8 days, SQS split as in `HudFlowErrors`. |
| `HudFunctionDayOverWeekDegradation` | 1h | p90 ratio 1.7 | `function_metrics_lean_1h` | Not imported unless `--include-inert`. Same placeholder regex as the OpenSearch file, which is also not deployed until you set it. |

### How they differ from the OpenSearch rules

- **Hourly rules read complete hours.** An hour is the SDK exports that end in it, so the hour in progress is not counted, and "the last hour" is the last complete one. The 5-minute rules read the fine tables (a row per export) and are not rounded. `HudLatencySpike` reads `endpoint_durations` and not the view, which would add a day of counter rows to every evaluation.
- **Percentiles come from the SDK's exponential buckets** through `hud_duration_quantile`, not the AMP native histogram and not Hud's DDSketch.
- **An issue has no status code column.** HTTP 4xx issues are left out by `framework_result`, the response status of an HTTP flow's issue, and an issue's method and path are its flow's.
- **No grouping or repeat interval.** HyperDX notifies every firing group on every evaluation (every 5 minutes for `HudFlowErrors`), and sends a resolve notification when a group stops returning a row.
- **No 10-minute schedule.** `HudLatencySpike` evaluates every 5m over a 10m window, and reads a day of the fine table each time.
- **A missed evaluation can land the value on the oldest bucket of the backfill.** The row time is the window start. A run that covers exactly one interval matches the bucket the evaluator checks.
- **No per-entity overrides, exclusion lists, session tags, or issue lifecycle.** Same as the OpenSearch rules.
- **A forensics saved search is not an error-rate alert.** `hud.forensic` error events are sampled (at most one forensic per issue per 6 hours), so these rules do not use one.
- **The function rule is inert** until the regex is set. Function durations are sampled, so its p90 is of the sampled calls; its call counts are of all calls.

The **Live alerts** table on **Hud: 2. Service Overview** is unchanged: it is still a query that runs when the dashboard loads (flows with more than 5% 5xx in the last 5 minutes), not one of these alerts.

## Known limitations

- **Column widths.** ClickStack splits a table's width equally between its columns and does not save widths you drag, so long flow and function names can be cut off.
- **One environment at a time.** The Environment selector takes exactly one value.
- **The environment is not remembered.** ClickStack has no per-user saved selection: a dashboard opened from the list starts on the environment it was imported with. Moving between the dashboards by clicking rows keeps the selected environment.
- **Selector values ignore the time range.** The Service, Flow and Function selectors list every matching value the labels table has, also values that have no data in the selected range. The Fingerprint selector of **Hud: 5. Forensic Details** does follow the range: widen it if the list is empty.
- **Forensic Details looks back 7 days.** The forensics tables on the other dashboards follow the selected time range, so on a short range they can be empty: a forensic is recorded once per fingerprint. **Hud: 5. Forensic Details** shows the latest forensic of the last 7 days, whatever the selected range, so a link from an issue still finds it.
- **No "mapped" columns.** The metrics do not carry the SDK's static map of a service, so the Services table shows the functions and flows invoked in the selected range, not Hud's Functions mapped and Flows mapped.
- **The `Env` column** (and `Service` or `Flow` in some tables) is there because a row link can only pass values that are in the row.
- **Live alerts on the dashboard are not these rules.** The **Live alerts** table of **Hud: 2. Service Overview** is computed when the dashboard loads (flows with more than 5% 5xx responses in the last 5 minutes). The background rules are [Install the alert rules](#install-the-alert-rules).

## How the dashboards count

- **Totals count every SDK export that ends in the selected range.** Ranges that start within the last 30 days read
  the hourly tables for the whole hours and the fine tables (a row per export) for the partial first and last hour;
  older ranges read the hourly tables, so they start at the hour. Tiles read the `_lean_1h` hourly tables
  (kept 180 days), except the ones by session, caller, error type or version, which read the full `_1h` ones (kept 90 days).
- **Week-over-week compares complete hours**: the hours of the range that have ended
  against the same hours a week earlier.
- **Line charts follow the selected granularity, but never finer than the data**: 5 minutes, the SDK's default export
  interval, in the last 30 days, and an hour before. Rates are a bucket's count divided by the time it covers (the last bucket ends with the data, at the range
  end or now), not a 10-minute `rate()`.
- **"Top 10" picks the top 10 of the whole range**, not of every point, so the lines don't come and go.
- **Flows and functions are narrowed by environment too**: every row of the Hud tables has the environment.
- **The Flow dashboard has a "By feature flag" table**: runs, error rate, latency and issues per combination of the
  feature flags the flow's runs set, from the endpoint tables' `feature_flags` column.

## Development

`python3 -m unittest discover -s clickstack-ui/tests -v` runs the tests: checks of the dashboard file (links, sources, selectors, SQL-only tiles, portable content, individual files in sync with `hud-all-dashboards.json`), the alert file, and import/export runs against an in-memory mock of the ClickStack API. The `dashboard-loader-tests` workflow runs them on every pull request and push to `main`, on Python 3.10 (the minimum) and 3.14; its two jobs, `test (3.10)` and `test (3.14)`, are required checks on `main`. To run it by hand, use **Run workflow** on the workflow's page in the Actions tab. To regenerate the alert file after editing `clickstack-ui/alerts/definitions.py`, run `python3 clickstack-ui/alerts/definitions.py`.
