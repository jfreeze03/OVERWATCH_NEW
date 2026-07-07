from app.logic.scoring import platform_score


def test_clean_signals_score_100():
    result = platform_score({})
    assert result.score == 100
    assert result.state == "Healthy"
    assert result.drivers == ()


def test_every_penalty_names_its_evidence():
    result = platform_score({
        "budget_pct": 120,
        "critical_alerts": 2,
        "high_alerts": 4,
        "query_fail_pct": 6,
        "task_fail_pct": 5,
        "queue_minutes": 40,
        "spill_gb": 12,
        "stale_sources": 2,
        "open_high_actions": 3,
    })
    assert result.score < 60
    assert all(d.evidence for d in result.drivers)
    # ranked by penalty descending
    penalties = [d.penalty for d in result.drivers]
    assert penalties == sorted(penalties, reverse=True)


def test_score_floor_zero():
    result = platform_score({
        "budget_pct": 500, "critical_alerts": 50, "high_alerts": 50,
        "query_fail_pct": 100, "task_fail_pct": 100, "queue_minutes": 10000,
        "spill_gb": 1000, "stale_sources": 20, "open_high_actions": 100,
    })
    assert result.score >= 0
    assert result.state == "At risk"


def test_single_driver_attribution():
    result = platform_score({"critical_alerts": 1})
    assert result.score == 94
    assert result.drivers[0].driver == "Critical alerts"
