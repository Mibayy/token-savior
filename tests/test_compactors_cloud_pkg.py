"""Tests for F1b cloud/package compactors (kubectl, aws, npm/pip, curl)."""
from __future__ import annotations

import json

from token_savior.compactors import compact, registry
from token_savior.compactors.aws import _unwrap_ddb


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_registry_includes_f1b_compactors():
    names = {c.__class__.__name__ for c in registry}
    expected = {
        "KubectlGetCompactor",
        "KubectlLogsCompactor",
        "AwsStsIdentityCompactor",
        "AwsEc2DescribeInstancesCompactor",
        "AwsLambdaListFunctionsCompactor",
        "AwsLogsGetLogEventsCompactor",
        "AwsIamListRolesCompactor",
        "AwsDynamoDbScanCompactor",
        "AwsS3LsCompactor",
        "NpmListCompactor",
        "PipListCompactor",
        "CurlCompactor",
    }
    missing = expected - names
    assert not missing, f"missing compactors: {missing}"


# ---------------------------------------------------------------------------
# kubectl get pods
# ---------------------------------------------------------------------------


KUBECTL_GET_PODS = """NAME                                       READY   STATUS    RESTARTS   AGE
api-gateway-7d9c8f5b6c-abcde               1/1     Running   0          17h
api-gateway-7d9c8f5b6c-fghij               1/1     Running   2          3d
worker-deployment-5c7f9d8b6a-klmno         1/1     Running   0          5d
worker-deployment-5c7f9d8b6a-pqrst         0/1     CrashLoopBackOff   12  41m
postgres-statefulset-0                     1/1     Running   0          14d
redis-master-0                             1/1     Running   1          14d
ingress-nginx-controller-xy123             1/1     Running   0          14d
"""


def test_kubectl_get_pods_keeps_status_age_drops_ready_restarts():
    r = compact("kubectl get pods", KUBECTL_GET_PODS)
    assert r is not None
    assert "STATUS" in r.text and "AGE" in r.text
    assert "READY" not in r.text
    assert "RESTARTS" not in r.text
    assert "CrashLoopBackOff" in r.text
    assert "postgres-statefulset-0" in r.text
    assert r.savings_pct >= 30.0


KUBECTL_GET_SVC = """NAME                  TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)         AGE
kubernetes            ClusterIP   10.0.0.1        <none>        443/TCP         30d
api-gateway           ClusterIP   10.0.32.45      <none>        8080/TCP        17h
postgres              ClusterIP   10.0.18.99      <none>        5432/TCP        14d
ingress-nginx         LoadBalancer 10.0.55.20    34.122.5.6    80:30080/TCP    14d
"""


def test_kubectl_get_services_keeps_cluster_ip():
    r = compact("kubectl get services", KUBECTL_GET_SVC)
    assert r is not None
    assert "CLUSTER-IP" in r.text
    assert "EXTERNAL-IP" not in r.text
    assert "PORT(S)" not in r.text
    assert "10.0.32.45" in r.text


KUBECTL_LOGS = """2024-01-15T10:00:01Z INFO  starting api server
2024-01-15T10:00:02Z INFO  connected to postgres
2024-01-15T10:00:03Z INFO  request handled GET /health
2024-01-15T10:00:04Z INFO  request handled GET /health
2024-01-15T10:00:05Z INFO  request handled GET /health
2024-01-15T10:00:06Z INFO  request handled GET /health
2024-01-15T10:00:07Z INFO  request handled GET /health
2024-01-15T10:00:08Z ERROR db connection lost
2024-01-15T10:00:09Z INFO  reconnecting to postgres
2024-01-15T10:00:10Z INFO  reconnecting to postgres
2024-01-15T10:00:11Z INFO  reconnecting to postgres
2024-01-15T10:00:12Z INFO  reconnected
"""


def test_kubectl_logs_dedupes_repeated_lines():
    r = compact("kubectl logs api-gateway-7d9c8f5b6c-abcde", KUBECTL_LOGS)
    assert r is not None
    assert "(x5)" in r.text  # /health spam
    assert "(x3)" in r.text  # reconnecting spam
    assert "db connection lost" in r.text
    assert r.savings_pct >= 30.0


# ---------------------------------------------------------------------------
# aws sts get-caller-identity
# ---------------------------------------------------------------------------


