"""UNDEPLOYED opt-in entry; original app remains unchanged."""
import os,hashlib
from urllib.parse import urlsplit
from app import app,get_generation_reader
import app as ui_module
from automation_status_backend.automation_status import StatusHTTP
from automation_status_backend.automation_status_wsgi import StatusMiddleware
application=app
if os.environ.get('AUTOMATION_STATUS_REMOTE_ENABLED')=='true':
    endpoint=os.environ['AUTOMATION_STATUS_ENDPOINT'];p=urlsplit(endpoint)
    if p.scheme!='https' or p.hostname!='script.google.com' or p.path!='/macros/s/'+os.environ['AUTOMATION_STATUS_DEPLOYMENT_ID']+'/exec' or p.query or p.fragment or p.username or p.password or hashlib.sha256(endpoint.encode()).hexdigest()!=os.environ['AUTOMATION_STATUS_ENDPOINT_SHA256']:
        raise RuntimeError('reviewed status target required')
    reader=StatusHTTP(endpoint,os.environ['GENERATION_READ_TOKEN'],enabled=True,timeout=5)
    def revision():
        current=get_generation_reader()
        return current.resolve().revision if current else None
    application=StatusMiddleware(app.wsgi_app,reader,os.environ['AUTOMATION_STATUS_CACHE_ROOT'],revision)
if os.environ.get('AUTOMATION_INVENTORY_GENERATION_ENABLED')=='true':
    from automation_status_backend.inventory_generation_runtime import InventoryBridge,InventoryMiddleware
    bridge=InventoryBridge(ui_module,get_generation_reader)
    legacy_refresh=ui_module.refresh_raw_inventory
    def refresh():
        if bridge.apply() is None and bridge.last_revision is None:return legacy_refresh()
    ui_module.refresh_raw_inventory=refresh
    application=InventoryMiddleware(application,bridge)
