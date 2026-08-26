"""Guard test: the site is one product, and stays one product.

Before the shell existed there were five different hand-maintained copies of the
navigation across twelve pages, and four pages carried no navigation at all — a
reader who landed on the eval registry or the erasure observatory could only
leave with the back button. Nothing caught that, because nothing could: every
page owned its own nav, so drift was invisible until someone looked at all
twelve side by side.

This file is what makes that class of drift fail the build instead:

  * every page listed in scripts/sync_nav.py carries the canonical nav, byte for
    byte, and any page that has drifted is named;
  * every page that carries the nav also loads the stylesheet that styles it and
    the script that opens it, in the right order, with the body class the layer
    system needs;
  * no page has quietly grown a private copy of the nav CSS again;
  * the anchors the navigation and home page link into actually exist in the
    pages they point at, so a nav link cannot rot into a scroll-to-top;
  * the DDTI_EMBED marker that inject_ddti.py rewrites every three hours is
    still present and still matches inject_ddti's own expectation.

That last one is the highest-consequence check here. The marker is how live data
reaches the project's flagship signal, and a well-meaning HTML edit that renamed
or reformatted it would break the pipeline silently: the page would still render,
just frozen at whatever the data said the moment it broke. A frozen board that
looks live is worse than a board that is visibly down.

Standard library only, like the rest of tests/.
"""
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import site_nav  # noqa: E402
import sync_nav  # noqa: E402

# Pages a Python generator writes wholesale. Discovery also stamps their managed
# marker blocks, while this list separately verifies that future generator runs
# keep importing the canonical source instead of restoring private nav markup.
GENERATED = {
    "china-brief.html",
    "weekly-situation.html",
    "news/china/situation/index.html",
    "news/china/rumour/index.html",
    "readings/generative-firewall-index.html",
    "belt-and-road/index.html",
} | {
    str(path.relative_to(ROOT))
    for path in (ROOT / "journal").glob("**/index.html")
}

NAV_BLOCK = re.compile(
    re.escape(site_nav.BEGIN) + r".*?" + re.escape(site_nav.END), re.DOTALL
)

# A page carrying its own nav styling is the drift vector this whole file exists
# to close. The shell owns .pnav's replacement; nobody else may define it.
PRIVATE_NAV_CSS = re.compile(r"^\s*\.pnav[\s{,_]", re.MULTILINE)


def _pages():
    """(relative path, served path) for every page sync_nav is responsible for."""
    for rel, current in sync_nav.PAGES.items():
        path = ROOT / rel
        if path.exists():
            yield rel, current, path


def _tracked_html_pages() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.html"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {
        raw_relative_path.decode("utf-8")
        for raw_relative_path in result.stdout.split(b"\0")
        if raw_relative_path
    }


def test_nav_discovery_covers_every_tracked_managed_html_page():
    tracked = _tracked_html_pages()
    expected = tracked - sync_nav.EXCLUDED_HTML

    assert len(expected) > 2_000
    assert sync_nav.EXCLUDED_HTML <= tracked
    assert set(sync_nav.PAGES) == expected
    assert all(
        (ROOT / relative_path).read_text(encoding="utf-8").count(site_nav.BEGIN) == 1
        and (ROOT / relative_path).read_text(encoding="utf-8").count(site_nav.END) == 1
        for relative_path in expected
    )
    assert sync_nav._served_path("index.html") == "/"
    assert sync_nav._served_path("news/wire/page/2/index.html") == "/news/wire/page/2/"
    assert sync_nav._served_path("readings/inside-view.html") == "/readings/inside-view.html"

    for relative_path in sync_nav.EXCLUDED_HTML:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert site_nav.BEGIN not in text
        assert site_nav.END not in text


def test_nav_discovery_does_not_forget_a_page_when_both_markers_disappear(
    tmp_path, monkeypatch
):
    page = tmp_path / "managed.html"
    page.write_text("<!doctype html><main id=\"main\"></main>", encoding="utf-8")
    monkeypatch.setattr(sync_nav, "ROOT", tmp_path)

    assert sync_nav.discover_pages() == {"managed.html": "/managed.html"}
    changed, note = sync_nav.apply(page, "/managed.html")
    assert changed is False
    assert note == "INVALID MARKERS"


def test_every_managed_page_exists():
    """sync_nav's page list is the site map; a missing entry means a broken link."""
    missing = [rel for rel in sync_nav.PAGES if not (ROOT / rel).exists()]
    assert not missing, (
        "scripts/sync_nav.py lists pages that do not exist, so the nav links to "
        f"404s: {missing}"
    )


