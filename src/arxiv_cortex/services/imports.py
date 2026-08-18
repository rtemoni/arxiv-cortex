from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
from pypdf import PdfReader

from arxiv_cortex.db import transaction
from arxiv_cortex.services.arxiv_sync import ArxivSyncService, result_to_record
from arxiv_cortex.services.papers import PaperService
from arxiv_cortex.utils import isoformat, metadata_hash, paper_content_hash

IMPORT_USER_AGENT = "ArxivCortex/0.1 (private local paper importer)"
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
ARXIV_ID_RE = re.compile(r"(?:abs/|pdf/)?(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$", re.I)
ABSTRACT_RE = re.compile(
    r"\babstract\b\s*(.+?)(?=\n\s*(?:1\.?|I\.?)\s+(?:introduction|overview)\b)",
    re.I | re.S,
)


class PaperImportError(ValueError):
    pass


@dataclass(slots=True)
class ImportedPaper:
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    source_kind: str = "web"
    source_name: str = "Web"
    source_identifier: str = ""
    venue: str = ""
    published_at: str = ""
    updated_at: str = ""
    webpage_url: str = ""
    pdf_url: str = ""
    categories: list[str] = field(default_factory=lambda: ["external"])
    doi: str | None = None
    comment: str | None = None


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.pdf_links: list[str] = []
        self._capture: str | None = None
        self._buffer: list[str] = []
        self.headings: list[str] = []
        self.abstract_parts: list[str] = []
        self._after_abstract_heading = False
        self._description_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if self._description_depth:
            self._description_depth += 1
        elif tag == "div" and "field-name-field-paper-description" in values.get(
            "class", ""
        ).split():
            self._description_depth = 1
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key, []).append(content)
        elif tag == "link":
            href = values.get("href", "")
            link_type = values.get("type", "").lower()
            if href and (link_type == "application/pdf" or href.lower().endswith(".pdf")):
                self.pdf_links.append(href)
        elif tag == "a":
            href = values.get("href", "")
            if href and urlsplit(href).path.lower().endswith(".pdf"):
                self.pdf_links.append(href)
        elif tag in {"title", "h1", "h2", "h3"}:
            if self._after_abstract_heading and tag in {"h2", "h3"}:
                self._after_abstract_heading = False
            self._capture = tag
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._description_depth:
            self._description_depth -= 1
        if self._capture != tag:
            return
        value = " ".join("".join(self._buffer).split())
        if value:
            self.headings.append(value)
            if tag in {"h2", "h3"} and value.casefold() == "abstract":
                self._after_abstract_heading = True
        self._capture = None
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)
        elif (self._after_abstract_heading or self._description_depth) and data.strip():
            self.abstract_parts.append(data.strip())

    def first(self, *keys: str) -> str:
        for key in keys:
            values = self.meta.get(key, [])
            if values:
                return values[0]
        return ""

    def all(self, *keys: str) -> list[str]:
        for key in keys:
            values = self.meta.get(key, [])
            if values:
                return values
        return []


