"""Database-independent ledger operations. Storage adapters own transactions/locks."""
import copy
import json
import uuid
try:
    from .manual_sales_common import ManualSalesError, encoded, now
except ImportError:
    from manual_sales_common import ManualSalesError, encoded, now


class LedgerOperations:
    def _lock_key(self, conn, key):
        pass  # SQLite uses the encompassing BEGIN IMMEDIATE.

    def _lock_sale(self, conn, sale_id):
        pass

    @staticmethod
    def _sale(conn, sale_id):
        row = conn.execute('SELECT document FROM sales WHERE sale_id=?', (sale_id,)).fetchone()
        if row is None:
            raise ManualSalesError(404, 'SALE_NOT_FOUND', '판매기록을 찾을 수 없습니다.')
        return json.loads(row['document'])

    def get(self, sale_id):
        with self.transaction() as conn:
            sale = self._sale(conn, sale_id)
            count = conn.execute('SELECT COUNT(*) FROM events WHERE sale_id=?', (sale_id,)).fetchone()[0]
            return dict(success=True, sale=sale, history_count=count)

    def listing(self, limit, offset, status=None, date_from=None, date_to=None):
        terms, values = [], []
        for column, operator, value in (('status', '=', status), ('sale_date', '>=', date_from), ('sale_date', '<=', date_to)):
            if value is not None:
                terms.append(f'{column}{operator}?'); values.append(value)
        where = ' WHERE ' + ' AND '.join(terms) if terms else ''
        with self.transaction() as conn:
            total = conn.execute('SELECT COUNT(*) FROM sales' + where, values).fetchone()[0]
            rows = conn.execute('SELECT document FROM sales' + where + ' ORDER BY created_at DESC,sale_id DESC LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
            return dict(success=True, items=[json.loads(r['document']) for r in rows], total=total, limit=limit, offset=offset)

    def history(self, sale_id, limit, offset):
        with self.transaction() as conn:
            self._sale(conn, sale_id)
            total = conn.execute('SELECT COUNT(*) FROM events WHERE sale_id=?', (sale_id,)).fetchone()[0]
            rows = conn.execute('SELECT * FROM events WHERE sale_id=? ORDER BY revision LIMIT ? OFFSET ?', (sale_id, limit, offset)).fetchall()
            items = [dict(event_id=r['event_id'], revision=r['revision'], type=r['type'], reason=r['reason'], actor=json.loads(r['actor']),
                          before=json.loads(r['before_document']) if r['before_document'] else None, after=json.loads(r['after_document']), created_at=r['created_at']) for r in rows]
            return dict(success=True, items=items, total=total, limit=limit, offset=offset)

    def idempotency(self, key):
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM idempotency WHERE key=?', (key,)).fetchone()
            if row is None:
                abandoned = conn.execute('SELECT abandoned_at FROM idempotency_abandonments WHERE key=?', (key,)).fetchone()
                if abandoned is not None:
                    return self._abandoned(key, abandoned['abandoned_at'])
                raise ManualSalesError(404, 'IDEMPOTENCY_NOT_FOUND', '아직 확인된 저장 결과가 없습니다.')
            return dict(success=True, key=key, operation=row['operation'], sale_id=row['sale_id'], http_status=row['http_status'], result=json.loads(row['response']))

    @staticmethod
    def _abandoned(key, stamp):
        return dict(success=True, resolution='abandoned', key=key, abandoned_at=stamp,
                    operation=None, sale_id=None, http_status=None, result=None)

    def resolve(self, key, actor):
        with self.transaction() as conn:
            self._lock_key(conn, key)
            row = conn.execute('SELECT * FROM idempotency WHERE key=?', (key,)).fetchone()
            if row is not None:
                return dict(success=True, resolution='committed', key=key, operation=row['operation'],
                            sale_id=row['sale_id'], http_status=row['http_status'], result=json.loads(row['response']))
            abandoned = conn.execute('SELECT abandoned_at FROM idempotency_abandonments WHERE key=?', (key,)).fetchone()
            if abandoned is not None:
                return self._abandoned(key, abandoned['abandoned_at'])
            stamp = now()
            conn.execute('INSERT INTO idempotency_abandonments VALUES(?,?,?)', (key, stamp, encoded(actor)))
            return self._abandoned(key, stamp)

    def mutate(self, operation, request_key, request_hash, actor, document=None, sale_id=None, expected_revision=None, reason=None, enrich=None):
        with self.transaction() as conn:
            self._lock_key(conn, request_key)
            if sale_id is not None:
                self._lock_sale(conn, sale_id)
            idem = conn.execute('SELECT * FROM idempotency WHERE key=?', (request_key,)).fetchone()
            if idem is not None:
                if idem['request_hash'] != request_hash:
                    raise ManualSalesError(409, 'IDEMPOTENCY_CONFLICT', '같은 재시도 키에 다른 요청이 전달되었습니다.')
                return json.loads(idem['response']), idem['http_status'], True
            if conn.execute('SELECT 1 FROM idempotency_abandonments WHERE key=?', (request_key,)).fetchone():
                raise ManualSalesError(409, 'IDEMPOTENCY_ABANDONED', '서버에서 확정 종료한 요청 키입니다. 이 요청은 저장되지 않습니다.')
            stamp = now()
            before = None if operation == 'create' else self._sale(conn, sale_id)
            if before and before['revision'] != expected_revision:
                raise ManualSalesError(409, 'REVISION_CONFLICT', '다른 수정이 먼저 저장되었습니다. 최신 내용을 다시 조회하세요.', current_revision=before['revision'])
            if before and before['status'] == 'cancelled':
                if operation != 'cancel':
                    raise ManualSalesError(409, 'SALE_CANCELLED', '취소된 판매기록은 수정할 수 없습니다.')
                event = conn.execute('SELECT event_id FROM events WHERE sale_id=? ORDER BY revision DESC LIMIT 1', (sale_id,)).fetchone()
                response = dict(success=True, sale=before, event_id=event['event_id'], message='판매기록 취소 완료 · 실제 환불 완료 아님')
                conn.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)', (request_key, operation, request_hash, sale_id, encoded(response), 200, stamp))
                return response, 200, False
            if operation == 'cancel':
                sale = copy.deepcopy(before)
                sale.update(status='cancelled', reflection_status='not_applicable', cancellation=dict(reason=reason, cancelled_at=stamp, actor=actor))
            else:
                sale = enrich(copy.deepcopy(document))
                known = {line['line_id'] for line in before['lines']} if before else set()
                for line in sale['lines']:
                    line_id = line.get('line_id')
                    if line_id and line_id not in known:
                        raise ManualSalesError(422, 'VALIDATION_ERROR', '이 판매기록에 없는 품목 ID입니다.', 'lines.line_id')
                    line['line_id'] = line_id or str(uuid.uuid4())
                    line.update(cost=None, profit=None, physical_quantity=None, included_in_public_sales=False)
                    link = line.get('online_link')
                    line['representative_source'] = 'online_link_declared' if link else 'manual_unreviewed'
                    if link:
                        link['match_status'] = 'declared_unverified'
                sale.update(status='submitted', reflection_status='pending', recognition_status='unreviewed', included_in_public_sales=False, cost=None, profit=None)
            sale_id = sale_id or str(uuid.uuid4())
            revision = before['revision'] + 1 if before else 1
            sale.update(sale_id=sale_id, revision=revision, created_at=before['created_at'] if before else stamp,
                        created_by=before['created_by'] if before else actor, updated_at=stamp, updated_by=actor)
            if before:
                changed = conn.execute('UPDATE sales SET revision=?,status=?,sale_date=?,document=?,updated_at=? WHERE sale_id=? AND revision=?',
                                       (revision, sale['status'], sale['sale_date'], encoded(sale), stamp, sale_id, expected_revision)).rowcount
                if changed != 1:
                    raise ManualSalesError(409, 'REVISION_CONFLICT', '수정 버전이 변경되었습니다.')
            else:
                conn.execute('INSERT INTO sales VALUES(?,?,?,?,?,?,?)', (sale_id, revision, sale['status'], sale['sale_date'], encoded(sale), stamp, stamp))
            for line in sale['lines']:
                link = line.get('online_link')
                if not link:
                    continue
                identity = (link['channel'], link['order_key'], link['item_key'])
                owner = conn.execute('SELECT sale_id FROM online_links WHERE channel=? AND order_key=? AND item_key=?', identity).fetchone()
                if owner and owner['sale_id'] != sale_id:
                    raise ManualSalesError(409, 'ONLINE_LINK_CONFLICT', '같은 온라인 주문품목이 다른 판매기록에 연결되어 있습니다.')
                linked = conn.execute('INSERT INTO online_links VALUES(?,?,?,?,?) ON CONFLICT(channel,order_key,item_key) DO UPDATE SET line_id=excluded.line_id WHERE online_links.sale_id=excluded.sale_id', (*identity, sale_id, line['line_id'])).rowcount
                if linked != 1:
                    raise ManualSalesError(409, 'ONLINE_LINK_CONFLICT', '같은 온라인 주문품목이 다른 판매기록에 연결되어 있습니다.')
            event_id = str(uuid.uuid4())
            conn.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)', (event_id, sale_id, revision, operation, reason or '판매기록 등록', encoded(actor), encoded(before) if before else None, encoded(sale), stamp))
            conn.execute("UPDATE reflection_queue SET status='superseded' WHERE sale_id=? AND status='pending'", (sale_id,))
            conn.execute('INSERT INTO reflection_queue VALUES(?,?,?,?)', (sale_id, revision, sale['reflection_status'], stamp))
            response = dict(success=True, sale=sale, event_id=event_id, message='판매기록 취소 완료 · 실제 환불 완료 아님' if operation == 'cancel' else '판매기록 저장 완료 · 대시보드 반영 대기')
            status = 201 if operation == 'create' else 200
            conn.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)', (request_key, operation, request_hash, sale_id, encoded(response), status, stamp))
            return response, status, False