def test_nav_is_identical_everywhere():
    """No page may carry a nav that differs from the canonical render."""
    stale = []
    for rel, current, path in _pages():
        text = path.read_text(encoding="utf-8")
        found = NAV_BLOCK.search(text)
        if not found:
            stale.append(f"{rel}: no PS_NAV markers")
            continue
        if found.group(0) != site_nav.render(current):
            stale.append(f"{rel}: nav differs from canonical")
    assert not stale, (
        "navigation has drifted. Run: python3 scripts/sync_nav.py\n  "
        + "\n  ".join(stale)
    )


def test_pages_load_the_shell_they_depend_on():
    """Nav markup without shell.css is unstyled markup; without shell.js it does
    not open. Both must be present, and tikto.css must come first because the
    shell reads its tokens."""
    broken = []
    for rel, _current, path in _pages():
        text = path.read_text(encoding="utf-8")

        if "/assets/shell.css" not in text:
            broken.append(f"{rel}: missing shell.css")
        if "/assets/shell.js" not in text:
            broken.append(f"{rel}: missing shell.js")
        if "/dashboards/assets/tikto.css" not in text:
            broken.append(f"{rel}: missing tikto.css")

        # Order matters: shell.css consumes --tk-* tokens tikto.css defines.
        tikto = text.find("/dashboards/assets/tikto.css")
        shell = text.find("/assets/shell.css")
        if -1 not in (tikto, shell) and tikto > shell:
            broken.append(f"{rel}: tikto.css must load before shell.css")

        if not re.search(r"<body[^>]*\bclass=\"[^\"]*\bps\b", text):
            broken.append(f"{rel}: <body> is missing class=\"ps\"")

        if 'id="main"' not in text:
            broken.append(f"{rel}: no id=\"main\" for the skip link to target")

    assert not broken, "shell not applied consistently:\n  " + "\n  ".join(broken)


def test_no_page_redefines_the_nav_css():
    """The exact failure that produced five different navs. Never again."""
    offenders = []
    for rel, _current, path in _pages():
        if PRIVATE_NAV_CSS.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "these pages define their own nav CSS instead of using the shell, which "
        f"is how the navigation drifted apart the first time: {offenders}"
    )


def test_nav_targets_resolve():
    """Every internal nav link points at a file that exists, and every anchored
    link points at an id that is actually in that file."""
    dangling, missing_anchor = [], []

    def check(href):
        if href.startswith("http") or href.startswith("mailto:"):
            return
        path_part, _, frag = href.partition("#")
        rel = path_part.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = ROOT / rel
        if not target.exists():
            dangling.append(href)
            return
        if frag:
            text = target.read_text(encoding="utf-8")
            if f'id="{frag}"' not in text and f"id='{frag}'" not in text:
                missing_anchor.append(href)

    for item in site_nav.NAV:
        if "href" in item:
            check(item["href"])
        for col in item.get("columns", []):
            for entry in col["links"]:
                check(entry[0])

    assert not dangling, f"nav links to files that do not exist: {dangling}"
    assert not missing_anchor, (
        f"nav links to anchors that are not in the target page: {missing_anchor}"
    )


def test_ddti_embed_marker_survives():
    """The live-data seam. inject_ddti.py rewrites this block on a cron; if an
    HTML edit renames or reshapes it the injection silently stops landing and the
    flagship board freezes while still looking live."""
    sys.path.insert(0, str(ROOT))
    import inject_ddti  # noqa: E402

    for rel in inject_ddti.DASHBOARDS:
        path = ROOT / rel
        assert path.exists(), f"{rel} is gone but inject_ddti.py still targets it"
        text = path.read_text(encoding="utf-8")
        assert inject_ddti.EMBED_MARKER in text, (
            f"{rel} no longer contains {inject_ddti.EMBED_MARKER}. "
            "inject_ddti.py can no longer publish live DDTI data into this page."
        )


def test_generated_pages_use_the_shared_nav():
    """The generators must import site_nav rather than keeping their own copy,
    or the next cron run reverts the site to the old navigation."""
    for script, page in (
        ("scripts/build_bri_observatory.py", "belt-and-road/index.html"),
        ("scripts/build_china_brief.py", "china-brief.html"),
        ("scripts/build_china_situation.py", "news/china/situation/index.html"),
        ("scripts/generative_firewall_reading.py",
         "readings/generative-firewall-index.html"),
        ("scripts/build_eval_findings.py", "journal/index.html"),
        ("scripts/build_rumour_board.py", "news/china/rumour/index.html"),
    ):
        src = (ROOT / script).read_text(encoding="utf-8")
        assert "site_nav" in src, (
            f"{script} does not import site_nav, so the page it generates "
            f"({page}) will revert to a stale hand-maintained nav on the next run"
        )

    for page in GENERATED:
        path = ROOT / page
        if not path.exists():
            continue  # not yet regenerated; the generator check above is the gate
        text = path.read_text(encoding="utf-8")
        assert "ps-nav" in text, f"{page} was generated without the shared nav"


