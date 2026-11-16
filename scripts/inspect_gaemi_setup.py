"""Read-only inventory/setup inspection; never prints account values."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values
from common import ROOT, read_excel_sheets

for path in (ROOT / 'raw_data/inventory').glob('*3pl*'):
    for name, rows in read_excel_sheets(path):
        print('Inventory:', path.name, 'sheet:', name, 'rows:', len(rows), 'header/sample:', rows[:2])
env = dotenv_values(ROOT / 'config/.env')
print('WMS settings present:', {k: bool(env.get(k)) for k in ('GAEMI_WMS_URL', 'GAEMI_WMS_ID', 'GAEMI_WMS_PW')})
print('WMS-related setting names:', [k for k,v in env.items() if re.search('gaemi|개미|wms', k+' '+str(v), re.I)])
store_path = ROOT / 'web_dashboard/inventory_master_store.json'
store = json.loads(store_path.read_text(encoding='utf-8-sig'))
print('Store keys:', list(store))
print('Gaemi sample structure:', list(store.get('gaemi_inventory', {}).values())[:1])
files = list((ROOT/'config').glob('*.json')) + list((ROOT/'scripts').glob('*.py')) + [store_path]
for file in files:
    for value in re.findall(r'https?://[^\s"<>]+', file.read_text(encoding='utf-8-sig')):
        if re.search('gaemi|개미|wms', value, re.I):
            url = urlsplit(value)
            print('WMS URL clue:', file.relative_to(ROOT), url.hostname, url.path)
