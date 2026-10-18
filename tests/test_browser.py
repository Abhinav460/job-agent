from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess

import pytest

from jobagent.browser import (BrowserError, FillResult, INSPECT_FIELDS, fill_page, plan_fields,
                              plan_lever_fields, prevent_automatic_writes, run_headed, supported_url)
from jobagent.profile import ApplicantProfile, ProfileError, load_profile
from jobagent.safepaths import PathOutsideWorkingDirs

URL = "https://boards.greenhouse.io/fakecompany/jobs/123"
LEVER_URL = "https://jobs.lever.co/fakecompany/00000000-0000-4000-8000-000000000001/apply"


@pytest.fixture
def private(tmp_path):
    home = tmp_path / "home"
    pdf = home / "output" / "resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"fake PDF")
    profile = ApplicantProfile(first_name="Fake", last_name="Applicant",
                               email="fake@example.invalid", country="Canada")
    return home, pdf, profile


def field(identifier, label, kind="text", **kwargs):
    return {"id": identifier, "label": label, "type": kind, **kwargs}


FIELDS = [field("first_name", "First Name *"), field("last_name", "Last Name *"),
          field("email", "Email *", "email"), field("resume", "Resume/CV *", "file"),
          field("country", "Country", "select", options=[{"label": "Canada", "value": "CA"}]),
          field("salary", "Desired salary"), field("sponsor", "Require sponsorship?", "checkbox"),
          field("submit_app", "Submit application", "submit")]


class FakeControl:
    def __init__(self, page, identifier):
        self.page, self.identifier = page, identifier
    def count(self): return 1
    def fill(self, value, **kwargs): self.page.actions.append(("fill", self.identifier, value))
    def select_option(self, value, **kwargs): self.page.actions.append(("select", self.identifier, value))
    def set_input_files(self, value, **kwargs): self.page.actions.append(("upload", self.identifier, value))
    def click(self, *args, **kwargs): raise AssertionError("no click is allowed")
    def press(self, *args, **kwargs): raise AssertionError("no keypress is allowed")


class FakeForm:
    def __init__(self, page): self.page = page
    def count(self): return self.page.forms
    def locator(self, selector):
        if selector == "input, select, textarea":
            return SimpleNamespace(evaluate_all=lambda script: self.page.fields)
        return FakeControl(self.page, selector.split('"')[1])


class FakePage:
    def __init__(self, fields=None):
        self.url, self.fields, self.actions = URL, fields or FIELDS, []
        self.forms, self.challenge = 1, False
    def locator(self, selector):
        if selector == "#application_form": return FakeForm(self)
        return SimpleNamespace(count=lambda: int(self.challenge))
    def goto(self, url, **kwargs): self.url = url


LEVER_FIELDS = [field("", "Full name✱", name="name"), field("", "Email✱", "email", name="email"),
                field("resume-upload", "Resume/CV✱", "file", name="resume"),
                field("", "Phone", "tel", name="phone"), field("", "Gender", "select", name="gender"),
                field("", "I agree", "checkbox", name="consent"),
                field("", "Submit application", "submit", name="submit")]


class FakeLeverForm(FakeForm):
    def locator(self, selector):
        if selector == "input, select, textarea":
            def inspect(script, site):
                assert site == "lever"
                return self.page.fields
            return SimpleNamespace(evaluate_all=inspect)
        return super().locator(selector)


class FakeLeverPage(FakePage):
    def __init__(self, fields=None):
        super().__init__(fields or LEVER_FIELDS)
        self.url = LEVER_URL
    def locator(self, selector):
        if selector == "form": return FakeLeverForm(self)
        return SimpleNamespace(count=lambda: int(self.challenge))


@pytest.mark.parametrize("host", ["jobs.lever.co", "jobs.eu.lever.co"])
def test_lever_fills_only_name_and_email(private, host):
    home, pdf, profile = private
    page = FakeLeverPage()
    page.url = LEVER_URL.replace("jobs.lever.co", host)
    assert supported_url(page.url)
    assert fill_page(page, profile, pdf, home) == FillResult("review_needed", 2, 5)
    assert page.actions == [("fill", "name", "Fake Applicant"), ("fill", "email", "fake@example.invalid")]


