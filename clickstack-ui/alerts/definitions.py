"""ClickStack equivalents of the OpenSearch alert rules, on the Hud collector's SQL tables.

HyperDX 2.43.1 can alert on an inline raw-SQL line chart. Each alert is one line
chart: its SQL reads the tables hud-migrate creates (hud-otel-collector,
hud-migrate/schema/0002_sql.sql), and every string column besides the timestamp
is a group, so one notification fires per flow or fingerprint. A row is only
returned when every condition of the rule holds; its value is the ratio the
threshold names.

Which tables a rule reads:
  * 5-minute rules read the fine tables (a row per SDK export, kept 30 days): HudFlowErrors through
    endpoint_metrics_union, the view of both; HudLatencySpike only needs durations, so it reads
    endpoint_durations (through the view it would also scan a day of counter rows).
  * hourly rules read the lean hourly rollups (kept 180 days), over complete
    hours only: endpoint_metrics_lean_1h and function_metrics_lean_1h.

Durations are the SDK's exponential buckets, read with hud_duration_quantile,
defined in the query's WITH clause as on the SQL dashboards, so no function is needed on the server.

Run this file to rewrite clickstack-ui/alerts/hud-alerts.json.
"""

import json
import pathlib

FORMAT = "hud-clickstack-alerts/v2"
DATABASE = "hud_otel"
HERE = pathlib.Path(__file__).resolve().parent
JSON_FILE = HERE / "hud-alerts.json"

# The end of the window the evaluator asks for, and the hour it is in. The hourly rules only read hours before it.
END = "toDateTime(fromUnixTimestamp64Milli({endDateMilliseconds:Int64}), 'UTC')"
HOUR = "toStartOfHour(" + END + ")"
# DateTime (not DateTime64) so the alert evaluator treats the column as the bucket time. The stamp is the window
# start: a steady-state evaluation covers exactly one interval, and that is the bucket it checks.
TS = ("toDateTime(toStartOfInterval(fromUnixTimestamp64Milli({startDateMilliseconds:Int64}), "
      "INTERVAL {intervalSeconds:Int64} SECOND), 'UTC') AS ts")

# The function rule matches nothing until this is edited (a whole-name regex, like PromQL's =~).
FUNCTION_REGEX = "__NO_FUNCTION_SELECTED__"

FLOW = "env, service, flow_type, flow, method, route"
FLOW_NO_ROUTE = "env, service, flow_type, flow"
FN = "env, service, function, file"

# [column of the alert, the expression it is]. The names are the OpenSearch rules' labels, so a webhook body or
# message written for those keeps working.
FLOW_COLUMNS = [
    ["job", "service"],
    ["deployment_environment_name", "env"],
    ["code_flow_type", "flow_type"],
    ["code_flow_name", "flow"],
    ["endpoint_method", "method"],
    ["endpoint_path", "if(route = '', flow, route)"],
]
FLOW_FP_COLUMNS = FLOW_COLUMNS + [["issue_fingerprint", "fingerprint"]]
FN_COLUMNS = [
    ["job", "service"],
    ["deployment_environment_name", "env"],
    ["code_function_name", "function"],
    ["code_file_path", "file"],
    ["endpoint_path", "function"],
]

# An issue row has no status code column; framework_result is the response status of an HTTP flow's issue.
NOT_4XX = "NOT match(framework_result, '^4..$')"
P90 = "hud_duration_quantile(0.9, {})"
QUANTILE = """WITH (q, buckets) -> if(empty(buckets), nan,
  arrayFirst((v, c) -> c >= q * arrayCumSum(mapValues(buckets))[-1],
    arrayMap((k, n, c) -> exp2((k + 1 - (c - q * arrayCumSum(mapValues(buckets))[-1]) / n) / 4),
      mapKeys(buckets), mapValues(buckets), arrayCumSum(mapValues(buckets))),
    arrayCumSum(mapValues(buckets)))) AS hud_duration_quantile
"""


def table(name):
    return DATABASE + "." + name


def select(columns, value, source, where):
    """The line-chart query: the bucket time, the group columns, the value."""
    head = [TS] + [expr + " AS " + alias for alias, expr in columns] + [value + " AS value"]
    sql = "SELECT\n  " + ",\n  ".join(head) + "\nFROM (\n" + source + "\n)\nWHERE " + "\n  AND ".join(where) + "\n"
    return (QUANTILE if "hud_duration_quantile(" in sql else "") + sql


