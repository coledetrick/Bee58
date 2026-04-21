# Observability

**Location**: Instrumented within Lambda handlers  
**Phase**: 3 (DevOps Differentiator)  
**Service**: AWS CloudWatch (Logs + Metrics + Dashboards)

---

## Overview

Phase 3 adds operational visibility to the deployed system. This is a deliberate differentiator for DevOps/Cloud roles — it demonstrates that you don't just build systems, you instrument them to know when they're broken and why.

Three pillars:
1. **Structured JSON logging** — machine-parseable logs from every Lambda
2. **Custom metrics** — business-level signals, not just Lambda defaults
3. **Dashboard** — unified view of system health

---

## Structured JSON Logging

Replace ad-hoc `print()` / unstructured `logging` calls with JSON-formatted log output. CloudWatch treats each log line as a queryable JSON document when formatted correctly.

### Pattern

```python
import json, logging, time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log(level, event, **kwargs):
    logger.log(level, json.dumps({
        'event': event,
        'timestamp': time.time(),
        **kwargs
    }))
```

### What to log per Lambda

**Upload Lambda**:
```python
log(INFO, 'upload_requested', job_id=job_id)
log(INFO, 'presigned_url_generated', job_id=job_id, expires_in=300)
```

**Analysis Lambda**:
```python
log(INFO, 'analysis_started', job_id=job_id, platform=platform, log_rows=len(df))
log(INFO, 'analysis_complete', job_id=job_id, score=result.score,
    triggered_rules=list(result.flags), duration_ms=elapsed_ms)
log(ERROR, 'analysis_failed', job_id=job_id, error=str(e), exc_info=True)
```

**Results Lambda**:
```python
log(INFO, 'result_fetched', job_id=job_id, status=status)
```

### CloudWatch Logs Insights queries

Once logs are structured, these become one-liners:

```
# All failed analyses in the last 24h
fields job_id, error
| filter event = "analysis_failed"
| sort @timestamp desc

# Average analysis duration
filter event = "analysis_complete"
| stats avg(duration_ms) as avg_ms, max(duration_ms) as max_ms

# Which rules trigger most often
filter event = "analysis_complete"
| stats count(*) by triggered_rules
```

---

## Custom CloudWatch Metrics

Lambda provides default metrics (invocations, errors, duration, throttles). Custom metrics add business-level visibility.

Emit metrics using the CloudWatch Embedded Metric Format (EMF) — structured JSON that CloudWatch automatically parses into metrics without a separate `put_metric_data` API call.

```python
import aws_embedded_metrics as emf

@emf.metric_scope
def handler(event, context, metrics):
    metrics.set_namespace('Bee58Analyzer')
    metrics.set_dimensions({'Environment': os.environ['ENVIRONMENT']})
    
    # ... analysis logic ...
    
    metrics.put_metric('AnalysisCompleted', 1, 'Count')
    metrics.put_metric('AnalysisDurationMs', elapsed_ms, 'Milliseconds')
    metrics.put_metric('EngineScore', result.score, 'None')
    
    for flag in result.flags:
        metrics.put_metric(f'Rule_{flag}', 1, 'Count')
```

### Metric definitions

| Metric | Namespace | Unit | Why |
|---|---|---|---|
| `AnalysisCompleted` | `Bee58Analyzer` | Count | Total volume |
| `AnalysisFailed` | `Bee58Analyzer` | Count | Error rate |
| `AnalysisDurationMs` | `Bee58Analyzer` | Milliseconds | Performance baseline |
| `EngineScore` | `Bee58Analyzer` | None (0-100) | Distribution of log health |
| `Rule_{flag_name}` | `Bee58Analyzer` | Count | Which rules fire most |
| `Platform_MHD` / `Platform_BM3` | `Bee58Analyzer` | Count | User platform split |

### Alarm: high error rate

```hcl
resource "aws_cloudwatch_metric_alarm" "analysis_error_rate" {
  alarm_name          = "bee58-analysis-error-rate-${var.environment}"
  namespace           = "Bee58Analyzer"
  metric_name         = "AnalysisFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_description   = "More than 5 analysis failures in 10 minutes"
}
```

---

## CloudWatch Dashboard

A single dashboard visible in the AWS Console showing system health at a glance.

### Widgets

**Row 1 — Traffic**:
- Upload Lambda invocations (line chart, 1h window)
- Analysis Lambda invocations (line chart)
- Results Lambda invocations (line chart)

**Row 2 — Health**:
- Analysis error rate (`AnalysisFailed / AnalysisCompleted`, %)
- Analysis p50/p95 duration
- Lambda throttle count (from default Lambda metrics)

**Row 3 — Business signals**:
- Engine score distribution (histogram)
- Top triggered rules (bar chart from `Rule_*` metrics)
- Platform split: MHD vs BM3

Define the dashboard as a Terraform resource (`aws_cloudwatch_dashboard`) in the `functions` module. A JSON dashboard body defined in Terraform is reviewable in PRs.

---

## Cost Tagging Impact

All resources are tagged with `Project=bee58-analyzer` and `Environment={env}` (see [infrastructure.md](infrastructure.md)). In AWS Cost Explorer, filter by these tags to see the total monthly cost of the project broken down by environment. This is a useful data point to include in resume discussions about cloud cost management.

---

## Log Retention

By default CloudWatch log groups have indefinite retention. Set retention explicitly to control cost:

```hcl
resource "aws_cloudwatch_log_group" "analysis_lambda" {
  name              = "/aws/lambda/bee58-analysis-${var.environment}"
  retention_in_days = 30
}
```

30 days is sufficient for debugging. Set 7 days in dev, 30 in prod.
