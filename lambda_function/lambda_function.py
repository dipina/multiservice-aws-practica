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


"""
import boto3
from PIL import Image
import io
import os
import json

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Read the bucket from env var set at deployment time
THUMB_BUCKET = os.environ['THUMB_BUCKET']          # <-- NEW
TABLE_NAME   = os.getenv('TABLE_NAME', 'ImageMetadata')

table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    try:
        for record in event['Records']:
            body = json.loads(record['body'])
            bucket_name = body['bucket_name']
            image_key = body['image_key']

            # Download original
            resp = s3_client.get_object(Bucket=bucket_name, Key=image_key)
            img = Image.open(io.BytesIO(resp['Body'].read()))

            # Make thumbnail
            img.thumbnail((128, 128))
            thumbnail_buffer = io.BytesIO()
            img.save(thumbnail_buffer, format="JPEG")
            thumbnail_buffer.seek(0)

            # Upload thumbnail to env-configured bucket
            thumbnail_key = f"thumbnails/{image_key}"
            s3_client.put_object(
                Bucket=THUMB_BUCKET,                 # <-- NEW
                Key=thumbnail_key,
                Body=thumbnail_buffer,
                ContentType='image/jpeg'
            )

            # Save metadata
            table.put_item(Item={
                'ImageID': image_key,
                'OriginalURL': f"https://{bucket_name}.s3.amazonaws.com/{image_key}",
                'ThumbnailURL': f"https://{THUMB_BUCKET}.s3.amazonaws.com/{thumbnail_key}",  # <-- NEW
                'Size': f"{img.size[0]}x{img.size[1]}"
            })

            print(f"Procesado correctamente: {image_key}")

        return {'statusCode': 200, 'body': json.dumps('Procesamiento completado.')}
    except Exception as e:
        print(f"Error en el procesamiento: {e}")
        return {'statusCode': 500, 'body': json.dumps(f"Error: {e}")}


"""



"""
import boto3
from PIL import Image
import io
import os
import json

# Inicializar clientes de AWS
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('ImageMetadata')

def lambda_handler(event, context):
    try:
        for record in event['Records']:
            # Obtener datos del mensaje SQS
            body = json.loads(record['body'])
            bucket_name = body['bucket_name']
            image_key = body['image_key']

            # Descargar la imagen original desde S3
            response = s3_client.get_object(Bucket=bucket_name, Key=image_key)
            img = Image.open(io.BytesIO(response['Body'].read()))

            # Generar un thumbnail
            img.thumbnail((128, 128))
            thumbnail_buffer = io.BytesIO()
            img.save(thumbnail_buffer, format="JPEG")
            thumbnail_buffer.seek(0)

            # Subir el thumbnail al bucket de thumbnails
            thumbnail_key = f"thumbnails/{image_key}"
            s3_client.put_object(
                Bucket='image-thumbnails-bucket-unique-id',
                Key=thumbnail_key,
                Body=thumbnail_buffer,
                ContentType='image/jpeg'
            )

            # Guardar metadatos en DynamoDB
            table.put_item(Item={
                'ImageID': image_key,
                'OriginalURL': f"https://{bucket_name}.s3.amazonaws.com/{image_key}",
                'ThumbnailURL': f"https://image-thumbnails-bucket-unique-id.s3.amazonaws.com/{thumbnail_key}",
                'Size': f"{img.size[0]}x{img.size[1]}"
            })

            print(f"Procesado correctamente: {image_key}")

        return {
            'statusCode': 200,
            'body': json.dumps('Procesamiento completado.')
        }
    except Exception as e:
        print(f"Error en el procesamiento: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {e}")
        }
"""