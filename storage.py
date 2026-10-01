"""طبقة تخزين الملفات.

- إذا ضُبطت متغيرات S3_* تُحفظ الملفات في تخزين سحابي متوافق مع S3
  (Cloudflare R2 أو Backblaze B2 أو AWS S3 ...).
- إذا لم تُضبط، تُحفظ محليًا في static/uploads (للتشغيل المحلي فقط).
"""
import os
import logging

log = logging.getLogger(__name__)

_local_folder = None
_client = None
_bucket = None


def init_app(app):
    global _local_folder, _client, _bucket
    _local_folder = app.config['UPLOAD_FOLDER']

    bucket = os.environ.get('S3_BUCKET')
    access_key = os.environ.get('S3_ACCESS_KEY_ID')
    secret_key = os.environ.get('S3_SECRET_ACCESS_KEY')

    if bucket and access_key and secret_key:
        import boto3
        from botocore.config import Config as BotoConfig

        _client = boto3.client(
            's3',
            endpoint_url=os.environ.get('S3_ENDPOINT_URL') or None,
            region_name=os.environ.get('S3_REGION', 'auto'),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(signature_version='s3v4',
                              s3={'addressing_style': 'path'}),
        )
        _bucket = bucket
        log.warning('التخزين السحابي مفعّل (bucket=%s)', bucket)
    else:
        _client = None
        _bucket = None
        log.warning('التخزين السحابي غير مفعّل: سيتم الحفظ محليًا في static/uploads')


def is_cloud():
    return _client is not None


def _local_path(key):
    path = os.path.normpath(os.path.join(_local_folder, key))
    if not path.startswith(os.path.normpath(_local_folder)):
        raise ValueError('مسار غير صالح')
    return path


def save_file(fileobj, key, content_type=None):
    """يحفظ ملفًا تحت المفتاح key."""
    try:
        fileobj.seek(0)
    except Exception:
        pass
    if is_cloud():
        extra = {'ContentType': content_type} if content_type else {}
        _client.upload_fileobj(fileobj, _bucket, key, ExtraArgs=extra)
    else:
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as out:
            out.write(fileobj.read())


def delete_file(key):
    """يحذف ملفًا (لا يرمي خطأ إذا لم يوجد)."""
    try:
        if is_cloud():
            _client.delete_object(Bucket=_bucket, Key=key)
        else:
            path = _local_path(key)
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        log.exception('تعذر حذف الملف %s', key)


def read_bytes(key):
    """يقرأ محتوى الملف، أو يعيد None إذا لم يوجد."""
    try:
        if is_cloud():
            return _client.get_object(Bucket=_bucket, Key=key)['Body'].read()
        path = _local_path(key)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return f.read()
    except Exception as exc:
        code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
        if code not in ('NoSuchKey', '404', 'NotFound'):
            log.exception('تعذر قراءة الملف %s', key)
        return None


def download_url(key, download_name):
    """رابط تحميل مؤقت (5 دقائق) للتخزين السحابي، أو None في الوضع المحلي."""
    if not is_cloud():
        return None
    return _client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': _bucket,
            'Key': key,
            'ResponseContentDisposition': f'attachment; filename="{download_name}"',
        },
        ExpiresIn=300,
    )


def local_folder():
    return _local_folder
