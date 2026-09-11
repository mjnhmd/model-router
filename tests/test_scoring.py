from model_router.scoring import ModelScore


def test_ewma_improves_with_fast_observations():
    s = ModelScore("a/b", default_tps=10.0)
    s.observe(100.0, 1.0)
    assert s.tps > 10.0
    s.observe(200.0, 1.0)
    assert s.tps > 100.0


def test_failure_penalizes():
    s = ModelScore("a/b", default_tps=50.0)
    s.observe(0.0, 5.0)
    assert s.tps < 50.0
    assert not s.healthy
