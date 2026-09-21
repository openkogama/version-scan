"""Ingest builds that predate the tracker into versions.json.

scan.py walks versions.md, which only covers packages the tracker knows about.
The oldest of those is 1.25.13.285 (2015-08-19). Everything before it lives in
S3 buckets and archive.org items instead, in shapes scan.py cannot open: raw
UnityWeb (LZMA) bundles, a loose .unity3d and a .rar.

This script fetches those, reads the version out of them, repackages each as a
zip in the same shape as the tracker's packages, and emits schema-2 entries.
Nothing is written to versions.json and nothing is uploaded unless asked.
"""

import argparse
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile

EU = "http://eu.kogama.com.s3.amazonaws.com/player/"
TEST = "http://test.kogama.com.s3.amazonaws.com/player/"
CDN = "https://cdn.openkogama.org/"
SOURCE = "https://raw.githubusercontent.com/openkogama/data/main/versions.json"
BUCKET = "openkogama"
PREFIX = "versions/"

# 2014 web-player builds. eu/ is production, test/ is staging and holds five
# builds production never got. Keys are the md5 of the file.
WEBPLAYER = [
    ("fce0cdb99bdbe84818cfd84eea344082", TEST),
    ("9e7b101483077ee3421b54ae9ecde01c", EU),
    ("394c039725c29775b1cfdf9fd08c5e5c", EU),
    ("72d53c106dd821161e86e4d1b7e8e094", EU),
    ("e0c83c3a992105ce514fc87062e18995", EU),
    ("6245ac01ac47901bba51d9618c972eb9", EU),
    ("812599f82542a6c32d96a3e3157ecd4b", TEST),
    ("a3b9f42b8d5e531596ec4ef5d804d63f", EU),
    ("5c7c6ac82eb0c13afed5d2e6f786897d", TEST),
    ("b862384e4d308fdff859b1908fe8c09a", EU),
    ("1855373468c6b3e2265b566ac9f528ac", EU),
    ("5b08654ae4006fc90f1f864ef26b1201", TEST),
    ("4388665a433282ec1ee7bfdf790e4607", EU),
    ("b751e8f6d68e2465fb0143a719a7a2d6", EU),
    ("b680e3ba74b429ef713f152e134c1270", EU),
    ("b07a914a5990a27035f895cd0859572e", TEST),
    ("70d200c7e81ed590fc98d3d83ce386d0", EU),
    ("88049bbd7aef1f02afa62c82324ef7bb", EU),
]

SOURCES = [
    {
        "name": "koga2011.unity3d",
        "kind": "bundle",
        "urls": ["https://archive.org/download/kogama-2012E-client/koga2011.unity3d"],
    },
] + [
    {
        "name": key + ".unityweb",
        "kind": "bundle",
        "md5": key,
        "urls": [base + key] + ([TEST + key] if base is EU else []),
    }
    for key, base in WEBPLAYER
] + [
    {
        "name": "kogama_Data2015",
        "kind": "rar",
        "urls": ["https://archive.org/download/kogama_Data2015/kogama_Data2015.rar"],
    },
]

PREFAB = re.compile(
    rb"(?s)(.{4})(.{4})(.{4})(.{4})\x24\x00\x00\x00[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
ASSETS = ("resources.assets", "mainData", "sharedassets0.assets", "level0", "globalgamemanagers.assets")
VERSION_FIELDS = ("versionMajor", "versionMinor", "versionMicro", "versionBuild")


def fetch(url, path):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "version-scan"})
            md5 = hashlib.md5()
            size = 0
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                while chunk := r.read(1 << 20):
                    md5.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
            return md5.hexdigest(), size
        except Exception as e:
            err = e
            print(f"  retry {attempt + 1}: {e}", flush=True)
    raise RuntimeError(err)


# --- UnityWeb (LZMA) bundles -------------------------------------------------
# UnityWeb is the pre-Unity-5 web format: a plain header followed by one LZMA
# alone stream. UnityFS (scan.py's data.unity3d path) is LZ4 and unrelated.


