#!/usr/bin/env python3
"""UBGME — Ultra Basic Generic Mod Enabler, version 1.0."""
from __future__ import annotations

APP_VERSION = "1.0"

import copy
import hashlib
import json
import os
import queue
import shutil
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath


tk = ttk = messagebox = None


def _load_tkinter() -> None:
    global tk, ttk, messagebox
    if tk is None:
        import tkinter as _tk
        from tkinter import messagebox as _messagebox, ttk as _ttk
        tk, ttk, messagebox = _tk, _ttk, _messagebox


class ModError(Exception):
    """Erreur de validation ou de déploiement affichable à l'utilisateur."""


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ModError(f"La destination n'est pas un fichier ordinaire : {path}")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _file_digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _selection_contains(index: int, selected_indices) -> bool:
    return any(int(selected) == index for selected in selected_indices)


def _selection_label(names: list[str]) -> str:
    if not names:
        return "Aucun mod sélectionné"
    noun = "mod sélectionné" if len(names) == 1 else "mods sélectionnés"
    return f"{len(names)} {noun} : {', '.join(names)}"


class ModManager:
    STATE_VERSION = 1
    README_NAMES = ("readme.txt",)

    def __init__(self, game_root: str | Path):
        self.root = Path(game_root).resolve()
        if not self.root.is_dir():
            raise ModError(f"Racine de jeu introuvable : {self.root}")
        self.mods_dir = self.root / "MODS"
        self.meta_dir = self.root / ".modmanager"
        self.backups_dir = self.meta_dir / "backups"
        self.state_file = self.meta_dir / "state.json"
        self.transaction_file = self.meta_dir / "transaction.json"
        self.transactions_dir = self.meta_dir / "transactions"
        self.mods_dir.mkdir(exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        self.state = {"version": self.STATE_VERSION, "active": [], "files": {}, "created_dirs": {}}
        self._recover_pending_transaction()
        self.state = self._load_state()
        self._cleanup_orphan_transaction_dirs()
        self._validate_state_on_startup()

    def _load_state(self) -> dict:
        if not self.state_file.exists():
            return {"version": self.STATE_VERSION, "active": [], "files": {}, "created_dirs": {}}
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if state.get("version") != self.STATE_VERSION or not isinstance(state.get("active"), list) or not isinstance(state.get("files"), dict):
                raise ValueError("format/version inconnue")
            state.setdefault("created_dirs", {})
            if not isinstance(state["created_dirs"], dict):
                raise ValueError("registre de répertoires invalide")
            return state
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ModError(f"État UBGME illisible; aucune opération effectuée : {exc}") from exc

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _save_state(self) -> None:
        self._write_json_atomic(self.state_file, self.state)

    def _parent_directories(self, relative: str) -> list[str]:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ModError(f"Chemin relatif invalide : {relative}")
        return [PurePosixPath(*pure.parts[:index]).as_posix() for index in range(1, len(pure.parts))]

    def _missing_parent_directories(self, relatives: list[str]) -> list[str]:
        missing: set[str] = set()
        for relative in relatives:
            for directory in self._parent_directories(relative):
                path = self.root.joinpath(*PurePosixPath(directory).parts)
                if path.exists() and not path.is_dir():
                    raise ModError(f"Un fichier empêche la création du répertoire {directory}.")
                if not path.exists():
                    missing.add(directory)
        return sorted(missing, key=lambda item: len(PurePosixPath(item).parts))

    def _remove_empty_directories(self, relatives: list[str]) -> dict[str, list[str]]:
        blocked: set[str] = set()
        for relative in sorted(set(relatives), key=lambda item: len(PurePosixPath(item).parts), reverse=True):
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise ModError("Chemin de répertoire dangereux dans l’état UBGME.")
            directory = self.root
            linked = False
            for part in pure.parts:
                directory = directory / part
                if directory.is_symlink():
                    blocked.add(relative)
                    linked = True
                    break
            if linked:
                continue
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                blocked.add(relative)

        # Collapse nested failures so one retained parent represents its blocked descendants.
        retained_dirs: list[str] = []
        for relative in sorted(blocked, key=lambda item: len(PurePosixPath(item).parts)):
            parts = PurePosixPath(relative).parts
            if not any(parts[:len(PurePosixPath(parent).parts)] == PurePosixPath(parent).parts for parent in retained_dirs):
                retained_dirs.append(relative)

        retained_files: set[str] = set()
        for relative in retained_dirs:
            base = self.root.joinpath(*PurePosixPath(relative).parts)
            if base.is_symlink():
                retained_files.add(relative)
            elif base.is_file():
                retained_files.add(relative)
            elif base.is_dir():
                for current, directories, filenames in os.walk(base, followlinks=False):
                    current_path = Path(current)
                    directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                    for filename in filenames:
                        path = current_path / filename
                        if path.is_file() or path.is_symlink():
                            retained_files.add(path.relative_to(self.root).as_posix())
        return {"directories": sorted(retained_dirs), "files": sorted(retained_files)}

    def _begin_transaction(
        self,
        relatives: list[str],
        progress=None,
        progress_offset: int = 0,
        progress_total: int | None = None,
        progress_label: str = "",
    ) -> dict:
        transaction_id = uuid.uuid4().hex
        txn_dir = self.transactions_dir / transaction_id
        before_dir = txn_dir / "before"
        before_dir.mkdir(parents=True)
        snapshots = []
        try:
            for index, relative in enumerate(relatives, 1):
                self._report_progress(progress, f"Préparation transactionnelle {progress_label}: {index}/{len(relatives)} — {relative}", progress_offset + index - 1, progress_total or len(relatives))
                destination = self._destination(relative)
                exists = destination.exists()
                snapshot_name = f"{index:06d}.bak" if exists else None
                before_hash = _digest(destination) if exists else None
                if exists:
                    snapshot = before_dir / snapshot_name
                    shutil.copy2(destination, snapshot)
                    if _digest(snapshot) != before_hash:
                        raise ModError(f"Échec de vérification de la pré-image transactionnelle : {relative}")
                snapshots.append({"path": relative, "exists": exists, "sha256": before_hash, "snapshot": snapshot_name})
                self._report_progress(progress, f"Préparation transactionnelle {progress_label}: {index}/{len(relatives)}", progress_offset + index, progress_total or len(relatives))
            rollback_delete = []
            before_state = copy.deepcopy(self.state)
            for item in snapshots:
                relative = item["path"]
                if item["exists"] and relative not in before_state["files"]:
                    backup = self.backup_path(relative)
                    if not backup.exists():
                        rollback_delete.append(backup.relative_to(self.meta_dir).as_posix())
            journal = {
                "id": transaction_id,
                "phase": "prepared",
                "state_before": before_state,
                "files": snapshots,
                "new_directories": self._missing_parent_directories(relatives),
                "rollback_delete": rollback_delete,
                "post_commit_delete": [],
            }
            self._write_json_atomic(self.transaction_file, journal)
            return journal
        except BaseException:
            shutil.rmtree(txn_dir, ignore_errors=True)
            raise

    def _transaction_dir(self, journal: dict) -> Path:
        transaction_id = journal.get("id", "")
        if len(transaction_id) != 32 or any(ch not in "0123456789abcdef" for ch in transaction_id):
            raise ModError("Journal UBGME invalide : identifiant de transaction incorrect.")
        return self.transactions_dir / transaction_id

    def _remove_transaction_marker(self, journal: dict) -> None:
        txn_dir = self._transaction_dir(journal)
        self.transaction_file.unlink(missing_ok=True)
        shutil.rmtree(txn_dir, ignore_errors=True)

    def _rollback_transaction(self, journal: dict) -> None:
        before_dir = self._transaction_dir(journal) / "before"
        for item in reversed(journal.get("files", [])):
            relative = item["path"]
            destination = self._destination(relative)
            if item.get("exists"):
                snapshot = before_dir / item["snapshot"]
                if _digest(snapshot) != item.get("sha256"):
                    raise ModError(f"Pré-image transactionnelle absente ou altérée : {relative}")
                self._restore_backup(snapshot, destination)
            else:
                destination.unlink(missing_ok=True)
        self.state = journal["state_before"]
        self.state.setdefault("created_dirs", {})
        self._remove_empty_directories(journal.get("new_directories", []))
        self._save_state()
        for relative in journal.get("rollback_delete", []):
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts:
                raise ModError("Journal UBGME invalide : chemin de rollback dangereux.")
            (self.meta_dir / pure).unlink(missing_ok=True)
        self._remove_transaction_marker(journal)

    def _finish_committed_transaction(self, journal: dict) -> None:
        for relative in journal.get("post_commit_delete", []):
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts:
                raise ModError("Journal UBGME invalide : chemin de nettoyage dangereux.")
            (self.meta_dir / pure).unlink(missing_ok=True)
        self._remove_transaction_marker(journal)

    def _commit_transaction(self, journal: dict, post_commit_delete: list[str] | None = None) -> None:
        journal["phase"] = "committed"
        journal["post_commit_delete"] = post_commit_delete or []
        self._write_json_atomic(self.transaction_file, journal)
        self._finish_committed_transaction(journal)

    def _recover_pending_transaction(self) -> None:
        if not self.transaction_file.exists():
            return
        try:
            journal = json.loads(self.transaction_file.read_text(encoding="utf-8"))
            if journal.get("phase") == "committed":
                self._finish_committed_transaction(journal)
            elif journal.get("phase") == "prepared":
                self._rollback_transaction(journal)
            else:
                raise ValueError("étape de transaction inconnue")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModError(f"Récupération UBGME impossible; journal conservé : {exc}") from exc

    def _cleanup_orphan_transaction_dirs(self) -> None:
        if self.transaction_file.exists():
            return
        for entry in self.transactions_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)

    def _validate_state_on_startup(self) -> None:
        """Validate state structure only; verify payload bytes lazily on operations."""
        active = self.state["active"]
        if any(not isinstance(name, str) for name in active) or len(active) != len(set(active)):
            raise ModError("État UBGME incohérent : noms actifs invalides ou dupliqués.")
        for name in active:
            self._mod_root(name)

        files = self.state["files"]
        layer_counts = {name: 0 for name in active}
        for relative, record in files.items():
            if not isinstance(relative, str):
                raise ModError("État UBGME incohérent : chemin de fichier non textuel.")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.as_posix() != relative:
                raise ModError(f"État UBGME incohérent : chemin de fichier invalide ({relative}).")
            if not isinstance(record, dict) or not isinstance(record.get("layers"), list) or not record["layers"]:
                raise ModError(f"État UBGME incohérent : couches manquantes ({relative}).")
            if not _is_sha256(record.get("deployed_hash")):
                raise ModError(f"État UBGME incohérent : empreinte déployée invalide ({relative}).")
            if not isinstance(record.get("original_exists"), bool):
                raise ModError(f"État UBGME incohérent : indicateur d’original invalide ({relative}).")
            if record["original_exists"]:
                backup_rel = record.get("backup")
                backup = PurePosixPath(backup_rel or "")
                if backup.is_absolute() or ".." in backup.parts or not backup.parts or backup.parts[0] != "backups" or not _is_sha256(record.get("original_hash")):
                    raise ModError(f"État UBGME incohérent : référence de sauvegarde invalide ({relative}).")
            elif record.get("backup") is not None or record.get("original_hash") is not None:
                raise ModError(f"État UBGME incohérent : sauvegarde originale inattendue ({relative}).")
            seen_layers = set()
            for layer in record["layers"]:
                if not isinstance(layer, dict):
                    raise ModError(f"État UBGME incohérent : couche invalide ({relative}).")
                name = layer.get("mod")
                source_hash = layer.get("source_hash")
                if not isinstance(name, str) or name not in layer_counts or name in seen_layers or not _is_sha256(source_hash):
                    raise ModError(f"État UBGME incohérent : référence de couche invalide ({relative}).")
                seen_layers.add(name)
                layer_counts[name] += 1
        for name, count in layer_counts.items():
            if count == 0:
                raise ModError(f"État UBGME incomplet : aucun fichier suivi pour le mod actif {name}.")

        created_dirs = self.state.get("created_dirs", {})
        for relative, record in created_dirs.items():
            if not isinstance(relative, str):
                raise ModError("État UBGME incohérent : chemin de répertoire non textuel.")
            pure = PurePosixPath(relative)
            owners = record.get("owners") if isinstance(record, dict) else None
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or not isinstance(owners, list) or not owners:
                raise ModError(f"État UBGME incohérent : entrée de répertoire invalide ({relative}).")
            if any(not isinstance(owner, str) or owner not in active for owner in owners) or len(owners) != len(set(owners)):
                raise ModError(f"État UBGME incohérent : propriétaires de répertoire invalides ({relative}).")

    def list_mods(self) -> list[str]:
        if not self.mods_dir.exists():
            return []
        return sorted((p.name for p in self.mods_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.casefold)

    def active_mods(self) -> list[str]:
        return list(self.state["active"])

    def description(self, mod_name: str) -> str:
        mod_root = self._mod_root(mod_name)
        files = [item for item in mod_root.iterdir() if item.is_file() and not item.is_symlink()]
        for supported_name in self.README_NAMES:
            for item in files:
                if item.name.casefold() == supported_name:
                    return item.read_text(encoding="utf-8", errors="replace")
        return "Aucune description disponible."

    def backup_path(self, relative: str) -> Path:
        key = _file_digest_bytes(relative.encode("utf-8"))
        return self.backups_dir / key

    def _mod_root(self, name: str) -> Path:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ModError("Nom de mod invalide.")
        path = self.mods_dir / name
        if not path.is_dir() or path.is_symlink():
            raise ModError(f"Mod introuvable : {name}")
        return path

    def _mod_file(self, name: str, relative: str) -> Path | None:
        base = self._mod_root(name)
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ModError(f"Chemin source de mod dangereux : {relative}")
        source = base
        for part in pure.parts:
            source = source / part
            if source.is_symlink():
                raise ModError(f"Lien symbolique interdit dans le mod {name} : {relative}")
        return source if source.is_file() else None

    def _inventory(self, name: str, progress=None) -> dict[str, Path]:
        base = self._mod_root(name)
        result: dict[str, Path] = {}
        folded_paths: set[str] = set()
        processed = 0
        for path in base.rglob("*"):
            processed += 1
            if progress is not None and processed % 250 == 0:
                self._report_progress(progress, f"Inventaire {name} : {processed} entrées parcourues", 0, 0)
            rel_parts = path.relative_to(base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if path.is_symlink():
                raise ModError(f"Lien symbolique interdit dans le mod : {path.relative_to(base)}")
            if not path.is_file():
                continue
            if len(rel_parts) == 1 and path.name.casefold() in self.README_NAMES:
                continue
            rel = PurePosixPath(*rel_parts).as_posix()
            if not rel_parts or ".." in rel_parts or PurePosixPath(rel).is_absolute():
                raise ModError(f"Chemin dangereux dans le mod : {rel}")
            key = rel.casefold()
            if key in folded_paths:
                raise ModError(f"Collision de casse interne au mod {name} : {rel}")
            folded_paths.add(key)
            result[rel] = path
        if progress is not None:
            self._report_progress(progress, f"Inventaire {name} terminé : {len(result)} fichiers", 0, 0)
        return result

    def collisions(self, mod_name: str) -> dict[str, list[str]]:
        candidate = self._inventory(mod_name)
        owners: dict[str, list[str]] = {}
        for active in self.state["active"]:
            if active == mod_name:
                continue
            for rel in self._inventory(active):
                owners.setdefault(rel.casefold(), []).append((active, rel))
        found: dict[str, list[str]] = {}
        for rel in candidate:
            matches = owners.get(rel.casefold(), [])
            if matches:
                found[rel] = [f"{name} ({other_rel})" for name, other_rel in matches]
        return found

    def conflict_warning(self, mod_name: str, earlier_mods: list[str] | None = None) -> str | None:
        """Build concise conflicts using persisted layers for already-active mods."""
        earlier = self.state["active"] if earlier_mods is None else earlier_mods
        owners = {
            relative.casefold()
            for relative, record in self.state["files"].items()
            if any(layer.get("mod") in earlier for layer in record.get("layers", []))
        }
        for name in earlier:
            if name not in self.state["active"] and name != mod_name:
                owners.update(relative.casefold() for relative in self._inventory(name))
        paths = sorted((rel for rel in self._inventory(mod_name) if rel.casefold() in owners), key=str.casefold)
        if not paths:
            return None
        return f"Le mod {mod_name} va modifier des fichiers déjà modifiés par d'autres mods : {', '.join(paths)}"

    def prepare_activation(self, mod_names: list[str], progress=None) -> dict:
        """Inventory candidates and compute collisions before any game files change."""
        names = list(mod_names)
        if len(names) != len(set(names)):
            raise ModError("La sélection contient plusieurs fois le même mod.")
        inventories = {}
        for index, name in enumerate(names, 1):
            if name in self.state["active"]:
                raise ModError(f"Le mod est déjà actif : {name}")
            self._report_progress(progress, f"Inventaire {name} ({index}/{len(names)})", 0, 0)
            inventory = self._inventory(name, progress=progress)
            if not inventory:
                raise ModError(f"Le mod {name} ne peut pas être activé : aucun fichier à appliquer.")
            inventories[name] = inventory

        owners: dict[str, list[tuple[str, str]]] = {}
        for relative, record in self.state["files"].items():
            for layer in record.get("layers", []):
                owners.setdefault(relative.casefold(), []).append((layer["mod"], relative))
        warnings = []
        for name in names:
            inventory = inventories[name]
            paths = sorted((relative for relative in inventory if relative.casefold() in owners), key=str.casefold)
            if paths:
                warnings.append(f"Le mod {name} va modifier des fichiers déjà modifiés par d'autres mods : {', '.join(paths)}")
            for relative in inventory:
                owners.setdefault(relative.casefold(), []).append((name, relative))
        return {"names": names, "inventories": inventories, "warnings": warnings}

    def activate_many(self, mod_names: list[str], progress=None, prepared: dict | None = None) -> list[str]:
        """Activate mods sequentially in caller order and report file progress."""
        names = list(mod_names)
        if not names:
            return []
        if len(names) != len(set(names)):
            raise ModError("La sélection contient plusieurs fois le même mod.")
        if prepared is None:
            prepared = self.prepare_activation(names, progress=progress)
        if prepared.get("names") != names or not isinstance(prepared.get("inventories"), dict):
            raise ModError("Plan d’activation invalide ou obsolète.")
        inventories = prepared["inventories"]
        for name in names:
            if name in self.state["active"]:
                raise ModError(f"Le mod est déjà actif : {name}")
            if name not in inventories or not inventories[name]:
                raise ModError(f"Plan d’activation incomplet pour {name}.")
        total = sum(len(inventories[name]) * 3 + 1 for name in names)
        offset = 0
        activated: list[str] = []
        for index, name in enumerate(names, 1):
            count = len(inventories[name])
            label = f"{name} ({index}/{len(names)})"
            self.activate(
                name,
                progress=progress,
                _inventory_cache=inventories[name],
                _progress_offset=offset,
                _progress_total=total,
                _progress_label=label,
            )
            activated.append(name)
            offset += count * 3 + 1
        return activated

    @staticmethod
    def _report_progress(progress, label: str, current: int, total: int) -> None:
        if progress is not None:
            progress(label, current, total)

    def _destination(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ModError(f"Chemin de destination dangereux : {relative}")
        destination = self.root.joinpath(*pure.parts)
        # Refuse symlinks in every destination component, even when they point inside root.
        cursor = self.root
        for part in pure.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ModError(f"Destination lien symbolique refusée : {relative}")
        # Resolve the parent and ensure it remains inside the game root.
        parent = destination.parent.resolve()
        try:
            parent.relative_to(self.root)
        except ValueError as exc:
            raise ModError(f"Destination hors de la racine du jeu : {relative}") from exc
        if destination.is_symlink():
            raise ModError(f"Destination lien symbolique refusée : {relative}")
        return destination

    def _write_from(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".ubgme-", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(source, temp_name)
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _restore_backup(self, backup: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".ubgme-", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(backup, temp_name)
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _ensure_original_record(self, relative: str, destination: Path) -> dict:
        record = self.state["files"].get(relative)
        if record is not None:
            return record
        original_hash = _digest(destination)
        record = {"original_exists": original_hash is not None, "original_hash": original_hash, "backup": None, "deployed_hash": original_hash, "layers": []}
        if original_hash is not None:
            backup = self.backup_path(relative)
            if backup.exists():
                raise ModError(f"Sauvegarde préexistante inattendue; opération arrêtée : {relative}")
            shutil.copy2(destination, backup)
            try:
                backup.chmod(0o444)
            except OSError:
                pass
            if _digest(backup) != original_hash:
                raise ModError(f"Vérification de sauvegarde échouée : {relative}")
            record["backup"] = str(backup.relative_to(self.meta_dir))
        self.state["files"][relative] = record
        return record

    def _check_managed_file(self, relative: str, record: dict) -> Path:
        destination = self._destination(relative)
        actual = _digest(destination)
        if actual != record.get("deployed_hash"):
            raise ModError(f"Le fichier {relative} a été modifié en dehors d’UBGME; opération interrompue.")
        return destination

    def activate(
        self,
        mod_name: str,
        progress=None,
        _inventory_cache: dict[str, Path] | None = None,
        _progress_offset: int = 0,
        _progress_total: int | None = None,
        _progress_label: str | None = None,
    ) -> None:
        if mod_name in self.state["active"]:
            raise ModError(f"Le mod est déjà actif : {mod_name}")
        inventory = _inventory_cache if _inventory_cache is not None else self._inventory(mod_name, progress=progress)
        if not inventory:
            raise ModError("aucun fichier à appliquer")
        local_total = 3 * len(inventory) + 1
        total = _progress_total if _progress_total is not None else local_total
        label = _progress_label or mod_name
        self._report_progress(progress, f"Vérification de {label}", _progress_offset, total)
        # Preflight every target before creating the transaction or changing files.
        for index, rel in enumerate(inventory, 1):
            destination = self._destination(rel)
            record = self.state["files"].get(rel)
            expected = record.get("deployed_hash") if record else _digest(destination)
            if _digest(destination) != expected:
                raise ModError(f"Le fichier {rel} a été modifié en dehors d’UBGME; opération interrompue.")
            if record and record.get("original_exists"):
                backup = self.meta_dir / PurePosixPath(record["backup"])
                if _digest(backup) != record.get("original_hash"):
                    raise ModError(f"Sauvegarde originale absente ou altérée : {rel}")
            self._report_progress(progress, f"Vérification de {label} : {index}/{len(inventory)}", _progress_offset + index, total)
        journal = self._begin_transaction(
            list(inventory),
            progress=progress,
            progress_offset=_progress_offset + len(inventory),
            progress_total=total,
            progress_label=label,
        )
        try:
            for index, (rel, source) in enumerate(inventory.items(), 1):
                destination = self._destination(rel)
                record = self._ensure_original_record(rel, destination)
                record.setdefault("layers", [])
                source_hash = _digest(source)
                self._write_from(source, destination)
                record["deployed_hash"] = _digest(destination)
                record["layers"].append({"mod": mod_name, "source_hash": source_hash})
                self._report_progress(progress, f"Déploiement de {label} : {index}/{len(inventory)}", _progress_offset + 2 * len(inventory) + index, total)
            new_directories = set(journal.get("new_directories", []))
            created_dirs = self.state.setdefault("created_dirs", {})
            for relative in inventory:
                for directory in self._parent_directories(relative):
                    if directory in new_directories or directory in created_dirs:
                        entry = created_dirs.setdefault(directory, {"owners": []})
                        owners = entry.setdefault("owners", [])
                        if mod_name not in owners:
                            owners.append(mod_name)
            self.state["active"].append(mod_name)
            self._save_state()
        except Exception:
            self._rollback_transaction(journal)
            raise
        self._commit_transaction(journal)
        self._report_progress(progress, f"Finalisation de {label}", _progress_offset + local_total, total)

    def deactivate(
        self,
        mod_name: str,
        progress=None,
        _progress_offset: int = 0,
        _progress_total: int | None = None,
        _progress_label: str | None = None,
    ) -> dict[str, list[str]]:
        if mod_name not in self.state["active"]:
            raise ModError(f"Le mod n’est pas actif : {mod_name}")
        remaining = [name for name in self.state["active"] if name != mod_name]
        affected = [rel for rel, record in self.state["files"].items() if any(layer.get("mod") == mod_name for layer in record.get("layers", []))]
        label = _progress_label or mod_name
        local_total = 3 * len(affected) + 1
        total = _progress_total if _progress_total is not None else local_total
        plans: dict[str, Path | None] = {}
        self._report_progress(progress, f"Vérification de {label}", _progress_offset, total)

        # Validate each affected file and only the source layers that own that path.
        for index, rel in enumerate(affected, 1):
            record = self.state["files"].get(rel)
            if record is None:
                raise ModError(f"État UBGME incomplet pour {rel}; opération interrompue.")
            self._check_managed_file(rel, record)
            own_layer = next((layer for layer in record.get("layers", []) if layer.get("mod") == mod_name), None)
            source = self._mod_file(mod_name, rel)
            if own_layer is None or source is None or _digest(source) != own_layer.get("source_hash"):
                raise ModError(f"Le source du mod {mod_name} a changé depuis son activation ({rel}); rétablissez-le avant de continuer.")
            for layer in record.get("layers", []):
                active = layer.get("mod")
                if active == mod_name or active not in remaining:
                    continue
                active_source = self._mod_file(active, rel)
                if active_source is None or _digest(active_source) != layer.get("source_hash"):
                    raise ModError(f"Le source du mod {active} a changé depuis son activation ({rel}); rétablissez-le avant de continuer.")
            remaining_layers = [layer for layer in record.get("layers", []) if layer.get("mod") != mod_name]
            winner = None
            for active in reversed(remaining):
                winner_layer = next((layer for layer in remaining_layers if layer.get("mod") == active), None)
                if winner_layer is not None:
                    winner = self._mod_file(active, rel)
                    if winner is None:
                        raise ModError(f"État UBGME incomplet : {active} ne fournit plus {rel}.")
                    break
            if winner is None and record["original_exists"]:
                backup = self.meta_dir / record["backup"]
                if _digest(backup) != record["original_hash"]:
                    raise ModError(f"Sauvegarde originale absente ou altérée : {rel}")
            plans[rel] = winner
            self._report_progress(progress, f"Vérification de {label} : {index}/{len(affected)}", _progress_offset + index, total)

        journal = self._begin_transaction(
            affected,
            progress=progress,
            progress_offset=_progress_offset + len(affected),
            progress_total=total,
            progress_label=label,
        )
        backups_to_delete: list[str] = []
        directory_warnings = {"directories": [], "files": []}
        try:
            for index, (rel, winner) in enumerate(plans.items(), 1):
                destination = self._destination(rel)
                record = self.state["files"][rel]
                record["layers"] = [layer for layer in record.get("layers", []) if layer.get("mod") != mod_name]
                if winner is not None:
                    self._write_from(winner, destination)
                    record["deployed_hash"] = _digest(destination)
                elif record["original_exists"]:
                    self._restore_backup(self.meta_dir / record["backup"], destination)
                    record["deployed_hash"] = record["original_hash"]
                else:
                    destination.unlink(missing_ok=True)
                    record["deployed_hash"] = None
                self._report_progress(progress, f"Restauration de {label} : {index}/{len(affected)}", _progress_offset + 2 * len(affected) + index, total)
            self.state["active"] = remaining
            for rel in affected:
                if not self.state["files"][rel].get("layers"):
                    record = self.state["files"].pop(rel)
                    if record.get("backup"):
                        backups_to_delete.append(record["backup"])
            empty_directories = []
            created_dirs = self.state.setdefault("created_dirs", {})
            for directory, entry in list(created_dirs.items()):
                owners = entry.get("owners", [])
                if mod_name in owners:
                    owners.remove(mod_name)
                if not owners:
                    empty_directories.append(directory)
            directory_warnings = self._remove_empty_directories(empty_directories)
            for directory in empty_directories:
                created_dirs.pop(directory, None)
            self._save_state()
        except Exception:
            self._rollback_transaction(journal)
            raise
        self._commit_transaction(journal, backups_to_delete)
        self._report_progress(progress, f"Finalisation de {label}", _progress_offset + local_total, total)
        return directory_warnings

    def deactivate_many(self, mod_names: list[str], progress=None) -> dict[str, list[str]]:
        """Deactivate selected mods, removing higher-priority layers first."""
        names = list(mod_names)
        if not names:
            return {"directories": [], "files": []}
        if len(names) != len(set(names)):
            raise ModError("La sélection contient plusieurs fois le même mod.")
        for name in names:
            if name not in self.state["active"]:
                raise ModError(f"Le mod n’est pas actif : {name}")
        processing_order = list(reversed(names))
        work = {name: 0 for name in processing_order}
        for record in self.state["files"].values():
            for layer in record.get("layers", []):
                name = layer.get("mod")
                if name in work:
                    work[name] += 1
        total = sum(count * 3 + 1 for count in work.values())
        offset = 0
        warnings = {"directories": set(), "files": set()}
        for index, name in enumerate(processing_order, 1):
            count = work[name]
            label = f"{name} ({index}/{len(processing_order)})"
            result = self.deactivate(name, progress, offset, total, label)
            warnings["directories"].update(result["directories"])
            warnings["files"].update(result["files"])
            offset += count * 3 + 1
        return {key: sorted(values) for key, values in warnings.items()}


class UBGMEWindow:
    def __init__(self, manager: ModManager):
        _load_tkinter()
        self.manager = manager
        self.root = tk.Tk()
        self.root.title(f"UBGME v{APP_VERSION} — Ultra Basic Generic Mod Enabler")
        self.root.minsize(700, 430)
        self._operation_running = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Racine du jeu : {manager.root}").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Mods disponibles").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="Mods actifs (priorité croissante)").grid(row=1, column=2, sticky="w")
        self.available = tk.Listbox(
            frame,
            exportselection=False,
            selectmode=tk.EXTENDED,
            selectbackground="#3584e4",
            selectforeground="#ffffff",
            activestyle="dotbox",
        )
        self._selection_anchor: int | None = None
        self._active_selection_anchor: int | None = None
        self._bind_multiselect(self.available, "_selection_anchor")
        self.active = tk.Listbox(
            frame,
            exportselection=False,
            selectmode=tk.EXTENDED,
            selectbackground="#3584e4",
            selectforeground="#ffffff",
            activestyle="dotbox",
        )
        self._bind_multiselect(self.active, "_active_selection_anchor")
        self.available.grid(row=2, column=0, sticky="nsew")
        self.active.grid(row=2, column=2, sticky="nsew")
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=1, padx=12)
        ttk.Button(buttons, text=">", command=self.activate).pack(pady=4)
        ttk.Button(buttons, text="<", command=self.deactivate).pack(pady=4)
        ttk.Button(buttons, text="Rafraîchir", command=self.refresh).pack(pady=16)
        self.selection_status = ttk.Label(frame, text="Aucun mod sélectionné", wraplength=340)
        self.selection_status.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.active_selection_status = ttk.Label(frame, text="Aucun mod actif sélectionné", wraplength=340)
        self.active_selection_status.grid(row=3, column=2, sticky="w", pady=(4, 0))
        ttk.Label(frame, text="Ctrl+clic : ajouter/retirer · Maj+clic : plage · Maj+↑/↓ : étendre").grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.description = tk.Text(frame, height=7, wrap="word", state="disabled")
        self.description.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.available.bind("<<ListboxSelect>>", self.show_description)
        self.active.bind("<<ListboxSelect>>", self.show_active_selection)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(3, weight=0)
        self.refresh()

    def refresh(self) -> None:
        self._selection_anchor = None
        self._active_selection_anchor = None
        self.available.delete(0, tk.END)
        self.active.delete(0, tk.END)
        self.selection_status.configure(text=_selection_label([]))
        self.active_selection_status.configure(text="Aucun mod actif sélectionné")
        active = self.manager.active_mods()
        for name in self.manager.list_mods():
            if name not in active:
                self.available.insert(tk.END, name)
        for name in active:
            self.active.insert(tk.END, name)

    def _bind_multiselect(self, listbox, anchor_attr: str) -> None:
        listbox.bind("<Button-1>", lambda event: self._on_listbox_click(listbox, anchor_attr, event))
        for key, direction in (("Up", -1), ("Down", 1), ("Home", "home"), ("End", "end")):
            listbox.bind(f"<Shift-{key}>", lambda event, widget=listbox, attr=anchor_attr, step=direction: self._move_listbox_selection_with_shift(widget, attr, step))

    def _set_listbox_range(self, listbox, anchor: int, target: int) -> None:
        low, high = sorted((anchor, target))
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(low, high)
        listbox.activate(target)
        listbox.focus_set()
        listbox.see(target)
        listbox.event_generate("<<ListboxSelect>>")

    def _on_listbox_click(self, listbox, anchor_attr: str, event):
        index = listbox.nearest(event.y)
        bounds = listbox.bbox(index)
        if bounds is None or not (bounds[1] <= event.y < bounds[1] + bounds[3]):
            return "break"
        shift_pressed = bool(event.state & 0x0001)
        control_pressed = bool(event.state & 0x0004)
        anchor = getattr(self, anchor_attr)
        if shift_pressed:
            if anchor is None:
                selected = listbox.curselection()
                anchor = int(selected[0]) if selected else index
                setattr(self, anchor_attr, anchor)
            self._set_listbox_range(listbox, anchor, index)
        elif control_pressed:
            if _selection_contains(index, listbox.curselection()):
                listbox.selection_clear(index)
            else:
                listbox.selection_set(index)
            setattr(self, anchor_attr, index)
            listbox.activate(index)
            listbox.focus_set()
            listbox.see(index)
            listbox.event_generate("<<ListboxSelect>>")
        else:
            setattr(self, anchor_attr, index)
            self._set_listbox_range(listbox, index, index)
        return "break"

    def _move_listbox_selection_with_shift(self, listbox, anchor_attr: str, direction: int | str):
        size = listbox.size()
        if size == 0:
            return "break"
        current = int(listbox.index(tk.ACTIVE))
        anchor = getattr(self, anchor_attr)
        if anchor is None:
            selected = listbox.curselection()
            anchor = int(selected[0]) if selected else current
            setattr(self, anchor_attr, anchor)
        if direction == "home":
            target = 0
        elif direction == "end":
            target = size - 1
        else:
            target = max(0, min(size - 1, current + direction))
        self._set_listbox_range(listbox, anchor, target)
        return "break"

    def show_description(self, _event=None) -> None:
        selection = self.available.curselection()
        names = [self.available.get(index) for index in selection]
        self.selection_status.configure(text=_selection_label(names))
        text = self.manager.description(names[0]) if names else ""
        self.description.configure(state="normal")
        self.description.delete("1.0", tk.END)
        self.description.insert("1.0", text)
        self.description.configure(state="disabled")

    def show_active_selection(self, _event=None) -> None:
        selection = self.active.curselection()
        names = [self.active.get(index) for index in selection]
        self.active_selection_status.configure(text=_selection_label(names))

    def activate(self) -> None:
        indices = self.available.curselection()
        if not indices:
            return
        names = [self.available.get(index) for index in indices]
        self._run(
            lambda progress: self.manager.prepare_activation(names, progress=progress),
            refresh_on_success=False,
            on_success=self._confirm_activation,
        )

    def _confirm_activation(self, plan: dict) -> None:
        names = plan["names"]
        warnings = plan["warnings"]
        if warnings:
            prompt = "\n\n".join(warnings) + "\n\nPoursuivre ?"
        elif len(names) == 1:
            prompt = f"Activer le mod « {names[0]} » ?"
        else:
            prompt = "Activer ces mods dans l'ordre affiché ?\n" + " → ".join(names)
        if messagebox.askyesno("Confirmer l’activation", prompt, parent=self.root):
            self._run(lambda progress: self.manager.activate_many(names, progress=progress, prepared=plan))

    def deactivate(self) -> None:
        selection = self.active.curselection()
        if not selection:
            return
        names = [self.active.get(index) for index in selection]
        if len(names) == 1:
            prompt = f"Désactiver « {names[0]} » et restaurer les fichiers concernés ?"
        else:
            prompt = "Désactiver ces mods actifs ?\n" + " → ".join(reversed(names))
            prompt += "\n\nLes plus prioritaires seront retirés en premier."
        if messagebox.askyesno("Confirmer la désactivation", prompt, parent=self.root):
            self._run(lambda progress: self.manager.deactivate_many(names, progress=progress), warning_title="Répertoires conservés")

    def _run(self, action, warning_title: str | None = None, refresh_on_success: bool = True, on_success=None) -> None:
        if self._operation_running:
            return
        self._operation_running = True
        dialog = tk.Toplevel(self.root)
        dialog.title("Opération en cours")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        status = ttk.Label(frame, text="Préparation…", width=54, wraplength=420)
        status.pack(fill="x", pady=(0, 10))
        bar = ttk.Progressbar(frame, mode="indeterminate", length=420)
        bar.pack(fill="x")
        bar.start(12)
        dialog.grab_set()

        progress_updates = queue.Queue(maxsize=1)
        results = queue.Queue(maxsize=1)

        def report_progress(label: str, current: int, total: int) -> None:
            event = (label, current, total)
            try:
                progress_updates.put_nowait(event)
            except queue.Full:
                try:
                    progress_updates.get_nowait()
                except queue.Empty:
                    pass
                try:
                    progress_updates.put_nowait(event)
                except queue.Full:
                    pass

        def worker() -> None:
            try:
                results.put(("ok", action(report_progress)))
            except Exception as exc:
                results.put(("error", exc))

        threading.Thread(target=worker, name="ubgme-operation", daemon=True).start()

        def poll() -> None:
            try:
                label, current, total = progress_updates.get_nowait()
            except queue.Empty:
                pass
            else:
                status.configure(text=label)
                if total > 0:
                    if bar.cget("mode") != "determinate":
                        bar.stop()
                        bar.configure(mode="determinate", maximum=total, value=0)
                    elif int(float(bar.cget("maximum"))) != total:
                        bar.configure(maximum=total, value=0)
                    bar.configure(value=min(current, total))
                elif bar.cget("mode") != "indeterminate":
                    bar.stop()
                    bar.configure(mode="indeterminate")
                    bar.start(12)
            try:
                result_kind, result = results.get_nowait()
            except queue.Empty:
                dialog.after(80, poll)
                return

            if bar.cget("mode") == "indeterminate":
                bar.stop()
            dialog.grab_release()
            dialog.destroy()
            self._operation_running = False
            if result_kind == "error":
                messagebox.showerror("UBGME", str(result), parent=self.root)
                return
            if refresh_on_success:
                self.refresh()
            if warning_title and result and result.get("directories"):
                message = "Répertoire(s) conservé(s) :\n" + "\n".join(f"• {name}" for name in result["directories"])
                if result.get("files"):
                    message += "\n\nFichier(s) restant(s) :\n" + "\n".join(f"• {name}" for name in result["files"])
                else:
                    message += "\n\nAucun fichier ordinaire n’a pu être listé; un autre élément empêche la suppression."
                messagebox.showwarning(warning_title, message, parent=self.root)
            if on_success is not None:
                on_success(result)

        dialog.after(80, poll)

    def _on_close(self) -> None:
        if not self._operation_running:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=f"UBGME v{APP_VERSION} — gestionnaire de mods générique")
    parser.add_argument("--version", action="version", version=f"UBGME {APP_VERSION}")
    parser.add_argument("--game-root", type=Path, help="Racine du jeu (par défaut : dossier de ubgme.py)")
    args = parser.parse_args()
    root = args.game_root.resolve() if args.game_root else Path(__file__).resolve().parent
    try:
        UBGMEWindow(ModManager(root)).run()
    except (ModError, ImportError, tk.TclError if tk is not None else ModError) as exc:
        print(f"UBGME: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
