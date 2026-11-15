"""Export a private review report before applying explicitly approved rows."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply-reviewed', type=Path)
    parser.add_argument('--file-report', type=Path)
    parser.add_argument('--apply-files-reviewed', type=Path)
    args = parser.parse_args()
    import app
    from firebase_admin import auth
    if args.file_report:
        report = app.file_migration_report(app.db)
        args.file_report.parent.mkdir(parents=True, exist_ok=True)
        if args.file_report.exists():
            raise SystemExit('Choose a new report path to preserve the backup.')
        args.file_report.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print({'fileObjects': len(report['objects']), 'unresolved': len(report['unresolved'])})
    elif args.apply_files_reviewed:
        report = json.loads(args.apply_files_reviewed.read_text(encoding='utf-8'))
        print({'migratedFiles': app.apply_file_report(app.db, report)})
    elif args.report:
        report = app.identity_migration_report(app.db, list(auth.list_users().iterate_all()))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        if args.report.exists():
            raise SystemExit('Choose a new report path to preserve the backup.')
        args.report.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        print({'identities': len(report['identities']), 'unmatched': len(report['unmatched']), 'ordersForReview': len(report['orders'])})
    elif args.apply_reviewed:
        report = json.loads(args.apply_reviewed.read_text(encoding='utf-8'))
        app.normalize_identity_records(app.db, report)
        print({'appliedIdentities': app.apply_identity_report(app.db, report)})
    else:
        parser.error('Specify --report or --apply-reviewed.')


if __name__ == '__main__':
    main()