def cstr(b, o):
    e = b.index(b"\0", o)
    return b[o:e].decode("utf-8", "replace"), e + 1


def unpack_unityweb(raw):
    sig, o = cstr(raw, 0)
    if sig != "UnityWeb":
        return None, {}
    o += 4
    _, o = cstr(raw, o)
    engine, o = cstr(raw, o)
    o += 4
    header = struct.unpack_from(">i", raw, o)[0]
    data = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw[header:])
    count = struct.unpack_from(">i", data, 0)[0]
    o = 4
    out = {}
    for _ in range(count):
        name, o = cstr(data, o)
        start, size = struct.unpack_from(">ii", data, o)
        o += 8
        out[name] = data[start : start + size]
    return engine, out


# --- serialized assets -------------------------------------------------------


class Reader:
    def __init__(self, b, o=0, le=True):
        self.b, self.o, self.f = b, o, "<" if le else ">"

    def u8(self):
        v = self.b[self.o]
        self.o += 1
        return v

    def i32(self):
        v = struct.unpack_from(self.f + "i", self.b, self.o)[0]
        self.o += 4
        return v

    def u32(self):
        v = struct.unpack_from(self.f + "I", self.b, self.o)[0]
        self.o += 4
        return v

    def i16(self):
        v = struct.unpack_from(self.f + "h", self.b, self.o)[0]
        self.o += 2
        return v

    def cstr(self):
        v, self.o = cstr(self.b, self.o)
        return v

    def align(self, n=4):
        self.o = (self.o + n - 1) & ~(n - 1)


class Node:
    __slots__ = ("type", "name", "size", "flags", "kids")


def read_node(r):
    n = Node()
    n.type = r.cstr()
    n.name = r.cstr()
    n.size = r.i32()
    r.i32()  # index
    r.i32()  # isArray
    r.i32()  # version
    n.flags = r.i32()
    n.kids = [read_node(r) for _ in range(r.i32())]
    return n


def parse_assets(b):
    """Parse a serialized file far enough to reach its objects. v8/v9 only:
    those are the versions that still ship a type tree, which is what makes the
    field names readable."""
    h = Reader(b, 0, le=False)
    meta, size, version, data_off = h.u32(), h.u32(), h.u32(), h.u32()
    if version >= 9:
        le = h.u8() == 0
        h.o += 3
    else:
        h.o = size - meta
        le = h.u8() == 0
    if version > 9:
        return None
    r = Reader(b, h.o, le)
    r.cstr()  # unity version
    r.i32()  # platform
    types = {}
    for _ in range(r.i32()):
        class_id = r.i32()  # read before the node: order matters
        types[class_id] = read_node(r)
    r.i32()  # bigIDEnabled
    objects = []
    for _ in range(r.i32()):
        path_id, start, sz, type_id = r.i32(), r.i32(), r.i32(), r.i32()
        r.i16()
        r.i16()
        objects.append((path_id, start, sz, type_id))
    return {"b": b, "le": le, "data": data_off, "types": types, "objects": objects}


PRIMITIVES = {
    "SInt8": ("b", 1), "UInt8": ("B", 1), "char": ("B", 1), "bool": ("B", 1),
    "SInt16": ("h", 2), "UInt16": ("H", 2), "SInt32": ("i", 4), "int": ("i", 4),
    "UInt32": ("I", 4), "unsigned int": ("I", 4), "SInt64": ("q", 8),
    "UInt64": ("Q", 8), "float": ("f", 4), "double": ("d", 8),
}


def read_value(r, n):
    if n.type in PRIMITIVES:
        f, sz = PRIMITIVES[n.type]
        v = struct.unpack_from(r.f + f, r.b, r.o)[0]
        r.o += sz
        if n.flags & 0x4000:
            r.align()
        return v
    if n.type == "string":
        count = r.i32()
        v = r.b[r.o : r.o + count].decode("utf-8", "replace")
        r.o += count
        r.align()
        return v
    if n.kids and n.kids[0].type == "Array":
        array = n.kids[0]
        count = r.i32()
        element = array.kids[1]
        if element.type in PRIMITIVES and not element.kids:
            f, sz = PRIMITIVES[element.type]
            r.o += sz * count
        else:
            for _ in range(count):
                read_value(r, element)
        r.align()
        return None
    out = {k.name: read_value(r, k) for k in n.kids}
    if n.flags & 0x4000:
        r.align()
    return out


