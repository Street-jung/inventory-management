"""Collect best-effort channel data, then reconcile and export financial reports."""
import argparse
import sys
import logging
from pathlib import Path
from datetime import date, timedelta

try:
    from . import filter_bank_and_hometax, generate_monthly_pnl, export_dashboard_json, sync_to_cloud
    from .common import yesterday, read_json, ROOT
except ImportError:
    import filter_bank_and_hometax
    import generate_monthly_pnl
    import export_dashboard_json
    import sync_to_cloud
    from common import yesterday, read_json, ROOT


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=yesterday())
    parser.add_argument('--skip-fetch', action='store_true', help='기존 파일로 로컬 처리')
    parser.add_argument('--skip-api-fetch', action='store_true', help='카페24/스마트스토어 API만 생략하고 쿠팡 윙은 수집')
    parser.add_argument('--output-dir', type=Path, help='대조표/결산/대시보드 JSON 출력 디렉터리')
    parser.add_argument('--notify', action='store_true', help='텔레그램 알림 실제 전송')
    parser.add_argument('--skip-cloud', action='store_true', help='로컬 집계만 실행(백업 생략)')
    parser.add_argument('--require-cloud', action='store_true', help='백업 미설정/실패 시 오류 종료')
    parser.add_argument('--coupang-filename-month', action=argparse.BooleanOptionalAction,
                        default=read_json(ROOT / 'config/food_filter_rules.json').get('dashboard_export', {}).get('coupang_filename_month', False),
                        help='쿠팡 파일명 기준 임시 월 손익 반영')
    args = parser.parse_args()
    output_kwargs = {'output_dir': args.output_dir} if args.output_dir else {}
    reconciliation_kwargs = {'output_dir': args.output_dir / 'reconciliation'} if args.output_dir else {}
    dashboard_kwargs = {'output': args.output_dir / 'dashboard_data.json'} if args.output_dir else {}
    try:
        if not args.skip_fetch:
            try:
                from . import fetch_api_sales, fetch_coupang_wing, fetch_gaemi_wms
            except ImportError:
                import fetch_api_sales
                import fetch_coupang_wing
                import fetch_gaemi_wms
            print('[1/5] 쿠팡 윙 수집:', fetch_coupang_wing.run(args.date))
            try:
                print('[1/5] 개미창고 재고 수집:', fetch_gaemi_wms.run())
            except Exception as exc:
                logging.warning('개미창고 수집 실패 [%s]: %s. 기존 파일로 결산 계속',
                                type(exc).__name__, fetch_gaemi_wms.safe_reason(exc))
            if not args.skip_api_fetch:
                try:
                    print('[1/5] 매출 수집:', fetch_api_sales.run(args.date))
                except Exception as exc:
                    logging.warning('쇼핑몰 API 수집 초기화 실패 [%s]: %s. 기존 파일로 결산 계속',
                                    type(exc).__name__, fetch_api_sales.safe_failure(exc))
        print('[2/5] 은행/세금계산서 대조:', filter_bank_and_hometax.run(args.date + timedelta(days=1), args.notify, summary_only=True, **reconciliation_kwargs))
        print('[3/5] 월간 결산:', generate_monthly_pnl.run_all(args.date, args.coupang_filename_month, **output_kwargs))
        print('[4/5] 대시보드 JSON:', export_dashboard_json.run(args.date, args.date + timedelta(days=1), args.coupang_filename_month, **dashboard_kwargs))
        if not args.skip_cloud:
            print('[5/5] 클라우드 백업:', sync_to_cloud.run(required=args.require_cloud))
    except Exception as exc:
        print(f'파이프라인 중단: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