def test_skip_link_is_not_promoted_into_the_content_plane():
    """The generic body-plane selector must not override the skip link position."""
    css = (ROOT / "assets/shell.css").read_text(encoding="utf-8")

    assert "body.ps > *:not(.ps-nav):not(.ps-skip)" in css
    assert "body.ps > *:not(.ps-nav) { position: relative" not in css

    for stylesheet in ("assets/home.css", "assets/journal.css"):
        page_css = (ROOT / stylesheet).read_text(encoding="utf-8")
        assert "> * { position: relative; z-index: 1; }" not in page_css, (
            f"{stylesheet} promotes the nav into the content plane"
        )
        assert "> *:not(.ps-nav):not(.ps-skip)" in page_css


def test_wide_eval_menu_is_anchored_inside_the_desktop_viewport():
    css = (ROOT / "assets/shell.css").read_text(encoding="utf-8")

    assert ".ps-nav__item:nth-last-child(-n+3) .ps-flyout" in css
    assert "left: auto; right: 0" in css


def test_desktop_flyout_has_a_continuous_pointer_corridor():
    """Crossing the visual gap must not dismiss the menu under the pointer."""
    css = (ROOT / "assets/shell.css").read_text(encoding="utf-8")
    script = (ROOT / "assets/shell.js").read_text(encoding="utf-8")

    assert ".ps-flyout::before" in css
    assert "top: -8px; height: 8px" in css
    assert 'panel.addEventListener("pointerenter"' in script
    assert "clearTimeout(closeTimer);\n          open(item);" in script


def test_observatory_flyout_is_active_for_generated_china_routes():
    observatory = next(item for item in site_nav.NAV if item["label"] == "Observatory")

    assert site_nav._within(observatory, "/china/")
    assert site_nav._within(observatory, "/china/sources/nbs-national-data/")
    assert site_nav._within(observatory, "/dashboards/ddti_observatory.html")
    assert site_nav._within(observatory, "/news/china/situation/")
    assert (
        'href="/news/china/situation/" aria-current="page"'
        in site_nav.render("/news/china/situation/")
    )


def test_bri_regions_are_first_class_primary_navigation_destinations():
    regional = next(item for item in site_nav.NAV if item["label"] == "BRI regions")
    links = {
        title: href
        for column in regional["columns"]
        for href, title, *_rest in column["links"]
    }

    assert links == {
        "BRI & Corridors": "/belt-and-road/#bri-corridors",
        "Balochistan": "/belt-and-road/#balochistan",
        "Pakistan & Gwadar": "/belt-and-road/#pakistan-gwadar",
        "Myanmar": "/belt-and-road/#myanmar",
    }
    assert site_nav._within(regional, "/belt-and-road/")
    assert 'data-within=""' in site_nav.render("/belt-and-road/")


def test_no_javascript_navigation_exposes_flyouts_on_mobile_and_desktop():
    css = (ROOT / "assets/shell.css").read_text(encoding="utf-8")

    assert "@media (max-width: 940px) and (scripting: none)" in css
    assert "@media (min-width: 941px) and (scripting: none)" in css
    desktop = css.split(
        "@media (min-width: 941px) and (scripting: none)", 1
    )[1].split("/* ============================ SURFACES", 1)[0]
    assert "position: static" in desktop
    assert "opacity: 1" in desktop
    assert "visibility: visible" in desktop
    assert ".ps-nav__chev { display: none; }" in desktop


def test_mobile_menu_owns_focus_until_it_closes():
    """Pin the focus-entry, Tab containment and focus-return modal contract."""
    script = (ROOT / "assets/shell.js").read_text(encoding="utf-8")

    assert "function setMobileMenu(openNow, restoreFocus)" in script
    assert "if (firstLink) firstLink.focus();" in script
    assert 'if (e.key !== "Tab") return;' in script
    assert "last.focus();" in script
    assert "first.focus();" in script
    assert "burger.focus();" in script