def test_lever_documented_full_name_field_name(private):
    home, pdf, profile = private
    page = FakeLeverPage([field("", "Full name\u2731", name="fullName")])
    assert fill_page(page, profile, pdf, home) == FillResult("review_needed", 1, 0)
    assert page.actions == [("fill", "fullName", "Fake Applicant")]


@pytest.mark.parametrize("fields", [
    [field("", "Full name", name="name"), field("", "Full name", name="custom_name")],
    [field("", "Full name", name="name"), field("", "Other question", name="name")],
    [field("", "Full name", name="name", interactable=False)],
    [field("", "Email", name="email", interactable=False)],
    [field("", "Email", "hidden", name="email")],
    [field("", "Full name", name="name", readonly=True)],
    [field("", "Email", name="email", disabled=True)],
    [field("", "Full name", name="custom")],
])
def test_lever_ambiguous_hidden_or_custom_fields_skip(private, fields):
    _, _, profile = private
    assert plan_lever_fields(fields, profile) == ([], len(fields))


@pytest.mark.parametrize("url", [
    LEVER_URL.replace("https://", "http://"), LEVER_URL.replace("jobs.lever.co", "jobs.lever.co.evil.invalid"),
    LEVER_URL.replace("jobs.lever.co", "fake-user" + "@" + "jobs.lever.co"),
    LEVER_URL.removesuffix("/apply"), LEVER_URL + "/submit", LEVER_URL.replace("00000000-", "bad-"),
    LEVER_URL.replace("jobs.lever.co", "jobs.lever.co:444"),
])
def test_lever_unsupported_urls_block(private, url):
    home, pdf, profile = private
    page = FakeLeverPage()
    page.url = url
    assert not supported_url(url)
    assert fill_page(page, profile, pdf, home).status == "blocked"
    assert page.actions == []


def test_lever_multiple_forms_and_challenges_block(private):
    home, pdf, profile = private
    page = FakeLeverPage()
    page.forms = 2
    assert fill_page(page, profile, pdf, home).reason == "unsupported_form"
    page.forms, page.challenge = 1, True
    assert fill_page(page, profile, pdf, home).reason == "login_or_captcha"
    assert page.actions == []


@pytest.mark.parametrize("redirect_on", ["name", "email"])
@pytest.mark.parametrize("redirected", [LEVER_URL.replace("jobs.lever.co", "jobs.eu.lever.co"),
                                      LEVER_URL.replace("000000000001", "000000000002")])
def test_lever_navigation_during_action_blocks(private, redirect_on, redirected):
    home, pdf, profile = private
    page = FakeLeverPage()
    class RedirectControl(FakeControl):
        def fill(self, value, **kwargs):
            super().fill(value, **kwargs)
            if self.identifier == redirect_on:
                self.page.url = redirected
    class Form(FakeLeverForm):
        def locator(self, selector):
            if selector == "input, select, textarea": return super().locator(selector)
            return RedirectControl(self.page, selector.split('"')[1])
    page.locator = lambda selector: Form(page) if selector == "form" else SimpleNamespace(count=lambda: 0)
    result = fill_page(page, profile, pdf, home)
    assert result.status == "blocked"
    assert result.filled == (1 if redirect_on == "name" else 2)


def test_known_fields_and_upload_only(private):
    home, pdf, profile = private
    page = FakePage()
    result = fill_page(page, profile, pdf, home)
    assert result == FillResult("review_needed", filled=5, skipped=3)
    assert page.actions == [("fill", "first_name", "Fake"), ("fill", "last_name", "Applicant"),
                            ("fill", "email", "fake@example.invalid"),
                            ("upload", "resume", str(pdf.resolve())), ("select", "country", "CA")]


def test_ambiguous_missing_and_unknown_fields_skip(private):
    _, pdf, profile = private
    fields = [field("first", "First Name"), field("another", "First Name"),
              field("phone", "Phone"), field("gender", "Gender", "select"),
              field("resume_extra", "Resume", "file"), field("agree", "I agree", "checkbox"),
              field("duplicate", "Last Name"), field("duplicate", "Email"),
              field('unsafe"]', "Website")]
    actions, skipped = plan_fields(fields, profile, pdf)
    assert actions == [] and skipped == len(fields)
    fields = [field("country", "Country", "select", options=[
        {"label": "Canada", "value": "CA"}, {"label": "Canada", "value": "other"}])]
    assert plan_fields(fields, profile, pdf) == ([], 1)


