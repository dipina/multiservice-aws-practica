import os
import json
import boto3

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

THUMB_BUCKET = os.environ["THUMB_BUCKET"]                 # thumbnails bucket (env var)
TABLE_NAME   = os.getenv("TABLE_NAME", "ImageMetadata")

table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    try:
        for record in event["Records"]:
            body = json.loads(record["body"])
            src_bucket = body["bucket_name"]
            image_key  = body["image_key"]

            # 1) Get the original object (metadata + body stream)
            head = s3_client.head_object(Bucket=src_bucket, Key=image_key)
            content_type = head.get("ContentType", "application/octet-stream")
            size_bytes   = head.get("ContentLength", 0)

            # 2) Copy it as a "thumbnail" without modifying bytes (no native libs needed)
            #    If you want a prefix always, keep thumbnails/<key>. If key already includes folders,
            #    we keep the path under thumbnails/.
            thumbnail_key = f"thumbnails/{image_key}"

            # Efficient server-side copy (no data round-trip)
            s3_client.copy_object(
                Bucket=THUMB_BUCKET,
                Key=thumbnail_key,
                CopySource={"Bucket": src_bucket, "Key": image_key},
                MetadataDirective="REPLACE",               # ensure we set content-type below
                ContentType=content_type
            )

            # 3) Store metadata in DynamoDB (no pixel dimensions without an image lib)
            table.put_item(Item={
                "ImageID": image_key,
                "OriginalURL":  f"https://{src_bucket}.s3.amazonaws.com/{image_key}",
                "ThumbnailURL": f"https://{THUMB_BUCKET}.s3.amazonaws.com/{thumbnail_key}",
                "Bytes": size_bytes,
                "Note": "No resize performed (pure-Python build)."
            })

            print(f"Processed (copied as thumbnail): s3://{src_bucket}/{image_key} -> s3://{THUMB_BUCKET}/{thumbnail_key}")

        return {"statusCode": 200, "body": json.dumps("Processing completed.")}
    except Exception as e:
        print(f"Error: {e}")
        return {"statusCode": 500, "body": json.dumps(f"Error: {e}")}


