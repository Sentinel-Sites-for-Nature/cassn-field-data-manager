import json
from subprocess import CompletedProcess

from cassn.gui import platform_links


def test_wi_links_open_the_uploads_tab_for_known_projects():
    projects = "https://app.wildlifeinsights.org/manage/organizations/2001427/projects"
    assert platform_links.wi_project_uploads_url("2010517") == f"{projects}/2010517/uploads"
    assert platform_links.wi_project_uploads_url("2010527") == f"{projects}/2010527/uploads"
    assert platform_links.wi_project_uploads_url("unknown") == "https://app.wildlifeinsights.org/manage"


def test_box_link_uses_this_events_folder_and_falls_back_to_project(tmp_path):
    folder = tmp_path / "event"
    journal = folder / platform_links.STATE_PATH
    journal.parent.mkdir(parents=True)
    assert platform_links.box_web_url(folder, "700") == "https://app.box.com/folder/700"
    journal.write_text(json.dumps({"folder_id": "555"}))
    assert platform_links.box_web_url(folder, "700") == "https://app.box.com/folder/555"
    journal.write_text('{"folder_id":"https://other.invalid"}')
    assert platform_links.box_web_url(folder, "700") == "https://app.box.com/folder/700"


def test_mac_web_links_prefer_chrome_then_the_default_browser(monkeypatch):
    address = "https://app.box.com/folder/555"
    opened = []
    results = iter((0, 1))
    def run(args, **kwargs):
        opened.append(args)
        return CompletedProcess(args, next(results))
    monkeypatch.setattr(platform_links.sys, "platform", "darwin")
    monkeypatch.setattr(platform_links.subprocess, "run", run)
    monkeypatch.setattr(platform_links.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    platform_links.open_platform_url(None, address)
    platform_links.open_platform_url(None, address)
    chrome = ["/usr/bin/open", "-b", "com.google.Chrome", address]
    assert opened == [chrome, chrome, address]
