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
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import site_nav  # noqa: E402
import sync_nav  # noqa: E402

# Pages a Python generator writes wholesale. They import site_nav directly rather
# than being stamped by sync_nav, so they are checked for the shell but not for a
# byte-identical stamped block — their nav is rendered at build time.
GENERATED = {
    "china-brief.html",
    "readings/generative-firewall-index.html",
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
        ("scripts/build_china_brief.py", "china-brief.html"),
        ("scripts/generative_firewall_reading.py",
         "readings/generative-firewall-index.html"),
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


def test_mobile_menu_owns_focus_until_it_closes():
    """Pin the focus-entry, Tab containment and focus-return modal contract."""
    script = (ROOT / "assets/shell.js").read_text(encoding="utf-8")

    assert "function setMobileMenu(openNow)" in script
    assert "if (firstLink) firstLink.focus();" in script
    assert 'if (e.key !== "Tab") return;' in script
    assert "last.focus();" in script
    assert "first.focus();" in script
    assert "burger.focus();" in script


def test_newsroom_focus_and_status_colours_clear_a_contrast_floor():
    css = (ROOT / "assets/newsroom.css").read_text(encoding="utf-8")

    assert "--nw-red: #b4233a" in css
    assert "outline-color: #8fc2ff" in css
    assert "color: #53636d" in css
    assert ".nw-table-wrap:focus-visible" in css
    assert ".nw-table-cue" in css
