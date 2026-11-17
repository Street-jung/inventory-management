"""
Independent Read-Only HTTP Generation Consumer.
Polls validated generations from remote HTTP/GAS generation endpoint into local GenerationReader cache.
No dependency on scripts/ or backend packages (flat deployment ready).
Only calls read-only actions: generation_current, generation_get, generation_status.
NEVER calls mutation actions (generation_put, generation_commit, generation_rollback).
"""
import os
import sys
import time
import json
import base64
import hashlib
import tempfile
import shutil
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from generation_reader import (
    safe_revision,
    safe_filename,
    parse_strict_iso_date,
    parse_strict_iso_datetime,
    check_date_regression,
    validate_generation_payload,
    validate_generation_dir,
    GenerationValidationError,
    GenerationReader,
)


class RemoteError(RuntimeError):
    """Base exception for remote generation transport errors."""
    pass


class _ContentRedirect(HTTPRedirectHandler):
    """Google Apps Script ContentService redirect handler.
    Follows 302/303 redirects to script.googleusercontent.com via GET only.
    Never forwards credentials, headers, or body to redirected host.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if code not in (302, 303) or parsed.scheme != 'https' or parsed.hostname != 'script.googleusercontent.com' or parsed.username or parsed.password:
            raise RemoteError('unexpected generation redirect: untrusted target')
        # ContentService result retrieval is strictly GET-only; never forward credentials
        return Request(newurl, method='GET', headers={'Accept': 'application/json'})


class RemoteGenerationClient:
    """Read-only HTTP client for generation endpoint.
    Strictly restricted to generation_current, generation_get, and generation_status.
    Mutation actions (put, commit, rollback) are forbidden.
    """
    ALLOWED_ACTIONS = {'generation_current', 'generation_get', 'generation_status'}

    def __init__(self, endpoint: str, token: str, *, enabled: bool = False, allow_loopback: bool = False, timeout: int = 15):
        if not enabled:
            raise ValueError('explicit enabled=True opt-in required')
        if not token or not isinstance(token, str):
            raise ValueError('explicit read token required')

        parsed = urlsplit(endpoint)
        local = allow_loopback and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('endpoint URL must not contain query, fragment, or userinfo')
        if parsed.scheme != 'https' and not (local and parsed.scheme == 'http'):
            raise ValueError('HTTPS required for generation endpoint')

        self.endpoint = endpoint
        self._token = token
        self.timeout = timeout
        self._allow_loopback = allow_loopback

    def call(self, action: str, payload: Optional[dict] = None) -> Any:
        """Call remote endpoint with action envelope. Enforces read-only permissions."""
        if action not in self.ALLOWED_ACTIONS:
            raise RemoteError(f'mutation action forbidden in read-only consumer: {action}')

        body = json.dumps(
            {'action': action, 'token': self._token, 'payload': payload or {}},
            ensure_ascii=False
        ).encode('utf-8')

        req = Request(
            self.endpoint,
            data=body,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST'
        )

        try:
            with build_opener(_ContentRedirect()).open(req, timeout=self.timeout) as resp:
                result = json.load(resp)
        except HTTPError as exc:
            # Mask status and never leak internal URLs or headers
            raise RemoteError(f'remote HTTP error {exc.code}') from None
        except URLError as exc:
            raise RemoteError(f'remote connection failed: {exc.reason}') from None
        except Exception as exc:
            raise RemoteError('remote response unavailable') from None

        if not isinstance(result, dict) or result.get('ok') is not True:
            code = result.get('code') if isinstance(result, dict) else 'invalid_envelope'
            allowed_codes = {'unauthorized', 'forbidden', 'invalid', 'conflict', 'missing', 'regression', 'busy', 'internal'}
            err_code = code if code in allowed_codes else 'unknown_error'
            raise RemoteError(f'generation API error: {err_code}')

        return result.get('data')

    def current(self) -> Optional[dict]:
        """Fetch remote current generation pointer."""
        data = self.call('generation_current')
        if data is None:
            return None
        if not isinstance(data, dict):
            raise RemoteError('invalid current pointer structure')

        rev = data.get('revision')
        if not rev or not isinstance(rev, str):
            raise RemoteError('missing or invalid revision in current pointer')
        safe_revision(rev)

        prev = data.get('previous_revision')
        if prev is not None:
            if not isinstance(prev, str):
                raise RemoteError('invalid previous_revision type')
            safe_revision(prev)

        rb = data.get('rollback')
        if not isinstance(rb, bool):
            raise RemoteError(f'pointer rollback must be boolean literal, got {type(rb).__name__}')

        return {
            'revision': rev,
            'previous_revision': prev,
            'rollback': rb
        }

    def status(self, revision: str) -> dict:
        """Fetch status of specific revision."""
        safe_revision(revision)
        return self.call('generation_status', {'revision': revision})

    def get(self, revision: str, name: str) -> bytes:
        """Fetch and verify an immutable object from remote generation."""
        safe_revision(revision)
        safe_filename(name)
        result = self.call('generation_get', {'revision': revision, 'name': name})
        if not isinstance(result, dict):
            raise RemoteError(f'invalid get response structure for {name}')

        b64_content = result.get('content_base64')
        expected_bytes = result.get('bytes')
        expected_sha = result.get('sha256')

        if not b64_content or expected_bytes is None or not expected_sha:
            raise RemoteError(f'incomplete get response fields for {name}')

        try:
            blob = base64.b64decode(b64_content, validate=True)
        except Exception:
            raise RemoteError(f'invalid base64 encoding for {name}')

        if len(blob) != expected_bytes:
            raise RemoteError(f'byte length mismatch for {name}: expected {expected_bytes}, got {len(blob)}')

        actual_sha = hashlib.sha256(blob).hexdigest()
        if actual_sha != expected_sha:
            raise RemoteError(f'sha256 mismatch for {name}')

        return blob


class RemoteGenerationSyncer:
    """Orchestrates pulling remote generations into local GenerationReader cache root.
    Pipeline:
      1. Fetch current remote pointer (revision, previous_revision, rollback)
      2. If already active locally: verify integrity of local cache. If intact -> no-op; if corrupted -> re-download & repair!
      3. Download to isolated staging directory (.staging/<rev>_<ts>)
      4. Complete unified validation (manifest SHA256, schema 1.5, dashboard_data.json schema/meta alignment,
         full payload file set exact match, monthly Excel completeness, 3 time axes non-regression)
      5. Double-check remote pointer: compare FULL normalized pointer tuple (revision, previous_revision, rollback).
         If remote pointer changed in any field during download, abort and clean staging!
      6. Safe atomic swap: swap staging into generations/<rev> with rollback backup preservation on error,
         and update current.json atomically via os.replace.
    On any failure:
      - Staging is cleaned up
      - Existing local current generation is preserved (Last-Good Retained)
      - Failure status is recorded for propagation to UI
    """
    def __init__(
        self,
        client: RemoteGenerationClient,
        cache_root: Path,
        local_reader: Optional[GenerationReader] = None,
        poll_interval: Optional[float] = None,
        failure_backoff: Optional[float] = None
    ):
        self.client = client
        self.cache_root = Path(cache_root).resolve()
        self.local_reader = local_reader
        self._lock = threading.Lock()
        self._inflight_lock = threading.Lock()

        # Polling configuration: default 30s interval, 10s failure backoff
        env_interval = os.environ.get('GENERATION_POLL_INTERVAL')
        self.poll_interval = float(poll_interval if poll_interval is not None else (env_interval or 30.0))

        env_backoff = os.environ.get('GENERATION_FAILURE_BACKOFF')
        self.failure_backoff = float(failure_backoff if failure_backoff is not None else (env_backoff or 10.0))

        self.last_sync_time: Optional[float] = None
        self.last_attempt_time: Optional[float] = None
        self.last_success_time: Optional[float] = None
        self.last_sync_success: bool = False
        self.last_sync_error: Optional[str] = None
        self.last_synced_revision: Optional[str] = None
        self.consecutive_failures: int = 0

    def should_poll(self, now: Optional[float] = None) -> bool:
        """Evaluate whether a remote poll should be dispatched.
        Conditions:
          - If a sync is already running in another flight: False
          - If consecutive_failures > 0 and (now - last_attempt_time) < failure_backoff: False
          - If has valid local cache and (now - last_success_time) < poll_interval: False
          - Otherwise: True
        """
        current_time = time.time() if now is None else now

        # If already syncing in flight, do not dispatch another poll
        if self._inflight_lock.locked():
            return False

        # If in failure backoff window, suppress polling
        if self.consecutive_failures > 0 and self.last_attempt_time is not None:
            if (current_time - self.last_attempt_time) < self.failure_backoff:
                return False

        # If we have a successful sync and within poll_interval
        if self.last_success_time is not None:
            has_valid_cache = False
            if self.local_reader is not None:
                has_valid_cache = self.local_reader.resolve() is not None
            else:
                has_valid_cache = (self.cache_root / 'current.json').is_file()

            if has_valid_cache and (current_time - self.last_success_time) < self.poll_interval:
                return False

        return True

    def sync_if_needed(self, force: bool = False) -> Tuple[bool, Optional[str]]:
        """Controlled poll dispatch: respects poll_interval, failure_backoff, and single-flight.
        If another thread is already syncing, non-blockingly returns last-known state
        without waiting or hammering the remote server.
        """
        if not force and not self.should_poll():
            return self.last_sync_success, self.last_synced_revision

        # Single-flight non-blocking acquisition
        acquired = self._inflight_lock.acquire(blocking=False)
        if not acquired:
            # Another poll is in-flight: return last-known state immediately
            return self.last_sync_success, (self.last_synced_revision or 'in_flight')

        try:
            self.last_attempt_time = time.time()
            ok, res = self._execute_sync()
            self.last_sync_time = time.time()
            if ok:
                self.last_success_time = self.last_sync_time
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
            return ok, res
        finally:
            self._inflight_lock.release()

    def sync_once(self) -> Tuple[bool, Optional[str]]:
        """Perform one atomic poll-and-sync iteration. Thread-safe (forced execution)."""
        return self.sync_if_needed(force=True)

    def _verify_staging_integrity(self, staging_dir: Path, target_rev: str, is_rollback: bool) -> Tuple[dict, dict, Dict[str, bytes]]:
        """Perform unified strict verification on staging directory before committing.
        Uses shared validate_generation_dir rule from generation_reader.
        Returns (manifest, dashboard_data, files_data).
        """
        try:
            manifest, dd, files_data = validate_generation_dir(staging_dir, expected_revision=target_rev)
        except (ValueError, FileNotFoundError, RemoteError) as e:
            raise RemoteError(str(e))

        # Time regression check against currently active local generation (if any)
        local_current_file = self.cache_root / 'current.json'
        if local_current_file.is_file():
            try:
                cur_p = json.loads(local_current_file.read_text(encoding='utf-8'))
                cur_rev = cur_p.get('revision')
                if cur_rev:
                    cur_manifest_file = self.cache_root / 'generations' / cur_rev / 'manifest.json'
                    if cur_manifest_file.is_file():
                        cur_m = json.loads(cur_manifest_file.read_text(encoding='utf-8'))
                        try:
                            check_date_regression(manifest, cur_m, rollback=is_rollback)
                        except ValueError as ve:
                            raise RemoteError(str(ve))
            except RemoteError:
                raise
            except Exception:
                pass

        return manifest, dd, files_data

    def _execute_sync(self) -> Tuple[bool, Optional[str]]:
        """Internal atomic poll-and-sync implementation."""
        with self._lock:
            self.last_sync_time = time.time()
            staging_dir: Optional[Path] = None
            backup_dir: Optional[Path] = None
            try:
                # 1. Fetch remote current pointer
                pointer = self.client.current()
                if not pointer:
                    return False, 'no remote generation published'

                target_rev = pointer['revision']
                prev_rev = pointer['previous_revision']
                is_rollback = pointer['rollback']
                initial_pointer_tuple = (target_rev, prev_rev, is_rollback)

                # 2. Check if already active in local cache AND verify local integrity
                local_current_file = self.cache_root / 'current.json'
                local_gen_dir = self.cache_root / 'generations' / target_rev
                if local_current_file.is_file() and local_gen_dir.is_dir():
                    try:
                        local_pointer = json.loads(local_current_file.read_text(encoding='utf-8'))
                        if local_pointer.get('revision') == target_rev:
                            # Verify integrity of existing local cache!
                            checker = GenerationReader(self.cache_root)
                            checker.verify_generation(target_rev)
                            # Intact! Fast-path success
                            self.last_sync_success = True
                            self.last_sync_error = None
                            self.last_synced_revision = target_rev
                            return True, target_rev
                    except Exception:
                        # Local cache is corrupted or missing files!
                        # Fall through to re-download from remote and repair!
                        pass

                # 3. Create isolated staging directory inside cache_root/.staging
                staging_base = self.cache_root / '.staging'
                staging_base.mkdir(parents=True, exist_ok=True)
                staging_dir = staging_base / f"{target_rev}_{int(time.time() * 1000)}"
                staging_dir.mkdir(parents=True, exist_ok=True)

                # 4. Download manifest.json
                manifest_bytes = self.client.get(target_rev, 'manifest.json')
                (staging_dir / 'manifest.json').write_bytes(manifest_bytes)

                try:
                    manifest_preview = json.loads(manifest_bytes.decode('utf-8'))
                    files_dict = manifest_preview.get('files', {})
                except Exception:
                    raise RemoteError('manifest is not valid JSON')

                # Download each file listed in manifest
                for fname in files_dict.keys():
                    safe_filename(fname)
                    blob = self.client.get(target_rev, fname)
                    (staging_dir / fname).write_bytes(blob)

                # 5. Strict unified validation of entire staging directory
                self._verify_staging_integrity(staging_dir, target_rev, is_rollback)

                # 6. Double-check remote pointer: compare FULL normalized pointer tuple
                re_pointer = self.client.current()
                final_pointer_tuple = (
                    re_pointer.get('revision'),
                    re_pointer.get('previous_revision'),
                    re_pointer.get('rollback')
                ) if re_pointer else None

                if initial_pointer_tuple != final_pointer_tuple:
                    raise RemoteError('remote pointer changed during download; candidate invalidated')

                # 7. Safe atomic swap: move staging to destination with rollback backup
                target_gen_dir = self.cache_root / 'generations' / target_rev
                target_gen_dir.parent.mkdir(parents=True, exist_ok=True)

                if target_gen_dir.exists():
                    # Move existing to backup_dir in staging instead of deleting
                    backup_dir = staging_base / f"backup_{target_rev}_{int(time.time() * 1000)}"
                    shutil.move(str(target_gen_dir), str(backup_dir))

                try:
                    shutil.move(str(staging_dir), str(target_gen_dir))
                    staging_dir = None  # successfully placed

                    # Update current.json atomically
                    current_payload = {
                        'revision': target_rev,
                        'previous_revision': prev_rev,
                        'rollback': is_rollback
                    }
                    tmp_current = self.cache_root / f"current.tmp.{int(time.time() * 1000)}"
                    tmp_current.write_text(json.dumps(current_payload, indent=2, ensure_ascii=False), encoding='utf-8')
                    os.replace(str(tmp_current), str(local_current_file))

                    # Success: clean up old backup
                    if backup_dir and backup_dir.exists():
                        shutil.rmtree(backup_dir, ignore_errors=True)
                        backup_dir = None

                except Exception as swap_err:
                    # Rollback from backup if swap failed
                    if backup_dir and backup_dir.exists():
                        if target_gen_dir.exists():
                            shutil.rmtree(target_gen_dir, ignore_errors=True)
                        shutil.move(str(backup_dir), str(target_gen_dir))
                        backup_dir = None
                    raise swap_err

                self.last_sync_success = True
                self.last_sync_error = None
                self.last_synced_revision = target_rev
                return True, target_rev

            except Exception as exc:
                err_msg = str(exc)
                self.last_sync_success = False
                self.last_sync_error = err_msg
                if self.local_reader:
                    self.local_reader._last_error = f"원격 세대 동기화 실패: {err_msg}"
                return False, err_msg

            finally:
                if staging_dir and staging_dir.exists():
                    try:
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    except Exception:
                        pass
                if backup_dir and backup_dir.exists():
                    try:
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    except Exception:
                        pass


def is_remote_generation_configured() -> bool:
    """Check if remote generation polling mode is explicitly configured."""
    enabled_str = os.environ.get('GENERATION_REMOTE_ENABLED', '')
    is_enabled = enabled_str.lower() in ('true', '1', 'yes')
    has_endpoint = bool(os.environ.get('GENERATION_REMOTE_ENDPOINT'))
    has_token = bool(os.environ.get('GENERATION_READ_TOKEN'))
    has_cache = bool(os.environ.get('GENERATION_CACHE_ROOT'))
    return is_enabled and has_endpoint and has_token and has_cache


def create_remote_syncer_from_env(cache_root: Optional[Path] = None, local_reader: Optional[GenerationReader] = None) -> Optional[RemoteGenerationSyncer]:
    """Factory helper: creates RemoteGenerationSyncer if configured by environment."""
    if not is_remote_generation_configured():
        return None

    endpoint = os.environ['GENERATION_REMOTE_ENDPOINT']
    token = os.environ['GENERATION_READ_TOKEN']
    c_root = Path(cache_root or os.environ['GENERATION_CACHE_ROOT']).resolve()
    allow_loopback = os.environ.get('GENERATION_ALLOW_LOOPBACK', '').lower() in ('true', '1', 'yes')
    timeout = int(os.environ.get('GENERATION_POLL_TIMEOUT', '15'))

    client = RemoteGenerationClient(
        endpoint=endpoint,
        token=token,
        enabled=True,
        allow_loopback=allow_loopback,
        timeout=timeout
    )
    poll_interval_str = os.environ.get('GENERATION_POLL_INTERVAL')
    poll_interval = float(poll_interval_str) if poll_interval_str else 30.0
    failure_backoff_str = os.environ.get('GENERATION_FAILURE_BACKOFF')
    failure_backoff = float(failure_backoff_str) if failure_backoff_str else 10.0

    return RemoteGenerationSyncer(
        client=client,
        cache_root=c_root,
        local_reader=local_reader,
        poll_interval=poll_interval,
        failure_backoff=failure_backoff
    )
