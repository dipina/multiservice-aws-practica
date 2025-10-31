# AWS Image Processing Project

Este proyecto integra varios servicios de AWS para procesar imágenes subidas a S3, generar thumbnails, guardar metadatos en DynamoDB y mostrar los resultados en una página web estática.

---

## **Arquitectura del Proyecto**

- **Amazon S3**: Almacena las imágenes originales y los thumbnails generados.
- **Amazon SQS**: Cola de mensajes que desencadena el procesamiento de imágenes.
- **AWS Lambda**: Procesa las imágenes, genera thumbnails y guarda los metadatos.
- **Amazon DynamoDB**: Almacena los metadatos de las imágenes.
- **Página Web Estática**: Muestra los thumbnails generados, alojada en S3.

---

## **Requisitos**

### Herramientas
- **AWS CLI** configurado con credenciales.
- **Python 3.8 o superior**.
- **Pillow** para manejo de imágenes.
- **Boto3** para interacción con AWS.

### Credenciales para los programas en Python
Crea un archivo `.env` en la raíz del proyecto con las siguientes variables. Toma `.env.sample` como ejemplo. Recoge los credenciales de AWS Details->AWS CLI->Show:
```plaintext
aws_access_key_id=YOUR_ACCESS_KEY
aws_secret_access_key=YOUR_SECRET_KEY
aws_session_token=YOUR_SESSION_TOKEN
aws_default_region=us-east-1
```

### Credenciales para AWS CLI
Actualizar el fichero `credentials`, en mi entorno Windows se encuentra en `C:\Users\dipin\.aws` con el siguiente código:
```plaintext
[default]
aws_access_key_id=YOUR_ACCESS_KEY
aws_secret_access_key=YOUR_SECRET_KEY
aws_session_token=YOUR_SESSION_TOKEN
aws_default_region=us-east-1
```

---

## **Instalación**

0. Instalar AWS CLI
   Ir a `https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html`
1. Instala las dependencias:
   ```bash
   pip install -r requirements.txt
   ```

---

## **Ejecución paso a paso**
### 1. Preparar los buckets, cola, tabla, función Lambda y página web
```bash
python setup.py
```

### 2. Subir imágenes de carpeta IMG a S3 AWS
```bash
python upload_folder_images.py
```

### 3. Eliminar todos los recursos AWS creados
```bash
python teardown.py
```

## **Ejecución paso a paso, sólo para ver cómo las diferentes partes del proyecto se configuran**

### 1. Crear Buckets de S3
Ejecuta el script para crear los buckets necesarios:
```bash
python setup_scripts/create_s3_buckets.py
```

Verifica que los buckets existen:
```bash
aws s3 ls
```

---

### 2. Configurar la Cola SQS
Crea la cola SQS:
```bash
python setup_scripts/create_sqs_queue.py
```

Verifica que la cola exista:
```bash
aws sqs list-queues
```
Esto sería un ejemplo de posible mensaje a enviar en la cola:
```json
{
  "Records": [
    {
      "messageId": "19dd0b57-b21e-4ac1-bd88-01bbb068cb78",
      "receiptHandle": "MessageReceiptHandle",
      "body": "{\"bucket_name\":\"image-uploads-bucket-20251017-fe41ce32\",\"image_key\":\"statue_small.jpg\"}",
      "attributes": {
        "ApproximateReceiveCount": "1",
        "SentTimestamp": "1523232000000",
        "SenderId": "123456789012",
        "ApproximateFirstReceiveTimestamp": "1523232000001"
      },
      "messageAttributes": {},
      "md5OfBody": "d41d8cd98f00b204e9800998ecf8427e",
      "eventSource": "aws:sqs",
      "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:MyQueue",
      "awsRegion": "us-east-1"
    }
  ]
}
```

---

### 3. Configurar la Tabla DynamoDB
Crea la tabla DynamoDB para los metadatos:
```bash
python setup_scripts/create_dynamodb_table.py
```

Observar que hay un shelve llamado ```aws_resources.db``` donde todos los resultados intermedios del workflow se van guardando.
Por ejemplo, nombre del bucket creado para guardar imágenes. El siguiente comando muestra sus contenidos.
```bash
python show_shelve.py
```

Verifica que la tabla exista:
```bash
aws dynamodb list-tables
```

---

### 4. Configurar la Función Lambda
0. Ejecutar comando para recuperar referencia a LabRole:
  ```bash
   python .\setup_scripts\get_LabRole_arn.py
   ```

