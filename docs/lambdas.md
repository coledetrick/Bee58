# Lambda Functions

**Location**: `lambdas/`  
**Phase**: 2 (Cloud Architecture)

---

## Overview

Three Lambda functions implement the async pipeline. Each has a narrow, single responsibility. No function talks directly to another — they communicate through S3 events and DynamoDB state.

```
lambdas/
├── upload/     # POST /upload — returns job_id + presigned S3 URL
├── analysis/   # S3-triggered — runs engine, writes result to DynamoDB
└── results/    # GET /result/{job_id} — reads DynamoDB, returns status + report
```

---

## Upload Lambda

**Trigger**: `POST /upload` via API Gateway  
**Responsibility**: Generate a `job_id`, create a presigned S3 PUT URL, return both to the frontend. Does not receive the file.

### Handler logic

```python
import uuid, boto3, os

def handler(event, context):
    job_id = str(uuid.uuid4())
    s3 = boto3.client('s3')
    presigned_url = s3.generate_presigned_url(
        'put_object',
        Params={'Bucket': os.environ['LOG_BUCKET'], 'Key': f'raw/{job_id}.csv'},
        ExpiresIn=300,
        HttpMethod='PUT'
    )
    return {
        'statusCode': 200,
        'body': json.dumps({'job_id': job_id, 'upload_url': presigned_url})
    }
```

### Notes

- The presigned URL must be `PUT`, not `POST` — standard S3 direct upload.
- Key path `raw/{job_id}.csv` — the Analysis Lambda uses this prefix to find the file.
- 5-minute expiry is sufficient; if the user takes longer than 5 minutes to upload, issue a new URL.
- IAM role needs only: `s3:PutObject` on `LOG_BUCKET/raw/*`.

---

## Analysis Lambda

**Trigger**: S3 `ObjectCreated` event on `raw/` prefix  
**Responsibility**: Download the log from S3, run the diagnostic engine, write result to DynamoDB.

### Handler logic

```python
def handler(event, context):
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']
    job_id = key.split('/')[-1].replace('.csv', '')

    # Download log
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket=bucket, Key=key)
    df = pd.read_csv(obj['Body'])

    # Run engine
    engine = B58DiagnosticEngine(df)
    result = engine.analyze()

    # Idempotent write
    dynamo = boto3.resource('dynamodb').Table(os.environ['RESULTS_TABLE'])
    dynamo.put_item(
        Item={
            'job_id': job_id,
            'status': 'complete',
            'report': result.model_dump(),
            'triggered_rules': list(result.flags),
            'platform': result.platform,
            'created_at': int(time.time()),
            'ttl': int(time.time()) + 86400
        },
        ConditionExpression='attribute_not_exists(job_id)'
    )
```

### Idempotency (required)

S3 event notifications are at-least-once delivery. A retried event must not overwrite a completed result. The `ConditionExpression='attribute_not_exists(job_id)'` write ensures the first write wins and subsequent retries are silently rejected.

On `ConditionalCheckFailedException`, log the job_id and return — this is expected behavior, not an error.

### Failure handling

On any exception during analysis:
1. Catch the exception
2. Write `status: 'failed'` + `error_message: str(e)` to DynamoDB (also with `attribute_not_exists` guard)
3. Re-raise so Lambda marks the invocation as failed (enables dead-letter queue visibility)

Never leave a job in DynamoDB without a terminal status (`complete` or `failed`). The polling frontend will hang indefinitely on a missing record.

### IAM role needs

- `s3:GetObject` on `LOG_BUCKET/raw/*`
- `dynamodb:PutItem` on `RESULTS_TABLE`

---

## Results Lambda

**Trigger**: `GET /result/{job_id}` via API Gateway  
**Responsibility**: Read DynamoDB and return the current job status.

### Handler logic

```python
def handler(event, context):
    job_id = event['pathParameters']['job_id']
    dynamo = boto3.resource('dynamodb').Table(os.environ['RESULTS_TABLE'])
    response = dynamo.get_item(Key={'job_id': job_id})

    if 'Item' not in response:
        return {'statusCode': 200, 'body': json.dumps({'status': 'processing'})}

    item = response['Item']
    return {'statusCode': 200, 'body': json.dumps({
        'status': item['status'],
        'report': item.get('report'),
        'error_message': item.get('error_message')
    })}
```

### Status model

Return one of three explicit statuses — never make the frontend infer state:

| `status` | Meaning |
|---|---|
| `processing` | Record not yet in DynamoDB (analysis still running) |
| `complete` | Analysis finished; `report` is populated |
| `failed` | Analysis threw an exception; `error_message` is set |

### IAM role needs

- `dynamodb:GetItem` on `RESULTS_TABLE`

---

## Shared Concerns

### Environment variables (all Lambdas)

| Variable | Used by |
|---|---|
| `LOG_BUCKET` | Upload, Analysis |
| `RESULTS_TABLE` | Analysis, Results |

Set via Terraform `aws_lambda_function.environment`. Never hardcode.

### Packaging

Each Lambda is a separate deployment package. Shared code (the engine package) is a Lambda Layer — one layer, three functions reference it. Avoids duplicating the engine in each zip.

Lambda layer structure (install the `bee58` package into the layer):
```
layer/
└── python/
    └── bee58/
        └── engine/
            ├── __init__.py
            ├── rules.py
            ├── models.py
            └── thresholds.py
```

### Timeouts

| Lambda | Recommended timeout |
|---|---|
| Upload | 5s |
| Analysis | 30s (engine + DynamoDB write) |
| Results | 5s |

Set via Terraform. The Analysis Lambda reserved concurrency cap (cost protection) also lives in Terraform — see [infrastructure.md](infrastructure.md).
