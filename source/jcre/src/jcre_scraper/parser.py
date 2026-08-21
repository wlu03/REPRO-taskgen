from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import PublicationRecord, RelatedLink, ReplicationPackage
from .utils import (
    ARTICLE_DOI_RE,
    extract_article_doi,
    extract_doi,
    normalize_doi,
    normalize_whitespace,
    record_id_from_doi,
)

VOLUME_RE = re.compile(r"^(JCRE|IREE)\s*,?\s*Volume\s+(\d+)\s*,\s*(\d{4})", re.IGNORECASE)
ISSUE_RE = re.compile(r"Vol\.?\s*(\d+)\s*,\s*(\d{4})\s*[-–]\s*(\d+)", re.IGNORECASE)
AVAILABILITY_RE = re.compile(
    r"\b(code|data|files?|workfile|instructions?|replication package|simulation|not publicly available)\b",
    re.IGNORECASE,
)
JOURNAL_NAMES = {
    "JCRE": "Journal of Comments and Replications in Economics",
    "IREE": "International Journal for Re-Views in Empirical Economics",
}


@dataclass
class ParsedRecord:
    record: PublicationRecord
    fragment_html: str


@dataclass
class ParseOutput:
    records: list[ParsedRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _volume_info(heading: Tag | None) -> tuple[str, int | None, int | None, str]:
    if heading is None:
        return "JCRE", None, None, ""
    text = normalize_whitespace(heading.get_text(" ", strip=True))
    match = VOLUME_RE.match(text)
    if not match:
        return "JCRE", None, None, text
    return match.group(1).upper(), int(match.group(2)), int(match.group(3)), text


def _matching_volume_heading(tag: Tag | None) -> bool:
    if tag is None or tag.name not in {"h2", "h3", "h4", "h5", "h6"}:
        return False
    return bool(VOLUME_RE.match(normalize_whitespace(tag.get_text(" ", strip=True))))


def _nearest_volume_heading(tag: Tag) -> Tag | None:
    previous = tag.find_all_previous(["h2", "h3", "h4", "h5", "h6"])
    for heading in previous:
        if _matching_volume_heading(heading):
            return heading
    return None


def _contains_article_doi(tag: Tag) -> bool:
    if extract_article_doi(tag.get_text(" ", strip=True)):
        return True
    return any(extract_article_doi(anchor.get("href")) for anchor in tag.find_all("a", href=True))


def _contains_package_link(tag: Tag) -> bool:
    for anchor in tag.find_all("a", href=True):
        href = anchor.get("href", "")
        doi = extract_doi(href) or extract_doi(anchor.get_text(" ", strip=True))
        host = (urlparse(href).hostname or "").lower()
        if host in {"journaldata.zbw.eu", "www.journaldata.zbw.eu"}:
            return True
        if doi and not doi.startswith("10.18718/"):
            return True
    return False


def _article_dois_in(node: Tag) -> set[str]:
    """Every distinct article DOI reachable from a node, in text or in an href."""
    haystack = [node.get_text(" ", strip=True)]
    haystack.extend(anchor.get("href", "") for anchor in node.find_all("a", href=True))
    found: set[str] = set()
    for value in haystack:
        found.update(match.group(0).lower() for match in ARTICLE_DOI_RE.finditer(unquote(value or "")))
    return found


def _split_multi_doi_block(tag: Tag) -> list[Tag]:
    """Split one hand-authored block that packs several publications into one tag.

    The publications page is maintained by hand, and an editor occasionally
    separates consecutive entries with <br/> inside a single <p> instead of
    starting a new one. Such a block holds several DOIs but ``_parse_one`` only
    ever emits the first, so the rest would be dropped silently. Rewrite the
    block in place as one sibling per publication, splitting on the top-level
    <br/> that follows a completed entry. The replacements stay in the document
    so volume-heading lookup and tail-block scanning keep working.
    """
    children = list(tag.children)
    groups: list[list] = []
    current: list = []
    current_has_doi = False

    for index, child in enumerate(children):
        if (
            current_has_doi
            and isinstance(child, Tag)
            and child.name == "br"
            and any(
                _article_dois_in(later)
                for later in children[index + 1 :]
                if isinstance(later, Tag)
            )
        ):
            groups.append(current)
            current, current_has_doi = [], False
            continue
        current.append(child)
        if not current_has_doi and isinstance(child, Tag) and _contains_article_doi(child):
            current_has_doi = True
    if current:
        groups.append(current)

    if len(groups) < 2:
        return [tag]

    replacements: list[Tag] = []
    for group in groups:
        clone = BeautifulSoup(f"<{tag.name}></{tag.name}>", "html.parser").find(tag.name)
        assert clone is not None
        clone.attrs = dict(tag.attrs)
        for child in group:
            clone.append(child.__copy__() if isinstance(child, Tag) else str(child))
        if not _contains_article_doi(clone):
            continue
        # Keep the original document position so candidate ordering is preserved.
        clone.sourceline = tag.sourceline
        clone.sourcepos = tag.sourcepos
        tag.insert_before(clone)
        replacements.append(clone)

    if not replacements:
        return [tag]
    tag.extract()
    return replacements


def _candidate_blocks(container: Tag) -> list[Tag]:
    candidates: list[Tag] = []
    seen_ids: set[int] = set()

    for tag in list(container.find_all(["p", "li"])):
        if not _contains_article_doi(tag):
            continue
        for block in (
            _split_multi_doi_block(tag) if len(_article_dois_in(tag)) > 1 else [tag]
        ):
            candidates.append(block)
            seen_ids.add(id(block))

    # Fallback for hand-authored pages that use DIVs instead of paragraphs.
    for tag in container.find_all("div"):
        if not _contains_article_doi(tag):
            continue
        if any(_contains_article_doi(child) for child in tag.find_all(["p", "li", "div"], recursive=True)):
            continue
        if id(tag) not in seen_ids:
            candidates.append(tag)
            seen_ids.add(id(tag))

    candidates.sort(key=lambda item: item.sourceline or 0)
    return candidates


def _tail_blocks(tag: Tag) -> list[Tag]:
    tail: list[Tag] = []
    sibling = tag.next_sibling
    inspected = 0
    while sibling is not None and inspected < 5:
        inspected += 1
        if isinstance(sibling, NavigableString):
            sibling = sibling.next_sibling
            continue
        if not isinstance(sibling, Tag):
            sibling = sibling.next_sibling
            continue
        if _matching_volume_heading(sibling) or _contains_article_doi(sibling):
            break
        text = normalize_whitespace(sibling.get_text(" ", strip=True))
        if _contains_package_link(sibling) or (text and AVAILABILITY_RE.search(text)):
            tail.append(sibling)
            sibling = sibling.next_sibling
            continue
        if text:
            break
        sibling = sibling.next_sibling
    return tail


def _semantic_lines(parts: Iterable[Tag]) -> list[str]:
    lines: list[str] = []

    def render(node: Tag | NavigableString, pieces: list[str]) -> None:
        if isinstance(node, NavigableString):
            pieces.append(re.sub(r"\s+", " ", str(node)))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            pieces.append("\n")
            return
        for child in node.children:
            render(child, pieces)

    for part in parts:
        pieces: list[str] = []
        render(part, pieces)
        text = "".join(pieces)
        lines.extend(normalize_whitespace(line) for line in text.split("\n") if normalize_whitespace(line))
    return lines


def _absolute_href(source_url: str, anchor: Tag) -> str:
    return urljoin(source_url, anchor.get("href", "").strip())


def _select_title_anchor(anchors: list[Tag], article_doi: str | None) -> Tag | None:
    if article_doi:
        for anchor in anchors:
            href_doi = extract_article_doi(anchor.get("href"))
            label = normalize_whitespace(anchor.get_text(" ", strip=True))
            if href_doi == article_doi and normalize_doi(label) != article_doi and len(label) > 3:
                return anchor
    viable = []
    for anchor in anchors:
        label = normalize_whitespace(anchor.get_text(" ", strip=True))
        if not label or label.lower().strip("» ") == "here" or extract_doi(label):
            continue
        viable.append(anchor)
    return max(viable, key=lambda item: len(normalize_whitespace(item.get_text(" ", strip=True))), default=None)


def _select_package_anchor(anchors: list[Tag], source_url: str, article_doi: str | None) -> Tag | None:
    preferred: list[Tag] = []
    fallback: list[Tag] = []
    for anchor in anchors:
        href = _absolute_href(source_url, anchor)
        host = (urlparse(href).hostname or "").lower()
        href_doi = extract_doi(href) or extract_doi(anchor.get_text(" ", strip=True))
        if href_doi == article_doi or (href_doi and href_doi.startswith("10.18718/")):
            continue
        label = normalize_whitespace(anchor.get_text(" ", strip=True)).lower()
        if host in {"journaldata.zbw.eu", "www.journaldata.zbw.eu"}:
            preferred.append(anchor)
        elif href_doi and not href_doi.startswith("10.18718/"):
            preferred.append(anchor)
        elif (
            label.strip("» ") == "here"
            and AVAILABILITY_RE.search(normalize_whitespace(anchor.parent.get_text(" ", strip=True)))
        ):
            fallback.append(anchor)
    return (preferred or fallback or [None])[0]


def _parse_one(tag: Tag, source_url: str, source_order: int) -> ParsedRecord:
    tail = _tail_blocks(tag)
    parts = [tag, *tail]
    fragment_html = "\n".join(str(part) for part in parts)
    wrapper = BeautifulSoup(f"<div>{fragment_html}</div>", "html.parser").div
    assert wrapper is not None

    heading = _nearest_volume_heading(tag)
    journal_code, heading_volume, heading_year, volume_heading = _volume_info(heading)
    lines = _semantic_lines(parts)
    anchors = list(wrapper.find_all("a", href=True))

    article_doi = extract_article_doi(" ".join(lines))
    if not article_doi:
        for anchor in anchors:
            article_doi = extract_article_doi(anchor.get("href"))
            if article_doi:
                break

    title_anchor = _select_title_anchor(anchors, article_doi)
    anchor_title = normalize_whitespace(title_anchor.get_text(" ", strip=True)) if title_anchor else ""

    citation_index = None
    for index, line in enumerate(lines):
        if article_doi and article_doi in line.lower():
            citation_index = index
            break
        if extract_article_doi(line):
            citation_index = index
            break

    citation_start = citation_index
    if citation_index is not None and citation_index > 0:
        previous_line = lines[citation_index - 1]
        if re.search(r"\b(?:Vol\.?|DOI|Journal|Economics)\b", previous_line, re.IGNORECASE):
            citation_start = citation_index - 1

    pre_lines = lines[:citation_start] if citation_start is not None else lines[:2]
    first_pre_line = pre_lines[0] if pre_lines else ""
    if anchor_title and (not first_pre_line or anchor_title in first_pre_line or first_pre_line in anchor_title):
        title = anchor_title
    else:
        title = first_pre_line or anchor_title or "Untitled publication"

    if citation_index is not None and citation_start is not None:
        citation_text = normalize_whitespace(" ".join(lines[citation_start : citation_index + 1]))
    else:
        citation_text = ""
    authors_text = ""
    if pre_lines:
        title_line_index = next((i for i, line in enumerate(pre_lines) if title in line or line in title), 0)
        author_lines = pre_lines[title_line_index + 1 :]
        if author_lines:
            authors_text = normalize_whitespace(" ".join(author_lines))
        elif pre_lines[title_line_index].startswith(title):
            authors_text = normalize_whitespace(pre_lines[title_line_index][len(title) :])

    availability_lines = lines[citation_index + 1 :] if citation_index is not None else []
    availability_text = normalize_whitespace(" ".join(availability_lines))

    issue_match = ISSUE_RE.search(citation_text)
    volume = int(issue_match.group(1)) if issue_match else heading_volume
    year = int(issue_match.group(2)) if issue_match else heading_year
    issue = f"{issue_match.group(2)}-{issue_match.group(3)}" if issue_match else None

    article_url = None
    if title_anchor and extract_article_doi(title_anchor.get("href")) == article_doi:
        article_url = _absolute_href(source_url, title_anchor)
    if article_url is None:
        for anchor in anchors:
            if extract_article_doi(anchor.get("href")) == article_doi:
                article_url = _absolute_href(source_url, anchor)
                break

    package_anchor = _select_package_anchor(anchors, source_url, article_doi)
    package_url = _absolute_href(source_url, package_anchor) if package_anchor else None
    package_doi = extract_doi(package_url) if package_url else None
    if package_anchor and not package_doi:
        package_doi = extract_doi(package_anchor.get_text(" ", strip=True))

    related_links: list[RelatedLink] = []
    seen_links: set[str] = set()
    for anchor in anchors:
        href = _absolute_href(source_url, anchor)
        if not href or href in seen_links:
            continue
        seen_links.add(href)
        href_article_doi = extract_article_doi(href)
        if href_article_doi == article_doi:
            continue
        if package_anchor is not None and anchor is package_anchor:
            continue
        label = normalize_whitespace(anchor.get_text(" ", strip=True)) or href
        related_links.append(RelatedLink(label=label, url=href, doi=extract_doi(href)))

    fallback_seed = f"{journal_code}|{volume_heading}|{title}|{authors_text}|{citation_text}"
    record_id = record_id_from_doi(journal_code, article_doi, fallback_seed)
    replication = ReplicationPackage(
        availability_text=availability_text,
        link_url=package_url,
        doi=package_doi,
        inventory_status="link_discovered" if package_url else "no_link",
    )
    record = PublicationRecord(
        record_id=record_id,
        journal_code=journal_code,
        journal_name=JOURNAL_NAMES.get(journal_code, journal_code),
        volume=volume,
        year=year,
        issue=issue,
        title=title,
        authors_text=authors_text,
        citation_text=citation_text,
        article_doi=article_doi,
        article_url=article_url,
        publications_url=source_url,
        volume_heading=volume_heading,
        source_order=source_order,
        related_links=related_links,
        replication=replication,
        status="package_link_discovered" if package_url else "no_package_link",
    )
    return ParsedRecord(record=record, fragment_html=fragment_html)


def parse_publications(html: str, source_url: str) -> ParseOutput:
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.body
        or soup
    )
    candidates = _candidate_blocks(container)
    output = ParseOutput()
    seen_record_ids: set[str] = set()

    for source_order, tag in enumerate(candidates, start=1):
        try:
            parsed = _parse_one(tag, source_url, source_order)
        except Exception as exc:  # Keep one malformed paragraph from killing discovery.
            output.warnings.append(f"Could not parse publication block {source_order}: {type(exc).__name__}: {exc}")
            continue
        if parsed.record.record_id in seen_record_ids:
            output.warnings.append(f"Duplicate publication ignored: {parsed.record.record_id}")
            continue
        seen_record_ids.add(parsed.record.record_id)
        output.records.append(parsed)

    if not output.records:
        output.warnings.append("No publication records containing a 10.18718/81781.* DOI were found.")

    # Discovery must account for every article DOI on the page. A silent
    # shortfall here means publications never reach the catalog at all.
    dois_on_page = _article_dois_in(container)
    dois_emitted = {
        parsed.record.article_doi.lower()
        for parsed in output.records
        if parsed.record.article_doi
    }
    missing = sorted(dois_on_page - dois_emitted)
    if missing:
        output.warnings.append(
            f"{len(missing)} article DOI(s) present on the page produced no record: "
            + ", ".join(missing)
        )
    return output
