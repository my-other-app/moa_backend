import boto3
from sqlalchemy.types import TypeDecorator, String
from PIL import Image
import io
import uuid
import os
from typing import Dict, Optional, Union


from app.config import settings


class S3Image(dict):
    def __init__(self, variations, **kwargs):
        self.variations = variations
        super().__init__(kwargs)

    def delete(self):
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        print(self.variations)

        for key, value in self.variations.items():
            try:
                s3_client.delete_object(Bucket=settings.S3_BUCKET, Key=value)
            except Exception as e:
                print(f"Error deleting object: {str(e)}")


class S3File(str):
    def __new__(cls, file_path):
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": file_path},
            ExpiresIn=3600,
        )
        obj = super().__new__(cls, url)
        obj.file_path = file_path
        return obj

    def delete(self):
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        try:
            s3_client.delete_object(Bucket=settings.S3_BUCKET, Key=self.file_path)
        except Exception as e:
            print(f"Error deleting object: {str(e)}")


class S3ImageField(TypeDecorator):
    """
    A custom SQLAlchemy field for handling images with S3 storage.
    Only stores the original file path and generates variants on demand.
    """

    impl = String
    cache_ok = True

    def __init__(
        self,
        upload_to: str = "uploads",
        max_size: int = 10 * 1024 * 1024,
        allowed_extensions: list[str] = ["jpg", "jpeg", "png", "gif"],
        variations: dict = {},
    ):
        super().__init__()
        self.max_size = max_size
        self.allowed_extensions = allowed_extensions
        self.bucket_name = settings.S3_BUCKET
        self.base_path = os.path.join(settings.S3_BASE_PATH, upload_to)
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        self.variations = variations

    def _process_image_file(
        self, image_data: Union[bytes, io.BytesIO], filename: Optional[str] = None
    ) -> tuple:
        """Process image data and return PIL Image and format."""
        if isinstance(image_data, io.BytesIO):
            img = Image.open(image_data)
        else:
            img = Image.open(io.BytesIO(image_data))

        if not img.format:
            raise ValueError("Invalid image format")

        fmt = img.format.lower()

        if self.allowed_extensions and fmt not in self.allowed_extensions:
            raise ValueError(
                f"Unsupported image format. Allowed formats: {self.allowed_extensions}"
            )

        return img, fmt

    def generate_variants(self, image: Image.Image, variations: dict) -> dict:
        def resize_image(img: Image.Image, width: int, height: int) -> Image.Image:
            img_ratio = img.width / img.height
            target_ratio = width / height

            if img_ratio > target_ratio:
                # Image is wider than target, fit by width
                new_width = width
                new_height = round(width / img_ratio)
            else:
                # Image is taller than target, fit by height
                new_height = height
                new_width = round(height * img_ratio)

            return img.resize((new_width, new_height), Image.LANCZOS)

        variants = {}
        for key, value in variations.items():
            width = value.get("width")
            height = value.get("height")
            if width and height:
                variants[key] = resize_image(image, width, height)

        return variants

    def process_bind_param(
        self, value: Union[Dict, bytes, io.BytesIO], dialect
    ) -> Optional[str]:
        """Process the value before saving to database."""

        print(value)
        if not value:
            return None

        if isinstance(value, str):
            return value

        try:
            # Handle different input types
            if isinstance(value, dict):
                if "file" in value:
                    # Handle file-like object (e.g., from Flask/FastAPI)
                    image_data = value["file"].read()
                    filename = value["file"].filename
                elif "bytes" in value:
                    # Handle raw bytes with optional filename
                    image_data = value["bytes"]
                    filename = value.get("filename")
                else:
                    raise ValueError("Invalid input format")
            elif isinstance(value, (bytes, io.BytesIO)):
                # Handle direct bytes or BytesIO input
                image_data = value
                filename = None
            else:
                raise ValueError("Unsupported input type")

            # Process the image
            img, fmt = self._process_image_file(image_data, filename)

            file_uid = str(uuid.uuid4())

            # Generate filename and path
            new_filename = f"{file_uid}.{fmt}"
            full_path = os.path.join(self.base_path, new_filename)

            # Save to S3
            buffer = io.BytesIO()
            img.save(buffer, format=fmt)
            buffer.seek(0)

            if self.max_size and buffer.getbuffer().nbytes > self.max_size:
                raise ValueError(
                    f"Image size exceeds maximum allowed size of {self.max_size} bytes"
                )

            self.s3_client.upload_fileobj(
                buffer,
                self.bucket_name,
                full_path,
                ExtraArgs={"ContentType": f"image/{fmt}"},
            )

            if self.variations:
                # Generate image variants
                variants = self.generate_variants(img, self.variations)
                for key, variant in variants.items():
                    # variant_fmt = variant.format.lower()
                    buffer = io.BytesIO()
                    variant.save(buffer, format=fmt)
                    buffer.seek(0)

                    variant_path = os.path.join(
                        self.base_path, f"{file_uid}.{key}.{fmt}"
                    )
                    self.s3_client.upload_fileobj(
                        buffer,
                        self.bucket_name,
                        variant_path,
                        ExtraArgs={"ContentType": f"image/{fmt}"},
                    )

            return full_path
        except Exception as e:
            raise ValueError(f"Error processing image: {str(e)}")

    def process_result_value(self, value: str, dialect) -> Optional[str]:
        """Process the value when retrieving from database."""
        if not value:
            return None

        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        ext = value.split(".")[-1]
        file_name = ".".join(value.split(".")[:-1]).split("/")[-1]

        variations = {}
        variation_paths = {}
        for variation in self.variations:
            variation_path = os.path.join(
                self.base_path, f"{file_name}.{variation}.{ext}"
            )
            variation_paths[variation] = variation_path
            try:
                # s3_client.head_object(Bucket=self.bucket_name, Key=variation_path)
                url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": settings.S3_BUCKET, "Key": variation_path},
                    ExpiresIn=3600,
                )
                variations[variation] = url
            except:
                pass

        variations["original"] = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": value},
            ExpiresIn=3600,
        )
        variation_paths["original"] = value
        return S3Image(variations=variation_paths, **variations)


