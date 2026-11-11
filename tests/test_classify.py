import time

from argus import classify, db


def _act(app, title, cat="browse", realm="unknown"):
    now = time.time()
    db.add_activity(app, app.lower() + ".exe", title, cat, realm, now - 60, now, False, False)


def test_builtin_classification():
    assert classify.classify("Code.exe", "main.py - argus - Visual Studio Code")[:2] == ("VS Code", "code")
    assert classify.classify("chrome.exe", "Some article - Google Chrome")[1] == "browse"
    assert classify.classify("chrome.exe", "youtube.com - watch")[1] == "media"
    assert classify.classify("WindowsTerminal.exe", "deploy@build-box: ~")[1] == "remote"


def test_remote_counts_as_productive():
    assert "remote" in classify.PRODUCTIVE
    assert "remote" in classify.CATEGORY_ORDER


def test_rules_taught_elsewhere_reach_this_process_without_restart(monkeypatch):
    monkeypatch.setattr(classify, "RECHECK", 0.0)
    classify.classify("chrome.exe", "x")                 # warm the cache
    # what `argus teach` does from another process: write the memory row only
    db.remember("classify", "code|work", key="northwind", weight=2.0)
    app, cat, realm = classify.classify("chrome.exe", "Northwind admin - Chrome")
    assert (cat, realm) == ("code", "work")


def test_teach_relabels_past_sessions():
    _act("Chrome", "Nimbus dashboard - Google Chrome")
    _act("Chrome", "Something else")
    n = classify.teach("nimbus", "code", "personal")
    assert n == 1
    r = db.q1("SELECT category, realm FROM activity WHERE title LIKE 'Nimbus%'")
    assert (r["category"], r["realm"]) == ("code", "personal")
    other = db.q1("SELECT category FROM activity WHERE title='Something else'")
    assert other["category"] == "browse"


def test_teach_treats_wildcards_literally():
    _act("Chrome", "100% done")
    _act("Chrome", "1000 done")
    assert classify.teach("100%", "docs") == 1


def test_teach_realm_only_keeps_category():
    _act("Slack", "acme-general", cat="comms")
    classify.teach("acme", realm="work")
    r = db.q1("SELECT category, realm FROM activity")
    assert (r["category"], r["realm"]) == ("comms", "work")


import pytest  # noqa: E402


@pytest.mark.parametrize("title,cat", [
    ("Watch later - YouTube - Google Chrome", "media"),
    ("YouTube - Google Chrome", "media"),
    ("(2) WhatsApp - Google Chrome", "comms"),
    ("Acme Mattermost - Google Chrome", "comms"),
    ("Inbox (3) - me@gmail.com - Gmail - Google Chrome", "comms"),
    ("Pull requests · northwind/platform - Google Chrome", "code"),
    ("acme-corp/atlas: self-hosted memory - Google Chrome", "code"),
    ("Meet - uhh-axcn-uri - Google Chrome", "meeting"),
    ("Notification Architecture Strategy - Claude - Google Chrome", "ai"),
    ("Budget 2026 - Google Sheets - Google Chrome", "docs"),
    ("How to render a list - Stack Overflow - Personal - Microsoft​ Edge", "code"),
    ("Meeting notes about youtube strategy - Notion - Google Chrome", "docs"),
    # things that must stay neutral
    ("New Tab - Google Chrome", "browse"),
    ("Northwind CRM - Google Chrome", "browse"),
    ("The render loop explained - Google Chrome", "browse"),
    ("Why teams meet on zoom less - A very long article title here - Google Chrome", "browse"),
])
def test_browser_titles_by_site_name(title, cat):
    assert classify.category_from_title(title, "browse") == cat


@pytest.mark.parametrize("exe,cat", [
    ("claude.exe", "ai"), ("mstsc.exe", "remote"), ("stremio-shell-ng.exe", "media"),
    ("notepad++.exe", "code"), ("Notepad.exe", "docs"), ("dbeaver.exe", "code"),
])
def test_apps_that_used_to_be_other(exe, cat):
    assert classify.app_of(exe)[1] == cat


def test_reclassify_upgrades_stored_rows_once():
    import time as _t
    now = _t.time()
    db.add_activity("Chrome", "chrome.exe", "Watch later - YouTube - Google Chrome",
                    "browse", "unknown", now - 60, now, False, False)
    db.add_activity("Claude", "claude.exe", "Claude", "other", "unknown", now - 60, now,
                    False, False)
    db.add_activity("Chrome", "<inferred:browser>", "YouTube", "browse", "unknown",
                    now - 60, now, False, False)
    db.set_state("classifier_version", 1)
    out = classify.upgrade_stored()
    assert "re-labelled 2 of 2" in out
    cats = {r["exe"]: r["category"] for r in db.q("SELECT exe, category FROM activity")}
    assert cats == {"chrome.exe": "media", "claude.exe": "ai", "<inferred:browser>": "browse"}
    assert "nothing to do" in classify.upgrade_stored()


@pytest.mark.parametrize("title, app, cat", [
    ("Sticky Notes", "Sticky Notes", "docs"),
    ("Calculator", "Calculator", "system"),
    ("IMG_2041.jpg – Photos", "Photos", "media"),
    ("Inbox - Mail", "Mail", "comms"),
    ("", "Windows App", "system"),
    ("Passwords - Settings", "Windows App", "system"),   # a private title names nothing
])
def test_store_apps_are_named_after_their_window(title, app, cat):
    from argus import classify
    got_app, got_cat, _ = classify.classify("ApplicationFrameHost.exe", title)
    assert (got_app, got_cat) == (app, cat)


def test_reclassify_renames_old_rows_too():
    import time

    from argus import classify, db
    now = time.time()
    rid = db.add_activity("Windows App", "ApplicationFrameHost.exe", "Sticky Notes",
                          "system", "unknown", now - 60, now, False, False)
    classify.reclassify()
    row = db.q1("SELECT app, category FROM activity WHERE id=?", (rid,))
    assert (row["app"], row["category"]) == ("Sticky Notes", "docs")


def test_the_bare_desktop_is_not_file_explorer():
    from argus import classify
    assert classify.classify("explorer.exe", "Program Manager")[0] == "Desktop"
    assert classify.classify("explorer.exe", "Downloads - File Explorer")[0] == "File Explorer"
