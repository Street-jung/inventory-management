"""Generation Consumer Reader for Iljeomodo Management Dashboard.
Standalone module designed to run in flat deployment roots (no dependency on scripts/).
Strictly validates generation manifests, hashes, schemas, complete monthly Excels,
path boundaries, date regressions, and pointer changes during verification.
Thread-safe with double-checked pointer locking for gunicorn multi-threading.
"""
import os
import re
import json
import hashlib
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

DATE_REGEX = re.compile(r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')
MONTH_REGEX = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


class GenerationValidationError(ValueError):
    """Raised when generation payload, manifest, or directory fails integrity validation."""
    pass


def safe_revision(value: str) -> str:
    """Validate 64-character lowercase hex revision."""
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError(f"Invalid generation revision: {value!r}")
    return value


def safe_filename(name: str) -> str:
    """Validate that filename is a simple basename without path traversal."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid filename: {name!r}")
    if Path(name).name != name or '/' in name or '\\' in name or name.startswith('.'):
        raise ValueError(f"Path traversal or hidden file rejected: {name!r}")
    return name


def parse_strict_iso_date(value: str) -> datetime.date:
    """Parse strict ISO YYYY-MM-DD string, rejecting invalid calendar dates."""
    if not isinstance(value, str) or not DATE_REGEX.match(value):
        raise ValueError(f"Invalid date format (must be YYYY-MM-DD): {value!r}")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"Invalid calendar date: {value!r} ({e})")


def parse_strict_iso_datetime(value: str) -> datetime.datetime:
    """Parse strict ISO datetime string, enforcing explicit timezone and UTC conversion."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid datetime string: {value!r}")
    norm = value.replace('Z', '+00:00') if value.endswith('Z') else value
    try:
        dt = datetime.datetime.fromisoformat(norm)
    except Exception as e:
        raise ValueError(f"Invalid ISO datetime: {value!r} ({e})")
    if dt.tzinfo is None:
        raise ValueError(f"Datetime must include explicit timezone: {value!r}")
    return dt.astimezone(datetime.timezone.utc)


class GenerationRecord:
    """In-memory validated generation bundle."""
    def __init__(self, revision: str, manifest: dict, dashboard_data: dict, files: Dict[str, bytes],
                 rollback: bool = False, is_retained_fallback: bool = False, last_error: Optional[str] = None):
        self.revision = revision
        self.manifest = manifest
        self.dashboard_data = dashboard_data
        self.files = files
        self.rollback = rollback
        self.is_retained_fallback = is_retained_fallback
        self.last_error = last_error

    @property
    def schema(self) -> str:
        return self.manifest.get('schema', '')

    @property
    def requested_date(self) -> str:
        return self.manifest.get('requested_date', '')

    @property
    def source_as_of(self) -> str:
        return self.manifest.get('source_as_of', '')

    @property
    def captured_at(self) -> str:
        return self.manifest.get('captured_at', '')

    @property
    def json_bytes(self) -> bytes:
        return self.files.get('dashboard_data.json', b'{}')

    def get_excel_bytes(self, month: str) -> Optional[bytes]:
        filename = f"{month}_식품부문_손익결산서.xlsx"
        return self.files.get(filename)


def check_date_regression(new_meta: dict, old_meta: dict, rollback: bool = False):
    """Enforce forward-only date progression on requested_date, source_as_of, and captured_at unless rollback.
    Rollback allows regression, but both sides must still have strictly valid dates.
    """
    new_req = parse_strict_iso_date(new_meta['requested_date'])
    old_req = parse_strict_iso_date(old_meta['requested_date'])
    new_src = parse_strict_iso_date(new_meta['source_as_of'])
    old_src = parse_strict_iso_date(old_meta['source_as_of'])
    new_cap = parse_strict_iso_datetime(new_meta['captured_at'])
    old_cap = parse_strict_iso_datetime(old_meta['captured_at'])

    if rollback:
        return  # Explicit authorized rollback allowed

    if new_req < old_req:
        raise ValueError(f"requested_date regression rejected without rollback flag: {new_req} < {old_req}")
    if new_src < old_src:
        raise ValueError(f"source_as_of regression rejected without rollback flag: {new_src} < {old_src}")
    if new_cap < old_cap:
        raise ValueError(f"captured_at regression rejected without rollback flag: {new_cap} < {old_cap}")


