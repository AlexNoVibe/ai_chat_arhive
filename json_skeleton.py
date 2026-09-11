import json
from pathlib import Path
import re

def repair_json_text(raw):
    return re.sub(r',(\s*[\]\}])', r'\1', raw.lstrip("\ufeff"))

def skeleton(value):
    if isinstance(value, dict):
        return {k: skeleton(v) for k, v in value.items()}
    if isinstance(value, list):
        seen = []
        for item in value:
            s = skeleton(item)
            if s not in seen:
                seen.append(s)
        return seen
    if isinstance(value, str):
        return ""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    return None

def main():
    here = Path(__file__).resolve().parent
    files = sorted(here.glob("*.json"))
    done = 0
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(repair_json_text(raw))
        except Exception as exc:
            print(f"skip {path.name}: {exc}")
            continue
        out = here / f"{path.stem}_skeleton.json"
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(skeleton(data), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        done += 1
        print(f"{path.name} -> {out.name}")
    print(f"{done} of {len(files)} JSON file(s) processed")

if __name__ == "__main__":
    main()