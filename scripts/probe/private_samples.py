"""Read capture fixtures from a private file; importing this module has no side effects."""
import json
import os
from pathlib import Path


def load_sample(name: str) -> dict:
    path = Path(os.environ.get("SVE_SIGNING_SAMPLES_FILE") or
                Path(__file__).resolve().parents[2] / "config/signing-samples.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        sample = data[name]
        if not isinstance(sample, dict):
            raise ValueError()
        return sample
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Missing/invalid private signing samples; set SVE_SIGNING_SAMPLES_FILE. "
                           "See config/signing-samples.example.json; do not upload captures.") from None