def validate_generation_payload(
    manifest: dict,
    files_data: Dict[str, bytes],
    expected_revision: Optional[str] = None
) -> Tuple[dict, dict]:
    """Strictly validates in-memory generation manifest and payload files_data against common generation rules.
    
    Unified validation rules:
      1. Manifest schema must be exactly '1.5'
      2. Strict date validation: requested_date, source_as_of, captured_at
      3. Manifest files dictionary must be non-empty and all filenames must be safe basenames
      4. manifest.json is a verification document outside the payload (never in manifest files)
      5. dashboard_data.json must be present in files_data, valid JSON, schema_version == '1.5'
      6. Metadata alignment between dashboard_data and manifest (report_date, source_as_of, captured_at)
      7. monthly_series completeness and integrity:
         - Each entry must have valid month format YYYY-MM
         - No duplicate months permitted in monthly_series
         - Permitted payload files must be EXACTLY: {'dashboard_data.json'} + {f"{m}_식품부문_손익결산서.xlsx" for m in months}
         - Zero extra files and zero missing files allowed
      8. Payload byte length and SHA256 matches manifest records exactly
      
    Returns (manifest, dashboard_data).
    """
    if not isinstance(manifest, dict):
        raise GenerationValidationError("Manifest content must be a JSON object")

    if manifest.get('schema') != '1.5':
        raise GenerationValidationError(f"Unsupported schema version: {manifest.get('schema')!r} (expected '1.5')")

    # Strict date validation for all generations
    parse_strict_iso_date(manifest.get('requested_date'))
    parse_strict_iso_date(manifest.get('source_as_of'))
    parse_strict_iso_datetime(manifest.get('captured_at'))

    manifest_files = manifest.get('files')
    if not isinstance(manifest_files, dict) or not manifest_files:
        raise GenerationValidationError("Manifest contains no files specification")

    if 'manifest.json' in manifest_files:
        raise GenerationValidationError("manifest.json must not be listed in manifest files payload")

    for name, record in manifest_files.items():
        safe_filename(name)
        if not isinstance(record, dict):
            raise GenerationValidationError(f"Manifest file record for {name} must be a dict")
        exp_bytes = record.get('bytes')
        exp_sha = record.get('sha256')
        if exp_bytes is None or not isinstance(exp_bytes, int) or exp_bytes < 0:
            raise GenerationValidationError(f"Invalid byte count for {name}: {exp_bytes!r}")
        if not exp_sha or not isinstance(exp_sha, str) or len(exp_sha) != 64:
            raise GenerationValidationError(f"Invalid sha256 for {name}: {exp_sha!r}")

    # Validate dashboard_data.json payload
    if 'dashboard_data.json' not in files_data:
        raise GenerationValidationError("dashboard_data.json missing from generation files")

    try:
        dd = json.loads(files_data['dashboard_data.json'].decode('utf-8'))
    except Exception as e:
        raise GenerationValidationError(f"Invalid dashboard_data.json JSON: {e}")

    if not isinstance(dd, dict):
        raise GenerationValidationError("dashboard_data.json must be a JSON object")

    if dd.get('schema_version') != '1.5':
        raise GenerationValidationError(f"Invalid dashboard_data schema_version: {dd.get('schema_version')}")

    # Validate dates inside dashboard_data
    parse_strict_iso_date(dd.get('metadata', {}).get('report_date'))
    parse_strict_iso_date(dd.get('source_selection', {}).get('source_as_of'))
    parse_strict_iso_datetime(dd.get('source_selection', {}).get('source_captured_at'))

    # Metadata consistency check
    meta_d = (
        dd.get('schema_version'),
        dd.get('metadata', {}).get('report_date'),
        dd.get('source_selection', {}).get('source_as_of'),
        dd.get('source_selection', {}).get('source_captured_at')
    )
    meta_m = (
        manifest.get('schema'),
        manifest.get('requested_date'),
        manifest.get('source_as_of'),
        manifest.get('captured_at')
    )
    if meta_d != meta_m:
        raise GenerationValidationError(f"Metadata mismatch between dashboard_data and manifest: {meta_d} != {meta_m}")

    # monthly_series verification
    raw_series = dd.get('monthly_series')
    if not isinstance(raw_series, list):
        raise GenerationValidationError(f"monthly_series must be a list (got {type(raw_series).__name__})")

    months = []
    for entry in raw_series:
        if not isinstance(entry, dict):
            raise GenerationValidationError(f"monthly_series entry must be a dict (got {type(entry).__name__})")
        m_str = entry.get('month')
        if not isinstance(m_str, str) or not MONTH_REGEX.match(m_str):
            raise GenerationValidationError(f"Invalid month format in monthly_series: {m_str!r}")
        months.append(m_str)

    if len(months) != len(set(months)):
        raise GenerationValidationError(f"Duplicate months detected in monthly_series: {months}")

    # Exact payload file set verification: permitted payload is strictly {'dashboard_data.json'} + {monthly Excel files}
    expected_payload_names = {'dashboard_data.json'} | {f"{m}_식품부문_손익결산서.xlsx" for m in months}

    manifest_names = set(manifest_files.keys())
    if manifest_names != expected_payload_names:
        extra = manifest_names - expected_payload_names
        missing = expected_payload_names - manifest_names
        raise GenerationValidationError(f"Manifest payload files mismatch: extra={extra}, missing={missing}")

    data_names = set(files_data.keys())
    if data_names != expected_payload_names:
        extra = data_names - expected_payload_names
        missing = expected_payload_names - data_names
        raise GenerationValidationError(f"Payload files data mismatch: extra={extra}, missing={missing}")

    # Byte length and SHA256 verification
    for name in expected_payload_names:
        blob = files_data[name]
        finfo = manifest_files[name]
        if len(blob) != finfo['bytes']:
            raise GenerationValidationError(f"File size mismatch for {name}: {len(blob)} != {finfo['bytes']}")
        actual_sha = hashlib.sha256(blob).hexdigest()
        if actual_sha != finfo['sha256']:
            raise GenerationValidationError(f"File hash mismatch for {name}: {actual_sha} != {finfo['sha256']}")

    return manifest, dd


