"""Result storage: S3-compatible (TACC Corral S3, AWS, MinIO, ...) or a plain directory.

[storage].url = "s3://bucket/prefix"  (+ endpoint_url; keys from env AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)
[storage].url = "/some/dir" or "file:///some/dir"   (testing, shared filesystems)
"""
import shutil
from pathlib import Path


class Storage:
    def __init__(self, cfg):
        url = cfg["url"]
        self.s3 = url.startswith("s3://")
        if self.s3:
            import boto3
            self.bucket, _, self.prefix = url[5:].partition("/")
            self.client = boto3.client("s3", endpoint_url=cfg.get("endpoint_url") or None)
        else:
            self.root = Path(url.removeprefix("file://")).expanduser()

    def _key(self, *parts):
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p)

    def upload_dir(self, local, remote):
        local = Path(local)
        if not self.s3:
            shutil.copytree(local, self.root / remote, dirs_exist_ok=True)
            return str(self.root / remote)
        for f in local.rglob("*"):
            if f.is_file():
                self.client.upload_file(str(f), self.bucket, self._key(remote, str(f.relative_to(local))))
        return f"s3://{self.bucket}/{self._key(remote)}"

    def upload_file(self, local, remote):
        if not self.s3:
            dest = self.root / remote
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(local, dest)
        else:
            self.client.upload_file(str(local), self.bucket, self._key(remote))

    def url(self, remote):
        return f"s3://{self.bucket}/{self._key(remote)}" if self.s3 else str(self.root / remote)

    def download_dir(self, remote, local):
        local = Path(local)
        if not self.s3:
            shutil.copytree(self.root / remote, local, dirs_exist_ok=True)
            return local
        prefix = self._key(remote) + "/"
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                dest = local / obj["Key"][len(prefix):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.client.download_file(self.bucket, obj["Key"], str(dest))
        return local
