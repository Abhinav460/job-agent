"""Conservative Greenhouse and Lever form support. Never clicks or presses keys.

Greenhouse uses #application_form; Lever requires one top-level form and only
receives name/email. Custom widgets, uploads on Lever, consent and screening
questions require human input.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from jobagent import safepaths
from jobagent.profile import ApplicantProfile

HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
LABELS = {
    "first name": "first_name", "last name": "last_name", "email": "email",
    "email address": "email", "phone": "phone", "phone number": "phone",
    "location": "location", "location (city)": "location", "country": "country",
    "linkedin profile": "linkedin", "linkedin": "linkedin", "website": "website",
    "resume": "resume", "resume/cv": "resume", "resume / cv": "resume",
}


class BrowserError(RuntimeError):
    pass


def adapter_for_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password):
            return None
        if parsed.hostname in HOSTS and re.fullmatch(r"/[A-Za-z0-9_-]+/jobs/[0-9]+/?", parsed.path):
            return "greenhouse"
        if parsed.hostname in LEVER_HOSTS and re.fullmatch(
                r"/[A-Za-z0-9_-]+/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/apply/?", parsed.path):
            return "lever"
        return None
    except ValueError:
        return None


def supported_url(url: str) -> bool:
    return adapter_for_url(url) is not None


def posting_target(url: str) -> tuple:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or 443, parsed.path


def normalized(label: str) -> str:
    return " ".join(label.lower().replace("*", "").replace("\u2731", "").strip().rstrip(":").split())


@dataclass(frozen=True, repr=False)
class FillAction:
    field_id: str
    kind: str
    value: str
    attribute: str = "id"

    def __repr__(self):
        return "FillAction(<private>)"


@dataclass(frozen=True)
class FillResult:
    status: str
    filled: int = 0
    skipped: int = 0
    reason: str = "human_review"


def plan_fields(fields: list[dict], profile: ApplicantProfile, pdf: Path):
    """Only exact labels map to private facts; duplicate/unknown fields are skipped."""
    identities = Counter(f.get("id", "") for f in fields)
    meanings = Counter(LABELS.get(normalized(f.get("label", ""))) for f in fields)
    actions, skipped = [], 0
    for field in fields:
        identifier = field.get("id", "")
        semantic = LABELS.get(normalized(field.get("label", "")))
        kind = field.get("type", "")
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", identifier)
                or identities[identifier] != 1 or semantic is None or meanings[semantic] != 1
                or field.get("disabled") or field.get("readonly")
                or not field.get("interactable", True)):
            skipped += 1
            continue
        if semantic == "resume":
            if kind == "file" and identifier == "resume" and not field.get("multiple"):
                actions.append(FillAction(identifier, "upload", str(pdf)))
            else:
                skipped += 1
            continue
        value = getattr(profile, semantic)
        if value is None:
            skipped += 1
            continue
        text = value.get_secret_value()
        if kind == "select":
            options = [o for o in field.get("options", [])
                       if o["label"].strip().casefold() == text.casefold() and not o.get("disabled")]
            if semantic == "country" and len(options) == 1 and options[0]["value"]:
                actions.append(FillAction(identifier, "select", options[0]["value"]))
            else:
                skipped += 1
        elif kind in ("text", "email", "tel", "url"):
            actions.append(FillAction(identifier, "fill", text))
        else:
            skipped += 1
    return actions, skipped


def plan_lever_fields(fields: list[dict], profile: ApplicantProfile):
    """Name/email only, with both unique labels and expected form field names."""
    names = Counter(f.get("name", "") for f in fields)
    labels = Counter(normalized(f.get("label", "")) for f in fields)
    actions, skipped = [], 0
    for field in fields:
        label = normalized(field.get("label", ""))
        name = field.get("name", "")
        if (labels[label] != 1 or names[name] != 1 or field.get("disabled")
                or field.get("readonly") or not field.get("interactable", True)):
            skipped += 1
            continue
        if label == "full name" and name in ("name", "fullName") and field.get("type") == "text":
            full_name = profile.first_name.get_secret_value() + " " + profile.last_name.get_secret_value()
            actions.append(FillAction(name, "fill", full_name, "name"))
        elif label == "email" and name == "email" and field.get("type") in ("text", "email"):
            actions.append(FillAction(name, "fill", profile.email.get_secret_value(), "name"))
        else:
            skipped += 1
    return actions, skipped


# Read metadata, never existing input values or page prose. IDs are validated
# before being used in selectors. Hidden file inputs are normal upload controls.
INSPECT_FIELDS = """(elements, site) => {
const interactive = e => {
    if (!e.getClientRects().length || e.closest('[aria-hidden="true"], [inert]')) return false;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.right <= 0 || r.bottom <= 0 ||
        r.left >= innerWidth || r.top >= innerHeight) return false;
    for (let parent = e; parent; parent = parent.parentElement) {
        const s = getComputedStyle(parent);
        if (s.display === 'none' || s.visibility !== 'visible' || Number(s.opacity) === 0 ||
            s.pointerEvents === 'none' || s.clipPath !== 'none' || s.clip !== 'auto') return false;
        if (parent !== e && /(hidden|clip|scroll|auto)/.test(s.overflow + s.overflowX + s.overflowY)) {
            const bounds = parent.getBoundingClientRect();
            if (r.left < bounds.left || r.right > bounds.right ||
                r.top < bounds.top || r.bottom > bounds.bottom) return false;
        }
    }
    const x = (Math.max(r.left, 0) + Math.min(r.right, innerWidth)) / 2;
    const y = (Math.max(r.top, 0) + Math.min(r.bottom, innerHeight)) / 2;
    const hit = document.elementFromPoint(x, y);
    return hit === e || e.contains(hit);
};
return elements.map(e => ({
    id: e.id, name: e.name, type: e.tagName === 'SELECT' ? 'select' : e.type,
    interactable: e.type === 'file' ? true : interactive(e),
    label: Array.from(e.labels || []).map(l => l.textContent).join(' ') || e.getAttribute('aria-label') ||
        (site === 'lever' ? e.closest('li.application-question')?.querySelector('.application-label')?.textContent : '') || '',
    disabled: e.disabled, readonly: e.readOnly, multiple: e.multiple,
    options: e.tagName === 'SELECT' ? Array.from(e.options).map(o =>
        ({label: o.label, value: o.value, disabled: o.disabled})) : []
}));
}"""


def blocked(page) -> bool:
    return bool(page.locator('input[type="password"]:visible, iframe[src*="recaptcha"]:visible, '
                             'iframe[src*="hcaptcha"]:visible, iframe[src*="challenges.cloudflare"]:visible').count())


def fill_page(page, profile: ApplicantProfile, pdf: Path, home: Path) -> FillResult:
    pdf = safepaths.resolve_within(pdf, [home / "output"])
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise BrowserError("generated resume PDF is unavailable")
    adapter = adapter_for_url(page.url)
    if adapter is None:
        return FillResult("blocked", reason="unsupported_url")
    target = posting_target(page.url)
    if blocked(page):
        return FillResult("blocked", reason="login_or_captcha")
    form = page.locator("form" if adapter == "lever" else "#application_form")
    if form.count() != 1:
        return FillResult("blocked", reason="unsupported_form")
    controls = form.locator("input, select, textarea")
    if adapter == "lever":
        fields = controls.evaluate_all(INSPECT_FIELDS, "lever")
        actions, skipped = plan_lever_fields(fields, profile)
    else:
        fields = controls.evaluate_all(INSPECT_FIELDS)
        actions, skipped = plan_fields(fields, profile, pdf)
    filled = 0
    for action in actions:
        # Revalidate between actions: a redirect/challenge ends all automation.
        if (not supported_url(page.url) or posting_target(page.url) != target or blocked(page)):
            return FillResult("blocked", filled, skipped, "navigation_or_challenge")
        control = form.locator(f'[{action.attribute}="{action.field_id}"]')
        if control.count() != 1:
            skipped += 1
            continue
        try:
            if action.kind == "fill":
                control.fill(action.value, timeout=3000)
            elif action.kind == "select":
                control.select_option(value=action.value, timeout=3000)
            elif action.kind == "upload":
                control.set_input_files(action.value, timeout=3000)
            filled += 1
        except Exception:
            # Playwright errors can include entered text and selectors.
            return FillResult("blocked", filled, skipped, "field_action_failed")
    if not supported_url(page.url) or posting_target(page.url) != target or blocked(page):
        return FillResult("blocked", filled, skipped, "navigation_or_challenge")
    return FillResult("review_needed", filled, skipped)


def prevent_automatic_writes(route):
    """Automation cannot send a form or trigger an application API write."""
    if route.request.method not in ("GET", "HEAD", "OPTIONS"):
        route.abort()
    else:
        route.continue_()


def run_headed(url: str, profile: ApplicantProfile, pdf: Path, settings, on_result,
               wait_for_review=None, playwright_factory=None) -> FillResult:
    if not supported_url(url):
        result = FillResult("blocked", reason="unsupported_url")
        on_result(result)
        return result
    if playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
            playwright_factory = sync_playwright
        except ImportError:
            raise BrowserError("install browser dependencies and Chromium before filling") from None
    if wait_for_review is None:
        wait_for_review = lambda: input("Review the browser. Press Enter here when finished to close it: ")
    directory = safepaths.ensure_private_dir(settings.browser_profile_dir, [settings.jobagent_home])
    try:
        with playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(directory), headless=False, accept_downloads=False, service_workers="block")
            try:
                context.route("**/*", prevent_automatic_writes)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    if not supported_url(page.url) or posting_target(page.url) != posting_target(url):
                        result = FillResult("blocked", reason="posting_changed")
                    else:
                        result = fill_page(page, profile, pdf, settings.jobagent_home)
                except Exception:
                    result = FillResult("blocked", reason="browser_or_navigation_failed")
                on_result(result)
                context.unroute("**/*", prevent_automatic_writes)
                # This wait is inside the browser lifetime, including blocked
                # pages, so a human can inspect and complete the form themselves.
                wait_for_review()
                return result
            finally:
                context.close()
    except (EOFError, KeyboardInterrupt):
        raise BrowserError("browser review ended; application remains unsubmitted by the agent") from None
    except BrowserError:
        raise
    except Exception:
        raise BrowserError("browser unavailable; local application remains saved") from None
