import argparse
import hashlib
import json
import os
import tempfile
import urllib.request

SOURCE = "https://raw.githubusercontent.com/openkogama/data/main/versions.json"
BUCKET = "openkogama"
PREFIX = "versions/"
CDN = "https://cdn.openkogama.org/"


def versions():
    if os.path.exists("versions.json"):
        return json.load(open("versions.json", encoding="utf-8"))["versions"]
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "version-scan"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["versions"]


def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "version-scan"})
    sha = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 20):
            sha.update(chunk)
            size += len(chunk)
            f.write(chunk)
    return sha.hexdigest(), size


def client():
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % os.environ["R2_ACCOUNT_ID"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    s3.head_bucket(Bucket=BUCKET)
    return s3


def mirror(s3, entry):
    key = PREFIX + entry["sha256"] + ".zip"
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return key, "cached"
    except Exception:
        pass
    sources = [u for u in entry["urls"] if not u.startswith(CDN)]
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        err = None
        for url in sources:
            try:
                digest, size = fetch(url, tmp)
            except Exception as e:
                err = e
                continue
            if digest != entry["sha256"]:
                err = RuntimeError("sha256 %s, expected %s" % (digest, entry["sha256"]))
                continue
            if size != entry["zipSize"]:
                err = RuntimeError("size %d, expected %d" % (size, entry["zipSize"]))
                continue
            with open(tmp, "rb") as f:
                s3.put_object(
                    Bucket=BUCKET,
                    Key=key,
                    Body=f,
                    ContentType="application/zip",
                    CacheControl="public, max-age=31536000, immutable",
                )
            return key, "uploaded"
        raise RuntimeError(err)
    finally:
        os.remove(tmp)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    everything = versions()
    mine = [e for i, e in enumerate(everything) if i % args.shards == args.shard]
    if args.limit:
        mine = mine[: args.limit]
    print(f"shard {args.shard}/{args.shards}: {len(mine)} of {len(everything)}", flush=True)

    s3 = client()
    os.makedirs("out", exist_ok=True)
    results = []
    for i, entry in enumerate(mine, 1):
        try:
            key, how = mirror(s3, entry)
            results.append({"id": entry["id"], "sha256": entry["sha256"], "key": key})
        except Exception as e:
            how = str(e)
            results.append({"id": entry["id"], "sha256": entry["sha256"], "error": str(e)})
        print(f"[{i}/{len(mine)}] {entry['version'] or entry['id']} {how}", flush=True)

    with open(f"out/shard-{args.shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    print(f"{sum(1 for r in results if 'key' in r)} mirrored, {sum(1 for r in results if 'error' in r)} failed")


main()
