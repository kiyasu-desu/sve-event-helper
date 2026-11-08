"""Synthetic legacy entry: deliberately unaware of deployment transactions."""
from types import SimpleNamespace


def load_all():
    pass


def log(*args):
    pass


def restore_schedulers():
    raise AssertionError("Synthetic scheduler must never actually execute")


def run_http(**kwargs):
    raise AssertionError("Synthetic HTTP service must never actually execute")


app = SimpleNamespace(run=run_http)


def main():
    load_all()
    restore_schedulers()
    app.run()