def flow_errors():
    """Issues of a fingerprint over the flow's runs (messages for SQS) in [end-20m, end-10m]."""
    window = "t > {end} - INTERVAL 20 MINUTE AND t <= {end} - INTERVAL 10 MINUTE".format(end=END)
    # The view is both fine endpoint tables as one: issue rows carry the fingerprint, run rows the method and route,
    # so a flow's runs, method and route are summed over all of its rows (the window) next to each fingerprint.
    source = (
        "  SELECT " + FLOW_NO_ROUTE + ", fingerprint,\n"
        "    sumIf(issues, " + NOT_4XX + ") AS errors,\n"
        "    sum(sum(if(flow_type = 'sqs', messages, toFloat64(calls)))) OVER flow_rows AS runs,\n"
        "    max(max(method)) OVER flow_rows AS method, max(max(route)) OVER flow_rows AS route\n"
        "  FROM " + table("endpoint_metrics_union") + "\n"
        "  WHERE " + window + "\n"
        "  GROUP BY " + FLOW_NO_ROUTE + ", fingerprint\n"
        "  WINDOW flow_rows AS (PARTITION BY " + FLOW_NO_ROUTE + ")"
    )
    return select(FLOW_FP_COLUMNS, "errors / runs", source, ["errors >= 1", "runs > 0", "errors / runs >= 0.01"])


def degradation(current_hours, reference_hours, current_buckets, reference_buckets, min_calls):
    """The average hourly p90 of the last current_hours against the reference_hours before them."""
    current = "hour >= {h} - INTERVAL {n} HOUR".format(h=HOUR, n=current_hours)
    source = (
        "  SELECT " + FLOW + ",\n"
        "    avgIf(p90, cur) AS cur_avg, stddevPopIf(p90, cur) AS cur_sd, countIf(cur) AS cur_n,\n"
        "    avgIf(p90, NOT cur) AS ref_avg, stddevPopIf(p90, NOT cur) AS ref_sd, countIf(NOT cur) AS ref_n,\n"
        "    sumIf(runs, cur) AS cur_runs, sumIf(runs, NOT cur) AS ref_runs,\n"
        "    " + P90.format("sumMapIf(bk, cur)") + " AS cur_p90\n"
        "  FROM (\n"
        "    SELECT " + FLOW + ", hour, " + current + " AS cur,\n"
        "      sum(duration_count) AS runs, sumMap(buckets) AS bk, " + P90.format("bk") + " AS p90\n"
        "    FROM " + table("endpoint_metrics_lean_1h") + "\n"
        "    WHERE hour >= {h} - INTERVAL {n} HOUR AND hour < {h}\n".format(h=HOUR, n=current_hours + reference_hours)
        + "    GROUP BY " + FLOW + ", hour\n"
        "    HAVING runs > 0\n"
        "  )\n"
        "  GROUP BY " + FLOW
    )
    return select(FLOW_COLUMNS, "cur_avg / ref_avg", source, [
        "cur_avg / ref_avg >= 1.7",
        "cur_n >= " + str(current_buckets),
        "ref_n >= " + str(reference_buckets),
        "cur_sd / cur_avg <= 1",
        "ref_sd / ref_avg <= 1",
        "ref_avg + 2.58 * ref_sd / sqrt(ref_n) < cur_avg - 2.58 * cur_sd / sqrt(cur_n)",
        "cur_p90 >= 0.15",
        "cur_runs >= " + str(min_calls),
        "ref_runs >= " + str(min_calls),
    ])


def latency_spike():
    """The p90 of the last 10 minutes against the 10-minute p90s of the 23h50m before them."""
    source = (
        "  SELECT " + FLOW + ",\n"
        "    " + P90.format("sumMapIf(bk, age = 0)") + " AS cur_p90, sumIf(runs, age = 0) AS cur_runs,\n"
        "    avgIf(p90, age > 0) AS ref_avg, stddevPopIf(p90, age > 0) AS ref_sd, countIf(age > 0) AS ref_n,\n"
        "    sumIf(runs, age > 0) AS ref_runs\n"
        "  FROM (\n"
        "    SELECT " + FLOW + ", intDiv(dateDiff('second', t, " + END + "), 600) AS age,\n"
        "      sum(duration_count) AS runs, sumMap(buckets) AS bk, " + P90.format("bk") + " AS p90\n"
        "    FROM " + table("endpoint_durations") + "\n"
        "    WHERE t > " + END + " - INTERVAL 24 HOUR AND t <= " + END + "\n"
        "    GROUP BY " + FLOW + ", age\n"
        "    HAVING runs > 0\n"
        "  )\n"
        "  GROUP BY " + FLOW
    )
    return select(FLOW_COLUMNS, "cur_p90 / ref_avg", source, [
        "cur_p90 / ref_avg >= 1.7",
        "cur_p90 >= 0.3",
        "ref_n >= 72",
        "ref_sd / ref_avg <= 1",
        "ref_avg + 2.58 * ref_sd / sqrt(ref_n) < cur_p90",
        "cur_runs >= 50",
        "ref_runs >= 50",
    ])