class PaperImportService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        session: requests.Session | None = None,
        resolve_host=None,
    ):
        self.connection = connection
        self.session = session or requests.Session()
        self.resolve_host = resolve_host or socket.getaddrinfo

    def import_url(self, url: str, *, title: str = "") -> dict[str, Any]:
        normalized_url = self._validated_url(url)
        arxiv_id = self._arxiv_id(normalized_url)
        if arxiv_id:
            imported = self.import_arxiv_ids([arxiv_id])
            if not imported:
                raise PaperImportError(f"arXiv paper {arxiv_id} was not found")
            return imported[0]

        response = self._fetch(normalized_url)
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" in content_type or urlsplit(response.url).path.lower().endswith(".pdf"):
            record = self._from_pdf(response.content, response.url, supplied_title=title)
        else:
            record = self._from_html(response.text, response.url, supplied_title=title)
        return self.import_metadata(record)

    @classmethod
    def arxiv_id_from_url(cls, url: str) -> str | None:
        return cls._arxiv_id(url.strip())

    def import_arxiv_ids(self, arxiv_ids: list[str]) -> list[dict[str, Any]]:
        unique_ids = list(dict.fromkeys(arxiv_ids))
        if not unique_ids:
            return []
        for arxiv_id in unique_ids:
            if not ARXIV_ID_RE.fullmatch(arxiv_id):
                raise PaperImportError(f"Invalid arXiv identifier: {arxiv_id}")
        response = self._fetch(
            "https://export.arxiv.org/api/query?id_list="
            + ",".join(unique_ids)
            + f"&max_results={len(unique_ids)}",
            max_bytes=MAX_HTML_BYTES,
        )
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise PaperImportError("arXiv returned an invalid metadata response")

        import arxiv

        records = []
        for entry in feed.entries:
            try:
                records.append(result_to_record(arxiv.Result._from_feed_entry(entry)))
            except (AttributeError, TypeError, ValueError, arxiv.Result.MissingFieldError):
                continue
        with transaction(self.connection):
            for record in records:
                ArxivSyncService._upsert(self.connection, record)
        papers = PaperService(self.connection).get_by_arxiv_ids(
            [record.arxiv_id for record in records]
        )
        by_id = {paper["arxiv_id"]: paper for paper in papers}
        return [by_id[arxiv_id] for arxiv_id in unique_ids if arxiv_id in by_id]

    def import_metadata(self, paper: ImportedPaper | dict[str, Any]) -> dict[str, Any]:
        record = paper if isinstance(paper, ImportedPaper) else ImportedPaper(**paper)
        record.title = " ".join(record.title.split())
        record.abstract = " ".join(record.abstract.split())
        record.authors = [" ".join(author.split()) for author in record.authors if author.strip()]
        record.categories = list(dict.fromkeys(record.categories or ["external"]))
        if not record.title:
            raise PaperImportError("A title is required for this paper")
        if not record.abstract:
            record.abstract = "No abstract was available from the source."
        if not record.webpage_url and not record.pdf_url:
            raise PaperImportError("A webpage or PDF URL is required")
        if record.webpage_url:
            record.webpage_url = self._validated_url(record.webpage_url, resolve=False)
        if record.pdf_url:
            record.pdf_url = self._validated_url(record.pdf_url, resolve=False)

        source_identifier = record.source_identifier or record.webpage_url or record.pdf_url
        key = self._external_key(record.source_kind, source_identifier)
        now = isoformat()
        published_at = self._normalized_date(record.published_at) or now
        updated_at = self._normalized_date(record.updated_at) or published_at
        authors_text = ", ".join(record.authors) or record.source_name
        metadata_payload = {
            "title": record.title,
            "abstract": record.abstract,
            "authors": record.authors,
            "categories": record.categories,
            "source": record.source_identifier,
            "venue": record.venue,
        }
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO papers(
                    arxiv_id, latest_version, title, abstract, authors_text,
                    primary_category, published_at, updated_at, doi, journal_ref,
                    comment, license_url, abstract_url, pdf_url, metadata_hash,
                    content_hash, fetched_at, source_kind, source_identifier,
                    source_name, venue
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(arxiv_id) DO UPDATE SET
                    title = excluded.title,
                    abstract = excluded.abstract,
                    authors_text = excluded.authors_text,
                    primary_category = excluded.primary_category,
                    published_at = excluded.published_at,
                    updated_at = excluded.updated_at,
                    doi = excluded.doi,
                    journal_ref = excluded.journal_ref,
                    comment = excluded.comment,
                    abstract_url = excluded.abstract_url,
                    pdf_url = excluded.pdf_url,
                    metadata_hash = excluded.metadata_hash,
                    content_hash = excluded.content_hash,
                    fetched_at = excluded.fetched_at,
                    source_kind = excluded.source_kind,
                    source_identifier = excluded.source_identifier,
                    source_name = excluded.source_name,
                    venue = excluded.venue
                """,
                (
                    key,
                    record.title,
                    record.abstract,
                    authors_text,
                    record.categories[0],
                    published_at,
                    updated_at,
                    record.doi,
                    record.venue or None,
                    record.comment,
                    record.webpage_url,
                    record.pdf_url,
                    metadata_hash(metadata_payload),
                    paper_content_hash(record.title, record.abstract),
                    now,
                    record.source_kind,
                    source_identifier,
                    record.source_name,
                    record.venue or None,
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM papers WHERE arxiv_id = ?", (key,)
            ).fetchone()
            paper_id = int(row["id"])
            self.connection.execute("DELETE FROM paper_authors WHERE paper_id = ?", (paper_id,))
            self.connection.executemany(
                "INSERT INTO paper_authors(paper_id, position, name) VALUES (?, ?, ?)",
                [(paper_id, position, name) for position, name in enumerate(record.authors)],
            )
            self.connection.execute("DELETE FROM paper_categories WHERE paper_id = ?", (paper_id,))
            self.connection.executemany(
                "INSERT INTO paper_categories(paper_id, category) VALUES (?, ?)",
                [(paper_id, category) for category in record.categories],
            )
        return PaperService(self.connection).get(key)  # type: ignore[return-value]

    def _from_html(self, html: str, url: str, *, supplied_title: str) -> ImportedPaper:
        parser = _MetadataParser()
        parser.feed(html)
        hostname = (urlsplit(url).hostname or "").lower()
        title = supplied_title or parser.first("citation_title", "og:title", "twitter:title")
        if not title:
            title = parser.headings[0] if parser.headings else ""
        title = re.sub(r"\s*[|–-]\s*(?:USENIX|RFC Editor)\s*$", "", title)
        abstract = parser.first(
            "citation_abstract", "description", "og:description", "twitter:description"
        )
        if not abstract and parser.abstract_parts:
            abstract = " ".join(" ".join(parser.abstract_parts).split())
        authors = parser.all("citation_author", "dc.creator", "author")
        pdf_url = parser.first("citation_pdf_url")
        if not pdf_url and parser.pdf_links:
            pdf_url = parser.pdf_links[0]
        pdf_url = urljoin(url, pdf_url) if pdf_url else ""
        published = parser.first("citation_publication_date", "date", "article:published_time")
        venue = parser.first("citation_conference_title", "citation_journal_title")

        if self._host_is(hostname, "usenix.org"):
            source_kind, source_name = "usenix", "USENIX"
            venue = venue or self._usenix_venue(url)
            published = published or self._usenix_year(url)
        elif self._host_is(hostname, "rfc-editor.org"):
            source_kind, source_name = "rfc", "RFC Editor"
            identifier_match = re.search(r"rfc(\d+)", url, re.I)
            venue = f"RFC {identifier_match.group(1)}" if identifier_match else "RFC"
        elif self._host_is(hostname, "p4.org"):
            source_kind, source_name = "p4", "P4.org"
            venue = venue or "P4 specification"
        else:
            source_kind = "web"
            source_name = hostname.removeprefix("www.") or "Web"
        return ImportedPaper(
            title=title,
            abstract=abstract,
            authors=authors,
            source_kind=source_kind,
            source_name=source_name,
            source_identifier=url,
            venue=venue,
            published_at=published,
            webpage_url=url,
            pdf_url=pdf_url,
            categories=["standards" if source_kind in {"rfc", "p4"} else "external.systems"],
        )

    def _from_pdf(self, content: bytes, url: str, *, supplied_title: str) -> ImportedPaper:
        try:
            reader = PdfReader(BytesIO(content))
            metadata = reader.metadata or {}
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        except Exception as error:
            raise PaperImportError("The PDF could not be read") from error
        title = supplied_title or str(metadata.get("/Title") or "").strip()
        if not title:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            title = lines[0] if lines else ""
        if not title:
            raise PaperImportError("The PDF has no identifiable title; enter one and try again")
        author_text = str(metadata.get("/Author") or "")
        authors = [item.strip() for item in re.split(r";|\band\b", author_text) if item.strip()]
        match = ABSTRACT_RE.search(text)
        abstract = " ".join(match.group(1).split()) if match else ""
        hostname = (urlsplit(url).hostname or "").removeprefix("www.")
        source_name = hostname or "PDF"
        venue = ""
        published_at = ""
        path = urlsplit(url).path.lower()
        if hostname == "engineering.fb.com" and path.endswith("/sigcomm24-final246.pdf"):
            source_name, venue, published_at = "Meta Engineering", "SIGCOMM 2024", "2024"
        elif hostname == "mcanini.github.io" and path.endswith("/sonata.sigcomm18.pdf"):
            source_name, venue, published_at = "ACM SIGCOMM", "SIGCOMM 2018", "2018"
        elif self._host_is(hostname, "p4.org") and path.endswith("/int_v2_1.pdf"):
            source_name, venue, published_at = "P4.org", "P4 specification", "2020-11-11"
        return ImportedPaper(
            title=title,
            abstract=abstract,
            authors=authors,
            source_kind="pdf",
            source_name=source_name,
            source_identifier=url,
            venue=venue,
            published_at=published_at,
            webpage_url="",
            pdf_url=url,
            categories=["external"],
        )

    def _fetch(self, url: str, *, max_bytes: int | None = None) -> requests.Response:
        current_url = self._validated_url(url)
        for _redirect in range(MAX_REDIRECTS + 1):
            try:
                response = self.session.get(
                    current_url,
                    headers={
                        "User-Agent": IMPORT_USER_AGENT,
                        "Accept": "text/html,application/pdf",
                    },
                    timeout=(10, 30),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as error:
                raise PaperImportError("The paper source could not be reached") from error
            self._validate_response_peer(response)
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise PaperImportError("The source returned an invalid redirect")
                current_url = self._validated_url(urljoin(current_url, location))
                continue
            try:
                response.raise_for_status()
            except requests.RequestException as error:
                raise PaperImportError(f"The source returned HTTP {response.status_code}") from error
            content_type = response.headers.get("Content-Type", "").lower()
            limit = max_bytes or (MAX_PDF_BYTES if "pdf" in content_type else MAX_HTML_BYTES)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > limit:
                    raise PaperImportError("The source document is too large to import")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response.url = current_url
            return response
        raise PaperImportError("The source redirected too many times")

    def _validated_url(self, url: str, *, resolve: bool = True) -> str:
        value = url.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise PaperImportError("Enter a public HTTP or HTTPS paper URL")
        if resolve:
            try:
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                addresses = {item[4][0] for item in self.resolve_host(parsed.hostname, port)}
            except (OSError, ValueError) as error:
                raise PaperImportError("The paper host could not be resolved") from error
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global:
                    raise PaperImportError("Private or local network URLs cannot be imported")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))

    def _validate_response_peer(self, response: requests.Response) -> None:
        if not isinstance(self.session, requests.Session):
            return
        try:
            address = response.raw._connection.sock.getpeername()[0]
            peer = ipaddress.ip_address(address)
        except (AttributeError, OSError, ValueError) as error:
            response.close()
            raise PaperImportError("The paper source connection could not be verified") from error
        if not peer.is_global:
            response.close()
            raise PaperImportError("Private or local network URLs cannot be imported")

    @staticmethod
    def _arxiv_id(url: str) -> str | None:
        hostname = (urlsplit(url).hostname or "").lower()
        if hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None
        match = ARXIV_ID_RE.search(urlsplit(url).path.lstrip("/"))
        return match.group(1) if match else None

    @staticmethod
    def _external_key(source_kind: str, source_identifier: str) -> str:
        digest = hashlib.sha256(f"{source_kind}:{source_identifier}".encode()).hexdigest()[:20]
        return f"ext-{digest}"

    @staticmethod
    def _normalized_date(value: str) -> str:
        if not value:
            return ""
        normalized = value.strip()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            for pattern in ("%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    parsed = datetime.strptime(normalized, pattern).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
            else:
                return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return isoformat(parsed)

    @staticmethod
    def _usenix_venue(url: str) -> str:
        match = re.search(r"/conference/([^/]+)/", url)
        return match.group(1).upper() if match else "USENIX"

    @staticmethod
    def _usenix_year(url: str) -> str:
        match = re.search(r"/conference/[a-z]+(\d{2})/", url, re.I)
        return f"20{match.group(1)}" if match else ""

    @staticmethod
    def _host_is(hostname: str, domain: str) -> bool:
        return hostname == domain or hostname.endswith(f".{domain}")
