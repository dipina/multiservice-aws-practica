#!/usr/bin/env python3
import os
import shelve
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DB_PATH = "aws_resources.db"

session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
sqs = session.client("sqs")
dynamodb = session.client("dynamodb")
lambda_client = session.client("lambda")

def empty_bucket(bucket, region=REGION):
    s3r = boto3.client("s3", region_name=region)
    # versions/delete markers
    try:
        paginator = s3r.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objs = []
            for v in page.get("Versions", []):
                objs.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for m in page.get("DeleteMarkers", []):
                objs.append({"Key": m["Key"], "VersionId": m["VersionId"]})
            if objs:
                for i in range(0, len(objs), 1000):
                    s3r.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i+1000], "Quiet": True})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchBucket",):
            raise
    # unversioned sweep
    try:
        paginator2 = s3r.get_paginator("list_objects_v2")
        for page in paginator2.paginate(Bucket=bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                for i in range(0, len(keys), 1000):
                    s3r.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i+1000], "Quiet": True})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchBucket",):
            raise
    # abort MPUs
    try:
        mp = s3r.get_paginator("list_multipart_uploads")
        for page in mp.paginate(Bucket=bucket):
            for u in page.get("Uploads", []):
                s3r.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchUpload", "NoSuchBucket"):
            raise

def bucket_region(name):
    loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
    return "us-east-1" if loc in (None, "", "US") else loc

def delete_event_source_mapping(function_name, queue_arn):
    try:
        mappings = lambda_client.list_event_source_mappings(
            FunctionName=function_name, EventSourceArn=queue_arn
        )["EventSourceMappings"]
        for m in mappings:
            lambda_client.delete_event_source_mapping(UUID=m["UUID"])
            print(f"[Lambda] Event source mapping eliminado: {m['UUID']}")
    except ClientError as e:
        print(f"[Lambda] No se pudo listar/eliminar mapping: {e}")

if __name__ == "__main__":
    with shelve.open(DB_PATH, flag="r") as db:
        images_bucket = db.get("images-bucket")
        thumbs_bucket = db.get("thumbnails-bucket")
        queue_url = db.get("messages-queue")
        queue_arn = db.get("messages-queue-arn")
        table_name = db.get("dynamodb-table")
        function_name = db.get("lambda-function")

    # 1) Trigger (detach first)
    if function_name and queue_arn:
        delete_event_source_mapping(function_name, queue_arn)

    # 2) Lambda
    if function_name:
        try:
            lambda_client.delete_function(FunctionName=function_name)
            print(f"[Lambda] Función eliminada: {function_name}")
        except ClientError as e:
            print(f"[Lambda] Error eliminando función: {e}")

    # 3) DynamoDB
    if table_name:
        try:
            dynamodb.delete_table(TableName=table_name)
            print(f"[DDB] Tabla eliminada: {table_name}")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                print(f"[DDB] Error eliminando tabla: {e}")

    # 4) SQS
    if queue_url:
        try:
            sqs.delete_queue(QueueUrl=queue_url)
            print(f"[SQS] Cola eliminada: {queue_url}")
        except ClientError as e:
            print(f"[SQS] Error eliminando cola: {e}")

    # 5) S3 buckets (vaciar y borrar)
    for b in [images_bucket, thumbs_bucket]:
        if not b:
            continue
        try:
            r = bucket_region(b)
            print(f"[S3] Vaciando y borrando bucket {b} (region {r})")
            empty_bucket(b, r)
            boto3.client("s3", region_name=r).delete_bucket(Bucket=b)
            print(f"[S3] Bucket eliminado: {b}")
        except ClientError as e:
            print(f"[S3] Error eliminando bucket {b}: {e}")

    print("Teardown completo.")