def test_noninteractive_known_label_is_skipped(private):
    _, pdf, profile = private
    assert plan_fields([field("first_name", "First Name", interactable=False)], profile, pdf) == ([], 1)


def test_inspection_rejects_hidden_offscreen_and_clipped_controls():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required to exercise DOM inspection against synthetic elements")
    script = "const inspect = " + INSPECT_FIELDS + ";\n" + r'''
const defaults = {display:'block', visibility:'visible', opacity:'1', pointerEvents:'auto',
                  clipPath:'none', clip:'auto', overflow:'visible', overflowX:'visible', overflowY:'visible'};
global.innerWidth = 1000; global.innerHeight = 1000;
global.getComputedStyle = e => ({...defaults, ...e.style});
let element;
global.document = {elementFromPoint: () => element.covered ? {} : element};
function setup() {
    const parent = {style:{}, parentElement:null,
        getBoundingClientRect: () => ({left:0, top:0, right:1000, bottom:1000})};
    element = {id:'first_name', tagName:'INPUT', type:'text', style:{}, parentElement:parent,
        labels:[{textContent:'First Name'}], getAttribute:()=>null, contains:()=>false,
        closest:()=>element.ariaHidden ? {} : null, getClientRects:()=>[{}],
        getBoundingClientRect:()=>({left:10, top:10, right:110, bottom:30, width:100, height:20})};
}
const cases = [
    () => {},
    () => {element.style.opacity='0'},
    () => {element.parentElement.style.opacity='0'},
    () => {element.style.visibility='hidden'},
    () => {element.ariaHidden=true},
    () => {element.style.pointerEvents='none'},
    () => {element.style.clipPath='inset(100%)'},
    () => {element.style.clip='rect(0px, 0px, 0px, 0px)'},
    () => {element.covered=true},
    () => {element.getBoundingClientRect=()=>({left:2000, top:10, right:2100, bottom:30, width:100, height:20})},
    () => {element.parentElement.style.overflow='hidden';
        element.parentElement.getBoundingClientRect=()=>({left:0, top:0, right:5, bottom:5})}
];
const results = cases.map(change => {setup(); change(); return inspect([element])[0].interactable});
setup(); element.labels=[]; element.name='name';
element.closest=selector => selector === 'li.application-question' ?
    {querySelector:()=>({textContent:'Full name\u2731'})} : null;
process.stdout.write(JSON.stringify({visibility:results, lever:inspect([element], 'lever')[0],
                                  greenhouse:inspect([element])[0]}));
'''
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, check=True)
    metadata = json.loads(result.stdout)
    assert metadata["visibility"] == [True] + [False] * 10
    assert metadata["lever"]["label"] == "Full name\u2731"
    assert metadata["lever"]["name"] == "name" and metadata["lever"]["interactable"]
    assert metadata["greenhouse"]["label"] == ""


@pytest.mark.parametrize("url", ["http://boards.greenhouse.io/fake/jobs/1",
    "https://boards.greenhouse.io.evil.invalid/fake/jobs/1", "file:///resume.pdf",
    "https://fake:password" + "@" + "boards.greenhouse.io/fake/jobs/1",
    "https://boards.greenhouse.io/fake/jobs/1/apply", "https://boards.greenhouse.io/fake/jobs/1:bad"])
def test_unsupported_urls_do_not_fill(private, url):
    home, pdf, profile = private
    page = FakePage()
    page.url = url
    assert not supported_url(url)
    assert fill_page(page, profile, pdf, home).status == "blocked"
    assert page.actions == []


def test_challenge_and_unsupported_form_block(private):
    home, pdf, profile = private
    page = FakePage()
    page.challenge = True
    assert fill_page(page, profile, pdf, home).reason == "login_or_captcha"
    page.challenge, page.forms = False, 0
    assert fill_page(page, profile, pdf, home).reason == "unsupported_form"
    assert page.actions == []


def test_outside_upload_is_rejected(private, tmp_path):
    home, _, profile = private
    with pytest.raises(PathOutsideWorkingDirs):
        fill_page(FakePage(), profile, tmp_path / "resume.pdf", home)


