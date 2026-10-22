"""Create the local S3 bucket and merge loopback CORS origins."""

import json
import os
from urllib.parse import urlparse

import boto3
import requests
from botocore.exceptions import ClientError


endpoint = os.environ["AWS_S3_ENDPOINT_URL"]
bucket = os.environ["AWS_S3_BUCKET_NAME"]
origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
legacy_origins = {"http://localhost:3000", "http://127.0.0.1:3000"}

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    config=boto3.session.Config(signature_version="s3v4"),
)

try:
    client.head_bucket(Bucket=bucket)
except ClientError as error:
    if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
        raise
    client.create_bucket(Bucket=bucket)
    print(f"Created local S3 bucket {bucket}.")

bucket_cors_supported = True
try:
    try:
        rules = client.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in (
            "NoSuchCORSConfiguration",
            "404",
        ):
            raise
        rules = []

    reconciled_rules = []
    for rule in rules:
        allowed_origins = []
        for origin in rule.get("AllowedOrigins", []):
            if origin in legacy_origins or origin in allowed_origins:
                continue
            allowed_origins.append(origin)
        if allowed_origins:
            updated_rule = dict(rule)
            updated_rule["AllowedOrigins"] = allowed_origins
            reconciled_rules.append(updated_rule)

    existing_origins = {
        origin
        for rule in reconciled_rules
        for origin in rule.get("AllowedOrigins", [])
    }
    missing_origins = [origin for origin in origins if origin not in existing_origins]
    if missing_origins:
        reconciled_rules.append(
            {
                "AllowedOrigins": missing_origins,
                "AllowedMethods": ["GET", "HEAD", "POST", "PUT"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3000,
            }
        )
    if reconciled_rules != rules:
        client.put_bucket_cors(
            Bucket=bucket, CORSConfiguration={"CORSRules": reconciled_rules}
        )
        print("Reconciled loopback origins in local S3 CORS.")
    elif not reconciled_rules:
        client.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": origins,
                        "AllowedMethods": ["GET", "HEAD", "POST", "PUT"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )
        print("Configured local S3 CORS.")
    else:
        print("Local S3 bucket CORS already configured.")
except (ClientError, NotImplementedError) as error:
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    if not isinstance(error, NotImplementedError) and code not in (
        "NotImplemented",
        "XMinioServerNotInitialized",
    ):
        raise
    bucket_cors_supported = False
    print("Silo does not support bucket CORS; using server-level CORS.")

if origins:
    parsed = urlparse(endpoint)
    probe_url = f"{endpoint.rstrip('/')}/{bucket}/plane-cors-probe"
    preflight = requests.options(
        probe_url,
        headers={
            "Origin": origins[0],
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=5,
    )
    allowed_origin = preflight.headers.get("Access-Control-Allow-Origin")
    if allowed_origin not in (origins[0], "*"):
        raise SystemExit(
            f"CORS preflight failed for {origins[0]} (bucket support={bucket_cors_supported})."
        )
    print(f"Verified server CORS for {origins[0]} via OPTIONS.")
    for legacy_origin in sorted(legacy_origins):
        legacy_preflight = requests.options(
            probe_url,
            headers={
                "Origin": legacy_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=5,
        )
        legacy_allowed_origin = legacy_preflight.headers.get(
            "Access-Control-Allow-Origin"
        )
        if legacy_allowed_origin in (legacy_origin, "*"):
            raise SystemExit(f"CORS preflight still allows legacy origin {legacy_origin}.")
    print("Verified legacy 3000 CORS origins are rejected.")

print(json.dumps({"bucket": bucket, "endpoint": endpoint, "origins": origins}))