def test_mobile_internal_navigation_closes_sheet_without_preventing_activation():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the shell interaction contract"
    harness = r'''
const fs = require("fs");
const vm = require("vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.listeners = {};
    this.parent = null;
    this.children = [];
    this.classList = { add() {} };
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  getAttribute(name) { return this.hasAttribute(name) ? this.attrs[name] : null; }
  addEventListener(kind, callback) { (this.listeners[kind] ||= []).push(callback); }
  emit(kind, event) { (this.listeners[kind] || []).forEach((callback) => callback(event)); }
  appendChild(child) { child.parent = this; this.children.push(child); }
  contains(node) {
    for (let current = node; current; current = current.parent) {
      if (current === this) return true;
    }
    return false;
  }
  closest(selector) {
    if (selector === "a[href]") {
      for (let current = this; current; current = current.parent) {
        if (current.tag === "a" && current.hasAttribute("href")) return current;
      }
    }
    return null;
  }
  focus() { document.activeElement = this; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

const body = new Element("body");
const nav = new Element("nav");
const menu = new Element("div");
const burger = new Element("button");
const scrim = new Element("div");
const link = new Element("a");
const nested = new Element("b");
link.setAttribute("href", "/belt-and-road/#myanmar");
link.appendChild(nested);
menu.appendChild(link);
nav.appendChild(menu);
nav.appendChild(burger);
nav.appendChild(scrim);
nav.querySelectorAll = () => [];
nav.querySelector = (selector) => ({
  ".ps-nav__burger": burger,
  ".ps-nav__items": menu,
  ".ps-nav__scrim": scrim
}[selector] || null);
menu.querySelectorAll = () => [link];
menu.querySelector = () => link;

global.document = {
  readyState: "complete",
  body,
  activeElement: null,
  hidden: false,
  title: "Shell interaction",
  documentElement: { classList: { add() {} } },
  head: { appendChild() {} },
  querySelector(selector) {
    if (selector === ".ps-nav") return nav;
    if (selector === "script[data-cf-beacon]") return new Element("script");
    return null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return new Element(tag); }
};
global.window = global;
window.location = {
  href: "https://palimpsest.info/current/",
  origin: "https://palimpsest.info",
  pathname: "/current/",
  reload() {}
};
global.location = window.location;
global.CSS = { supports() { return false; } };
global.matchMedia = (query) => ({
  matches: query.includes("max-width"),
  addEventListener() {},
  addListener() {}
});
global.requestAnimationFrame = (callback) => { callback(0); return 1; };
global.addEventListener = () => {};
global.scrollY = 0;
global.setInterval = () => 1;

vm.runInThisContext(fs.readFileSync("assets/shell.js", "utf8"), {
  filename: "assets/shell.js"
});

function activate(target) {
  let prevented = false;
  menu.emit("click", {
    target,
    preventDefault() { prevented = true; },
    stopPropagation() {}
  });
  return prevented;
}

burger.emit("click", { target: burger });
if (!body.hasAttribute("data-ps-menu")) throw new Error("menu did not open");
if (burger.getAttribute("aria-expanded") !== "true") throw new Error("burger not expanded");
if (activate(nested)) throw new Error("internal navigation was prevented");
if (body.hasAttribute("data-ps-menu")) throw new Error("internal link did not unlock body");
if (burger.getAttribute("aria-expanded") !== "false") throw new Error("burger stayed expanded");

burger.emit("click", { target: burger });
link.setAttribute("href", "https://example.org/out");
activate(nested);
if (!body.hasAttribute("data-ps-menu")) throw new Error("external link closed menu");

link.setAttribute("href", "#main");
activate(nested);
if (body.hasAttribute("data-ps-menu")) throw new Error("fragment link did not unlock body");
'''
    result = subprocess.run(
        [node, "-e", harness],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_mobile_menu_resets_when_the_viewport_becomes_desktop():
    script = (ROOT / "assets/shell.js").read_text(encoding="utf-8")

    assert 'matchMedia("(max-width: 940px)")' in script
    assert "function () {\n        if (!compactViewport.matches" in script
    assert "setMobileMenu(false, false);" in script


def test_china_flyout_is_active_for_every_generated_china_route():
    observatory = next(item for item in site_nav.NAV if item["label"] == "Observatory")

    assert site_nav._within(observatory, "/china/")
    assert site_nav._within(observatory, "/china/sources/nbs-national-data/")
    assert site_nav._within(observatory, "/china/releases/nbs-energy-output/")
    assert not site_nav._within(observatory, "/data.html")


def test_newsroom_focus_and_status_colours_clear_a_contrast_floor():
    css = (ROOT / "assets/newsroom.css").read_text(encoding="utf-8")

    assert "--nw-red: #b4233a" in css
    assert "outline-color: #8fc2ff" in css
    assert "color: #53636d" in css
    assert ".nw-table-wrap:focus-visible" in css
    assert ".nw-table-cue" in css
