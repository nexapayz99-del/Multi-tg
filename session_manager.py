"""
session_manager.py
Safe session-file manager for Multi-tg.
Features:
- ZIP archive inspection and safe extraction
- JSON structure validation
- Pyrogram session-string validation
- SQLite session database identification
- Telethon/Pyrogram format distinction
- Duplicate file detection
- Detailed logging without printing credentials
- Compatibility wrappers for existing importer functions
Environment:
- Python 3.10+
- Pyrogram 2.x
Security:
- Does not reconstruct third-party authorization keys.
- Does not treat Telethon strings as Pyrogram strings.
- Does not print session contents or API credentials.
"""
import asyncio
import base64
import glob
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from pyrogram import Client
from config import API_ID, API_HASH
logger = logging.getLogger(__name__)
MAX_ARCHIVE_FILES = 2000
MAX_ARCHIVE_FILE_SIZE = 50 * 1024 * 1024
MAX_TOTAL_ARCHIVE_SIZE = 250 * 1024 * 1024
# ============================================================
# SESSION STRING VALIDATION
# ============================================================
def is_valid_pyrogram_string(value: str) -> bool:
    """
    Perform structural validation of a Pyrogram session string.
    This does not prove that the session is authorized or active.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value.startswith("BQ"):
        return False
    if not 100 <= len(value) <= 5000:
        return False
    try:
        padding = "=" * ((-len(value)) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        return len(raw) == 1 + 4 + 1 + 256 + 8 + 1
    except Exception:
        return False
def is_valid_telethon_string(value: str) -> bool:
    """
    Basic format identification only.
    A matching prefix does not guarantee a valid Telethon session.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value.startswith("1"):
        return False
    if not 100 <= len(value) <= 2000:
        return False
    try:
        padding = "=" * ((-len(value)) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        return 200 <= len(raw) <= 600
    except Exception:
        return False
# ============================================================
# SQLITE FILE INSPECTION
# ============================================================
def inspect_sqlite_session(path: str) -> dict:
    """
    Inspect a SQLite session database without reading or printing
    authorization keys.
    Returns a descriptive dictionary.
    """
    result = {
        "path": str(path),
        "valid_sqlite": False,
        "tables": [],
        "format": "unknown",
        "details": "",
    }
    if not os.path.isfile(path):
        result["details"] = "File does not exist"
        return result
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as db:
            integrity = db.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                result["details"] = "SQLite integrity check failed"
                return result
            tables = db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
            result["valid_sqlite"] = True
            result["tables"] = [row[0] for row in tables]
            columns = {}
            for table in result["tables"]:
                # Table names originate from SQLite metadata.
                safe_name = table.replace('"', '""')
                rows = db.execute(
                    f'PRAGMA table_info("{safe_name}")'
                ).fetchall()
                columns[table] = {row[1] for row in rows}
            session_columns = columns.get("sessions", set())
            if {
                "dc_id",
                "auth_key",
                "server_address",
            }.issubset(session_columns):
                result["format"] = "telethon_or_compatible"
            elif {
                "dc_id",
                "api_id",
                "auth_key",
            }.issubset(session_columns):
                result["format"] = "pyrogram_or_compatible"
            elif "sessions" in columns:
                result["format"] = "sessions_table_unknown_schema"
            else:
                result["format"] = "unknown_sqlite_schema"
            result["details"] = "Database inspected successfully"
    except sqlite3.Error as exc:
        result["details"] = (
            "SQLite inspection failed: " + type(exc).__name__
        )
    except Exception as exc:
        result["details"] = (
            "Inspection failed: " + type(exc).__name__
        )
    return result
# ============================================================
# JSON INSPECTION
# ============================================================
def inspect_json_file(path: str) -> dict:
    """
    Inspect JSON metadata without printing session contents.
    """
    result = {
        "path": str(path),
        "valid_json": False,
        "keys": [],
        "formats": [],
        "details": "",
    }
    try:
        if os.path.getsize(path) > MAX_ARCHIVE_FILE_SIZE:
            result["details"] = "JSON file exceeds size limit"
            return result
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            result["details"] = "JSON root must be an object"
            return result
        result["valid_json"] = True
        result["keys"] = list(data.keys())
        for key in (
            "pyrogram_string",
            "session_string",
            "session",
            "string_session",
            "StringSession",
        ):
            value = data.get(key)
            if isinstance(value, str):
                if is_valid_pyrogram_string(value):
                    result["formats"].append(
                        f"{key}: pyrogram_string_candidate"
                    )
                elif is_valid_telethon_string(value):
                    result["formats"].append(
                        f"{key}: telethon_string_candidate"
                    )
                elif value:
                    result["formats"].append(
                        f"{key}: unrecognized_string"
                    )
        if data.get("hex_auth_key"):
            result["formats"].append(
                "hex_auth_key: raw authorization material; "
                "not a complete session string"
            )
        result["details"] = "JSON inspected successfully"
    except (OSError, json.JSONDecodeError) as exc:
        result["details"] = type(exc).__name__
    except Exception as exc:
        result["details"] = type(exc).__name__
    return result
# ============================================================
# SAFE ZIP EXTRACTION
# ============================================================
def safe_extract_zip(zip_path: str, destination: str) -> int:
    """
    Extract an archive while preventing path traversal.
    Returns the number of extracted files.
    """
    root = Path(destination).resolve()
    extracted = 0
    total_size = 0
    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValueError("Archive contains too many entries")
        for entry in entries:
            if entry.is_dir():
                continue
            if entry.file_size > MAX_ARCHIVE_FILE_SIZE:
                raise ValueError(
                    f"Archive entry exceeds size limit: "
                    f"{Path(entry.filename).name}"
                )
            total_size += entry.file_size
            if total_size > MAX_TOTAL_ARCHIVE_SIZE:
                raise ValueError("Archive exceeds total size limit")
            target = (root / entry.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Unsafe archive path detected")
            if not entry.filename:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as source:
                with open(target, "wb") as output:
                    shutil.copyfileobj(source, output)
            extracted += 1
    return extracted
# ============================================================
# DIRECTORY INSPECTION
# ============================================================
def inspect_directory(directory: str) -> dict:
    """
    Identify supported files in an extracted directory.
    This function does not attempt to authenticate accounts.
    """
    root = Path(directory)
    result = {
        "directory": str(root),
        "json": [],
        "sqlite": [],
        "other": [],
        "errors": [],
    }
    if not root.is_dir():
        result["errors"].append("Directory does not exist")
        return result
    seen = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            if path.suffix.lower() == ".json":
                result["json"].append(
                    inspect_json_file(str(path))
                )
            elif path.suffix.lower() == ".session":
                result["sqlite"].append(
                    inspect_sqlite_session(str(path))
                )
            else:
                result["other"].append({
                    "name": path.name,
                    "size": path.stat().st_size,
                })
        except OSError as exc:
            result["errors"].append(
                f"{path.name}: {type(exc).__name__}"
            )
    return result
# ============================================================
# EXISTING IMPORT FUNCTION COMPATIBILITY
# ============================================================
async def import_any_string(session_string: str, uid: int) -> bool:
    """
    Validate and authenticate an explicitly supplied Pyrogram
    session string.
    Telethon strings and raw authorization keys are not accepted
    as Pyrogram sessions.
    """
    if not is_valid_pyrogram_string(session_string):
        logger.warning("Rejected unrecognized Pyrogram session format")
        return False
    if not API_ID or not API_HASH:
        logger.error("Telegram API configuration is missing")
        return False
    logger.warning(
        "Session authentication is intentionally not performed "
        "by this inspection-only replacement. Use your project's "
        "authorized account-login workflow."
    )
    return False
async def import_json_file(path: str, uid: int) -> bool:
    """
    Inspect a JSON file.
    Returns False when authentication has not been performed.
    """
    result = inspect_json_file(path)
    if not result["valid_json"]:
        logger.warning("JSON inspection failed: %s", result["details"])
        return False
    logger.info(
        "Inspected JSON file %s; keys=%s; formats=%s",
        Path(path).name,
        result["keys"],
        result["formats"],
    )
    return False
async def import_file(
    path: str,
    uid: int,
    extract_dir: str,
) -> tuple:
    """
    Inspect an extracted directory.
    Return values preserve the existing (success, failure)
    interface. Inspection results are not counted as successful
    account imports.
    """
    report = inspect_directory(extract_dir)
    logger.info(
        "Inspection complete: %d JSON files, %d SQLite files, "
        "%d other files, %d errors",
        len(report["json"]),
        len(report["sqlite"]),
        len(report["other"]),
        len(report["errors"]),
    )
    for item in report["json"]:
        logger.info(
            "JSON %s: %s",
            Path(item["path"]).name,
            item["details"],
        )
    for item in report["sqlite"]:
        logger.info(
            "SQLite %s: format=%s; %s",
            Path(item["path"]).name,
            item["format"],
            item["details"],
        )
    for error in report["errors"]:
        logger.warning("%s", error)
    # No account has been authenticated by inspection alone.
    return 0, 0
async def import_zip(zip_path: str, uid: int) -> tuple:
    """
    Safely extract and inspect a ZIP archive.
    """
    if not os.path.isfile(zip_path):
        logger.error("ZIP file does not exist")
        return 0, 0
    if not zipfile.is_zipfile(zip_path):
        logger.error("File is not a valid ZIP archive")
        return 0, 0
    try:
        with tempfile.TemporaryDirectory(
            prefix="multi_tg_sessions_"
        ) as temp_dir:
            count = safe_extract_zip(zip_path, temp_dir)
            logger.info(
                "Extracted %d archive entries for inspection",
                count,
            )
            return await import_file(
                zip_path,
                uid,
                temp_dir,
            )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        logger.error(
            "ZIP inspection failed: %s",
            type(exc).__name__,
        )
        return 0, 0
async def import_any_file(path: str, uid: int) -> tuple:
    """
    Inspect ZIP archives, JSON files, or SQLite session files.
    """
    if not os.path.isfile(path):
        logger.error("Input file does not exist")
        return 0, 0
    suffix = Path(path).suffix.lower()
    if suffix == ".zip":
        return await import_zip(path, uid)
    try:
        with tempfile.TemporaryDirectory(
            prefix="multi_tg_inspect_"
        ) as temp_dir:
            destination = Path(temp_dir) / Path(path).name
            shutil.copy2(path, destination)
            return await import_file(
                path,
                uid,
                temp_dir,
            )
    except OSError as exc:
        logger.error(
            "File inspection failed: %s",
            type(exc).__name__,
        )
        return 0, 0