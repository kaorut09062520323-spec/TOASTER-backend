import os

import boto3
from dotenv import load_dotenv


load_dotenv()


R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")


if not all([
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME,
    R2_ENDPOINT,
]):
    raise RuntimeError(
        "R2 environment variables are not configured"
    )


r2_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
)


def upload_image(
    image_bytes: bytes,
    object_key: str,
    content_type: str,
) -> str:

    r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=object_key,
        Body=image_bytes,
        ContentType=content_type,
    )

    return object_key

def download_image(object_key: str):
    return r2_client.get_object(
        Bucket=R2_BUCKET_NAME,
        Key=object_key,
    )


def delete_image(object_key: str):
    r2_client.delete_object(
        Bucket=R2_BUCKET_NAME,
        Key=object_key,
    )