1. Empaqueta la función Lambda:
Observa que he eliminado el uso de PIL para hacer el thumbnail porque requiere código nativo para Linux y para mis pruebas he usado Windows. No se hace Thumbnails de momento.
   ```bash
   cd lambda_function/
   pip install -r requirements.txt -t .
   zip -r lambda_function.zip .
   cd ..
   ```

2. Configura la función Lambda:
   ```bash
   python setup_scripts/configure_lambda.py
   ```

3. Configura el trigger de SQS para Lambda:
   ```bash
   python setup_scripts/add_sqs_trigger.py
   ```

---
### 5. Subir Imágenes y Enviar Mensajes
1. Asegúrate de tener imágenes en la carpeta `img/`.
2. Sube las imágenes y envía mensajes a SQS:
   ```bash
   python setup_scripts/upload_folder_images.py
   ```

---

### 6. Verificar Resultados
1. **Bucket de Thumbnails**:
   ```bash
   aws s3 ls s3://image-thumbnails-bucket-unique-id/
   ```

2. **Tabla DynamoDB**:
   ```bash
   aws dynamodb scan --table-name ImageMetadata
   ```

3. **Logs de Lambda**:
   Revisa los logs para errores:
   ```bash
   aws logs describe-log-groups
   aws logs get-log-events --log-group-name /aws/lambda/ImageProcessingFunction --log-stream-name <log_stream_name>
   ```

---

### 7. Subir Página Web Estática
1. Asegúrate de que la página web esté configurada en `s3_static_website/`.
2. Ejecuta el script para subir la página:
   ```bash
   python setup_scripts/deploy_static_site.py
   ```

3. Accede a la página web:
   ```
   http://image-thumbnails-bucket-unique-id.s3-website-us-east-1.amazonaws.com/index.html 
   https://image-thumbnails-bucket-20251017-6761b8f5.s3.amazonaws.com/index.html
   
   ```


### 8. Queries over DynamoDB
Here’s the fast way to run queries against your DynamoDB table from the AWS Console.

1) Open the table

   * Go to DynamoDB in the AWS Console (make sure you’re in the same region where the table lives).

   * Tables → click your table (e.g., ```ImageMetadata```).

2) Explore items (point-and-click)

   * Tab Explore items.

   * At the top, switch between:

      * Query – efficient, but it requires the partition key.

      * Scan – reads the whole table and can then filter (slower, costs more).

   * With your current schema

      * Your table has only a partition key ImageID (no sort key). That means:

      * Query works only when you provide an exact ImageID value.

      * In the left panel, ```set ImageID = "statue_small.jpg"``` → Run.

   * For anything else (e.g., “all images”, “all with ‘thumb’ in URL”), use Scan and add a Filter.

      * Useful filters (Scan)

         * Click Add new filter:

            ```attribute_exists(ThumbnailURL)```

            ```contains(ThumbnailURL, "thumbnails/")```

            ```begins_with(ImageID, "statue_")``` (works, but still a Scan since you have no sort key)

   * Tip: Filters don’t reduce read capacity consumed by a Scan; they only reduce the results returned. Use sparingly on large tables.

3) Query with PartiQL (SQL-like)

In the table view, open PartiQL editor (top-right).

Run statements like:
```sql
-- Exact match by partition key (fast)
SELECT * FROM "ImageMetadata" WHERE ImageID = 'statue_small.jpg';

-- Return only certain attributes
SELECT ImageID, ThumbnailURL FROM "ImageMetadata" WHERE ImageID = 'statue_small.jpg';

-- Scan with a filter (because no sort key/index for these predicates)
SELECT * FROM "ImageMetadata" WHERE contains(ThumbnailURL, 'thumbnails/');

-- Update one item
UPDATE "ImageMetadata"
SET Note = 'Reprocessed'
WHERE ImageID = 'statue_small.jpg';

-- Delete one item
DELETE FROM "ImageMetadata" WHERE ImageID = 'old_image.jpg';
```

Click Run to execute, results appear below. PartiQL is great for quick ad-hoc reads/updates/deletes.

---

## **Solución de Problemas**

1. **Mensajes en SQS no procesados**:
   - Asegúrate de que Lambda tiene configurado el trigger de SQS.

2. **Thumbnails no generados**:
   - Verifica los logs de Lambda para errores.

3. **No se muestran imágenes en la página web**:
   - Asegúrate de que el hosting estático está habilitado.
   - Confirma que los thumbnails están subidos al bucket.

---

## **Licencia**
Este proyecto está bajo la licencia MIT. Consulta el archivo `LICENSE` para más detalles.

