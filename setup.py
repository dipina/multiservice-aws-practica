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

def build_lambda_zip_bytes(source_path: str = None) -> bytes:
    """
    Crea el paquete ZIP de la Lambda leyendo el código desde disco.
    Por defecto toma ./lambda_function/lambda_function.py y lo deja en la raíz del ZIP
    con el nombre 'lambda_function.py' (Handler: lambda_function.lambda_handler).
    """
    import os
    import io
    import zipfile

    # Ruta por defecto: ./lambda_function/lambda_function.py
    if source_path is None:
        source_path = os.path.join("lambda_function", "lambda_function.py")

    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"No se encontró el archivo de la Lambda en: {source_path}")

    # Leer el código fuente
    with open(source_path, "rb") as f:
        code_bytes = f.read()

    # Crear ZIP en memoria
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Guardar el código en la RAÍZ del zip con el nombre esperado por el handler
        z.writestr("lambda_function.py", code_bytes)

        # (Opcional) Si tienes dependencias puras ya vendorizadas en una carpeta (p.ej. lambda_function/build),
        # puedes incluirlas en el ZIP en la raíz:
        # deps_dir = os.path.join("lambda_function", "build")
        # if os.path.isdir(deps_dir):
        #     for root, _, files in os.walk(deps_dir):
        #         for name in files:
        #             abs_path = os.path.join(root, name)
        #             rel_path = os.path.relpath(abs_path, deps_dir)  # deja los paquetes en la raíz del zip
        #             z.write(abs_path, arcname=rel_path)

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


def deploy_static_site(thumbs_bucket, index_path=None):
    """
    Sube s3_static_website/index.html al bucket de thumbnails y habilita el hosting estático.
    """
    import os

    # Ruta por defecto: s3_static_website/index.html (portable en Win/Linux)
    if index_path is None:
        index_path = os.path.join("s3_static_website", "index.html")

    # 1) Asegurar BPA OFF y policy pública (LIST thumbnails/ + GET)
    disable_bucket_bpa(thumbs_bucket)
    apply_thumbs_public_policy(thumbs_bucket)

    # 2) Subir index.html (obligatorio que exista)
    if not os.path.exists(index_path):
        raise FileNotFoundError(
            f"No se encontró el fichero HTML en: {index_path}. "
            "Crea s3_static_website/index.html o pasa index_path explícito."
        )

    # Usa upload_file con ContentType correcto
    s3.upload_file(
        Filename=index_path,
        Bucket=thumbs_bucket,
        Key="index.html",
        ExtraArgs={"ContentType": "text/html; charset=utf-8"},
    )
    print(f"[S3] index.html subido desde {index_path}")

    # 3) Activar hosting estático
    s3.put_bucket_website(
        Bucket=thumbs_bucket,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
    )

    # 4) Mostrar URL
    region = s3.meta.region_name or "us-east-1"
    website_host = (
        "s3-website-us-east-1.amazonaws.com"
        if region == "us-east-1"
        else f"s3-website-{region}.amazonaws.com"
    )
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
