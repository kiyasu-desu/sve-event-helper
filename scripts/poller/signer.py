"""Development compatibility alias; excluded from production packages."""
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("sve_probe_signer", Path(__file__).resolve().parents[1] / "probe/signer.py")
_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_module)
if __name__ == "__main__":
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "probe/signer.py"), run_name="__main__")
sys.modules[__name__] = _module
