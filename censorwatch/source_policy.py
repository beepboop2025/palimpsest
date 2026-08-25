"""Reviewed network authority for each CensorWatch source.

CensorWatch intentionally parses hostile public pages.  The content may choose paths,
queries, and image references, but it must never choose a new network authority.  This
module is the one registry used before collection, archival, re-checks, redirects, and
browser subresource loads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.safe_fetch import FetchError


@dataclass(frozen=True)
class SourceNetworkPolicy:
    """Exact HTTPS hosts reviewed for one hostile-content adapter."""

    page_hosts: frozenset[str]
    asset_hosts: frozenset[str] = frozenset()
    render_hosts: frozenset[str] = frozenset()

    def hosts_for(self, purpose: str) -> frozenset[str]:
        if purpose == "page":
            return self.page_hosts
        if purpose == "asset":
            return self.page_hosts | self.asset_hosts
        if purpose == "render":
            return self.page_hosts | self.asset_hosts | self.render_hosts
        raise FetchError("unknown CensorWatch URL purpose")


_WEIBO_IMAGE_HOSTS = frozenset(
    {
        *(f"wx{i}.sinaimg.cn" for i in range(1, 5)),
        *(f"tvax{i}.sinaimg.cn" for i in range(1, 5)),
    }
)


SOURCE_NETWORK_POLICIES: dict[str, SourceNetworkPolicy] = {
    "eastmoney_guba": SourceNetworkPolicy(
        page_hosts=frozenset({"guba.eastmoney.com", "caifuhao.eastmoney.com"}),
        asset_hosts=frozenset(
            {
                "gbres.dfcfw.com",
                "np-newspic.dfcfw.com",
            }
        ),
        render_hosts=frozenset({"emcharts.dfcfw.com"}),
    ),
    "weibo_search": SourceNetworkPolicy(
        page_hosts=frozenset({"s.weibo.com", "weibo.com", "www.weibo.com"}),
        asset_hosts=_WEIBO_IMAGE_HOSTS,
        render_hosts=frozenset(
            {
                "h5.sinaimg.cn",
                "img.t.sinajs.cn",
                "js.t.sinajs.cn",
                "simg.s.weibo.com",
            }
        ),
    ),
    "xueqiu": SourceNetworkPolicy(
        page_hosts=frozenset({"xueqiu.com", "www.xueqiu.com"}),
        asset_hosts=frozenset({"xqimg.imedao.com"}),
        render_hosts=frozenset({"stock.xueqiu.com"}),
    ),
}


def source_network_policy(source: str) -> SourceNetworkPolicy:
    """Return the reviewed policy or fail closed for an unknown adapter."""
    try:
        return SOURCE_NETWORK_POLICIES[source]
    except (KeyError, TypeError) as exc:
        raise FetchError("unknown CensorWatch source policy") from exc


def enforce_source_url(source: str, url: str, *, purpose: str = "page") -> None:
    """Require a canonical, credential-free URL on one reviewed exact HTTPS host."""
    if type(url) is not str or not url or len(url) > 16 * 1024:
        raise FetchError("CensorWatch URL must be non-empty bounded text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise FetchError("CensorWatch URL contains control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("CensorWatch URL could not be parsed") from exc
    authority = parts.netloc
    if (
        parts.scheme != "https"
        or not authority
        or "%" in authority
        or "\\" in authority
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in authority)
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise FetchError("CensorWatch URL authority is not canonical HTTPS")
    host = parts.hostname
    allowed = source_network_policy(source).hosts_for(purpose)
    if not host or authority.lower() != host.lower() or host.lower() not in allowed:
        raise FetchError("CensorWatch URL host is not reviewed for this source")


def source_url_policy(source: str, *, purpose: str = "page") -> Callable[[str], None]:
    """Build the callback shape consumed by :func:`core.safe_fetch.safe_fetch_response`."""

    def _policy(url: str) -> None:
        enforce_source_url(source, url, purpose=purpose)

    return _policy


def source_url_is_allowed(source: str, url: str, *, purpose: str = "page") -> bool:
    """Boolean helper for parsers, which should skip poisoned links without raising."""
    try:
        enforce_source_url(source, url, purpose=purpose)
    except FetchError:
        return False
    return True
