# hud-clickhouse-resources

ClickHouse resources for data produced by the Hud SDK: dashboards for ClickStack (HyperDX), the UI on top of ClickHouse.

| Directory | Contents |
| --- | --- |
| [`clickstack-ui/`](clickstack-ui/README.md) | ClickStack (HyperDX) dashboards, the loader that imports them into your ClickStack and exports them back, and the loader's tests. |

```
.
├── README.md
└── clickstack-ui/
    ├── README.md
    ├── dashboards/
    │   ├── hud-all-dashboards.json        # all dashboards in one export: the file to import
    │   └── individual-dashboards/         # one export per dashboard, for reference
    ├── scripts/
    │   └── dashboard-loader.py
    └── tests/
        └── test_dashboard_loader.py
```

## How Hud data arrives in ClickHouse

The Hud SDK exports OTLP to the [Hud collector](https://github.com/code-hud/hud-otel-collector), which writes two kinds of data. They are stored differently, and the dashboards and alert rules in this repository read them differently:

| Hud data | Sent as | Where it lands (database `hud_otel` by default) | How to read it |
| --- | --- | --- | --- |
| **Investigations** (forensics: error, duration and baseline investigations) | **OTLP logs.** One log record per investigation. Each is an OpenTelemetry event: a log record with an event name, `EventName = 'hud.forensic'`, and the flow and issue details in `LogAttributes`. | `otel_logs` | The `Hud investigations` log source: search it in ClickStack, or filter `otel_logs` on `EventName = 'hud.forensic'`. |
| **Metrics** (flows, functions and process) | **OTLP metrics**, as deltas: each SDK export holds the counts and durations of its interval only. | The `endpoint_*`, `function_*` and `process_metrics` tables, one row per export, and their hourly `_1h` tables. | SQL: `sum` the rows of the time range, as the dashboards and the alert rules do. |

The tables and their columns are described in the collector's [README](https://github.com/code-hud/hud-otel-collector#clickhouse) and in the table and column comments `hud-migrate` creates.

Commands in the guide run from the repository root.

## License

Copyright Hud. Licensed under the [Apache License, Version 2.0](LICENSE).
