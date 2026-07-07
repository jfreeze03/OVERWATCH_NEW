from app.ui.status_colors import status_columns_in, status_css


def test_severity_colors():
    assert "background-color" in status_css("SEVERITY", "CRITICAL")
    assert status_css("SEVERITY", "critical") == status_css("SEVERITY", "CRITICAL")
    assert status_css("SEVERITY", None) == ""
    assert status_css("SEVERITY", "WEIRD") == ""


def test_true_is_good_inversion():
    # SLA_MET True should be green (good), plain booleans amber (attention).
    assert status_css("SLA_MET", True) != status_css("GOT_WORSE", True)
    assert "14532d" in status_css("SLA_MET", "True")   # green bg
    assert "7f1d1d" in status_css("SLA_MET", "False")  # red bg


def test_verdicts():
    assert "14532d" in status_css("VERDICT", "Better")
    assert "7f1d1d" in status_css("VERDICT", "Worse")


def test_column_detection_preserves_original_names():
    cols = ["Severity", "TITLE", "sla_met", "OWNER"]
    assert status_columns_in(cols) == ["Severity", "sla_met"]
