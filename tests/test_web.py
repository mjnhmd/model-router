from pathlib import Path


WEB = Path(__file__).parents[1] / "web" / "index.html"
CONSOLE = Path(__file__).parents[1] / "web" / "console.js"


def test_dashboard_exposes_all_models_as_multi_select_model_plaza():
    html = WEB.read_text(encoding="utf-8")
    assert "模型广场" in html
    assert "model-grid" in html
    assert "type=\"checkbox\"" in html
    assert "model-rows" not in html
    assert "add-model" not in html
    assert "模型标识（上游真实名称）" not in html
    assert "switchView('dashboard')" in html
    assert "保存中…" in html
    assert "model-filter" in html
    assert "选择当前显示" in html
    assert "select-visible" in html
    assert "indeterminate" in html
    assert "select-filtered" not in html
    assert "clear-filtered" not in html
    assert "全选全部" not in html
    assert "全不选" not in html
    assert "全选本服务" in html
    assert "view-mode" not in html
    assert "卡片模式" not in html
    assert "classList.add('model-list')" in html
    assert "routing-policy" not in html
    assert "优先级优先" not in html
    assert "sort-mode" in html
    assert "price_input" in html
    assert "status-stale" in html
    assert "aria-live=\"polite\"" in html
    assert "min-width:0" in html
    assert "AbortController" in html
    assert "保留当前选择" in html
    assert ".model-grid.model-list" in html
    assert "grid-template-columns:1fr" in html
    assert "立即测速" in html
    assert "测速中" in html
    assert "停止测速" in html
    assert "bench-cancel" in html
    assert "bench-interval" in html
    assert "stick-session" in html
    assert "同一会话固定模型" in html
    assert "Math.round(interval*60)" in html
    assert "completed" in html
    assert "当前已选模型" in html
    assert "请求日志" in html
    assert "统计看板" in html
    assert "/v1/logs" in html
    assert "/v1/stats" in html
    assert "dataTransfer.setData" in html
    assert "drop-target" in html
    assert "拖到这里调整优先级" in html
    assert "默认：已勾选优先" in html
    assert "sort-mode" in html
    assert "price_input" in html


def test_workbench_has_single_model_list_and_mode_panels():
    html = WEB.read_text(encoding="utf-8")
    assert html.count('id="model-grid"') == 1
    assert 'id="fastest-panel"' in html
    assert 'id="mapping-panel"' in html
    assert '加入自动择快池' in html
    assert '暴露给 Codex' in html
    assert 'Codex 接入' in html


def test_workbench_exposes_benchmark_freshness_and_tri_state_controls():
    html = WEB.read_text(encoding="utf-8")
    assert 'data-bench' in html
    assert 'indeterminate' in html
    assert '测速过期' in html


def test_status_refresh_tolerates_removed_legacy_dashboard_counters():
    js = CONSOLE.read_text(encoding="utf-8")
    assert "function setText(id,value)" in js
    for element_id in ("current", "pool-count", "healthy-count", "public-model"):
        assert f"setText('{element_id}'" in js


def test_speed_sort_puts_measured_models_before_unmeasured_defaults():
    js = CONSOLE.read_text(encoding="utf-8")
    assert "const aBenchmarked=Number(a.last_update)>0" in js
    assert "if(aBenchmarked!==bBenchmarked)" in js
    assert "measured?displayNumber(m.tps,'')" in js
