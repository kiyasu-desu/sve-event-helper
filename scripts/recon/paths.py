"""Shared path resolution for the recon scripts (read-only APK/bundle analysis).

Every script can be run from any working directory; all paths resolve against
the project root (the folder that contains apk/ and recon/).
"""
import os
import zipfile

# scripts/recon/paths.py -> project root is two levels up
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

APK_PATH = os.path.join(PROJECT_ROOT, "apk", "sve-1.2.0.apk")
RECON_DIR = os.path.join(PROJECT_ROOT, "recon")

BUNDLE_PATH = os.path.join(RECON_DIR, "index.android.bundle")
STRINGS_PATH = os.path.join(RECON_DIR, "strings.txt")
ANALYSIS_PATH = os.path.join(RECON_DIR, "analysis.txt")
CJK_ALL_PATH = os.path.join(RECON_DIR, "cjk_all.txt")
CJK_HITS_PATH = os.path.join(RECON_DIR, "cjk_hits.txt")

BUNDLE_IN_APK = "assets/index.android.bundle"


def ensure_bundle(apk_path: str = APK_PATH, bundle_path: str = BUNDLE_PATH) -> str:
    """Extract assets/index.android.bundle from the APK if it is not on disk yet."""
    if os.path.exists(bundle_path) and os.path.getsize(bundle_path) > 0:
        return bundle_path
    os.makedirs(os.path.dirname(bundle_path), exist_ok=True)
    with zipfile.ZipFile(apk_path) as zf:
        data = zf.read(BUNDLE_IN_APK)
    with open(bundle_path, "wb") as fh:
        fh.write(data)
    return bundle_path


def load_blob(strings_path: str = STRINGS_PATH) -> str:
    """Whole strings dump as one whitespace-normalised blob (Hermes packs runs together)."""
    with open(strings_path, encoding="utf-8", errors="replace") as fh:
        return " ".join(fh.read().splitlines())