def error_spike():
    """A fingerprint's rate in the last complete hour against the p99 of its hourly rates in the 8 days before."""
    hours = "hour >= {h} - INTERVAL 193 HOUR AND hour < {h}".format(h=HOUR)
    last = "hour = {h} - INTERVAL 1 HOUR".format(h=HOUR)
    source = (
        "  SELECT " + FLOW_NO_ROUTE + ", fingerprint, max(method) AS method, max(route) AS route,\n"
        "    sumIf(errors / runs, " + last + ") AS rate, sumIf(runs, " + last + ") AS last_runs,\n"
        "    quantileExactInclusiveIf(0.99)(errors / runs, NOT (" + last + ") AND runs >= 20) AS baseline,\n"
        "    countIf(NOT (" + last + ") AND runs >= 20) AS baseline_n\n"
        "  FROM (\n"
        "    SELECT " + FLOW_NO_ROUTE + ", fingerprint, hour, sum(issues) AS errors\n"
        "    FROM " + table("endpoint_metrics_lean_1h") + "\n"
        "    WHERE " + hours + " AND issues > 0 AND " + NOT_4XX + "\n"
        "    GROUP BY " + FLOW_NO_ROUTE + ", fingerprint, hour\n"
        "  ) AS i\n"
        "  INNER JOIN (\n"
        "    SELECT " + FLOW_NO_ROUTE + ", hour, max(method) AS method, max(route) AS route,\n"
        "      if(flow_type = 'sqs', sum(messages), toFloat64(sum(duration_count))) AS runs\n"
        "    FROM " + table("endpoint_metrics_lean_1h") + "\n"
        "    WHERE " + hours + "\n"
        "    GROUP BY " + FLOW_NO_ROUTE + ", hour\n"
        "    HAVING runs > 0\n"
        "  ) AS d USING (" + FLOW_NO_ROUTE + ", hour)\n"
        "  GROUP BY " + FLOW_NO_ROUTE + ", fingerprint"
    )
    return select(FLOW_FP_COLUMNS, "rate", source, [
        "rate >= 0.05",
        "baseline_n >= 1",
        "rate > 2 * baseline",
        "last_runs >= 50",
    ])


def function_degradation():
    """A function's p90 of the last 24 hours against its p90 of the 7 days before."""
    current = "hour >= {h} - INTERVAL 24 HOUR".format(h=HOUR)
    source = (
        "  SELECT " + FN + ",\n"
        "    " + P90.format("sumMapIf(buckets, " + current + ")") + " AS cur_p90,\n"
        "    " + P90.format("sumMapIf(buckets, NOT (" + current + "))") + " AS ref_p90,\n"
        "    sumIf(calls, " + current + ") AS cur_calls, sumIf(calls, NOT (" + current + ")) AS ref_calls\n"
        "  FROM " + table("function_metrics_lean_1h") + "\n"
        "  WHERE hour >= {h} - INTERVAL 192 HOUR AND hour < {h}\n".format(h=HOUR)
        + "    AND match(function, '^(?:" + FUNCTION_REGEX + ")$')\n"
        "  GROUP BY " + FN
    )
    return select(FN_COLUMNS, "cur_p90 / ref_p90", source, [
        "cur_p90 / ref_p90 >= 1.7",
        "cur_p90 >= 0.15",
        "cur_calls >= 100",
        "ref_calls >= 100",
    ])


def _alert(name, sql, columns, interval, threshold, severity, summary, description, note,
           install=True, tags=None, gaps=None):
    return {
        "name": name,
        "install": install,
        "interval": interval,
        "threshold": threshold,
        "thresholdType": "above",
        "severity": severity,
        "tags": tags if tags is not None else [severity],
        "summary": summary,
        "description": description,
        "note": note,
        "gaps": gaps or [],
        "columns": columns,
        "sql": sql,
    }


