"""Lambda: oilloop-get-upload-url

Route:   POST /upload-url
Body:    {"content_type": "image/jpeg"}   (or image/png)
Returns: {"upload_url": "...", "key": "uploads/<id>.jpg", "content_type": "image/jpeg"}

Env vars: BUCKET
CORS is configured in API Gateway, so no CORS headers are set here.
"""
import json
import os
import uuid

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BUCKET = os.environ["BUCKET"]

# Use the regional endpoint so the presigned URL points at
# <bucket>.s3.<region>.amazonaws.com (the global endpoint can answer a browser PUT
# with a redirect, which breaks CORS uploads on new buckets).
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

EXT = {"image/jpeg": "jpg", "image/png": "png"}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    content_type = body.get("content_type", "image/jpeg")
    ext = EXT.get(content_type)
    if ext is None:
        return _resp(400, {"error": "content_type must be image/jpeg or image/png"})

    key = f"uploads/{uuid.uuid4().hex}.{ext}"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=300,
    )
    # The browser must PUT the file with the SAME Content-Type header.
    return _resp(200, {"upload_url": url, "key": key, "content_type": content_type})


def _resp(code, payload):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
