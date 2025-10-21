#!/usr/bin/env python3
import os
import io
import json
import time
import uuid
import shelve
import zipfile
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# ---------- Config ----------
load_dotenv()
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DB_PATH = "aws_resources.db"
IMAGES_BASE = "image-uploads-bucket"
THUMBS_BASE = "image-thumbnails-bucket"
QUEUE_BASE  = "image-processing-queue"
TABLE_NAME  = "ImageMetadata"
FUNCTION_NAME = "ImageProcessingFunction"
ROLE_NAME = "LabRole"

session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
sqs = session.client("sqs")
dynamodb = session.client("dynamodb")
lambda_client = session.client("lambda")
sts = session.client("sts")

# ---------- Helpers ----------
def unique_suffix():
    return f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

def bucket_exists(name):
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError:
        return False

def create_bucket(name):
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": REGION}
        )

def ensure_bucket(name):
    if bucket_exists(name):
        print(f"[S3] Bucket ya existe: {name}")
    else:
        create_bucket(name)
        print(f"[S3] Bucket creado: {name}")

def disable_bucket_bpa(bucket):
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )

def apply_thumbs_public_policy(bucket):
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadObjects",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {
                "Sid": "PublicListThumbsPrefix",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {
                    "StringLike": {"s3:prefix": ["thumbnails/*", "thumbnails/"]}
                },
            },
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))

def labrole_arn():
    ident = sts.get_caller_identity()
    account = ident["Account"]
    partition = ident["Arn"].split(":")[1]  # aws / aws-us-gov / aws-cn
    return f"arn:{partition}:iam::{account}:role/{ROLE_NAME}"

def ensure_queue(name):
    resp = sqs.create_queue(QueueName=name)
    url = resp["QueueUrl"]
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]
    arn = attrs["QueueArn"]
    print(f"[SQS] Cola lista: {url}")
    return url, arn

def ensure_table(name):
    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "ImageID", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "ImageID", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"[DDB] Creando tabla: {name} (esperando ACTIVE)")
        waiter = session.resource("dynamodb").meta.client.get_waiter("table_exists")
        waiter.wait(TableName=name)
    except dynamodb.exceptions.ResourceInUseException:
        print(f"[DDB] Tabla ya existe: {name}")

    desc = dynamodb.describe_table(TableName=name)["Table"]
    arn = desc["TableArn"]
    return arn

