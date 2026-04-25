"""
tests/test_form4_xml.py — Commit 2: Form 4 XML enrichment.

Covers:
  - _parse_form4_xml extracts structured fields (shares, price, code, title)
  - _fetch_form4_xml returns {} on HTTP / parse errors
  - is_high_conviction_trade requires officer + open-market purchase + $10K
  - _XML_PACING_SEC is set for SEC rate limit compliance
  - accession_number populated from EDGAR FTS 'adsh' field
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


_FIXTURE_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>JANE DOE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>EVP, General Counsel</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-15</value></transactionDate>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


# ═══════════════════════════════════════════════════════════════════════════
# _parse_form4_xml
# ═══════════════════════════════════════════════════════════════════════════

class TestParseForm4XML:
    def test_parses_shares_and_price(self):
        from insider_intelligence import _parse_form4_xml
        d = _parse_form4_xml(_FIXTURE_FORM4_XML)
        assert d["transaction_shares"] == 1000.0
        assert d["transaction_price"] == 50.0

    def test_extracts_open_market_purchase_code(self):
        from insider_intelligence import _parse_form4_xml
        d = _parse_form4_xml(_FIXTURE_FORM4_XML)
        assert d["transaction_code"] == "P"
        assert d["acquired_disposed"] == "A"

    def test_extracts_officer_title_and_role_flags(self):
        from insider_intelligence import _parse_form4_xml
        d = _parse_form4_xml(_FIXTURE_FORM4_XML)
        assert d["officer_title"] == "EVP, General Counsel"
        assert d["is_officer"] is True
        assert d["is_director"] is False
        assert d["is_ten_percent_owner"] is False

    def test_extracts_issuer_trading_symbol(self):
        from insider_intelligence import _parse_form4_xml
        d = _parse_form4_xml(_FIXTURE_FORM4_XML)
        assert d["issuer_trading_symbol"] == "NVDA"
        assert d["issuer_name"] == "NVIDIA CORP"

    def test_returns_empty_on_parse_error(self):
        from insider_intelligence import _parse_form4_xml
        assert _parse_form4_xml("not xml at all <<<") == {}
        assert _parse_form4_xml("") == {}


# ═══════════════════════════════════════════════════════════════════════════
# _fetch_form4_xml — HTTP layer
# ═══════════════════════════════════════════════════════════════════════════

class TestFetchForm4XML:
    def test_returns_empty_on_index_404(self, monkeypatch):
        import insider_intelligence as ii

        def fake_get(url, **kw):
            r = MagicMock()
            r.status_code = 404
            return r

        monkeypatch.setattr("insider_intelligence.requests.get", fake_get)
        # Skip the pacing sleep
        monkeypatch.setattr("insider_intelligence._time.sleep", lambda _s: None)

        assert ii._fetch_form4_xml("12345", "0000123456-26-000001") == {}

    def test_returns_empty_when_no_xml_in_filing(self, monkeypatch):
        import insider_intelligence as ii

        def fake_get(url, **kw):
            r = MagicMock()
            r.status_code = 200
            r.json = lambda: {"directory": {"item": [
                {"name": "primary.htm"}, {"name": "summary.txt"},
            ]}}
            return r

        monkeypatch.setattr("insider_intelligence.requests.get", fake_get)
        monkeypatch.setattr("insider_intelligence._time.sleep", lambda _s: None)
        assert ii._fetch_form4_xml("12345", "0000123456-26-000001") == {}

    def test_full_path_returns_parsed_fields(self, monkeypatch):
        import insider_intelligence as ii

        calls: list[str] = []

        def fake_get(url, **kw):
            calls.append(url)
            r = MagicMock()
            r.status_code = 200
            if "index.json" in url:
                r.json = lambda: {"directory": {"item": [{"name": "form4.xml"}]}}
            else:
                r.text = _FIXTURE_FORM4_XML
            return r

        monkeypatch.setattr("insider_intelligence.requests.get", fake_get)
        monkeypatch.setattr("insider_intelligence._time.sleep", lambda _s: None)

        d = ii._fetch_form4_xml("0001045810", "0000123456-26-000001")
        assert d["transaction_shares"] == 1000.0
        assert d["transaction_price"] == 50.0
        assert d["officer_title"] == "EVP, General Counsel"
        # Two GETs: one for index.json, one for the xml
        assert len(calls) == 2

    def test_pacing_sleep_called_on_success(self, monkeypatch):
        import insider_intelligence as ii

        def fake_get(url, **kw):
            r = MagicMock()
            r.status_code = 200
            if "index.json" in url:
                r.json = lambda: {"directory": {"item": [{"name": "form4.xml"}]}}
            else:
                r.text = _FIXTURE_FORM4_XML
            return r

        sleeps: list[float] = []
        monkeypatch.setattr("insider_intelligence.requests.get", fake_get)
        monkeypatch.setattr("insider_intelligence._time.sleep",
                            lambda s: sleeps.append(s))
        ii._fetch_form4_xml("0001045810", "0000123456-26-000001")
        assert len(sleeps) == 1
        assert sleeps[0] == ii._XML_PACING_SEC

    def test_returns_empty_on_missing_inputs(self, monkeypatch):
        import insider_intelligence as ii
        # Don't mock — must short-circuit before any HTTP call
        assert ii._fetch_form4_xml("", "ABC") == {}
        assert ii._fetch_form4_xml("123", "") == {}


# ═══════════════════════════════════════════════════════════════════════════
# is_high_conviction_trade
# ═══════════════════════════════════════════════════════════════════════════

class TestHighConvictionPredicate:
    def test_grant_does_not_set_high_conviction(self):
        from insider_intelligence import is_high_conviction_trade
        # Code='A' = grant; never high conviction even if officer + $50K
        trade = {
            "is_officer": True, "transaction_code": "A",
            "transaction_shares": 10000, "transaction_price": 50.0,
        }
        assert is_high_conviction_trade(trade) is False

    def test_option_exercise_does_not_set_high_conviction(self):
        from insider_intelligence import is_high_conviction_trade
        trade = {
            "is_officer": True, "transaction_code": "M",
            "transaction_shares": 10000, "transaction_price": 50.0,
        }
        assert is_high_conviction_trade(trade) is False

    def test_purchase_under_threshold_does_not_set_high_conviction(self):
        from insider_intelligence import is_high_conviction_trade
        trade = {
            "is_officer": True, "transaction_code": "P",
            "transaction_shares": 10, "transaction_price": 50.0,
        }
        assert is_high_conviction_trade(trade) is False

    def test_director_purchase_alone_does_not_set_high_conviction(self):
        from insider_intelligence import is_high_conviction_trade
        # is_officer=False but is_director=True — predicate requires officer
        trade = {
            "is_officer": False, "is_director": True, "transaction_code": "P",
            "transaction_shares": 1000, "transaction_price": 50.0,
        }
        assert is_high_conviction_trade(trade) is False

    def test_large_officer_purchase_fires(self):
        from insider_intelligence import is_high_conviction_trade
        trade = {
            "is_officer": True, "transaction_code": "P",
            "transaction_shares": 1000, "transaction_price": 50.0,
        }
        assert is_high_conviction_trade(trade) is True


# ═══════════════════════════════════════════════════════════════════════════
# Pacing constant
# ═══════════════════════════════════════════════════════════════════════════

class TestPacingConstant:
    def test_xml_pacing_under_sec_limit(self):
        """SEC rate limit is 10 req/sec; pacing must keep us under that."""
        from insider_intelligence import _XML_PACING_SEC
        assert _XML_PACING_SEC > 0
        # 1/_XML_PACING_SEC must be <= 10 (SEC's documented limit)
        assert 1.0 / _XML_PACING_SEC <= 10.0


# ═══════════════════════════════════════════════════════════════════════════
# Integration — accession_number populated
# ═══════════════════════════════════════════════════════════════════════════

class TestAccessionNumberPopulated:
    def test_fetch_edgar_form4_uses_adsh_for_accession(self, monkeypatch):
        """EDGAR FTS exposes accession as 'adsh', not 'accession_no'.
        Trade record's accession_number must be populated from 'adsh'."""
        import insider_intelligence as ii

        sample_search = {"hits": {"hits": [{"_source": {
            "adsh": "0001234567-26-000001",
            "ciks": ["0001234567", "0001045810"],
            "display_names": ["JANE DOE  (CIK 0001234567)"],
            "period_of_report": "2026-04-15",
            "category": "insider",
        }}]}}

        # First requests.get returns the FTS search; subsequent calls go to
        # _fetch_form4_xml which we short-circuit by returning a 404 on index.
        call_idx = [0]

        def fake_get(url, **kw):
            r = MagicMock()
            call_idx[0] += 1
            if "efts.sec.gov" in url:
                r.status_code = 200
                r.json = lambda: sample_search
                r.raise_for_status = lambda: None
            elif "index.json" in url:
                r.status_code = 200
                r.json = lambda: {"directory": {"item": [{"name": "form4.xml"}]}}
            else:
                r.status_code = 200
                r.text = _FIXTURE_FORM4_XML
            return r

        monkeypatch.setattr("insider_intelligence.requests.get", fake_get)
        monkeypatch.setattr("insider_intelligence._time.sleep", lambda _s: None)

        trades = ii._fetch_edgar_form4("NVDA", "2026-03-25", "2026-04-25")
        assert len(trades) == 1
        t = trades[0]
        assert t["accession_number"] == "0001234567-26-000001"
        # XML enrichment also flowed through
        assert t["transaction_shares"] == 1000.0
        assert t["transaction_price"] == 50.0
        assert t["officer_title"] == "EVP, General Counsel"
        assert t["transaction_code"] == "P"
        assert t["high_conviction"] is True   # officer + P + $50K > $10K
