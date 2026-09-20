import glob
import json
import urllib.request

SOURCE = "https://raw.githubusercontent.com/openkogama/data/main/versions.json"
CDN = "https://cdn.openkogama.org/"

mirrored = {}
for path in sorted(glob.glob("artifacts/**/shard-*.json", recursive=True)):
    for r in json.load(open(path, encoding="utf-8")):
        if "key" in r:
            mirrored[r["sha256"]] = CDN + r["key"]

req = urllib.request.Request(SOURCE, headers={"User-Agent": "version-scan"})
with urllib.request.urlopen(req, timeout=120) as r:
    doc = json.load(r)

added = 0
for entry in doc["versions"]:
    url = mirrored.get(entry["sha256"])
    if not url:
        continue
    entry["urls"] = [url] + [u for u in entry["urls"] if u != url]
    added += 1

with open("versions.json", "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=4)
    f.write("\n")

print(f"{added} of {len(doc['versions'])} entries point at the cdn")
