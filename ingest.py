"""Ingest builds the tracker does not have into versions.json.

scan.py walks versions.md, which only covers packages the tracker knows about.
The oldest of those is 1.25.13.285 (2015-08-19). Everything before it lives in
S3 buckets and archive.org items instead, in shapes scan.py cannot open: raw
UnityWeb (LZMA) bundles, a loose .unity3d and a .rar. Later web-player bundles
and some launcher packages the tracker missed only survive in web archives.

This script fetches those, reads the version out of them, repackages each as a
zip in the same shape as the tracker's packages (launcher packages already are
one and stay as they are), and emits schema-2 entries.
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
import time
import urllib.request
import uuid
import zipfile

import scan

EU = "http://eu.kogama.com.s3.amazonaws.com/player/"
TEST = "http://test.kogama.com.s3.amazonaws.com/player/"
WAYBACK = "https://web.archive.org/web/"
ARQUIVO = "https://arquivo.pt/wayback/"
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

# 2015-2018 web-player bundles that only web archives kept. They need the
# Unity 5 web player. md5 is the S3 ETag where an archive kept the headers.
WEBPLAYER5 = [
    ("c0ccaf78-4da4-42b2-4d58-98a94a881985", ARQUIVO + "20150409175128id_/http://game-static.kogstatic.com/c0ccaf78-4da4-42b2-4d58-98a94a881985.bin", "ec901c04de69640610d956c587b231c5"),
    ("eca5d205-a3c9-4e9a-5993-bc3260c215fd", WAYBACK + "20151029130537id_/http://game-static.kogstatic.com/eca5d205-a3c9-4e9a-5993-bc3260c215fd.bin", None),
    ("82a9e2a5-a374-40e6-54c6-4f22300ccc77", WAYBACK + "20151112074238id_/http://game-static.kogstatic.com/82a9e2a5-a374-40e6-54c6-4f22300ccc77.bin", None),
    ("75eee924-8ee3-4e2a-4df0-13eef585f2f1", WAYBACK + "20230614225242id_/http://game-static.kogstatic.com/75eee924-8ee3-4e2a-4df0-13eef585f2f1.bin", None),
    ("0dceed3d-9818-432e-6833-537832d13ac5", WAYBACK + "20150807223305id_/http://game-static.kogstatic.com/0dceed3d-9818-432e-6833-537832d13ac5.bin", None),
    ("34301157-dc66-4900-6a62-88c15f6c8d44", WAYBACK + "20150810221702id_/http://game-static.kogstatic.com/34301157-dc66-4900-6a62-88c15f6c8d44.bin", None),
    ("2e4b1f74-389c-496f-5070-076340144290", ARQUIVO + "20150815012556id_/http://game-static.kogstatic.com/2e4b1f74-389c-496f-5070-076340144290.bin", "ed0d3082a0fedcd669ce8d9db519dae9"),
    ("41c9b1ac-baad-4016-9891-e15c2a24b943", ARQUIVO + "20151113225727id_/http://files.kogstatic.com:8089/41c9b1ac-baad-4016-9891-e15c2a24b943/KogamaData?_t=1447229353", "c4daa5ef900b510750d35760fb225c45"),
    ("eb2f74db-a28b-4bca-4974-a733a767d3bf", WAYBACK + "20151213115333id_/http://game-static.kogstatic.com/eb2f74db-a28b-4bca-4974-a733a767d3bf.bin", None),
    ("3b6ff6fc-c608-42eb-a952-35376dd2baa0", ARQUIVO + "20160205235028id_/http://files.kogstatic.com:8089/3b6ff6fc-c608-42eb-a952-35376dd2baa0/KogamaData?_t=1454479849", "bea5c281fdec011eb6df3df557e06a7c"),
    ("dd04eb1c-4385-4862-9305-4e2d386c7ffa", ARQUIVO + "20160622070425id_/http://files.kogstatic.com:8089/dd04eb1c-4385-4862-9305-4e2d386c7ffa/KogamaData?_t=1465384277", "be7b4f9d1ec4f1c26dadceb9476cb0a4"),
    ("43a374bc-b9f3-4f97-6628-5009884e59b3", WAYBACK + "20160730193510id_/http://game-static.kogstatic.com/43a374bc-b9f3-4f97-6628-5009884e59b3.bin", None),
    ("08b9a9ee-d325-41f2-64ed-cc314d78e137", WAYBACK + "20160920095741id_/http://game-static.kogstatic.com/08b9a9ee-d325-41f2-64ed-cc314d78e137.bin", None),
    ("a958aa27-f674-435f-bf54-57c9e3ab6c7e", ARQUIVO + "20161102040953id_/http://files.kogstatic.com:8089/a958aa27-f674-435f-bf54-57c9e3ab6c7e/KogamaData?_t=1477570639", "e335db0acb6efca4d592cdac8a995b4a"),
    ("80e5a54a-b64f-40f7-8284-832cf92bb041", ARQUIVO + "20170211124921id_/http://files.kogstatic.com:8089/80e5a54a-b64f-40f7-8284-832cf92bb041/KogamaData?_t=1484835145", "fdab749bcfff0c1642f446574c72a4a1"),
    ("7bb266c5-9260-43af-9412-d3db5e7fff8e", ARQUIVO + "20170802115920id_/http://files.kogstatic.com:8089/7bb266c5-9260-43af-9412-d3db5e7fff8e/KogamaData?_t=1500889147", "51af107be6fbc08be16276390f60d834"),
    ("29f118c2-a5a4-4111-7e70-c9f5770b10dc", WAYBACK + "20220926040738id_/http://game-static.kogstatic.com/29f118c2-a5a4-4111-7e70-c9f5770b10dc.bin", None),
    ("8b5be47a-79c8-42f3-9dac-3f91a8e22c0a", ARQUIVO + "20171210074038id_/http://files.kogstatic.com:8089/8b5be47a-79c8-42f3-9dac-3f91a8e22c0a/KogamaData?_t=1512724249", "aa43f46927cc9c8adf471688dc07bfe1"),
    ("1745d267-4f10-4549-b149-84700b59b3f2", ARQUIVO + "20180415011408id_/http://files.kogstatic.com:8089/1745d267-4f10-4549-b149-84700b59b3f2/KogamaData?_t=1522159530", "9a11dcb50d65c6b3944957c55d6846a7"),
]

# launcher packages from 2018-2023 the tracker never saw
STANDALONE = [
    ("53b9554b-f483-4101-bdb2-4e41ccadef17", WAYBACK + "20180823060037id_/https://files.kogstatic.com/53b9554b-f483-4101-bdb2-4e41ccadef17/KogamaData?_t=1534959803"),
    ("38946fc6-65ab-496d-95d3-cfd44dc70eb3", WAYBACK + "20180906073733id_/https://files.kogstatic.com/38946fc6-65ab-496d-95d3-cfd44dc70eb3/KogamaData?_t=1535981492"),
    ("bb1fe52a-3188-48c6-99f0-1ec7e9d9f60d", WAYBACK + "20180913072233id_/https://files.kogstatic.com/bb1fe52a-3188-48c6-99f0-1ec7e9d9f60d/KogamaData?_t=1536583289"),
    ("c2ec033b-8d50-4c22-ba5f-2d1eb2528c35", WAYBACK + "20180920075222id_/https://files.kogstatic.com/c2ec033b-8d50-4c22-ba5f-2d1eb2528c35/KogamaData?_t=1536848240"),
    ("78d93b38-a52b-4d04-ae63-4a83dadab659", WAYBACK + "20181004100018id_/https://files.kogstatic.com/78d93b38-a52b-4d04-ae63-4a83dadab659/KogamaData?_t=1538122720"),
    ("9541e5e5-b504-4cb5-9112-849f9cc4ae5e", WAYBACK + "20181018120306id_/https://files.kogstatic.com/9541e5e5-b504-4cb5-9112-849f9cc4ae5e/KogamaData?_t=1539851075"),
    ("2f252e69-182e-463b-a075-ae5931a08e4d", WAYBACK + "20181025153443id_/https://files.kogstatic.com/2f252e69-182e-463b-a075-ae5931a08e4d/KogamaData?_t=1540478334"),
    ("84d80dd8-d6fb-4178-b654-9274d6718d3b", WAYBACK + "20181107064522id_/https://files.kogstatic.com/84d80dd8-d6fb-4178-b654-9274d6718d3b/KogamaData?_t=1541425541"),
    ("c054e354-3722-4d7a-a6fe-7b559e753f5c", WAYBACK + "20181108144031id_/https://files.kogstatic.com/c054e354-3722-4d7a-a6fe-7b559e753f5c/KogamaData?_t=1541669091"),
    ("dd53584b-1da0-45f3-b5f5-27ca8be70730", WAYBACK + "20181115151347id_/https://files.kogstatic.com/dd53584b-1da0-45f3-b5f5-27ca8be70730/KogamaData?_t=1542286913"),
    ("0966248c-f44a-4654-977f-17b72944866f", WAYBACK + "20190208013058id_/https://files.kogstatic.com/0966248c-f44a-4654-977f-17b72944866f/KogamaData?_t=1549275671"),
    ("c0b5ed9d-1ae5-46ff-9968-d7ed5160550a", WAYBACK + "20190215020624id_/https://files.kogstatic.com/c0b5ed9d-1ae5-46ff-9968-d7ed5160550a/KogamaData?_t=1549892827"),
    ("69883aaf-96e7-4d60-be1e-1c53a8142f80", WAYBACK + "20190222023814id_/https://files.kogstatic.com/69883aaf-96e7-4d60-be1e-1c53a8142f80/KogamaData?_t=1550669204"),
    ("ed38b0d2-7596-44d7-a2c9-0946cfbc27dd", WAYBACK + "20190308030141id_/https://files.kogstatic.com/ed38b0d2-7596-44d7-a2c9-0946cfbc27dd/KogamaData?_t=1551957579"),
    ("32534dd0-66d6-4115-9ed7-c97bde23a69f", WAYBACK + "20190506035922id_/https://files.kogstatic.com/32534dd0-66d6-4115-9ed7-c97bde23a69f/KogamaData?_t=1555327319"),
    ("d6dc67b5-c689-464d-9c12-2611dc8c341a", WAYBACK + "20190617200302id_/https://files.kogstatic.com/d6dc67b5-c689-464d-9c12-2611dc8c341a/KogamaData?_t=1560348748"),
    ("2c7196ea-c535-45ec-a2f5-59aaa4ad83be", WAYBACK + "20190624205109id_/https://files.kogstatic.com/2c7196ea-c535-45ec-a2f5-59aaa4ad83be/KogamaData?_t=1561367559"),
    ("4e3222cf-d04c-460c-af4d-abd75ce20e16", WAYBACK + "20190724012134id_/https://files.kogstatic.com/4e3222cf-d04c-460c-af4d-abd75ce20e16/KogamaData?_t=1563354148"),
    ("92b15127-3809-4aeb-a322-bada388a8179", WAYBACK + "20190731011129id_/https://files.kogstatic.com/92b15127-3809-4aeb-a322-bada388a8179/KogamaData?_t=1564487557"),
    ("d5db238c-695b-4550-803f-6fae0c531153", WAYBACK + "20190807033205id_/https://files.kogstatic.com/d5db238c-695b-4550-803f-6fae0c531153/KogamaData?_t=1564652117"),
    ("9b421f13-bd68-4503-8f99-a94612906a5d", WAYBACK + "20190925071920id_/https://files.kogstatic.com/9b421f13-bd68-4503-8f99-a94612906a5d/KogamaData?_t=1569311562"),
    ("ea700e71-4683-4e55-bd9a-bb4895231858", WAYBACK + "20191002051618id_/https://files.kogstatic.com/ea700e71-4683-4e55-bd9a-bb4895231858/KogamaData?_t=1569834709"),
    ("1f63ddff-daaf-4b1f-8b53-a25f7059ac7b", WAYBACK + "20191009055444id_/https://files.kogstatic.com/1f63ddff-daaf-4b1f-8b53-a25f7059ac7b/KogamaData?_t=1570089234"),
    ("2db39f9f-6ddf-45ab-9953-062d856571b3", WAYBACK + "20191029002008id_/https://files.kogstatic.com/2db39f9f-6ddf-45ab-9953-062d856571b3/KogamaData?_t=1571748788"),
    ("735e6a7b-e9fd-49fe-bcb6-b8ee26ea63a0", WAYBACK + "20191108034125id_/https://files.kogstatic.com/735e6a7b-e9fd-49fe-bcb6-b8ee26ea63a0/KogamaData?_t=1572956193"),
    ("036576e4-a609-461a-9d97-1fa8eb643cab", WAYBACK + "20191115011529id_/https://files.kogstatic.com/036576e4-a609-461a-9d97-1fa8eb643cab/KogamaData?_t=1573742862"),
    ("3677a71c-3fc9-4e96-b9bf-0eaa345258d2", WAYBACK + "20191122065638id_/https://files.kogstatic.com/3677a71c-3fc9-4e96-b9bf-0eaa345258d2/KogamaData?_t=1574335382"),
    ("f6248219-40b8-4ded-bc9e-c7ba46591607", WAYBACK + "20200125061510id_/https://files.kogstatic.com/f6248219-40b8-4ded-bc9e-c7ba46591607/KogamaData?_t=1579693110"),
    ("54fb36bb-6b3c-4cca-b48e-802f13b539dc", WAYBACK + "20200328111826id_/https://files.kogstatic.com/54fb36bb-6b3c-4cca-b48e-802f13b539dc/KogamaData?_t=1585216739"),
    ("5ed21e86-2007-4d5b-a2b5-2b94bfd87172", WAYBACK + "20210531181842id_/https://files.kogstatic.com/5ed21e86-2007-4d5b-a2b5-2b94bfd87172/KogamaData?_t=1621580417"),
    ("fda1339a-a66e-4fd4-a8f7-196f01dfd47b", WAYBACK + "20210827000045id_/https://files.kogstatic.com/fda1339a-a66e-4fd4-a8f7-196f01dfd47b/KogamaData?_t=1629701779"),
    ("880192b9-81db-4b25-a14f-4d2fbe38c744", WAYBACK + "20240114174252id_/http://files.kogstatic.com/880192b9-81db-4b25-a14f-4d2fbe38c744/KogamaData?_t=1692269634"),
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
] + [
    {
        "name": key + ".unityweb",
        "kind": "bundle",
        "group": "webplayer5",
        "id": key,
        "urls": [url],
        **({"md5": md5} if md5 else {}),
    }
    for key, url, md5 in WEBPLAYER5
] + [
    {
        "name": key + ".zip",
        "kind": "zip",
        "group": "standalone",
        "id": key,
        "urls": [url],
    }
    for key, url in STANDALONE
]
GROUPS = ("legacy", "webplayer5", "standalone")

PREFAB = re.compile(
    rb"(?s)(.{4})(.{4})(.{4})(.{4})\x24\x00\x00\x00[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
ASSETS = ("resources.assets", "mainData", "sharedassets0.assets", "level0", "globalgamemanagers.assets")
VERSION_FIELDS = ("versionMajor", "versionMinor", "versionMicro", "versionBuild")


def fetch(url, path):
    for attempt in range(5):
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
            time.sleep(30 * (attempt + 1))
    raise RuntimeError(err)


# --- UnityWeb (LZMA) bundles -------------------------------------------------
# UnityWeb up to format 5 is a plain header followed by one LZMA alone stream.
# Format 6 (the Unity 5.3 web player) keeps the signature but is laid out like
# UnityFS (plus one byte after the header): a block table, LZ4 or LZMA
# blocks, and a directory of nodes.


def cstr(b, o):
    e = b.index(b"\0", o)
    return b[o:e].decode("utf-8", "replace"), e + 1


def decompress(chunk, flags, size):
    kind = flags & 0x3F
    if kind == 1:
        props, dict_size = chunk[0], struct.unpack_from("<I", chunk, 1)[0]
        lzma1 = {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": props % 9, "lp": props // 9 % 5, "pb": props // 45}
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[lzma1]).decompress(chunk[5:], size)
    if kind in (2, 3):
        return scan.lz4(chunk, size)
    return chunk


def unpack_archive(raw, o):
    _, packed, size, flags = struct.unpack_from(">qIII", raw, o)
    o += 21
    if flags & 0x80:
        info = raw[len(raw) - packed :]
    else:
        info = raw[o : o + packed]
        o += packed
    info = decompress(info, flags, size)
    data = bytearray()
    p = 20
    for _ in range(struct.unpack_from(">i", info, 16)[0]):
        u, c, block_flags = struct.unpack_from(">IIH", info, p)
        p += 10
        data += decompress(raw[o : o + c], block_flags, u)
        o += c
    out = {}
    count = struct.unpack_from(">i", info, p)[0]
    p += 4
    for _ in range(count):
        start, length, _ = struct.unpack_from(">qqI", info, p)
        name, p = cstr(info, p + 20)
        out[name] = bytes(data[start : start + length])
    return out


def unpack_unityweb(raw):
    sig, o = cstr(raw, 0)
    if sig != "UnityWeb":
        return None, {}
    fmt = struct.unpack_from(">i", raw, o)[0]
    o += 4
    _, o = cstr(raw, o)
    engine, o = cstr(raw, o)
    if fmt >= 6:
        return engine, unpack_archive(raw, o)
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


def keep(path, out_dir):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
    digest = sha.hexdigest()
    dest = os.path.join(out_dir, digest + ".zip")
    shutil.copyfile(path, dest)
    return digest, os.path.getsize(dest), dest


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


def upload(entries, zips):
    s3 = client()
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


def publish(path):
    with open(path, "rb") as f:
        client().put_object(Bucket=BUCKET, Key="versions.json", Body=f, ContentType="application/json")
    print("versions.json published", flush=True)


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
    p.add_argument("--publish", action="store_true", help="with --merge, put versions.json on the cdn (needs R2_* env)")
    p.add_argument("--keep-zips", action="store_true", help="do not delete zips after the run")
    p.add_argument("--limit", type=int, default=0, help="only the first N sources, for a smoke test")
    p.add_argument("--only", default="", help="comma separated source groups (" + ", ".join(GROUPS) + "), none for no sources")
    p.add_argument("--merge", action="store_true", help="write versions.json with these entries folded in")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    entries, zips, failed = [], {}, []

    wanted = {g.strip() for g in args.only.split(",") if g.strip()}
    if wanted - set(GROUPS) - {"none"}:
        p.error(f"unknown groups: {', '.join(sorted(wanted - set(GROUPS) - {'none'}))}")
    sources = [s for s in SOURCES if not wanted or s.get("group", "legacy") in wanted]
    sources = sources[: args.limit] if args.limit else sources
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

            if source["kind"] == "zip":
                with zipfile.ZipFile(raw) as z:
                    bad = z.testzip()
                    il2cpp = any(n.endswith("GameAssembly.dll") for n in z.namelist())
                if bad:
                    raise RuntimeError(f"bad crc in {bad}")
                unpacked, version, engine, found_in = scan.inspect(raw)
                stamp = int(re.search(r"[?&]_t=(\d+)", source["urls"][0]).group(1))
                zip_sha, zip_size, zip_path = keep(raw, args.out)
            else:
                files, members, scratch = read_source(source, raw)
                version, found_in = find_version(files)
                engine = unity_version(files)
                stamp = pe_timestamp(files)
                il2cpp = False
                zip_sha, zip_size, zip_path = package(members, args.out)
                unpacked = sum(len(v) for v in files.values()) if source["kind"] == "bundle" else sum(
                    os.path.getsize(f) for f, _ in members
                )

            entries.append(
                {
                    "id": source.get("id") or str(uuid.uuid5(uuid.NAMESPACE_OID, zip_sha)),
                    "version": version or "",
                    "unityVersion": engine or "",
                    "timestamp": stamp or 0,
                    "il2cpp": il2cpp,
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
        if args.publish:
            publish(os.path.join(args.out, "versions.json"))

    versioned = sum(1 for e in entries if e["version"])
    print(f"{len(entries)} packages, {versioned} with a version, {len(entries) - versioned} without, {len(failed)} failed")
    for f in failed:
        print(f"  {f['name']}: {f['error']}")


if __name__ == "__main__":
    main()
