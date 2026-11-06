import argparse
import hashlib
import os
import urllib.request

BUCKET = "openkogama"
PIECES = [
    (
        "webplayer/unity-webplayer-stable5-5.3.8f1-win.zip",
        "https://archive.org/download/unity-web-player-archive/5.3.8f1/Stable5.x.x.zip",
        "26128d29a20980e2fef2e2ca253796b456e231c20bc351cc6702d838214d5399",
    ),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "version-scan"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % os.environ["R2_ACCOUNT_ID"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--upload", action="store_true", help="put the pieces on the cdn (needs R2_* env)")
    args = p.parse_args()
    s3 = client() if args.upload else None
    for key, url, sha256 in PIECES:
        data = fetch(url)
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise SystemExit(f"{key}: sha256 {actual}, expected {sha256}")
        print(f"{key}: {len(data)} bytes, sha256 ok", flush=True)
        if s3:
            s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/zip")
            print(f"{key}: uploaded", flush=True)


if __name__ == "__main__":
    main()
