import glob
import json

entries = []
for path in sorted(glob.glob("artifacts/**/shard-*.json", recursive=True)):
    entries.extend(json.load(open(path, encoding="utf-8")))

failed = [e for e in entries if "error" in e]
done = sorted((e for e in entries if "error" not in e), key=lambda e: e["timestamp"], reverse=True)

with open("versions.json", "w", encoding="utf-8") as f:
    json.dump({"schema": 2, "versions": done}, f, indent=4)
    f.write("\n")

print(f"{len(done)} packages, {sum(1 for e in done if not e['version'])} without version, {len(failed)} failed")
for e in failed:
    print(f"  {e['id']}: {e['error']}")
