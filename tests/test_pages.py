from pathlib import Path


WEB = Path(__file__).parents[1] / "web"


def test_pages_assets_distinguish_live_states_without_sample_or_secret_fallbacks():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    styles = (WEB / "styles.css").read_text(encoding="utf-8")
    combined = f"{index}\n{script}\n{styles}"
    for state in ("loading", "structure-blocked", "live-blocked", "empty", "history-only"):
        assert state in combined
    assert "SAMPLE_ROWS" not in combined
    assert "fixture" not in script.lower()
    assert "localStorage" not in script
    # plan_tokens is a plan-keyword list (not an API secret); only actual
    # secret patterns must be absent.
    assert "api_key" not in script.lower()
    assert "apikey" not in script.lower()
    assert "authorization" not in script.lower()
    assert ".innerHTML" not in script
    assert "textContent" in script
    assert "min-height: 44px" in styles
    for control in (
        "provider-filter",
        "region-filter",
        "currency-filter",
        "billing-filter",
        "availability-filter",
        "outcome-filter",
        "sort-rules",
        "sort-add",
        "sort-clear",
    ):
        assert control in index
    assert 'fetch("data/price_history.json"' in script
    assert "Promise.all" not in script
    assert "history-load" in index and "history-prev" in index and "history-next" in index
    assert "HISTORY_PAGE_SIZE = 50" in script


def test_pages_table_has_specs_disk_and_route_columns():
    """The table must expose CPU cores, RAM, disk type+size, and route info,
    with Beijing-fast premium routes highlighted (user requirement 2026-08-10)."""
    index = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    styles = (WEB / "styles.css").read_text(encoding="utf-8")
    # New table headers.
    for header in ("CPU", "内存", "硬盘", "线路"):
        assert f"<th>{header}</th>" in index
    # Spec + disk helpers in script.
    assert "function specValue" in script
    assert "function diskType" in script
    assert "function diskLabel" in script
    assert "specs" in script  # row.specs is read
    assert "routeCell" in script
    # Beijing-fast route highlight list + class.
    assert "BEIJING_FAST_ROUTES" in script
    assert "cn2 gia" in script
    assert "as9929" in script
    assert "route-fast" in styles
    assert "route-badge" in styles


def test_pages_multi_level_sorting_like_excel():
    """Excel-style multi-key sorting: up to 4 keys, each with direction,
    later keys break ties only (user requirement 2026-08-10)."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "sortRules" in script
    assert "SORT_FIELDS" in script
    assert "compareRows" in script
    assert "rule.dir === \"asc\" ? cmp : -cmp" in script  # per-rule direction
    assert "sortRules.length >= 4" in script  # bounded at 4 keys
    # Sortable fields include specs (cpu/ram/storage) and disk type.
    assert '"cpu", "CPU 核数"' in script
    assert '"ram_gb", "内存"' in script
    assert '"storage_gb", "硬盘大小"' in script
    assert '"disk_type", "硬盘类型"' in script
    # UI wiring.
    assert "sort-add" in script
    assert "sort-clear" in script
    # GLM review fixes (2026-08-10): adding a key must re-render, and the
    # bare "cn2" route must not highlight (CN2 GT false positive).
    assert "renderSortRules();\n  render();  // re-sort immediately" in script
    assert '"cug",\n];' in script  # no bare "cn2" in the fast list
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert index.count("</section>") == index.count("<section")  # no orphan close
