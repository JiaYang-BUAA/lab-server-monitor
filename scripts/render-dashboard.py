"""Export the common Windows/Linux Grafana dashboard without altering services."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labmon.dashboard import resource_dashboard

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--web-url", default="/")
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(resource_dashboard(args.web_url), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
