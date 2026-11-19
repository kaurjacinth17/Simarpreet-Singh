"""Build the web client.

  web/index.html          - served by the API; loads /api/bundle at runtime
  dist/sift-snapshot.html - self-contained prototype with today's processed stories embedded

Usage: python scripts/build_snapshot.py [fixture] [reviewed-summaries]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from newsintel.ingestion import FixtureAdapter  # noqa: E402
from newsintel.pipeline import result_json, run  # noqa: E402
from newsintel.summarization import CacheSummarizer  # noqa: E402

fixture = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "fixtures/articles_2026-11-19.json")
reviewed = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "fixtures/summaries_2026-11-19.json")
public_url = sys.argv[3] if len(sys.argv) > 3 else "https://claude.ai/artifact/D6uP6tydk1Jn7s8hvKwcSg"  # where the snapshot is hosted

fx = FixtureAdapter(fixture)
result = run(list(fx.fetch()), fx.snapshot_time, [CacheSummarizer(None, reviewed)])
bundle = result_json(result)
for s in bundle["stories"]:
    s["signals"].pop("summary_errors", None)

template = (ROOT / "web/app.template.html").read_text()
marker = "/*__SNAPSHOT__*/null"
assert marker in template
(ROOT / "web/feed.json").write_text(json.dumps(bundle, indent=1, ensure_ascii=False, default=str))
# Server build: no embedded data (null), so the client loads /api/bundle at runtime
(ROOT / "web/index.html").write_text(template.replace(marker, "null").replace("/*__PUBLIC_URL__*/", ""))
embedded = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), default=str).replace("</", "<\\/")
(ROOT / "dist").mkdir(exist_ok=True)
(ROOT / "dist/sift-snapshot.html").write_text(template.replace(marker, embedded).replace("/*__PUBLIC_URL__*/", public_url))
# Same snapshot without a fixed address, for hosting anywhere (shares link to wherever it is hosted)
public_dir = ROOT / "dist/public"
public_dir.mkdir(parents=True, exist_ok=True)
(public_dir / "index.html").write_text(template.replace(marker, embedded).replace("/*__PUBLIC_URL__*/", ""))
print(json.dumps(result.stats), "->", ROOT / "dist/sift-snapshot.html")
