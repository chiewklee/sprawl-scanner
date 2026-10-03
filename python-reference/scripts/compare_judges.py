"""Re-judge a report's gray pairs with another model and compare against the stored
verdicts. Disagreements get a third opinion from a tiebreak model.
Usage: compare_judges.py REPORT.json NEW_MODEL TIEBREAK_MODEL"""

import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sprawl_scanner.embed import load_key_file
from sprawl_scanner.judge import PROMPT

load_key_file()
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

report, new_model, tiebreak = sys.argv[1:4]
d = json.load(open(report))
caps = {c["id"]: c for c in d["capabilities"]}
pairs = [p for p in d["gray_pairs"] if p.get("verdict")]
client = genai.Client()
config = types.GenerateContentConfig(
    response_mime_type="application/json", temperature=0,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def ask(model: str, p: dict) -> dict:
    a, b = caps[p["a"]], caps[p["b"]]
    prompt = PROMPT.format(a_type=a["asset_type"], a_asset=a["asset"], a_name=a["name"],
                           a_desc=a["description"][:800], b_type=b["asset_type"],
                           b_asset=b["asset"], b_name=b["name"], b_desc=b["description"][:800])
    for attempt in range(3):
        try:
            return json.loads(client.models.generate_content(
                model=model, contents=prompt, config=config).text)
        except Exception as e:
            if attempt == 2:
                return {"verdict": "error", "rationale": f"{type(e).__name__}: {str(e)[:80]}"}
            time.sleep(2 * (attempt + 1))


def run(model: str, items: list[dict]) -> tuple[list[dict], float]:
    t = time.time()
    with ThreadPoolExecutor(8) as pool:
        out = list(pool.map(lambda p: ask(model, p), items))
    return out, time.time() - t


new, secs = run(new_model, pairs)
old_v = [p["verdict"] for p in pairs]
new_v = [r.get("verdict") for r in new]
agree = sum(o == n for o, n in zip(old_v, new_v))
print(f"pairs: {len(pairs)}   {new_model} took {secs:.0f}s")
print(f"agreement with stored verdicts: {agree}/{len(pairs)} ({agree / len(pairs):.0%})")
print("stored :", dict(Counter(old_v)))
print(f"{new_model}:", dict(Counter(new_v)))
print("transitions (stored -> new):",
      dict(Counter(f"{o}->{n}" for o, n in zip(old_v, new_v) if o != n)))

dis = [(p, n) for p, n, o in zip(pairs, new, old_v) if n.get("verdict") != o]
tb, tsecs = run(tiebreak, [p for p, _ in dis])
sides = Counter()
print(f"\n== {len(dis)} disagreements, tiebreak by {tiebreak} ({tsecs:.0f}s)")
for (p, n), t in zip(dis, tb):
    tv = t.get("verdict")
    side = ("stored" if tv == p["verdict"] else new_model if tv == n.get("verdict") else "neither")
    sides[side] += 1
    a, b = caps[p["a"]], caps[p["b"]]
    print(f"\n{p['score']:.3f} {a['asset']}/{a['name']}  ~  {b['asset']}/{b['name']}")
    print(f"   stored   {p['verdict']:9} {p['rationale'][:150]}")
    print(f"   new      {n.get('verdict'):9} {(n.get('rationale') or '')[:150]}")
    print(f"   tiebreak {tv:9} {(t.get('rationale') or '')[:150]}")
print("\ntiebreak sided with:", dict(sides))