def validate_generation_dir(
    gen_dir: Path,
    expected_revision: Optional[str] = None
) -> Tuple[dict, dict, Dict[str, bytes]]:
    """Strictly validates a generation directory on disk against common generation rules.
    Used by both GenerationReader (local cache) and RemoteGenerationSyncer (staging).
    
    Returns (manifest, dashboard_data, files_data).
    """
    gen_dir = Path(gen_dir).resolve()
    if not gen_dir.is_dir():
        raise FileNotFoundError(f"Generation directory does not exist: {gen_dir}")

    manifest_path = gen_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json missing in generation directory: {gen_dir}")

    raw_manifest = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
    if expected_revision:
        safe_revision(expected_revision)
        if manifest_sha != expected_revision:
            raise GenerationValidationError(f"Manifest SHA256 mismatch: expected {expected_revision}, got {manifest_sha}")

    try:
        manifest = json.loads(raw_manifest.decode('utf-8'))
    except Exception as e:
        raise GenerationValidationError(f"Failed to parse manifest JSON: {e}")

    if not isinstance(manifest, dict):
        raise GenerationValidationError("manifest.json must contain a JSON object")

    manifest_files = manifest.get('files')
    if not isinstance(manifest_files, dict) or not manifest_files:
        raise GenerationValidationError("Manifest contains no files specification")

    # Check files on disk match exactly manifest['files'] + {'manifest.json'}
    expected_disk_files = set(manifest_files.keys()) | {'manifest.json'}
    actual_disk_files = {p.name for p in gen_dir.iterdir() if p.is_file()}
    if actual_disk_files != expected_disk_files:
        extra = actual_disk_files - expected_disk_files
        missing = expected_disk_files - actual_disk_files
        raise GenerationValidationError(f"Files on disk mismatch: extra={extra}, missing={missing}")

    # Reject any subdirectories in generation directory
    subdirs = [p.name for p in gen_dir.iterdir() if p.is_dir()]
    if subdirs:
        raise GenerationValidationError(f"Unexpected subdirectories in generation directory: {subdirs}")

    files_data: Dict[str, bytes] = {}
    for name in manifest_files.keys():
        safe_filename(name)
        p = (gen_dir / name).resolve()
        if not p.is_relative_to(gen_dir):
            raise GenerationValidationError(f"Payload file escaped generation directory: {name}")
        if not p.is_file():
            raise FileNotFoundError(f"Payload file missing on disk: {name}")
        files_data[name] = p.read_bytes()

    manifest, dd = validate_generation_payload(manifest, files_data, expected_revision=manifest_sha)
    return manifest, dd, files_data