def build_lambda_zip_bytes():
    """ Lambda sin PIL: copia el objeto original a thumbnails/<key> y guarda metadatos. """
    code = f'''import os, json, boto3
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
THUMB_BUCKET = os.environ["THUMB_BUCKET"]
TABLE_NAME = os.getenv("TABLE_NAME", "{TABLE_NAME}")
table = dynamodb.Table(TABLE_NAME)

def _parse_body(record):
    payload = record.get("body")
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        return json.loads(payload)
    if isinstance(payload, dict):
        return payload
    raise ValueError("Unsupported body type")

def lambda_handler(event, context):
    for record in event.get("Records", []):
        body = _parse_body(record)
        src_bucket = body["bucket_name"]
        image_key = body["image_key"]

        head = s3.head_object(Bucket=src_bucket, Key=image_key)
        content_type = head.get("ContentType", "application/octet-stream")
        size_bytes = head.get("ContentLength", 0)

        thumb_key = f"thumbnails/{{image_key}}"
        s3.copy_object(
            Bucket=THUMB_BUCKET,
            Key=thumb_key,
            CopySource={{"Bucket": src_bucket, "Key": image_key}},
            MetadataDirective="REPLACE",
            ContentType=content_type
        )

        table.put_item(Item={{
            "ImageID": image_key,
            "OriginalURL":  f"https://{{src_bucket}}.s3.amazonaws.com/{{image_key}}",
            "ThumbnailURL": f"https://{{THUMB_BUCKET}}.s3.amazonaws.com/{{thumb_key}}",
            "Bytes": size_bytes,
            "Note": "No resize (pure-Python)"
        }})
    return {{"statusCode": 200, "body": json.dumps("OK")}}
'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lambda_function.py", code)
    buf.seek(0)
    return buf.read()

def ensure_lambda(function_name, role_arn, thumb_bucket):
    code_bytes = build_lambda_zip_bytes()
    env_vars = {"THUMB_BUCKET": thumb_bucket, "TABLE_NAME": TABLE_NAME}
    try:
        resp = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.11",
            Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": code_bytes},
            Timeout=30,
            MemorySize=256,
            Environment={"Variables": env_vars},
            Publish=True,
        )
        print(f"[Lambda] Función creada: {function_name}")
        return resp["FunctionArn"]
    except lambda_client.exceptions.ResourceConflictException:
        print(f"[Lambda] Función ya existe: {function_name}. Actualizando código/config...")
        lambda_client.update_function_code(
            FunctionName=function_name, ZipFile=code_bytes, Publish=True
        )
        waiter = lambda_client.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name)
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            Runtime="python3.11",
            Handler="lambda_function.lambda_handler",
            Timeout=30,
            MemorySize=256,
            Environment={"Variables": env_vars},
        )
        desc = lambda_client.get_function(FunctionName=function_name)["Configuration"]
        return desc["FunctionArn"]

def ensure_sqs_trigger(queue_arn, function_name, batch_size=3, enabled=True):
    existing = lambda_client.list_event_source_mappings(
        EventSourceArn=queue_arn, FunctionName=function_name
    ).get("EventSourceMappings", [])
    if existing:
        uuid = existing[0]["UUID"]
        lambda_client.update_event_source_mapping(
            UUID=uuid, Enabled=enabled, BatchSize=batch_size
        )
        print(f"[Lambda] Trigger SQS actualizado (UUID={uuid})")
        return uuid
    resp = lambda_client.create_event_source_mapping(
        EventSourceArn=queue_arn,
        FunctionName=function_name,
        Enabled=enabled,
        BatchSize=batch_size,
    )
    uuid = resp["UUID"]
    print(f"[Lambda] Trigger SQS creado (UUID={uuid})")
    return uuid

# -------- NEW: desplegar website en el bucket de thumbnails --------
GALLERY_HTML = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Galería de Thumbnails</title>
<style>
:root{--bg:#0b0d11;--fg:#e6e6e6;--muted:#9aa3af;--card:#111827}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:24px;max-width:1100px;margin:0 auto}
h1{margin:0 0 6px;font-size:24px}
.sub{color:var(--muted);font-size:14px}
#status{color:var(--muted);padding:0 24px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;padding:24px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border-radius:16px;text-decoration:none;color:inherit;box-shadow:0 4px 10px rgba(0,0,0,.2);overflow:hidden;display:flex;flex-direction:column}
img{width:100%;height:160px;object-fit:cover;display:block;background:#0b0d11}
.name{padding:10px 12px;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
footer{color:var(--muted);text-align:center;padding:24px}
</style></head>
<body>
<header><h1>Galería de Thumbnails</h1><div class="sub">Descubierta automáticamente desde <code>thumbnails/</code></div><div id="status">Cargando…</div></header>
<main id="grid" class="grid"></main><footer>Generado en el navegador</footer>
<script>
const PREFIX="thumbnails/";
function listingUrl(t){const e=location.origin,n=new URLSearchParams({"list-type":"2",prefix:PREFIX,"max-keys":"1000"});return t&&n.set("continuation-token",t),e+"/?"+n.toString()}
function objectUrl(t){return location.origin+"/"+encodeURIComponent(t)}
async function listAllKeys(){const t=[];let e=null;for(;;){const n=await fetch(listingUrl(e));if(!n.ok)throw new Error("List error: "+n.status+" "+n.statusText);const i=await n.text(),o=(new DOMParser).parseFromString(i,"application/xml"),a=Array.from(o.getElementsByTagName("Contents"));a.forEach(e=>{const n=e.getElementsByTagName("Key")[0]?.textContent||"";!n.endsWith("/")&&/\.(jpe?g|png|webp|gif|bmp|tiff)$/i.test(n)&&t.push(n)});if("true"!==o.getElementsByTagName("IsTruncated")[0]?.textContent)break;e=o.getElementsByTagName("NextContinuationToken")[0]?.textContent||null}return t}
function render(t){const e=document.getElementById("status"),n=document.getElementById("grid");if(!t.length)return void(e.textContent="No se encontraron imágenes en thumbnails/.");e.textContent=`${t.length} imágenes.`;const i=document.createDocumentFragment();t.forEach(t=>{const e=document.createElement("a");e.className="card",e.href=objectUrl(t),e.target="_blank",e.rel="noopener";const n=document.createElement("img");n.src=objectUrl(t),n.alt=t.split("/").pop(),n.loading="lazy";const o=document.createElement("div");o.className="name",o.textContent=t.split("/").pop(),e.append(n,o),i.appendChild(e)}),n.appendChild(i)}
(async()=>{try{render(await listAllKeys())}catch(t){document.getElementById("status").textContent="Error: "+(t?.message||t),console.error(t)}})();
</script>
</body></html>
"""

def deploy_static_site(thumbs_bucket, index_path="s3_static_website/index.html"):
    # 1) Asegurar BPA OFF y policy pública (LIST thumbnails/ + GET)
    disable_bucket_bpa(thumbs_bucket)
    apply_thumbs_public_policy(thumbs_bucket)

    # 2) Subir index.html (archivo local o generado)
    if os.path.exists(index_path):
        s3.upload_file(
            Filename=index_path,
            Bucket=thumbs_bucket,
            Key="index.html",
            ExtraArgs={"ContentType": "text/html"}
        )
        print(f"[S3] index.html subido desde {index_path}")
    else:
        s3.put_object(
            Bucket=thumbs_bucket,
            Key="index.html",
            Body=GALLERY_HTML.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        print("[S3] index.html generado y subido (galería JS)")

    # 3) Activar hosting estático
    s3.put_bucket_website(
        Bucket=thumbs_bucket,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}}
    )

    # 4) Mostrar URL
    region = s3.meta.region_name or "us-east-1"
    website_host = ("s3-website-us-east-1.amazonaws.com"
                    if region == "us-east-1"
                    else f"s3-website-{region}.amazonaws.com")
    url = f"http://{thumbs_bucket}.{website_host}/"
    print(f"[S3] Website habilitado: {url}")
    return url

# ---------- Main ----------
if __name__ == "__main__":
    suffix = unique_suffix()
    images_bucket = f"{IMAGES_BASE}-{suffix}"
    thumbs_bucket = f"{THUMBS_BASE}-{suffix}"
    queue_name    = f"{QUEUE_BASE}-{suffix}"

    # S3 buckets
    ensure_bucket(images_bucket)
    ensure_bucket(thumbs_bucket)

    # Desplegar website en thumbnails
    website_url = deploy_static_site(thumbs_bucket)

    # SQS
    queue_url, queue_arn = ensure_queue(queue_name)

    # DynamoDB
    table_arn = ensure_table(TABLE_NAME)

    # Lambda + trigger
    role = labrole_arn()
    func_arn = ensure_lambda(FUNCTION_NAME, role, thumbs_bucket)
    mapping_uuid = ensure_sqs_trigger(queue_arn, FUNCTION_NAME)

    # Guardar recursos en shelve
    with shelve.open(DB_PATH) as db:
        db["images-bucket"] = images_bucket
        db["thumbnails-bucket"] = thumbs_bucket
        db["messages-queue"] = queue_url
        db["messages-queue-arn"] = queue_arn
        db["dynamodb-table"] = TABLE_NAME
        db["dynamodb-table-arn"] = table_arn
        db["lambda-function"] = FUNCTION_NAME
        db["labrole-arn"] = role
        db["event-source-uuid"] = mapping_uuid
        db["website-url"] = website_url

    print("\n=== RECURSOS CREADOS ===")
    print(f"S3 imágenes     : s3://{images_bucket}")
    print(f"S3 thumbnails   : s3://{thumbs_bucket}")
    print(f"Website         : {website_url}")
    print(f"SQS URL         : {queue_url}")
    print(f"SQS ARN         : {queue_arn}")
    print(f"DynamoDB table  : {TABLE_NAME} ({table_arn})")
    print(f"Lambda          : {FUNCTION_NAME} ({func_arn})")
    print(f"Trigger UUID    : {mapping_uuid}")
