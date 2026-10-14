"""
文件存储：会话的上传文件、中间文件和产物最终保存的地方。

workspaces/ 下的目录只是脚本运行时的工作副本；每次运行结束后同步到这里，
工作副本被回收或服务换了一台机器时，再从这里取回。

    FILE_STORE=local（默认）   存在本机目录 FILE_STORE_DIR（默认 data/files）
    FILE_STORE=s3             存在 S3 兼容的对象存储（私有化部署可用 MinIO），需要：
                              S3_BUCKET、S3_ENDPOINT_URL、S3_ACCESS_KEY、S3_SECRET_KEY，可选 S3_REGION
"""
import os
import shutil
from pathlib import Path

from core import config


class FileStore:
    def put(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def get(self, key: str, path: Path) -> None:
        """把对象取到本地路径 path；不存在时抛 FileNotFoundError。"""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError


class LocalFileStore(FileStore):
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"非法的存储路径: {key}")
        return path

    def put(self, key, path):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(path, tmp)
        os.replace(tmp, dest)

    def get(self, key, path):
        src = self._path(key)
        if not src.is_file():
            raise FileNotFoundError(key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix):
        shutil.rmtree(self._path(prefix.rstrip("/")), ignore_errors=True)


class S3FileStore(FileStore):
    def __init__(self, bucket: str, endpoint_url: str = None, access_key: str = None,
                 secret_key: str = None, region: str = None):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region or "us-east-1",
            # MinIO 等自建服务通常用路径风格的地址
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )

    def ensure_bucket(self):
        from botocore.exceptions import ClientError
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key, path):
        self.client.upload_file(str(path), self.bucket, key)

    def get(self, key, path):
        from botocore.exceptions import ClientError
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, str(path))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise FileNotFoundError(key)
            raise

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})


_store: FileStore | None = None


def get_store() -> FileStore:
    global _store
    if _store is None:
        kind = os.getenv("FILE_STORE", "local").strip().lower()
        if kind == "s3":
            bucket = os.getenv("S3_BUCKET", "").strip()
            if not bucket:
                raise RuntimeError("FILE_STORE=s3 时必须设置 S3_BUCKET")
            store = S3FileStore(
                bucket=bucket,
                endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                access_key=os.getenv("S3_ACCESS_KEY"),
                secret_key=os.getenv("S3_SECRET_KEY"),
                region=os.getenv("S3_REGION"),
            )
            store.ensure_bucket()
            _store = store
        elif kind == "local":
            _store = LocalFileStore(os.getenv("FILE_STORE_DIR") or config.PROJECT_ROOT / "data" / "files")
        else:
            raise RuntimeError(f"未知的 FILE_STORE: {kind}（可选 local / s3）")
    return _store


def configure(store: FileStore) -> FileStore:
    """换用另一个存储（测试用）。"""
    global _store
    _store = store
    return store