class S3FileField(TypeDecorator):
    """
    A custom SQLAlchemy field for handling file uploads with S3 storage.
    Stores the file path and handles direct file uploads without variants.
    """

    impl = String
    cache_ok = True

    def __init__(
        self,
        upload_to: str = "uploads",
        max_size: int = 50 * 1024 * 1024,  # 50MB default
        allowed_extensions: Optional[list[str]] = None,
    ):
        super().__init__()
        self.max_size = max_size
        self.allowed_extensions = allowed_extensions
        self.bucket_name = settings.S3_BUCKET
        self.base_path = os.path.join(settings.S3_BASE_PATH, upload_to)
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )

    def process_bind_param(
        self, value: Union[Dict, bytes, io.BytesIO], dialect
    ) -> Optional[str]:
        """Process the value before saving to database."""
        if not value:
            return None

        if isinstance(value, str):
            return value

        try:
            # Handle different input types
            if isinstance(value, dict):
                if "file" in value:
                    # Handle file-like object (e.g., from Flask/FastAPI)
                    file_data = value["file"].read()
                    filename = value["file"].filename
                elif "bytes" in value:
                    # Handle raw bytes with optional filename
                    file_data = value["bytes"]
                    filename = value.get("filename")
                else:
                    raise ValueError("Invalid input format")
            elif isinstance(value, (bytes, io.BytesIO)):
                # Handle direct bytes or BytesIO input
                file_data = value
                filename = None
            else:
                raise ValueError("Unsupported input type")

            if not filename:
                raise ValueError("Filename is required")

            # Check file extension
            ext = filename.split(".")[-1].lower()
            if self.allowed_extensions and ext not in self.allowed_extensions:
                raise ValueError(
                    f"Unsupported file extension. Allowed extensions: {self.allowed_extensions}"
                )

            # Generate unique filename
            file_uid = str(uuid.uuid4())
            new_filename = f"{file_uid}.{ext}"
            full_path = os.path.join(self.base_path, new_filename)

            # Check file size
            if isinstance(file_data, bytes):
                file_size = len(file_data)
                buffer = io.BytesIO(file_data)
            else:
                file_data.seek(0, os.SEEK_END)
                file_size = file_data.tell()
                file_data.seek(0)
                buffer = file_data

            if self.max_size and file_size > self.max_size:
                raise ValueError(
                    f"File size exceeds maximum allowed size of {self.max_size} bytes"
                )

            # Upload to S3
            self.s3_client.upload_fileobj(
                buffer,
                self.bucket_name,
                full_path,
            )

            return full_path

        except Exception as e:
            raise ValueError(f"Error processing file: {str(e)}")

    def process_result_value(self, value: str, dialect) -> Optional[S3File]:
        """Process the value when retrieving from database."""
        if not value:
            return None
        return S3File(value)
