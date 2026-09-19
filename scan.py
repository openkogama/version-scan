import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.request
import zipfile

ROW = re.compile(
    r"^(?P<id>[0-9a-fA-F-]{36})\|(?P<v>[^|]*)\|(?P<il2cpp>[^|]*)\|(?P<ts>\d+)\|.*?\((?P<url>https://[^)]+)\)"
)
PREFAB = re.compile(
    rb"(?s)(.{4})(.{4})(.{4})(.{4})\x24\x00\x00\x00[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
NUMBER = re.compile(rb"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}")
ASSETS = ("resources.assets", "sharedassets0.assets", "globalgamemanagers.assets", "level0", "mainData")
SETTINGS = ("globalgamemanagers", "mainData")


def packages():
    out = []
    for line in open("versions.md", encoding="utf-8"):
        m = ROW.match(line.strip())
        if m:
            out.append(
                {
                    "id": m["id"],
                    "timestamp": int(m["ts"]),
                    "il2cpp": "heavy_check_mark" in m["il2cpp"],
                    "url": m["url"],
                }
            )
    return out


def fetch(url, path):
    for attempt in range(3):
        try:
            sha = hashlib.sha256()
            size = 0
            req = urllib.request.Request(url, headers={"User-Agent": "version-scan"})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
                while chunk := r.read(1 << 20):
                    sha.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
            return sha.hexdigest(), size
        except Exception as e:
            err = e
            print(f"  retry {attempt + 1}: {e}", flush=True)
    raise RuntimeError(err)


def prefab_version(data):
    for m in PREFAB.finditer(data):
        n = [int.from_bytes(m.group(i), "little") for i in range(1, 5)]
        if 1 <= n[0] <= 20 and n[1] < 1000 and n[2] < 1000 and n[3] < 1000000:
            return ".".join(map(str, n))
    return None


def bundle_version(data):
    head = NUMBER.search(data[:64])
    engine = head.group() if head else b""
    found = [m.group().decode() for m in NUMBER.finditer(data[64:]) if m.group() != engine]
    found = [v for v in found if v != "1.0"]
    return next((v for v in found if v.count(".") >= 2), found[0] if found else None)


def entry(names, wanted):
    return next((n for n in names if n == wanted or n.endswith("/" + wanted)), None)


def inspect(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        unpacked = sum(i.file_size for i in z.infolist())
        for name, reader in [(f, prefab_version) for f in ASSETS] + [(f, bundle_version) for f in SETTINGS]:
            found = entry(names, name)
            if found:
                version = reader(z.read(found))
                if version:
                    return unpacked, version, name
    return unpacked, None, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    args = p.parse_args()

    all_packages = packages()
    mine = [p for i, p in enumerate(all_packages) if i % args.shards == args.shard]
    print(f"shard {args.shard}/{args.shards}: {len(mine)} of {len(all_packages)}", flush=True)

    os.makedirs("out", exist_ok=True)
    results = []
    for i, pkg in enumerate(mine, 1):
        print(f"[{i}/{len(mine)}] {pkg['id']}", flush=True)
        fd, zip_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            sha, zip_size = fetch(pkg["url"], zip_path)
            unpacked, version, source = inspect(zip_path)
        except Exception as e:
            print(f"  failed: {e}", flush=True)
            results.append({**pkg, "error": str(e)})
            continue
        finally:
            os.unlink(zip_path)

        print(f"  {version or 'no version'} ({source or '-'})", flush=True)
        results.append(
            {
                "id": pkg["id"],
                "version": version or "",
                "timestamp": pkg["timestamp"],
                "il2cpp": pkg["il2cpp"],
                "zipSize": zip_size,
                "unpackedSize": unpacked,
                "sha256": sha,
                "url": pkg["url"],
            }
        )

    with open(f"out/shard-{args.shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4)


if __name__ == "__main__":
    main()