class GenerationReader:
    """Opt-in reader for validated generations cache. Thread-safe."""

    def __init__(self, cache_root: Optional[Any] = None):
        self.cache_root: Optional[Path] = Path(cache_root).resolve() if cache_root else None
        self._configured: bool = self.cache_root is not None
        self._current_record: Optional[GenerationRecord] = None
        self._retained_records: Dict[str, GenerationRecord] = {}
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        """Whether generation mode was explicitly configured by environment or config."""
        return self._configured

    @property
    def enabled(self) -> bool:
        """Generation mode is enabled whenever configured (even if directory is not yet created)."""
        return self._configured

    def _read_pointer(self) -> Tuple[str, bool, dict]:
        """Read and strictly validate current.json pointer."""
        if not self.cache_root or not self.cache_root.is_dir():
            raise FileNotFoundError(f"Cache root directory does not exist: {self.cache_root}")

        current_file = self.cache_root / 'current.json'
        if not current_file.is_file():
            raise FileNotFoundError(f"current.json not found in cache root: {self.cache_root}")

        raw = current_file.read_text(encoding='utf-8')
        try:
            pointer = json.loads(raw)
        except Exception as e:
            raise ValueError(f"Failed to parse current.json JSON: {e}")

        if not isinstance(pointer, dict):
            raise ValueError("current.json content must be a JSON object")

        revision = pointer.get('revision')
        if not revision:
            raise ValueError("current.json missing revision field")
        safe_revision(revision)

        rollback_val = pointer.get('rollback', False)
        if rollback_val is not True and rollback_val is not False:
            raise ValueError(f"rollback field must be a boolean literal (got {rollback_val!r})")

        return revision, bool(rollback_val), pointer

    def verify_generation(self, revision: str) -> Tuple[dict, dict, Dict[str, bytes]]:
        """Verify generation directory structure, manifest hash, file integrity, schema, and metadata."""
        if not self.cache_root:
            raise ValueError("Cache root not configured")

        safe_revision(revision)
        gen_dir = (self.cache_root / 'generations' / revision).resolve()
        base_gens = (self.cache_root / 'generations').resolve()

        if not gen_dir.is_relative_to(base_gens):
            raise ValueError(f"Generation directory escaped cache root: {gen_dir}")

        return validate_generation_dir(gen_dir, expected_revision=revision)

    def check_date_regression(self, new_meta: dict, old_meta: dict, rollback: bool = False):
        check_date_regression(new_meta, old_meta, rollback=rollback)



    def resolve(self) -> Optional[GenerationRecord]:
        """Poll or resolve current generation with double-checked pointer locking.
        If cache is unavailable or corrupted:
          - If a previously validated record exists, maintain it (is_retained_fallback=True) with error logged.
          - If no valid record exists, return None (explicitly unavailable).
        """
        if not self.configured or not self.cache_root:
            return None

        with self._lock:
            # 1. Read pointer before verification
            try:
                revision_before, rollback_before, pointer_before = self._read_pointer()
            except Exception as e:
                self._last_error = f"Failed to resolve pointer: {e}"
                if self._current_record:
                    self._current_record.is_retained_fallback = True
                    self._current_record.last_error = self._last_error
                return self._current_record

            # If current active record already matches revision_before and is healthy, return it
            if self._current_record and self._current_record.revision == revision_before and not self._current_record.is_retained_fallback:
                return self._current_record

            # Check if revision already validated in memory
            if revision_before in self._retained_records:
                cached = self._retained_records[revision_before]
                if self._current_record:
                    try:
                        self.check_date_regression(cached.manifest, self._current_record.manifest, rollback_before)
                    except Exception as e:
                        self._last_error = str(e)
                        self._current_record.is_retained_fallback = True
                        self._current_record.last_error = self._last_error
                        return self._current_record

                # Re-check pointer after in-memory check
                try:
                    rev_after, _, pointer_after = self._read_pointer()
                    if pointer_after != pointer_before:
                        self._last_error = f"pointer changed during resolution: was {revision_before}, now {rev_after}"
                        if self._current_record:
                            self._current_record.is_retained_fallback = True
                            self._current_record.last_error = self._last_error
                        return self._current_record
                except Exception as e:
                    self._last_error = f"Failed to re-verify pointer: {e}"
                    if self._current_record:
                        self._current_record.is_retained_fallback = True
                        self._current_record.last_error = self._last_error
                    return self._current_record

                cached.is_retained_fallback = False
                cached.last_error = None
                self._current_record = cached
                self._last_error = None
                return self._current_record

            # Perform disk and schema verification
            try:
                m, d, files = self.verify_generation(revision_before)
                if self._current_record:
                    self.check_date_regression(m, self._current_record.manifest, rollback_before)

                # Re-check pointer after disk verification (prevent activating candidate if pointer changed)
                rev_after, _, pointer_after = self._read_pointer()
                if pointer_after != pointer_before:
                    self._last_error = f"current.json changed during verification: was {revision_before}, now {rev_after}"
                    if self._current_record:
                        self._current_record.is_retained_fallback = True
                        self._current_record.last_error = self._last_error
                    return self._current_record

                record = GenerationRecord(
                    revision=revision_before,
                    manifest=m,
                    dashboard_data=d,
                    files=files,
                    rollback=rollback_before,
                    is_retained_fallback=False,
                    last_error=None
                )
                self._retained_records[revision_before] = record
                self._current_record = record
                self._last_error = None
                return record
            except Exception as e:
                self._last_error = f"Validation failed for generation {revision_before}: {e}"
                if self._current_record:
                    self._current_record.is_retained_fallback = True
                    self._current_record.last_error = self._last_error
                    return self._current_record
                return None

    def get_excel_bytes(self, month: str, revision: Optional[str] = None) -> Optional[bytes]:
        """Get Excel bytes for a specific month and revision. Thread-safe.
        If revision is given, it MUST match a validated generation; unknown revision returns None.
        If revision is None, returns Excel from current valid generation.
        """
        target_record: Optional[GenerationRecord] = None
        if revision:
            try:
                safe_revision(revision)
            except ValueError:
                return None

            with self._lock:
                if revision in self._retained_records:
                    target_record = self._retained_records[revision]
                else:
                    try:
                        m, d, files = self.verify_generation(revision)
                        rec = GenerationRecord(revision, m, d, files)
                        self._retained_records[revision] = rec
                        target_record = rec
                    except Exception:
                        return None
        else:
            target_record = self.resolve()

        if not target_record:
            return None

        return target_record.get_excel_bytes(month)

    def get_status_info(self, record: Optional[GenerationRecord] = None) -> dict:
        """UI summary helper.
        If record is passed, generates status strictly matching that record without calling resolve() again.
        This prevents race conditions where the response data is Gen A but status is Gen B.
        """
        current = record if record is not None else self.resolve()
        if not current:
            return {
                "enabled": self.enabled,
                "configured": self.configured,
                "available": False,
                "revision": None,
                "status_text": "세대 자료 사용 불가 (초기 검증 세대 없음)" if self.configured else "레거시 고정 배포 모드",
                "last_error": self._last_error
            }

        short_rev = current.revision[:8]
        src_date = current.source_as_of
        # Short format for MD: MM/DD
        src_short = src_date[5:].replace('-', '/') if len(src_date) == 10 else src_date

        if current.is_retained_fallback:
            status_text = f"⚠️ 갱신 실패 · 이전 자료 유지 (기준일: {src_short})"
        else:
            status_text = f"자료 기준 {src_short}"

        return {
            "enabled": True,
            "configured": True,
            "available": True,
            "revision": current.revision,
            "short_revision": short_rev,
            "source_as_of": current.source_as_of,
            "source_as_of_short": src_short,
            "requested_date": current.requested_date,
            "is_retained": current.is_retained_fallback,
            "last_error": current.last_error or self._last_error,
            "status_text": status_text
        }
