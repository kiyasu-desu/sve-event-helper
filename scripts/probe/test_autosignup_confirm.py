"""Retired test command; scenarios live in tests/test_m20_retirement.py."""
if __name__ == "__main__":
    import runpy
    from pathlib import Path
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/test_m20_retirement.py"), run_name="__main__")