def alerts():
    histogram_gap = (
        "Percentiles come from the SDK's exponential buckets through hud_duration_quantile, "
        "not the AMP native histogram and not Hud's DDSketch."
    )
    hourly_gap = (
        "Reads complete hours of the hourly tables, by the hour an SDK export ends in: "
        "the hour in progress is not counted."
    )
    issue_gap = (
        "An issue row has no status code column: 4xx is told from framework_result, "
        "and the method and path come from the flow's runs."
    )
    return [
        _alert(
            "HudFlowErrors",
            flow_errors(),
            FLOW_FP_COLUMNS,
            "5m",
            0.01,
            "warning",
            "Hud-style errors on {{attributes.job}} {{attributes.code_flow_name}}",
            "Error {{attributes.issue_fingerprint}} rate {{value}} over [now-20m, now-10m] (>=1 error, >=1%).",
            "Both OpenSearch HudFlowErrors rules (non-SQS and SQS) are this one alert: "
            "an SQS flow divides by its messages, every other flow by its runs. "
            "HTTP 4xx are excluded. Value is the error rate; there is no row below 1 error or 1%.",
            gaps=[issue_gap],
        ),
        _alert(
            "HudWeekOverWeekDegradation",
            degradation(168, 168, 84, 84, 350),
            FLOW_COLUMNS,
            "1h",
            1.7,
            "warning",
            "Hud-style week-over-week p90 degradation on {{attributes.job}} {{attributes.code_flow_name}}",
            "Average hourly p90 over the last 7d is {{value}}x the previous 7d "
            "(alert at >= 1.7x, >= 350 invocations per week, >= 84 hourly buckets, p90 >= 150ms).",
            "Same gates as the OpenSearch rule: hourly p90 ratio, bucket counts, sd/avg, "
            "non-overlapping 99% intervals, window p90 and invocation counts. "
            "Evaluated every hour over 14 days of the hourly table.",
            gaps=[histogram_gap, hourly_gap,
                  "No per-entity overrides, exclusion lists, session tags, or issue lifecycle."],
        ),
        _alert(
            "HudDayOverWeekDegradation",
            degradation(24, 168, 12, 84, 100),
            FLOW_COLUMNS,
            "1h",
            1.7,
            "warning",
            "Hud-style day-over-week p90 degradation on {{attributes.job}} {{attributes.code_flow_name}}",
            "Average hourly p90 over the last 24h is {{value}}x the preceding 7 days "
            "(alert at >= 1.7x, >= 100 invocations per window, >= 12 hourly buckets today "
            "and >= 84 in the baseline, p90 >= 150ms).",
            "Scheduled equivalent of Hud's on-demand day-over-week check. Hourly.",
            gaps=[histogram_gap, hourly_gap],
        ),
        _alert(
            "HudLatencySpike",
            latency_spike(),
            FLOW_COLUMNS,
            "5m",
            1.7,
            "warning",
            "Hud-style latency spike on {{attributes.job}} {{attributes.code_flow_name}}",
            "p90 over the last 10m is {{value}}x the average 10m p90 of the previous day "
            "(alert at >= 1.7x, >= 50 invocations per window, p90 >= 300ms).",
            "The window is still 10m against the previous 23h50m, in 10-minute steps back from the evaluation time. "
            "ClickStack has no 10m schedule, so this evaluates every 5m.",
            gaps=[
                histogram_gap,
                "No 10-minute alert interval. The schedule is 5m; the window stays 10m.",
                "Reads a day of the fine table on every evaluation.",
            ],
        ),
        _alert(
            "HudErrorRateSpike",
            error_spike(),
            FLOW_FP_COLUMNS,
            "1h",
            0.05,
            "critical",
            "[alpha] Error rate spike on {{attributes.job}} {{attributes.code_flow_name}}",
            "Error {{attributes.issue_fingerprint}} rate {{value}} over the last hour (>= 5%), "
            "above twice its p99 hourly rate of the previous 8 days (>= 50 invocations).",
            "Alpha, same as the OpenSearch rule. SQS uses message count; other flows use the "
            "run count. A fingerprint with no baseline hour does not fire. "
            "Hours with fewer than 20 invocations are left out of the baseline.",
            tags=["critical", "alpha"],
            gaps=[issue_gap, hourly_gap],
        ),
        _alert(
            "HudFunctionDayOverWeekDegradation",
            function_degradation(),
            FN_COLUMNS,
            "1h",
            1.7,
            "warning",
            "Hud-style function p90 degradation on {{attributes.job}} {{attributes.code_function_name}}",
            "Function p90 over the last 24h is {{value}}x the preceding 7 days "
            "(alert at >= 1.7x, >= 100 calls in both windows, p90 >= 150ms).",
            "Not installed. The function regex is " + FUNCTION_REGEX + ", "
            "which matches nothing. Set that regex to the functions you watch and import "
            "with --include-inert. Function durations are sampled, calls are not.",
            install=False,
            gaps=[
                histogram_gap,
                hourly_gap,
                "Inert until the function regex is edited. Import skips it unless --include-inert.",
            ],
        ),
    ]


def document():
    return {"format": FORMAT, "database": DATABASE, "alerts": alerts()}


def write_json(path=JSON_FILE):
    path.write_text(json.dumps(document(), indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(write_json())
