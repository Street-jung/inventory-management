"""Verify both published files and their formal dashboard inventory snapshot."""
import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from fetch_gaemi_wms import ROOT, SNAPSHOT, EXPORT_HEADERS, inventory_sheet, snapshot_summary
from common import write_json

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--date',type=date.fromisoformat,required=True)
args=parser.parse_args()
canonical=ROOT/f'raw_data/inventory/{args.date}_gaemi_inventory.xlsx'
compatible=ROOT/f'raw_data/inventory/{args.date}_3pl_inventory.xlsx'
for path in (canonical,compatible):
    assert path.read_bytes().startswith(b'PK'), 'Expected actual XLSX'
    assert inventory_sheet(path)[2]==EXPORT_HEADERS
assert inventory_sheet(canonical)[1]==inventory_sheet(compatible)[1], 'Compatibility copy differs'
snapshot=json.loads(SNAPSHOT.read_text(encoding='utf-8'))
assert snapshot['verified_headers']==EXPORT_HEADERS
assert snapshot['download_filename'].endswith('.csv')
summary=snapshot_summary(args.date)
dashboard=json.loads((ROOT/'web_dashboard/dashboard_data.json').read_text(encoding='utf-8'))
assert dashboard['metadata']['report_date']==str(args.date)
assert dashboard['inventory_snapshots']['gaemi']==summary
assert str(canonical) in dashboard['metadata']['sources']
report=dict(verified=True,summary=summary,query_selector=snapshot['query']['selector'],
            export_selector=snapshot['export']['selector'],page_url=snapshot['page_url'],
            source_csv=snapshot['download_filename'],headers=EXPORT_HEADERS,
            canonical_sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            dashboard_generated_at=dashboard['metadata']['generated_at'])
write_json(ROOT/f'output_excel/{args.date}_gaemi_inventory_verification.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))