def typetree_version(data):
    """Read the version straight out of a MonoBehaviour, by field name.

    The 2014 client stores it as four ints on a behaviour whose remaining
    fields are UI pointers, so PREFAB's guid anchor never matches. The type
    tree is still present in these files, so the names can simply be read."""
    a = parse_assets(data)
    if not a:
        return None
    for type_id, root in a["types"].items():
        if not set(VERSION_FIELDS) <= {k.name for k in root.kids}:
            continue
        for path_id, start, size, obj_type in a["objects"]:
            if obj_type != type_id:
                continue
            v = read_value(Reader(a["b"], a["data"] + start, a["le"]), root)
            return ".".join(str(v[f]) for f in VERSION_FIELDS)
    return None


def prefab_version(data):
    """scan.py's method: four ints anchored by a trailing build guid."""
    for m in PREFAB.finditer(data):
        n = [int.from_bytes(m.group(i), "little") for i in range(1, 5)]
        if 1 <= n[0] <= 20 and n[1] < 1000 and n[2] < 1000 and n[3] < 1000000:
            return ".".join(map(str, n))
    return None


def find_version(files):
    for name in ASSETS:
        data = next((v for k, v in files.items() if k == name or k.endswith("/" + name)), None)
        if data is None:
            continue
        for reader in (typetree_version, prefab_version):
            try:
                found = reader(data)
            except Exception:
                found = None
            if found:
                return found, name
    return None, None


def unity_version(files):
    for data in files.values():
        m = re.match(rb"[0-9]{1,4}\.[0-9]+\.[0-9]+[a-z][0-9]+", data[20:40])
        if m:
            return m.group().decode()
    for data in files.values():
        m = re.search(rb"[0-9]\.[0-9]+\.[0-9]+[a-z][0-9]+", data[:64])
        if m:
            return m.group().decode()
    return None


def pe_timestamp(files):
    """Compile time of the game assembly. These builds predate the tracker, so
    there is no upstream timestamp to inherit; the PE header is the only date
    that describes the build rather than when someone re-uploaded it."""
    data = next((v for k, v in files.items() if k.endswith("Assembly-CSharp.dll")), None)
    if not data or len(data) < 0x40:
        return None
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off : off + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<I", data, off + 8)[0]


def read_source(source, path):
    """Return {name: bytes} for version reading, and the paths to archive."""
    if source["kind"] == "rar":
        out = tempfile.mkdtemp()
        # unar and p7zip both mis-handle this RAR5; only RARLAB unrar reads it.
        subprocess.run(["unrar", "x", "-y", "-idq", path, out + "/"], check=True)
        files = {}
        members = []
        for root, _, names in os.walk(out):
            for n in names:
                full = os.path.join(root, n)
                rel = os.path.relpath(full, out)
                members.append((full, rel))
                if n in ASSETS or n.endswith("Assembly-CSharp.dll"):
                    files[rel] = open(full, "rb").read()
        return files, members, out
    engine, files = unpack_unityweb(open(path, "rb").read())
    return files, [(path, source["name"])], None


def package(members, out_dir):
    """Zip the build and name it by its own sha256, which is also its cdn key.

    Two 2014 builds carry the same version string, so the version cannot name
    the file. Entries are written with a fixed timestamp and in sorted order so
    a re-run produces a byte-identical zip and does not re-upload."""
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=out_dir)
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, rel in sorted(members, key=lambda m: m[1]):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f:
                z.writestr(info, f.read())
    sha = hashlib.sha256()
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
    digest = sha.hexdigest()
    dest = os.path.join(out_dir, digest + ".zip")
    os.replace(tmp, dest)
    return digest, os.path.getsize(dest), dest


