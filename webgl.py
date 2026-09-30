import argparse
import gzip
import json
import os
import re
import struct
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ingest import find_version, merge, package, unity_version, upload
from scan import prefab_version, unpack_bundle

S3 = "https://s3.eu-west-1.amazonaws.com/webgl.kogstatic.com/"
WAYBACK = "https://web.archive.org/web/"
NAMES = [
    "UnityLoader.js", "UnityConfig.js", "fileloader.js", "WebGLBuild.json", "WebGLBuild.js",
    "WebGLBuild.data", "WebGLBuild.mem", "WebGLBuild.html.mem", "WebGLBuild.data.unityweb",
    "WebGLBuild.asm.code.unityweb", "WebGLBuild.asm.framework.unityweb", "WebGLBuild.asm.memory.unityweb",
    "WebGLBuild.wasm.code.unityweb", "WebGLBuild.wasm.framework.unityweb", "WebGLBuild.loader.js",
    "WebGLBuild.data.gz", "WebGLBuild.framework.js.gz", "WebGLBuild.wasm.gz", "WebGLBuild.data.br",
    "WebGLBuild.framework.js.br", "WebGLBuild.wasm.br",
]
CANDIDATES = ["Version.txt", "index.html"] + [d + n for d in ("Build/", "Release/") for n in NAMES]


def request(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "version-scan"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.headers, r.read() if method == "GET" else b""
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None, None
            err = e
        except Exception as e:
            err = e
    raise RuntimeError(f"{url}: {err}")


def listing(src):
    paths = set(CANDIDATES) | set(src["files"])
    with ThreadPoolExecutor(16) as ex:
        heads = dict(zip(paths, ex.map(lambda p: request(S3 + src["uuid"] + "/" + p, "HEAD")[0], paths)))
    found = {p: h for p, h in heads.items() if h is not None}
    for p in src["wayback"]:
        found.setdefault(p, None)
    return found


def download(src, path):
    headers, body = request(S3 + src["uuid"] + "/" + path)
    if body is None and path in src["wayback"]:
        headers, body = request(WAYBACK + src["wayback"][path].replace("/", "id_/", 1))
    return headers, body


def decode(name, body):
    if body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if name.endswith((".br", ".unityweb")):
        import brotli

        try:
            return brotli.decompress(body)
        except Exception:
            return body
    return body


def unpack_data(blob):
    files = {}
    if blob.startswith(b"UnityWebData1.0\0"):
        o = 16
        end = struct.unpack_from("<I", blob, o)[0]
        o += 4
        while o < end:
            start, size, n = struct.unpack_from("<III", blob, o)
            o += 12
            files[blob[o : o + n].decode("utf-8", "replace")] = blob[start : start + size]
            o += n
    else:
        files["data"] = blob
    engine = None
    for name, data in list(files.items()):
        if data.startswith(b"UnityFS"):
            engine, files[name] = unpack_bundle(data)
    return engine, files


def read_version(texts, files):
    raw = texts.get("Version.txt", "").strip()
    if raw.isdigit():
        n = int(raw)
        return f"{n // 100000}.{n // 1000 % 100}.{n % 1000}"
    found, _ = find_version(files)
    if found:
        return found
    for data in files.values():
        found = prefab_version(data)
        if found:
            return found
    return ""


def build(src, out_dir):
    found = listing(src)
    root = tempfile.mkdtemp(dir=out_dir)
    members, texts, files, engine, stamp, unpacked = [], {}, {}, None, None, 0
    for path in sorted(found):
        headers, body = download(src, path)
        if body is None:
            continue
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(body)
        members.append((full, path))
        plain = decode(path, body)
        unpacked += len(plain)
        if path.endswith((".txt", ".json")):
            texts[os.path.basename(path)] = plain.decode("utf-8", "replace")
        if ".data" in os.path.basename(path):
            engine, files = unpack_data(plain)
            if headers and headers.get("Last-Modified"):
                stamp = int(parsedate_to_datetime(headers["Last-Modified"]).timestamp())
    if not members:
        raise RuntimeError("nothing downloadable")
    if not files:
        raise RuntimeError("no data file")
    engine = engine or unity_version(files) or ""
    m = re.search(r'"unityVersion"\s*:\s*"([^"]+)"', texts.get("WebGLBuild.json", ""))
    if m:
        engine = m.group(1)
    if not stamp and src["firstSeen"]:
        stamp = int(datetime.strptime(src["firstSeen"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
    sha, size, zip_path = package(members, out_dir)
    for full, _ in members:
        os.unlink(full)
    entry = {
        "id": src["uuid"],
        "version": read_version(texts, files),
        "unityVersion": engine,
        "timestamp": stamp or 0,
        "il2cpp": True,
        "zipSize": size,
        "unpackedSize": unpacked,
        "sha256": sha,
        "urls": [],
    }
    kept = {rel for _, rel in members}
    return entry, zip_path, [p for p in found if p not in kept]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--upload", action="store_true", help="push zips to R2 (needs R2_* env)")
    p.add_argument("--merge", nargs="*", metavar="SHARD", help="fold shard results into versions.json")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    if args.merge is not None:
        entries = [e for f in args.merge for e in json.load(open(f, encoding="utf-8"))]
        merge(entries, os.path.join(args.out, "versions.json"))
        return

    sources = json.load(open("webgl-sources.json", encoding="utf-8"))[args.shard :: args.shards]
    if args.limit:
        sources = sources[: args.limit]
    entries, failed = [], []
    for i, src in enumerate(sources, 1):
        print(f"[{i}/{len(sources)}] {src['uuid']}", flush=True)
        try:
            entry, zip_path, missing = build(src, args.out)
        except Exception as e:
            print(f"  failed: {e}", flush=True)
            failed.append({"id": src["uuid"], "error": str(e)})
            continue
        if args.upload:
            upload([entry], {entry["sha256"]: zip_path})
        os.unlink(zip_path)
        entries.append(entry)
        note = f", missing {len(missing)}" if missing else ""
        print(f"  {entry['version'] or 'no version'} on unity {entry['unityVersion'] or '?'}, {entry['zipSize'] >> 20} MB{note}", flush=True)

    with open(os.path.join(args.out, f"webgl-{args.shard}.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=4)
        f.write("\n")
    print(f"{len(entries)} packaged, {len(failed)} failed")
    for f in failed:
        print(f"  {f['id']}: {f['error']}")


if __name__ == "__main__":
    main()
