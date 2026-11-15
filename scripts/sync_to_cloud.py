"""Back up all accumulated source/report files; publish only after all uploads succeed."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web_dashboard.cloud_storage import CloudStore, backup, load_cloud_env, restore_archive


def run(dry_run=False, required=False):
    load_cloud_env(ROOT)
    store = CloudStore()
    if not store.configured and not dry_run:
        if required:
            raise RuntimeError('Cloud backup required: configure GOOGLE_APPS_SCRIPT_URL/TOKEN or CLOUD_DRIVE_DIR')
        return dict(status='disabled', message='Cloud backup not configured; see config/cloud.env.example')
    return backup(ROOT, store, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--required', action='store_true')
    parser.add_argument('--restore-to', type=Path, help='Restore all archived original filenames into an empty directory')
    parser.add_argument('--revision', help='Optional manifest revision for archive recovery')
    args = parser.parse_args()
    if args.revision and not args.restore_to:
        parser.error('--revision requires --restore-to')
    try:
        if args.restore_to:
            load_cloud_env(ROOT)
            store = CloudStore()
            if not store.configured:
                raise RuntimeError('Cloud backup not configured')
            print(restore_archive(args.restore_to, store, args.revision))
        else:
            print(run(args.dry_run, args.required))
    except Exception as exc:
        print('Cloud backup failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