AWS_STS = json.dumps(
    {
        "UserId": "AIDA1234567890EXAMPLE",
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/louis",
    },
    indent=4,
)


def test_aws_sts_oneline():
    r = compact("aws sts get-caller-identity", AWS_STS)
    assert r is not None
    assert "account=123456789012" in r.text
    assert "arn=arn:aws:iam::123456789012:user/louis" in r.text
    assert r.savings_pct >= 25.0


# ---------------------------------------------------------------------------
# aws ec2 describe-instances
# ---------------------------------------------------------------------------


AWS_EC2 = json.dumps(
    {
        "Reservations": [
            {
                "ReservationId": "r-0123",
                "OwnerId": "123456789012",
                "Instances": [
                    {
                        "InstanceId": "i-0a1b2c3d4e5f6g7h8",
                        "InstanceType": "t3.medium",
                        "LaunchTime": "2024-01-10T08:00:00.000Z",
                        "State": {"Code": 16, "Name": "running"},
                        "Tags": [
                            {"Key": "Name", "Value": "web-prod-1"},
                            {"Key": "Env", "Value": "prod"},
                            {"Key": "CostCenter", "Value": "engineering"},
                        ],
                        "SecurityGroups": [
                            {"GroupId": "sg-0abc", "GroupName": "web-sg"},
                        ],
                        "BlockDeviceMappings": [
                            {
                                "DeviceName": "/dev/sda1",
                                "Ebs": {"VolumeId": "vol-aaa", "Status": "attached"},
                            }
                        ],
                        "NetworkInterfaces": [
                            {"NetworkInterfaceId": "eni-aaa", "PrivateIpAddress": "10.0.0.5"}
                        ],
                    },
                    {
                        "InstanceId": "i-1234567890abcdef0",
                        "InstanceType": "t3.large",
                        "LaunchTime": "2024-02-01T12:00:00.000Z",
                        "State": {"Code": 80, "Name": "stopped"},
                        "Tags": [{"Key": "Name", "Value": "worker-staging"}],
                        "SecurityGroups": [],
                        "BlockDeviceMappings": [],
                    },
                ],
            }
        ]
    },
    indent=4,
)


def test_aws_ec2_describe_instances_table():
    r = compact("aws ec2 describe-instances", AWS_EC2)
    assert r is not None
    assert "i-0a1b2c3d4e5f6g7h8" in r.text
    assert "running" in r.text
    assert "t3.medium" in r.text
    assert "web-prod-1" in r.text
    # Bulky fields dropped
    assert "BlockDeviceMappings" not in r.text
    assert "SecurityGroups" not in r.text
    assert "sg-0abc" not in r.text
    assert r.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# aws lambda list-functions
# ---------------------------------------------------------------------------


AWS_LAMBDA = json.dumps(
    {
        "Functions": [
            {
                "FunctionName": "image-resizer",
                "FunctionArn": "arn:aws:lambda:us-east-1:123:function:image-resizer",
                "Runtime": "python3.11",
                "Role": "arn:aws:iam::123:role/lambda-exec",
                "Handler": "lambda_function.lambda_handler",
                "CodeSize": 4567890,
                "MemorySize": 512,
                "Timeout": 30,
                "LastModified": "2024-01-15T10:00:00.000+0000",
                "Environment": {
                    "Variables": {
                        "BUCKET_NAME": "images-prod",
                        "REGION": "us-east-1",
                        "DEBUG": "false",
                        "API_KEY": "secret-value",
                    }
                },
                "Layers": [
                    {"Arn": "arn:aws:lambda:us-east-1:123:layer:pillow:1", "CodeSize": 12345678},
                    {"Arn": "arn:aws:lambda:us-east-1:123:layer:numpy:3", "CodeSize": 9876543},
                ],
            },
            {
                "FunctionName": "order-processor",
                "Runtime": "nodejs20.x",
                "MemorySize": 256,
                "LastModified": "2024-02-01T08:30:00.000+0000",
                "Environment": {"Variables": {"DB_URL": "postgres://x"}},
            },
        ]
    },
    indent=4,
)


def test_aws_lambda_list_functions_drops_env_layers():
    r = compact("aws lambda list-functions", AWS_LAMBDA)
    assert r is not None
    assert "image-resizer" in r.text
    assert "python3.11" in r.text
    assert "512" in r.text
    assert "BUCKET_NAME" not in r.text
    assert "secret-value" not in r.text
    assert "pillow" not in r.text
    assert r.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# aws logs get-log-events
