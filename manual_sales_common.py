"""Shared ledger errors and canonical serialization; no storage side effects."""
import json
from datetime import datetime, timezone


class ManualSalesError(Exception):
    def __init__(self, status, code, message, field=None, **details):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.field, self.details = field, details


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


