"""PII layers 2/3: content-based masking of result cells and final output."""

from data_agent import pii


def test_email_masked_in_rows():
    rows = [{"user_id": 7, "contact": "maria.r@example.com", "spend": 812.5}]
    masked, hits = pii.mask_result_rows(rows, touches_pii_table=False)
    assert hits == 1
    assert masked[0]["contact"] == "<EMAIL_1>"
    assert masked[0]["user_id"] == 7 and masked[0]["spend"] == 812.5


def test_placeholders_consistent_within_result_set():
    rows = [
        {"a": "bob@x.com", "b": "sue@y.com"},
        {"a": "bob@x.com", "b": "third@z.org"},
    ]
    masked, hits = pii.mask_result_rows(rows, touches_pii_table=False)
    assert masked[0]["a"] == masked[1]["a"] == "<EMAIL_1>"
    assert masked[0]["b"] == "<EMAIL_2>"
    assert masked[1]["b"] == "<EMAIL_3>"
    assert hits == 4


def test_person_masked_only_when_strict():
    rows = [{"name": "Maria Rodriguez"}]
    lax, _ = pii.mask_result_rows(rows, touches_pii_table=False)
    assert lax[0]["name"] == "Maria Rodriguez"  # brand-name-like values survive
    strict, hits = pii.mask_result_rows(rows, touches_pii_table=True)
    if pii._analyzer() is not None:  # NER available
        assert strict[0]["name"] == "<PERSON_1>"
        assert hits == 1


def test_phone_masked():
    text = "call +1 415 555 0199 today"
    cleaned, hits = pii.scan_output(text)
    assert hits == 1 and "<PHONE" in cleaned


def test_output_scan_catches_email_in_prose():
    cleaned, hits = pii.scan_output("Top customer is reachable at boss@corp.io.")
    assert hits == 1 and "boss@corp.io" not in cleaned


def test_clean_text_untouched():
    text = "Monthly revenue grew 4.2% to $1.2M; Jordan brand led volume."
    cleaned, hits = pii.scan_output(text, strict_person=False)
    assert hits == 0 and cleaned == text


def test_regex_fallback_covers_emails(monkeypatch=None):
    spans = pii._detect("write to a.b@c.de now", include_person=False)
    assert any(kind.startswith("EMAIL") for _, _, kind in spans)


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