# ---------------------------------------------------------------------------


AWS_LOGS = json.dumps(
    {
        "events": [
            {"timestamp": 1705320000000, "message": "START RequestId: abc\n", "ingestionTime": 1705320001000},
            {"timestamp": 1705320000500, "message": "processing event\n", "ingestionTime": 1705320001500},
            {"timestamp": 1705320001000, "message": "END RequestId: abc\n", "ingestionTime": 1705320002000},
        ],
        "nextForwardToken": "f/12345",
        "nextBackwardToken": "b/12345",
    },
    indent=4,
)


def test_aws_logs_get_log_events_keeps_timestamp_message():
    r = compact("aws logs get-log-events --log-group /aws/lambda/foo --log-stream foo", AWS_LOGS)
    assert r is not None
    assert "processing event" in r.text
    assert "1705320000000" in r.text
    assert "ingestionTime" not in r.text
    assert "nextForwardToken" not in r.text


# ---------------------------------------------------------------------------
# aws iam list-roles
# ---------------------------------------------------------------------------


AWS_IAM = json.dumps(
    {
        "Roles": [
            {
                "RoleName": "lambda-execution-role",
                "Arn": "arn:aws:iam::123456789012:role/lambda-execution-role",
                "Path": "/",
                "RoleId": "AROAEXAMPLE1",
                "CreateDate": "2023-01-15T10:00:00+00:00",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Description": "Execution role for Lambda functions",
            },
            {
                "RoleName": "ecs-task-role",
                "Arn": "arn:aws:iam::123456789012:role/ecs-task-role",
                "CreateDate": "2023-05-20T08:00:00+00:00",
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": []},
            },
        ]
    },
    indent=4,
)


def test_aws_iam_list_roles_drops_policy_docs():
    r = compact("aws iam list-roles", AWS_IAM)
    assert r is not None
    assert "lambda-execution-role" in r.text
    assert "ecs-task-role" in r.text
    assert "AssumeRolePolicyDocument" not in r.text
    assert "sts:AssumeRole" not in r.text
    assert r.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# aws dynamodb scan
# ---------------------------------------------------------------------------


AWS_DDB = json.dumps(
    {
        "Items": [
            {
                "user_id": {"S": "u-42"},
                "email": {"S": "louis@example.com"},
                "age": {"N": "34"},
                "active": {"BOOL": True},
                "deleted": {"NULL": True},
                "tags": {"L": [{"S": "admin"}, {"S": "beta"}]},
                "settings": {"M": {"theme": {"S": "dark"}, "limit": {"N": "100"}}},
            },
            {
                "user_id": {"S": "u-43"},
                "email": {"S": "alice@example.com"},
                "age": {"N": "29"},
                "active": {"BOOL": False},
            },
        ],
        "Count": 2,
        "ScannedCount": 2,
        "ConsumedCapacity": None,
    },
    indent=4,
)


def test_aws_ddb_scan_unwraps_type_annotations():
    r = compact("aws dynamodb scan --table-name users", AWS_DDB)
    assert r is not None
    parsed = json.loads(r.text)
    items = parsed["Items"]
    assert items[0]["user_id"] == "u-42"
    assert items[0]["age"] == 34
    assert items[0]["active"] is True
    assert items[0]["deleted"] is None
    assert items[0]["tags"] == ["admin", "beta"]
    assert items[0]["settings"] == {"theme": "dark", "limit": 100}
    assert r.savings_pct >= 30.0


def test_unwrap_ddb_passthrough_for_plain_values():
    assert _unwrap_ddb("foo") == "foo"
    assert _unwrap_ddb(42) == 42
    assert _unwrap_ddb({"plain": "value"}) == {"plain": "value"}


# ---------------------------------------------------------------------------
# aws s3 ls (long listing -> head/tail)
# ---------------------------------------------------------------------------


def _s3_lines(n: int) -> str:
    return "\n".join(
        f"2024-01-15 10:00:{i:02d}     1234 file-{i:04d}.json" for i in range(n)
    )


def test_aws_s3_ls_short_passthrough():
    short = _s3_lines(10)
    r = compact("aws s3 ls s3://bucket/", short)
    assert r is not None
    assert "file-0000.json" in r.text
    assert "skipped" not in r.text


