"""Opt-in generation inventory -> existing UI memory, no master-store writes."""
import copy,re
from datetime import date
from .automation_status import instant

def validate_snapshots(data,allowed):
    snapshots=data.get('inventory_snapshots_v2',{});metadata=data.get('inventory_snapshot_status',{})
    if not metadata:return None
    if set(metadata)!=set(('gaemi','coupang')) or not set(metadata)<=set(snapshots):raise ValueError('invalid_inventory_snapshot_fields')
    for name,meta in metadata.items():
        if set(meta)!=set(('snapshot_date','observed_at','source','basis','owned_keys','status')) or meta['status']!='validated_snapshot':raise ValueError('invalid_inventory_metadata')
        date.fromisoformat(meta['snapshot_date']);instant(meta['observed_at'])
        if not re.fullmatch('source:[a-f0-9]{64}',meta['source']):raise ValueError('invalid_inventory_source_reference')
        if meta['basis']!=('inventory_snapshot_current' if name=='gaemi' else 'own_seller_inventory_snapshot'):raise ValueError('inventory_basis_changed')
        items=snapshots[name]
        if not isinstance(items,dict) or not items or not set(items)<=set(allowed[name]) or type(meta['owned_keys']) is not int or meta['owned_keys']!=len(items):raise ValueError('inventory_mapping_mismatch')
        fields=set(('product_name','option_name','stock','reg_date','expiry_date','days_left')) if name=='gaemi' else set(('product_id','product_name','option_name','grade','is_return','stock','inbound','sales_7d','sales_30d','doc','updated_at'))
        for item in items.values():
            rows=item if name=='gaemi' else [item]
            if not isinstance(rows,list) or not rows:raise ValueError('invalid_inventory_lots')
            for row in rows:
                if not isinstance(row,dict) or set(row)!=fields:raise ValueError('invalid_inventory_row')
                for key,value in row.items():
                    if key in ('stock','inbound','sales_7d','sales_30d','doc','days_left'):
                        if value is None and key in ('days_left','sales_7d','sales_30d','doc'):continue
                        if type(value) is not int or abs(value)>1000000000:raise ValueError('invalid_inventory_quantity')
                    elif key=='is_return':
                        if type(value) is not bool:raise ValueError('invalid_inventory_grade')
                    elif not isinstance(value,str) or len(value)>500:raise ValueError('invalid_inventory_text')
    return {name:copy.deepcopy(snapshots[name]) for name in metadata},copy.deepcopy(metadata)

class InventoryBridge:
    def __init__(self,ui,reader_provider):self.ui=ui;self.reader_provider=reader_provider;self.last_revision=None
    def apply(self):
        reader=self.reader_provider();record=reader.resolve() if reader else None
        if record is None:return None
        try:
            result=validate_snapshots(record.dashboard_data,self.ui.inventory_whitelist())
            if result is None:return None
            snapshots,metadata=result
            with self.ui.data_lock:
                old=self.ui.app_state.get('inventory_snapshot_status',{})
                for name,meta in metadata.items():
                    if name in old and (meta['snapshot_date']<old[name]['snapshot_date'] or instant(meta['observed_at'])<instant(old[name]['observed_at'])):raise ValueError('inventory_source_regression')
                for name,value in snapshots.items():self.ui.app_state[name+'_inventory']=value
                self.ui.app_state['inventory_snapshot_status']=metadata
                self.ui.app_state['inventory_generation_revision']=record.revision
            self.last_revision=record.revision;return record.revision
        except Exception:return None

class InventoryMiddleware:
    def __init__(self,application,bridge):self.application=application;self.bridge=bridge
    def __call__(self,environ,start_response):
        revision=None
        if environ.get('PATH_INFO') in ('/api/admin/config','/api/summary'):
            try:revision=self.bridge.apply()
            except Exception:pass
        def respond(status,headers,exc_info=None):
            if revision:headers=list(headers)+[('X-Inventory-Generation-Revision',revision)]
            return start_response(status,headers,exc_info)
        return self.application(environ,respond)