def upload(entries, zips):
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % os.environ["R2_ACCOUNT_ID"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    s3.head_bucket(Bucket=BUCKET)
    for entry in entries:
        key = PREFIX + entry["sha256"] + ".zip"
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            print(f"  {entry['version'] or entry['id']} cached", flush=True)
        except Exception:
            with open(zips[entry["sha256"]], "rb") as f:
                s3.put_object(
                    Bucket=BUCKET,
                    Key=key,
                    Body=f,
                    ContentType="application/zip",
                    CacheControl="public, max-age=31536000, immutable",
                )
            print(f"  {entry['version'] or entry['id']} uploaded", flush=True)
        entry["urls"] = [CDN + key] + [u for u in entry["urls"] if u != CDN + key]


def merge(entries, dest):
    """Fold the new entries into the published versions.json, newest first."""
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "version-scan"})
    with urllib.request.urlopen(req, timeout=120) as r:
        doc = json.load(r)
    have = {e["sha256"] for e in doc["versions"]}
    added = [e for e in entries if e["sha256"] not in have]
    doc["versions"] = sorted(doc["versions"] + added, key=lambda e: e["timestamp"], reverse=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=4)
        f.write("\n")
    print(f"{len(added)} added, {len(doc['versions'])} entries total")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out", help="where entries and zips are written")
    p.add_argument("--upload", action="store_true", help="push zips to R2 (needs R2_* env)")
    p.add_argument("--keep-zips", action="store_true", help="do not delete zips after the run")
    p.add_argument("--limit", type=int, default=0, help="only the first N sources, for a smoke test")
    p.add_argument("--merge", action="store_true", help="write versions.json with these entries folded in")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    entries, zips, failed = [], {}, []

    sources = SOURCES[: args.limit] if args.limit else SOURCES
    for i, source in enumerate(sources, 1):
        print(f"[{i}/{len(sources)}] {source['name']}", flush=True)
        fd, raw = tempfile.mkstemp()
        os.close(fd)
        scratch = None
        try:
            err = None
            for url in source["urls"]:
                try:
                    md5, size = fetch(url, raw)
                    break
                except Exception as e:
                    err = e
            else:
                print(f"  failed: {err}", flush=True)
                failed.append({"name": source["name"], "error": str(err)})
                continue

            if "md5" in source and md5 != source["md5"]:
                print(f"  md5 {md5}, expected {source['md5']}", flush=True)
                failed.append({"name": source["name"], "error": f"md5 {md5}"})
                continue

            files, members, scratch = read_source(source, raw)
            version, found_in = find_version(files)
            engine = unity_version(files)
            stamp = pe_timestamp(files)

            zip_sha, zip_size, zip_path = package(members, args.out)
            unpacked = sum(len(v) for v in files.values()) if source["kind"] == "bundle" else sum(
                os.path.getsize(f) for f, _ in members
            )

            entries.append(
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_OID, zip_sha)),
                    "version": version or "",
                    "unityVersion": engine or "",
                    "timestamp": stamp or 0,
                    "il2cpp": False,
                    "zipSize": zip_size,
                    "unpackedSize": unpacked,
                    "sha256": zip_sha,
                    "urls": [source["urls"][0]],
                }
            )
            zips[zip_sha] = zip_path
            print(f"  {version or 'no version'} on unity {engine or '?'} ({found_in or '-'})", flush=True)
        except Exception as e:
            # unrar missing, a malformed bundle: report it and keep going
            print(f"  failed: {e}", flush=True)
            failed.append({"name": source["name"], "error": str(e)})
        finally:
            os.unlink(raw)
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)

    if args.upload:
        print("uploading", flush=True)
        upload(entries, zips)

    with open(os.path.join(args.out, "ingest.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=4)
        f.write("\n")

    if not args.keep_zips and not args.upload:
        for path in zips.values():
            os.unlink(path)

    if args.merge:
        merge(entries, os.path.join(args.out, "versions.json"))

    versioned = sum(1 for e in entries if e["version"])
    print(f"{len(entries)} packages, {versioned} with a version, {len(entries) - versioned} without, {len(failed)} failed")
    for f in failed:
        print(f"  {f['name']}: {f['error']}")


if __name__ == "__main__":
    main()
