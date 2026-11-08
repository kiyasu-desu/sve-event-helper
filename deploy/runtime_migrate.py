"""Offline storage relocation. Preview by default; never control services/network.

On POSIX, run as root with --service-user and all writers stopped. A durable
journal fences M05 startup; a legacy fence also blocks M01-M04 startup. Recovery
finishes the recorded operation. Reverse relocation copies the latest dataset,
never restores an old identity snapshot.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
import re
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release


FENCE = {"stage": "externalized", "requires": "offline runtime relocation"}


def roots(root, external):
    root, external = Path(root).absolute(), Path(external).absolute()
    release.safe_path(root, "runtime-migration.json")
    release.safe_path(external, "identity-v2.json")
    if root.resolve().is_relative_to(external.resolve()) or external.resolve().is_relative_to(root.resolve()):
        raise ValueError("External storage must be outside the code tree")
    return root, external


def files(location):
    if not location.exists():
        return []
    result = []
    for path in location.rglob("*"):
        release.safe_path(location, path.relative_to(location))
        if path.name == ".identity-writer.lock":
            continue
        if path.is_file():
            result.append(path)
        elif not path.is_dir():
            raise ValueError("Nonregular runtime entry")
    return sorted(result)


def mapping(root, external, reverse=False):
    legacy = root / "app/client/data"
    pairs = []
    if not reverse:
        for path in files(legacy):
            rel = path.relative_to(legacy)
            if rel.as_posix() in {"areas.json", "README.md"}:
                continue
            dest = external / ("logs/client.log" if rel.as_posix() == "client.log" else rel)
            pairs.append((path, dest))
        for label, location in (("capture", root / "capture"), ("legacy-config", root / "config"),
                                ("deployment-backups", root / ".deployment-backups"),
                                ("maintenance-backups", root / ".maintenance")):
            for path in files(location):
                rel = path.relative_to(location)
                if label in {"capture", "legacy-config"} and (rel.as_posix() == "README.md" or (label == "legacy-config" and rel.name.endswith((".example.json", ".env.example")))):
                    continue
                if label == "legacy-config" and rel.as_posix() == "settings.json":
                    dest = external / "settings.json"
                elif label == "capture" and path.suffix == ".log":
                    dest = external / "logs" / rel
                else:
                    dest = external / label / rel
                pairs.append((path, dest))
    else:
        for path in files(external):
            rel = path.relative_to(external)
            if rel.parts[0] == "logs":
                dest = legacy / "client.log" if rel.as_posix() == "logs/client.log" else root / "capture" / Path(*rel.parts[1:])
            elif rel.parts[0] in {"capture", "legacy-config", "deployment-backups", "maintenance-backups"}:
                folder = {"capture": "capture", "legacy-config": "config", "deployment-backups": ".deployment-backups",
                          "maintenance-backups": ".maintenance"}[rel.parts[0]]
                dest = root / folder / Path(*rel.parts[1:])
            elif rel.as_posix() == "settings.json":
                dest = root / "config/settings.json"
            else:
                dest = legacy / rel
            pairs.append((path, dest))
    destinations = [str(b) for _, b in pairs]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Runtime path collision")
    return pairs


def preview(root, external, reverse=False):
    root, external = roots(root, external)
    source = external if reverse else root / "app/client/data"
    value = release.core_module(Path(__file__).resolve().parents[1], "identity_store").IdentityStore(source).load()
    pairs = mapping(root, external, reverse)
    if not pairs or not (source / "identity-v2.json").exists():
        raise ValueError("Authoritative dataset required")
    return {"ok": True, "direction": "legacy" if reverse else "external",
            "files": len(pairs), "schema_version": value["schema_version"],
            "counts": {name: len(value[name]) for name in ("accounts", "subscriptions", "records", "pending")}}


def service_identity(user):
    if os.name == "nt" or os.geteuid() != 0 or not user:
        raise ValueError("Root and an explicit service user required")
    import pwd
    account = pwd.getpwnam(user)
    return account.pw_uid, account.pw_gid


def handoff(root, external, owner):
    # Never grant write access to the code root. Include fences, locks and empty
    # runtime directories so interrupted operations can also be resumed safely.
    entries = []
    legacy = root / "app/client/data"
    for base in (legacy, root / "config", root / "capture", root / ".deployment-backups", root / ".maintenance", external):
        release.safe_path(base, ".")
        if not base.exists():
            continue
        entries.append(base)
        for path in sorted(base.rglob("*")):
            release.safe_path(base, path.relative_to(base))
            if not path.is_dir() and not path.is_file():
                raise ValueError("Nonregular runtime entry")
            if base == legacy and path.relative_to(base).as_posix() in {"areas.json", "README.md"}:
                continue
            if base == root / "config" and (path.name == "README.md" or path.name.endswith((".example.json", ".env.example"))):
                continue
            entries.append(path)
    journal = release.safe_path(root, "runtime-migration.json")
    if journal.exists():
        entries.append(journal)
    # Validate the full set before changing ownership; symlinks are refused.
    for path in entries:
        os.chown(path, *owner)


def relocate(root, external, *, reverse=False, writers_stopped=False, recover=False, fail_after=None, service_user=None):
    if not writers_stopped:
        raise ValueError("Explicit stopped writers required")
    owner = service_identity(service_user) if service_user is not None else None
    root, external = roots(root, external)
    legacy = root / "app/client/data"
    journal_path = root / "runtime-migration.json"
    old = release.read(journal_path) if journal_path.exists() else None
    source, destination = (external, legacy) if reverse else (legacy, external)
    if old and old["stage"] != "complete":
        if not recover or old["external"] != str(external) or old["reverse"] != reverse:
            raise ValueError("Incomplete relocation; recover the recorded direction")
        journal = old
    else:
        if recover:
            raise ValueError("No incomplete relocation")
        if old and Path(old["active_data"]) == destination:
            if not reverse and files(root / ".maintenance"):
                raise ValueError("Legacy maintenance files remain; archive privately before retrying")
            if owner is not None:
                handoff(root, external, owner)
            return {"ok": True, "unchanged": True}
        if destination.exists() and os.name != "nt" and destination.stat().st_mode & 0o077:
            raise ValueError("Destination directory must be private")
        for existing in files(destination):
            if reverse and existing.relative_to(destination).as_posix() in {"areas.json", "README.md"}:
                continue
            if existing == destination / "identity-migration.json" and release.read(existing) == FENCE:
                continue
            raise ValueError("Destination contains unrelated private files")
        summary = preview(root, external, reverse)
        pairs = mapping(root, external, reverse)
        # Refuse clobbering any private destination, except our own legacy fence.
        for _, dest in pairs:
            if dest.exists() and not (dest == destination / "identity-migration.json" and release.read(dest) == FENCE):
                raise ValueError("Destination is not empty; preserve and reconcile offline")
        journal = {"stage": "copying", "external": str(external), "reverse": reverse,
                   "active_data": str(destination), "summary": summary,
                   "files": [{"source": str(a), "destination": str(b), "hash": release.digest(a.read_bytes())} for a, b in pairs]}
    if (journal.get("stage") not in {"copying", "retiring"}
            or journal.get("active_data") != str(destination)
            or journal.get("external") != str(external) or journal.get("reverse") != reverse):
        raise ValueError("Invalid relocation journal")
    legacy_roots = (legacy, root / "config", root / "capture", root / ".deployment-backups", root / ".maintenance")
    if not reverse:
        recorded = {row["source"] for row in journal["files"]}
        if any(str(path) not in recorded for path in files(root / ".maintenance")):
            raise ValueError("Unrecorded maintenance files; preserve and reconcile offline")
    for row in journal["files"]:
        if not re.fullmatch(r"[0-9a-f]{64}", row.get("hash", "")):
            raise ValueError("Invalid relocation hash")
        for field, allowed in (("source", (external,) if reverse else legacy_roots),
                               ("destination", legacy_roots if reverse else (external,))):
            path = Path(row[field])
            base = next((base for base in allowed if path.is_absolute() and path.is_relative_to(base)), None)
            if base is None:
                raise ValueError("Invalid relocation path")
            release.safe_path(base, path.relative_to(base))
    module = release.core_module(Path(__file__).resolve().parents[1], "identity_store")
    # Directory locks cover both authorities, including repeated recovery.
    with ExitStack() as stack:
        if owner is not None:
            stack.callback(handoff, root, external, owner)
        for directory in sorted((source, destination)):
            release.make_directory(directory, mode=0o700)
            stack.enter_context(module.WriterLock(directory))
        if not old or old["stage"] == "complete":
            # Recheck after taking locks; a preview is not a consistent snapshot.
            fresh = mapping(root, external, reverse)
            if [(str(a), str(b), release.digest(a.read_bytes())) for a, b in fresh] != [(r["source"], r["destination"], r["hash"]) for r in journal["files"]]:
                raise ValueError("Source changed; preview again")
            release.write_json(journal_path, journal)
        if journal["stage"] == "copying":
            source_journal = source / "identity-migration.json"
            saved_journal = next((r for r in journal["files"] if Path(r["source"]) == source_journal), None)
            if saved_journal:
                saved_path = Path(saved_journal["destination"])
                if source_journal.exists() and release.digest(source_journal.read_bytes()) == saved_journal["hash"]:
                    release.make_directory(saved_path.parent, mode=0o700)
                    release.write(saved_path, source_journal.read_bytes(), 0o600)
                elif not saved_path.exists() or release.digest(saved_path.read_bytes()) != saved_journal["hash"]:
                    raise ValueError("Original identity journal unavailable")
            release.write_json(source_journal, FENCE)
        for row in journal["files"]:
            a, b = Path(row["source"]), Path(row["destination"])
            # Journal paths must stay within the two reviewed layouts.
            for p in (a, b):
                base = root if p.is_relative_to(root) else external
                release.safe_path(base, p.relative_to(base))
            if journal["stage"] == "copying":
                if a == source / "identity-migration.json":
                    continue  # Original journal was saved before fencing the source.
                if release.digest(a.read_bytes()) != row["hash"]:
                    raise ValueError("Source changed while stopped")
                release.make_directory(b.parent, mode=0o700)
                release.write(b, a.read_bytes(), 0o600)
        if fail_after == "copying":
            raise OSError("Simulated interruption")
        for row in journal["files"]:
            if release.digest(Path(row["destination"]).read_bytes()) != row["hash"]:
                raise ValueError("Copy verification failed; preserve source")
        fence = destination / "identity-migration.json"
        if fence.exists() and release.read(fence) == FENCE and not any(Path(r["destination"]) == fence for r in journal["files"]):
            fence.unlink()
            release.sync_directory(destination)
        module.IdentityStore(destination).load()
        journal["stage"] = "retiring"
        release.write_json(journal_path, journal)
        # Block pre-M05 readers too. Preserve the original journal in the copy.
        release.write_json(source / "identity-migration.json", FENCE)
        if fail_after == "fenced":
            raise OSError("Simulated interruption")
        for row in journal["files"]:
            a = Path(row["source"])
            if a == source / "identity-migration.json":
                continue
            if a.exists():
                if release.digest(a.read_bytes()) != row["hash"]:
                    raise ValueError("Source changed before retirement")
                a.unlink()
                release.sync_directory(a.parent)
        if fail_after == "retired":
            raise OSError("Simulated interruption")
        journal["stage"] = "complete"
        release.write_json(journal_path, journal)
    return dict(journal["summary"], stage="complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--external", type=Path, required=True)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--service-user", help="POSIX migration owner; run apply/recover as root")
    args = parser.parse_args()
    try:
        if args.apply or args.recover:
            if os.name != "nt":
                service_identity(args.service_user)
            result = relocate(args.root, args.external, reverse=args.reverse,
                              recover=args.recover, writers_stopped=args.writers_stopped,
                              service_user=args.service_user)
        else:
            result = preview(args.root, args.external, args.reverse)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('{"ok": false, "error": "storage relocation refused; private details suppressed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
