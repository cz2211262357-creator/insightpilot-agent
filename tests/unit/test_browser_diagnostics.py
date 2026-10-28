from tests.browser.check_workbench import browser_failure_summary


def test_browser_failure_summary_reports_only_safe_diagnostics():
    log = (
        "Error: browserType.launch: Target page, context or browser has been closed\n"
        "secret-api-key-and-sensitive-data\n"
        "    at /home/runner/project/tests/browser/workbench.mjs:7:30\n"
    )
    summary = browser_failure_summary(log)
    assert "browser_launch_closed" in summary
    assert "workbench.mjs:7:30" in summary
    assert "secret" not in summary
    assert "/home/runner" not in summary


def test_browser_failure_summary_handles_timeout_without_echoing_selector():
    summary = browser_failure_summary(
        "TimeoutError: locator.waitFor: Timeout 20000ms exceeded.\nsecret-selector"
    )
    assert "timeout" in summary
    assert "secret" not in summary