def test_aws_s3_ls_long_truncates():
    long = _s3_lines(200)
    r = compact("aws s3 ls s3://bucket/", long)
    assert r is not None
    assert "file-0000.json" in r.text  # head kept
    assert "file-0199.json" in r.text  # tail kept (last second indexes vary; just check end region)
    assert "skipped" in r.text


# ---------------------------------------------------------------------------
# npm/yarn/pnpm list
# ---------------------------------------------------------------------------


NPM_LIST = """myapp@1.0.0 /home/louis/myapp
├── @types/node@20.10.5
├── express@4.18.2
│   ├── accepts@1.3.8
│   │   ├── mime-types@2.1.35
│   │   └── negotiator@0.6.3
│   ├── array-flatten@1.1.1
│   ├── body-parser@1.20.1
│   │   ├── bytes@3.1.2
│   │   ├── content-type@1.0.5
│   │   ├── debug@2.6.9
│   │   │   └── ms@2.0.0
│   │   └── on-finished@2.4.1
│   └── cookie@0.5.0
├── react@18.2.0-canary.1
│   └── loose-envify@1.4.0
│       └── js-tokens@4.0.0
└── typescript@5.3.3 (extraneous)
"""


def test_npm_list_collapses_to_toplevel():
    r = compact("npm list", NPM_LIST)
    assert r is not None
    assert "myapp@1.0.0" in r.text
    # Top-level kept
    assert "express@4.18" in r.text
    assert "react@18.2" in r.text
    # Nested deps dropped
    assert "accepts" not in r.text
    assert "mime-types" not in r.text
    assert "loose-envify" not in r.text
    # Patch + suffixes trimmed
    assert "18.2.0-canary" not in r.text
    assert r.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# pip list
# ---------------------------------------------------------------------------


PIP_LIST = """DEPRECATION: Loading egg at /usr/lib/python3/dist-packages/foo-1.0-py3.10.egg is deprecated
WARNING: You are using pip version 23.0; however, version 24.0 is available.
You should consider upgrading via the '/usr/bin/python -m pip install --upgrade pip' command.
Package           Version
----------------- -------
anyio             4.2.0
boto3             1.34.20
click             8.1.7
fastapi           0.109.0
numpy             1.26.3
pandas            2.1.4
pydantic          2.5.3
pytest            7.4.4
requests          2.31.0
sqlalchemy        2.0.25
"""


def test_pip_list_drops_deprecation():
    r = compact("pip list", PIP_LIST)
    assert r is not None
    assert "DEPRECATION" not in r.text
    assert "WARNING" not in r.text
    assert "upgrade pip" not in r.text
    assert "fastapi" in r.text
    assert "Package" in r.text  # header retained


# ---------------------------------------------------------------------------
# curl
# ---------------------------------------------------------------------------


def test_curl_small_body_unchanged():
    small = '{"ok": true}'
    r = compact("curl https://api.example.com/health", small)
    assert r is not None
    assert "ok" in r.text


def test_curl_truncates_large_body():
    large = "x" * 10_000
    r = compact("curl https://api.example.com/big", large)
    assert r is not None
    assert "truncated" in r.text
    assert len(r.text) < 4000
    assert r.savings_pct >= 60.0


def test_curl_keeps_status_and_content_type_with_headers():
    response = (
        "HTTP/1.1 200 OK\n"
        "Date: Mon, 19 May 2026 10:00:00 GMT\n"
        "Content-Type: application/json\n"
        "Content-Length: 28\n"
        "Server: nginx\n"
        "\n"
        '{"status":"ok","count":42}\n'
    )
    r = compact("curl -i https://api.example.com/users", response)
    assert r is not None
    assert "HTTP/1.1 200" in r.text
    assert "Content-Type: application/json" in r.text
    assert '"status":"ok"' in r.text
    # Other headers dropped
    assert "Server: nginx" not in r.text


def test_curl_strips_write_out_boilerplate():
    body = (
        '{"data": [1,2,3]}\n'
        "http_code: 200\n"
        "time_total: 0.123\n"
        "size_download: 1234\n"
    )
    r = compact("curl -w 'http_code: %{http_code}' https://x.example/", body)
    assert r is not None
    assert "data" in r.text
    assert "http_code" not in r.text
    assert "time_total" not in r.text