def test_navigation_between_actions_stops(private):
    home, pdf, profile = private
    page = FakePage()
    class RedirectControl(FakeControl):
        def fill(self, value, **kwargs):
            super().fill(value, **kwargs)
            self.page.url = URL.replace("123", "456")
    original = FakeForm.locator
    class Form(FakeForm):
        def locator(self, selector):
            if selector == "input, select, textarea": return original(self, selector)
            return RedirectControl(self.page, selector.split('"')[1])
    page.locator = lambda selector: Form(page) if selector == "#application_form" else SimpleNamespace(count=lambda: 0)
    result = fill_page(page, profile, pdf, home)
    assert result.status == "blocked" and result.filled == 1


def test_profile_validation_and_errors_are_private(private):
    home, _, profile = private
    assert "fake@example.invalid" not in repr(profile)
    path = home / "profile.yaml"
    path.write_text("first_name: Fake\nlast_name: Applicant\nemail: fake@example.invalid\n", encoding="utf-8")
    assert load_profile(path, home).first_name.get_secret_value() == "Fake"
    for source in ("email: private-bad-value", "first_name: [private: invalid", "extra: private-answer"):
        path.write_text(source, encoding="utf-8")
        with pytest.raises(ProfileError) as error:
            load_profile(path, home)
        assert "private-answer" not in str(error.value)
        assert "private-bad-value" not in str(error.value)


def test_network_guard_blocks_writes():
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        calls = []
        route = SimpleNamespace(request=SimpleNamespace(method=method),
                                abort=lambda: calls.append("abort"), continue_=lambda: calls.append("continue"))
        prevent_automatic_writes(route)
        assert calls == (["continue"] if method in ("GET", "HEAD", "OPTIONS") else ["abort"])


@pytest.mark.parametrize("url, page_type", [(URL, FakePage), (LEVER_URL, FakeLeverPage)])
def test_headed_context_survives_until_human_review(private, url, page_type):
    home, pdf, profile = private
    events = []
    page = page_type()
    class Context:
        def route(self, pattern, handler): events.append("guard_on")
        def unroute(self, pattern, handler): events.append("guard_off")
        def new_page(self): return page
        def close(self): events.append("close")
    class Chromium:
        def launch_persistent_context(self, directory, **kwargs):
            assert kwargs == {"headless": False, "accept_downloads": False, "service_workers": "block"}
            assert Path(directory).is_relative_to(home)
            events.append("open")
            return Context()
    class Playwright:
        chromium = Chromium()
        def __enter__(self): return self
        def __exit__(self, *args): events.append("exit")
    def review():
        assert "close" not in events
        events.append("human_review")
    settings = SimpleNamespace(jobagent_home=home, browser_profile_dir=home / "browser_profile")
    result = run_headed(url, profile, pdf, settings, lambda result: events.append(result.status),
                        wait_for_review=review, playwright_factory=Playwright)
    assert result.status == "review_needed"
    assert events == ["open", "guard_on", "review_needed", "guard_off", "human_review", "close", "exit"]


@pytest.mark.parametrize("requested, redirected", [
    (URL, URL.replace("boards.greenhouse.io", "job-boards.greenhouse.io")),
    (LEVER_URL, LEVER_URL.replace("jobs.lever.co", "jobs.eu.lever.co")),
    (LEVER_URL, LEVER_URL.replace("000000000001", "000000000002")),
])
def test_headed_rejects_redirected_target(private, requested, redirected):
    home, pdf, profile = private
    page = FakePage()
    page.goto = lambda url, **kwargs: setattr(page, "url", redirected)
    results = []
    class Context:
        def route(self, *args): pass
        def unroute(self, *args): pass
        def new_page(self): return page
        def close(self): pass
    class Playwright:
        chromium = SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: Context())
        def __enter__(self): return self
        def __exit__(self, *args): pass
    settings = SimpleNamespace(jobagent_home=home, browser_profile_dir=home / "browser_profile")
    result = run_headed(requested, profile, pdf, settings, results.append,
                        wait_for_review=lambda: None, playwright_factory=Playwright)
    assert result.status == "blocked" and result.reason == "posting_changed"
    assert page.actions == [] and results == [result]
