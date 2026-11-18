"""
Unit tests for Multichannel Phase 1 UI Contract & Semantic Defect Regressions
(docs/handover/multichannel_phase1_contract.md & multichannel_phase1_review_next_20261118.md).

Verifies:
1. All 5 state transitions: 수집0건, 미수집, 일부반영, 전건반영, 손익미산출
2. Defect 1: combined_revenue=null shows '통합 미산출' on All chip (never Coupang total under '전체 채널')
   and clarifies Coupang-only scope in top KPI, filter badge, table, and PnL.
3. Defect 2: Cafe24/Smartstore table headers & mobile cards match revenue_basis ('상품결제금액', '최종 상품금액'),
   never labeling refund-unadjusted Cafe24 126,000원 as '순매출액'.
4. Defect 3: B2B null displays '미수집' (never '0원' or '별도 입금 확인 기준'); 0원 only with verified zero evidence.
5. Defect 4: New 45-row Smartstore provisional subtotal (1,452,680원) separates collected 70 / included 45 / excluded 25,
   shows point-in-time caveat & remaining excluded reference without double-counting, and keeps profit/cost/BOM null.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import app as app_module

FORBIDDEN_RAW_TOKENS = [
    "multichannel_sales",
    "recognized_subtotal",
    "current_catalog_exact_proposal",
    "GW.IP_NOT_ALLOWED",
    "cafe24_goods_payment",
    "smartstore_final_product_display",
    "collected_zero",
    "partial_verified",
    "collected_unrecognized",
    "not_comparable",
    "source_lineage_private",
    "source:",
]


def _run_js_renderer(payload: dict, channel_filter: str = "all", pnl_month: str = "2026-11") -> dict:
    app_src = Path(app_module.BASE_DIR) / "app.py"
    src_text = app_src.read_text(encoding="utf-8")
    fmt_code = src_text[src_text.index("        function fmtWon(val) {"):src_text.index("        function cleanSettlementNote(note) {")]
    mobile_code = src_text[src_text.index("        function renderMobileDetailList(tab) {"):src_text.index("        function renderSalesTab(d) {")]
    pnl_code = src_text[src_text.index("        function renderPnLTab(d) {"):src_text.index("        function openManualTxModal() {")]
    sales_code = src_text[src_text.index("        function renderSalesTab(d) {"):src_text.index("        function getTxCategoryType(t) {")]

    node_harness = f"""
    const data = {json.dumps(payload, ensure_ascii=False)};
    function makeEl() {{
        const el = {{
            _innerText: '',
            _innerHTML: '',
            title: '',
            className: '',
            style: {{}},
            classList: {{
                _set: new Set(),
                add(c) {{ this._set.add(c); }},
                remove(c) {{ this._set.delete(c); }},
                toggle(c) {{ this._set.has(c) ? this._set.delete(c) : this._set.add(c); }},
                contains(c) {{ return this._set.has(c); }}
            }},
            get innerText() {{ return this._innerText; }},
            set innerText(v) {{ this._innerText = String(v ?? ''); }},
            get textContent() {{ return this._innerText || this._innerHTML.replace(/<[^>]+>/g, ' '); }},
            get innerHTML() {{ return this._innerHTML; }},
            set innerHTML(v) {{ this._innerHTML = String(v ?? ''); }},
            get rows() {{
                const trMatches = [...this._innerHTML.matchAll(/<tr[\\s\\S]*?<\\/tr>/gi)];
                return trMatches.map(m => {{
                    const tdMatches = [...m[0].matchAll(/<td[\\s\\S]*?<\\/td>/gi)];
                    return {{
                        cells: tdMatches.map(td => {{
                            const rawHtml = td[0];
                            const plain = rawHtml.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
                            return {{
                                textContent: plain,
                                querySelector(sel) {{
                                    if (sel === 'div') {{
                                        const dm = rawHtml.match(/<div[^>]*>([\\s\\S]*?)<\\/div>/i);
                                        if (dm) return {{ textContent: dm[1].replace(/<[^>]+>/g, ' ').trim() }};
                                    }}
                                    return null;
                                }}
                            }};
                        }})
                    }};
                }});
            }}
        }};
        return el;
    }}
    const elements = {{}};
    const document = {{
        getElementById(id) {{
            return elements[id] ||= makeEl();
        }},
        querySelectorAll() {{ return []; }}
    }};
    let currentPnlMonth = {json.dumps(pnl_month)};
    let currentSalesChannelFilter = {json.dumps(channel_filter)};
    let showZeroSalesInSalesTab = false;
    let allMasterData = [];
    function escapeHtml(x) {{ return String(x ?? ''); }}
    function findMasterByCode() {{ return null; }}
    """

    runner = node_harness + "\n" + fmt_code + "\n" + mobile_code + "\n" + pnl_code + "\n" + sales_code + """
    renderSalesTab(data);
    renderPnLTab(data);
    const out = {};
    for (const [k, el] of Object.entries(elements)) {
        out[k] = {
            innerText: el.innerText,
            innerHTML: el.innerHTML,
            hidden: el.classList.contains('hidden')
        };
    }
    console.log(JSON.stringify(out));
    """

    with tempfile.TemporaryDirectory() as td:
        js_path = Path(td) / "mc_ui_test.js"
        js_path.write_text(runner, encoding="utf-8")
        proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise AssertionError(f"Node runner failed: {proc.stderr}")
        return json.loads(proc.stdout)


def _base_payload(mc_sales: dict) -> dict:
    return {
        "schema_version": "1.5",
        "metadata": {"report_date": "2026-11-17", "as_of": "2026-11-18", "month": "2026-11"},
        "sales_profit": {
            "provisional": True,
            "basis": "coupang_sales_analysis",
            "daily_kpi": {
                "report_date": "2026-11-18",
                "sales_date": "2026-11-17",
                "status": "available",
                "revenue": 1222060,
                "contribution_profit": None,
                "total_quantity": 57,
            },
            "monthly": {
                "month": "2026-11",
                "revenue": 20724720,
                "total_quantity": 1016,
                "quantity_unit": "option_sales",
                "cost": None,
                "fees": None,
                "profit": None,
                "contribution_profit": None,
                "channels": [{"channel": "coupang_growth", "revenue": 20724720, "row_count": 431, "cost": None, "fees": None, "profit": None}],
            },
            "daily_series": [{"date": "2026-11-05", "revenue": 1200000}, {"date": "2026-11-17", "revenue": 1222060}],
            "transactions": [{"product_code": "20TZ01", "channel": "coupang_growth", "quantity": 10, "revenue": 374750, "cost": None, "fees": None, "profit": None}],
        },
        "monthly_series": [
            {"month": "2026-09", "revenue": 21324660, "cost": 10000000, "fees": 10858709, "profit": 465951, "provisional": False},
            {"month": "2026-10", "revenue": 24281610, "cost": 12000000, "fees": 13723431, "profit": -1441821, "provisional": False},
            {"month": "2026-11", "revenue": 20724720, "cost": None, "fees": None, "profit": None, "provisional": True, "basis": "coupang_sales_analysis", "quantity_unit": "option_sales", "total_quantity": 1016},
        ],
        "multichannel_sales": mc_sales,
    }


class MultichannelPhase1UITests(unittest.TestCase):

    def _assert_no_raw_field_names(self, rendered: dict):
        combined_text = " ".join((v.get("innerText", "") + " " + v.get("innerHTML", "")) for v in rendered.values())
        for tok in FORBIDDEN_RAW_TOKENS:
            self.assertNotIn(tok, combined_text, f"Raw API/field token leaked to UI: {tok}")

    def test_state_collected_zero_and_not_collected_and_b2b_uncollected(self):
        mc = {
            "period": {"start": "2026-11-01", "end": "2026-11-17"},
            "combined_revenue": None,
            "combined_status": "not_comparable",
            "legacy_coupang_revenue": 20724720,
            "channels": {
                "cafe24": {
                    "coverage_status": "collected_zero",
                    "revenue_basis": "cafe24_goods_payment",
                    "recognized_subtotal": 0,
                    "included_count": 0,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": 0,
                    "profit": None,
                    "daily_series": [],
                },
                "smartstore": {
                    "coverage_status": "not_collected",
                    "revenue_basis": "smartstore_final_product_display",
                    "recognized_subtotal": None,
                    "included_count": 0,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": None,
                    "profit": None,
                    "daily_series": [],
                },
            },
            "rows": [],
            "refund_ledger": [],
            "shipping_ledger": [],
            "settlement_ledger": [],
        }
        res = _run_js_renderer(_base_payload(mc), channel_filter="all")
        self.assertEqual(res["salesChipCafe24Rev"]["innerText"], "0원 (주문 0건)")
        self.assertIn("정상 조회 0건", res["salesChipCafe24Sub"]["innerText"])
        self.assertEqual(res["salesChipSmartstoreRev"]["innerText"], "미수집")
        # Defect 1: All chip must say '통합 미산출', NOT Coupang's 20,724,720원
        self.assertEqual(res["salesChipAllRev"]["innerText"], "통합 미산출")
        self.assertNotIn("20,724,720", res["salesChipAllRev"]["innerText"])
        self.assertIn("쿠팡", res["salesMonthlyRevenueLabel"]["innerText"])
        self.assertIn("쿠팡", res["salesForecastRevenueLabel"]["innerText"])
        self.assertIn("쿠팡", res["salesTotalQtyLabel"]["innerText"])
        self.assertFalse(res["salesActiveFilterBadge"]["hidden"])
        self.assertIn("쿠팡 기준", res["salesActiveFilterBadge"]["innerText"])
        # Defect 3: B2B null must be '미수집', NEVER '0원' or '별도 입금 확인 기준'
        self.assertEqual(res["salesChipB2bRev"]["innerText"], "미수집")
        self.assertNotIn("0원", res["salesChipB2bRev"]["innerText"])
        self.assertNotIn("입금 확인", res["salesChipB2bSub"]["innerText"])
        # PnL 11월 clarifies Coupang provisional scope & keeps profit null
        self.assertEqual(res["pnlProfit"]["innerText"], "산출 불가")
        self.assertIn("쿠팡 잠정매출", res["pnlRevenueTitle"]["innerText"])
        self.assertIn("2026-11 쿠팡 잠정", res["pnlMonthChips"]["innerHTML"])
        self.assertIn("쿠팡 누적", res["pnlMonthChips"]["innerHTML"])
        self._assert_no_raw_field_names(res)

        # B2B filter empty table & footer must NOT distort to 0원
        res_b2b = _run_js_renderer(_base_payload(mc), channel_filter="b2b")
        self.assertIn("미수집", res_b2b["salesDetailTableFoot"]["innerHTML"])
        self.assertNotIn("0원", res_b2b["salesDetailTableFoot"]["innerHTML"])

        # B2B shows 0원 ONLY when verified zero evidence exists
        payload_b2b_zero = _base_payload(mc)
        payload_b2b_zero["collection_status"] = {"b2b": {"status": "success_empty"}}
        res_b2b_zero = _run_js_renderer(payload_b2b_zero, channel_filter="all")
        self.assertEqual(res_b2b_zero["salesChipB2bRev"]["innerText"], "0원 (주문 0건)")

    def test_defect2_cafe24_basis_headers_and_mobile_cards_not_net_sales(self):
        mc = {
            "period": {"start": "2026-11-01", "end": "2026-11-17"},
            "combined_revenue": None,
            "combined_status": "not_comparable",
            "legacy_coupang_revenue": 20724720,
            "api_diagnostic": {"http_status": 403, "error_code": "GW.IP_NOT_ALLOWED", "caller_ip_matches": False},
            "physical_evidence": [{"order_quantity": 2, "observed_physical_quantity": 4}],
            "channels": {
                "cafe24": {
                    "coverage_status": "partial_verified",
                    "revenue_basis": "cafe24_goods_payment",
                    "recognized_subtotal": 126000,
                    "included_count": 1,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": 3,
                    "profit": None,
                    "subtotal_label": "검증된 부분합",
                    "provisional": True,
                    "period": {"start": "2026-11-01", "end": "2026-11-17"},
                    "daily_series": [{"date": "2026-11-05", "amount": 126000, "included_count": 1}],
                },
                "smartstore": {
                    "coverage_status": "collected_unrecognized",
                    "revenue_basis": "smartstore_final_product_display",
                    "recognized_subtotal": None,
                    "included_count": 0,
                    "excluded_count": 70,
                    "amount_unverified_count": 0,
                    "order_quantity": None,
                    "profit": None,
                    "subtotal_label": "검증된 부분합",
                    "provisional": True,
                    "period": {"start": "2026-11-01", "end": "2026-11-17"},
                    "daily_series": [],
                    "current_catalog_exact_proposal": {
                        "amount": 1452680,
                        "row_count": 45,
                        "effective_in_subtotal": False,
                        "approval_status": "supervisor_review_pending",
                    },
                },
            },
            "rows": [
                {
                    "channel": "cafe24",
                    "product_code": "S100011",
                    "date": "2026-11-05",
                    "amount": 126000,
                    "order_quantity": 3,
                    "included_in_channel_subtotal": True,
                }
            ],
            "shipping_ledger": [{"channel": "cafe24", "amount": 5000}],
            "refund_ledger": [
                {"channel": "cafe24", "event_date": "2026-11-04", "original_payment_date": "2026-10-12", "goods_amount": 336000},
                {"channel": "smartstore", "event_date": "2026-11-14", "goods_amount": None},
            ],
            "settlement_ledger": [{"id": 1}] * 105,
        }
        res_c24 = _run_js_renderer(_base_payload(mc), channel_filter="cafe24")
        # Defect 2: Cafe24 header and mobile card MUST say '상품결제금액', NEVER '순매출액'
        self.assertEqual(res_c24["salesColRevHeader"]["innerText"], "상품결제금액")
        self.assertEqual(res_c24["salesColQtyHeader"]["innerText"], "주문수량")
        self.assertEqual(res_c24["salesColAspHeader"]["innerText"], "평균 상품단가")
        self.assertIn("상품결제금액", res_c24["mobileSalesList"]["innerHTML"])
        self.assertNotIn("순매출액", res_c24["mobileSalesList"]["innerHTML"])
        self.assertIn("S100011", res_c24["salesDetailTableBody"]["innerHTML"])
        self.assertIn("126,000원", res_c24["salesDetailTableBody"]["innerHTML"])
        self.assertIn("42,000원", res_c24["salesDetailTableFoot"]["innerHTML"])  # ASP = 126000/3
        self.assertIn("배송/환불 별도", res_c24["salesDetailTableFoot"]["innerHTML"])
        # PnL Cafe24 never calls 126,000원 '순매출'
        self.assertIn("상품결제 126,000원", res_c24["pnlChannelDetailBody"]["innerHTML"])
        self.assertIn("월 순매출 미결정", res_c24["pnlChannelDetailBody"]["innerHTML"])
        self.assertIn("-210,000원", res_c24["pnlChannelDetailBody"]["innerHTML"])
        self._assert_no_raw_field_names(res_c24)

        # Smartstore unrecognized filter must NOT show 0원 in footer
        res_ss = _run_js_renderer(_base_payload(mc), channel_filter="smartstore")
        self.assertEqual(res_ss["salesColRevHeader"]["innerText"], "최종 상품금액")
        self.assertIn("미반영", res_ss["salesDetailTableFoot"]["innerHTML"])
        self.assertNotIn("0원", res_ss["salesDetailTableFoot"]["innerHTML"])
        self._assert_no_raw_field_names(res_ss)

    def test_defect4_smartstore_45_rows_provisional_subtotal_no_double_count(self):
        # Simulate the new 45-row Smartstore provisional subtotal candidate (수집 70 / 반영 45 / 제외 25)
        rows = []
        # 45 included rows totaling 1,452,680원 across 2 SKUs (30*30,000 + 15*36,845 + remainder 5)
        for i in range(30):
            rows.append({
                "channel": "smartstore",
                "product_code": "20TZ01",
                "date": "2026-11-10",
                "amount": 30000,
                "order_quantity": 1,
                "included_in_channel_subtotal": True,
            })
        for i in range(15):
            amt = 36846 if i < 5 else 36845  # 5*36846 + 10*36845 = 184230 + 368450 = 552680; total = 1,452,680
            rows.append({
                "channel": "smartstore",
                "product_code": "20PB01",
                "date": "2026-11-12",
                "amount": amt,
                "order_quantity": 2,
                "included_in_channel_subtotal": True,
            })
        # 16 excluded case-variant rows = 330,380원
        for i in range(16):
            amt = 20650 if i < 14 else 20640  # 14*20650 + 2*20640 = 289100 + 41280 = 330380
            rows.append({
                "channel": "smartstore",
                "product_code": "10gl02",
                "date": "2026-11-13",
                "amount": amt,
                "order_quantity": 1,
                "included_in_channel_subtotal": False,
                "exclusion_reason": "case_variant_review_pending",
            })
        # 1 excluded scope_review 80CN08 = 26,900원
        rows.append({
            "channel": "smartstore",
            "product_code": "80CN08",
            "date": "2026-11-14",
            "amount": 26900,
            "order_quantity": 1,
            "included_in_channel_subtotal": False,
            "exclusion_reason": "scope_review",
        })
        # 8 excluded cancellations
        for i in range(8):
            rows.append({
                "channel": "smartstore",
                "product_code": "20TZ01",
                "date": "2026-11-15",
                "amount": None,
                "order_quantity": 1,
                "included_in_channel_subtotal": False,
                "exclusion_reason": "cancelled_order",
            })

        mc = {
            "period": {"start": "2026-11-01", "end": "2026-11-17"},
            "combined_revenue": None,
            "combined_status": "not_comparable",
            "legacy_coupang_revenue": 20724720,
            "api_diagnostic": {"http_status": 403, "error_code": "GW.IP_NOT_ALLOWED", "caller_ip_matches": False},
            "physical_evidence": [{"order_quantity": 2, "observed_physical_quantity": 4}],
            "channels": {
                "cafe24": {
                    "coverage_status": "partial_verified",
                    "revenue_basis": "cafe24_goods_payment",
                    "recognized_subtotal": 126000,
                    "included_count": 1,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": 3,
                    "profit": None,
                    "subtotal_label": "검증된 부분합",
                    "provisional": True,
                    "period": {"start": "2026-11-01", "end": "2026-11-17"},
                    "daily_series": [{"date": "2026-11-05", "amount": 126000, "included_count": 1}],
                },
                "smartstore": {
                    "coverage_status": "partial_verified",
                    "revenue_basis": "smartstore_final_product_display",
                    "recognized_subtotal": 1452680,
                    "included_count": 45,
                    "excluded_count": 25,
                    "amount_unverified_count": 0,
                    "excluded_reference_amount": 357280,
                    "order_quantity": 60,
                    "profit": None,
                    "subtotal_label": "현재코드 기준 잠정 부분합",
                    "provisional": True,
                    "period": {"start": "2026-11-01", "end": "2026-11-17"},
                    "daily_series": [
                        {"date": "2026-11-10", "amount": 900000, "included_count": 30},
                        {"date": "2026-11-12", "amount": 552680, "included_count": 15},
                    ],
                    "current_catalog_exact_proposal": {
                        "amount": 1452680,
                        "row_count": 45,
                        "effective_in_subtotal": True,
                        "approval_status": "supervisor_provisional_approved",
                    },
                },
            },
            "rows": rows,
            "shipping_ledger": [{"channel": "cafe24", "amount": 5000}],
            "refund_ledger": [
                {"channel": "cafe24", "event_date": "2026-11-04", "original_payment_date": "2026-10-12", "goods_amount": 336000},
            ] + [{"channel": "smartstore", "event_date": "2026-11-15", "goods_amount": None}] * 8,
            "settlement_ledger": [{"id": 1}] * 105,
        }
        res_ss = _run_js_renderer(_base_payload(mc), channel_filter="smartstore")
        # Smartstore chip: 1,452,680원 + 수집 70·반영 45·제외 25 (NO '45건 미반영', NO double-count!)
        self.assertEqual(res_ss["salesChipSmartstoreRev"]["innerText"], "1,452,680원")
        self.assertIn("현재코드 기준 잠정 부분합", res_ss["salesChipSmartstoreSub"]["innerText"])
        self.assertIn("수집 70", res_ss["salesChipSmartstoreSub"]["innerText"])
        self.assertIn("반영 45", res_ss["salesChipSmartstoreSub"]["innerText"])
        self.assertIn("제외 25", res_ss["salesChipSmartstoreSub"]["innerText"])
        self.assertNotIn("45건 미반영", res_ss["salesChipSmartstoreSub"]["innerText"])
        self.assertNotIn("2,905,360", res_ss["salesReportPeriod"]["innerHTML"])
        # Diagnosis & PnL show point-in-time caveat, remaining 357,280원 (330,380 + 26,900), and cancellations excluded
        diag_html = res_ss["salesReportPeriod"]["innerHTML"]
        self.assertIn("수집 70건 중 반영 45건 · 제외 25건", diag_html)
        self.assertIn("주문 당시코드/회계확정 아님", diag_html)
        self.assertIn("357,280원", diag_html)
        self.assertIn("330,380원", diag_html)
        self.assertIn("26,900원", diag_html)
        self.assertIn("취소 완료 8건 매출 제외", diag_html)
        # Table header & mobile card use '최종 상품금액' (NOT '순매출액'), sum matches 1,452,680원, profit 미산출
        self.assertEqual(res_ss["salesColRevHeader"]["innerText"], "최종 상품금액")
        self.assertIn("최종 상품금액", res_ss["mobileSalesList"]["innerHTML"])
        self.assertNotIn("순매출액", res_ss["mobileSalesList"]["innerHTML"])
        self.assertIn("1,452,680원", res_ss["salesDetailTableFoot"]["innerHTML"])
        self.assertIn("반영 45건 · 제외 25건", res_ss["salesDetailTableFoot"]["innerHTML"])
        self.assertIn("미산출", res_ss["salesDetailTableFoot"]["innerHTML"])
        # PnL shows 1,452,680원 & remaining 357,280원 & keeps profit null
        pnl_html = res_ss["pnlChannelDetailBody"]["innerHTML"]
        self.assertIn("상품금액 1,452,680원", pnl_html)
        self.assertIn("수집 70건 (반영 45 · 제외 25)", pnl_html)
        self.assertIn("357,280원", pnl_html)
        self.assertNotIn("45건 · 미반영/승인 대기", pnl_html)
        self.assertEqual(res_ss["pnlProfit"]["innerText"], "산출 불가")
        # All chip remains '통합 미산출'
        self.assertEqual(res_ss["salesChipAllRev"]["innerText"], "통합 미산출")
        self._assert_no_raw_field_names(res_ss)

    def test_state_full_verified_transition_and_combined_approved(self):
        mc = {
            "period": {"start": "2026-11-01", "end": "2026-11-17"},
            "combined_revenue": 22303400,
            "combined_status": "comparable_verified",
            "legacy_coupang_revenue": 20724720,
            "channels": {
                "cafe24": {
                    "coverage_status": "complete_verified",
                    "revenue_basis": "cafe24_goods_payment",
                    "recognized_subtotal": 126000,
                    "included_count": 1,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": 3,
                    "profit": None,
                    "provisional": False,
                    "daily_series": [{"date": "2026-11-05", "amount": 126000, "included_count": 1}],
                },
                "smartstore": {
                    "coverage_status": "complete_verified",
                    "revenue_basis": "smartstore_final_product_display",
                    "recognized_subtotal": 1452680,
                    "included_count": 45,
                    "excluded_count": 0,
                    "amount_unverified_count": 0,
                    "order_quantity": 52,
                    "profit": None,
                    "provisional": False,
                    "daily_series": [{"date": "2026-11-15", "amount": 1452680, "included_count": 45}],
                },
            },
            "rows": [],
            "refund_ledger": [],
            "shipping_ledger": [],
            "settlement_ledger": [],
        }
        res = _run_js_renderer(_base_payload(mc), channel_filter="all")
        self.assertEqual(res["salesChipAllRev"]["innerText"], "22,303,400원")
        self.assertEqual(res["salesChipSmartstoreRev"]["innerText"], "1,452,680원")
        self.assertIn("전건 반영", res["salesChipSmartstoreSub"]["innerText"])
        self.assertIn("전건 반영", res["salesChipCafe24Sub"]["innerText"])
        self._assert_no_raw_field_names(res)


if __name__ == "__main__":
    unittest.main()
