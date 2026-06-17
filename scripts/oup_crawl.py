#!/usr/bin/env python
"""Crawl Oxford Academic search-result pages for OA/Free article PDFs."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REQUEST_TIMEOUT = 45
CROSSREF_URL = "https://api.crossref.org/works/{doi}"
INVALID_FILENAME_CHARS = r'<>:"/\|?*'


def sanitize_filename(value: str, max_length: int = 180) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    value = value.replace("\u00a0", " ")
    for char in INVALID_FILENAME_CHARS:
        value = value.replace(char, "_")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "untitled")[:max_length].rstrip(" .")


def strip_doi(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.strip()


def is_pdf_bytes(content: bytes, content_type: str = "", status: int = 200) -> bool:
    return status == 200 and (content.startswith(b"%PDF") or "application/pdf" in (content_type or "").lower())


def make_session(proxy: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome Safari/537.36",
            "Accept": "application/pdf,text/html,application/xhtml+xml,*/*",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def get_pdf_with_fallback(session: requests.Session, url: str) -> Tuple[Optional[requests.Response], str]:
    last_error = ""
    sessions = [("configured", session)]
    direct = make_session(None)
    direct.headers.update(session.headers)
    sessions.append(("direct", direct))
    for mode, active_session in sessions:
        try:
            response = active_session.get(url, timeout=max(REQUEST_TIMEOUT, 60), verify=False, allow_redirects=True)
        except Exception as exc:
            last_error = f"{mode}: {exc}"
            continue
        if is_pdf_bytes(response.content, response.headers.get("content-type", ""), response.status_code):
            return response, mode
        last_error = (
            f"{mode}: status={response.status_code} "
            f"content_type={response.headers.get('content-type', '')} bytes={len(response.content)}"
        )
    raise RuntimeError(last_error or "PDF download failed")


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_query(url: str, **params: Optional[str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = [value]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def search_page_url(base_url: str, data_url: str) -> str:
    if data_url.startswith("http"):
        return data_url
    parsed = urlparse(base_url)
    journal_path = parsed.path.rsplit("/", 1)[0] + "/search-results"
    return urljoin(base_url, journal_path + "?" + data_url.lstrip("?"))


def successful_existing(keyword_dir: Path) -> Tuple[set, int]:
    existing_dois = set()
    max_index = -1
    for pdf in keyword_dir.glob("*.pdf"):
        match = re.match(r"^(\d+)_", pdf.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    for json_path in keyword_dir.glob("*_metadata.json"):
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pdf_name = metadata.get("pdf_file") or json_path.name.replace("_metadata.json", ".pdf")
        pdf_path = keyword_dir / pdf_name
        doi = strip_doi(metadata.get("doi", ""))
        if doi and pdf_path.exists() and pdf_path.stat().st_size > 5000:
            try:
                if pdf_path.read_bytes()[:4] == b"%PDF":
                    existing_dois.add(doi.lower())
            except Exception:
                pass
    return existing_dois, max_index + 1


def find_downloaded_by_doi(journal_dir: Path, doi: str, exclude_dir: Path) -> Optional[Tuple[Path, Path, Dict[str, Any]]]:
    target = strip_doi(doi).lower()
    if not target:
        return None
    for json_path in journal_dir.glob("*/*_metadata.json"):
        if json_path.parent == exclude_dir:
            continue
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if strip_doi(metadata.get("doi", "")).lower() != target:
            continue
        pdf_name = metadata.get("pdf_file") or json_path.name.replace("_metadata.json", ".pdf")
        pdf_path = json_path.parent / pdf_name
        if not pdf_path.exists() or pdf_path.stat().st_size <= 5000:
            continue
        try:
            if pdf_path.read_bytes()[:4] != b"%PDF":
                continue
        except Exception:
            continue
        return pdf_path, json_path, metadata
    return None


def copy_existing_download(
    source: Tuple[Path, Path, Dict[str, Any]],
    keyword_dir: Path,
    index: int,
    metadata: Dict[str, Any],
    keyword: str,
    source_search_url: str,
) -> Tuple[Path, Path]:
    source_pdf, source_json, source_metadata = source
    title = metadata.get("title") or source_metadata.get("title") or source_pdf.stem
    prefix = f"{index:02d}_{sanitize_filename(title)}"
    pdf_path = keyword_dir / f"{prefix}.pdf"
    json_path = keyword_dir / f"{prefix}_metadata.json"
    shutil.copy2(source_pdf, pdf_path)
    copied_metadata = dict(source_metadata)
    copied_metadata.update(
        {
            "title": title,
            "search_keyword": keyword,
            "source_search_url": source_search_url,
            "pdf_file": pdf_path.name,
            "reused_existing_download": True,
            "reused_from_pdf": str(source_pdf),
            "reused_from_metadata": str(source_json),
        }
    )
    write_json(json_path, copied_metadata)
    return pdf_path, json_path


def authors_from_crossref(message: Dict[str, Any]) -> List[Dict[str, str]]:
    authors = []
    for author in message.get("author") or []:
        given = author.get("given", "")
        family = author.get("family", "")
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            authors.append({"name": name})
    return authors


def date_from_parts(parts: Iterable[Iterable[int]]) -> str:
    try:
        first = list(parts)[0]
    except Exception:
        return ""
    if not first:
        return ""
    year = str(first[0])
    month = f"{first[1]:02d}" if len(first) > 1 else "01"
    day = f"{first[2]:02d}" if len(first) > 2 else "01"
    return f"{year}-{month}-{day}"


def get_crossref(session: requests.Session, doi: str) -> Dict[str, Any]:
    if not doi:
        return {}
    try:
        response = session.get(CROSSREF_URL.format(doi=doi), timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json().get("message") or {}
    except Exception:
        return {}
    return {}


def normalize_html_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


async def fetch_with_browser(page, url: str) -> Dict[str, Any]:
    try:
        result = await page.evaluate(
            """async ({ url, timeoutMs }) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                const response = await fetch(url, {
                    credentials: 'include',
                    redirect: 'follow',
                    signal: controller.signal,
                    headers: { 'Accept': 'application/pdf,text/html,*/*' },
                });
                try {
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = '';
                    const chunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                    }
                    return {
                        status: response.status,
                        final_url: response.url,
                        content_type: response.headers.get('content-type') || '',
                        body_b64: btoa(binary),
                    };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {"url": url, "timeoutMs": REQUEST_TIMEOUT * 1000},
        )
        return {
            "ok": True,
            "status": result["status"],
            "final_url": result["final_url"],
            "content_type": result["content_type"],
            "content": base64.b64decode(result["body_b64"]),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def fetch_pdf_with_browser_context(context, page, url: str) -> Tuple[Optional[bytes], Dict[str, Any]]:
    errors = []
    try:
        response = await context.request.get(
            url,
            timeout=max(REQUEST_TIMEOUT, 60) * 1000,
            headers={"Accept": "application/pdf,text/html,*/*"},
        )
        content = await response.body()
        content_type = response.headers.get("content-type", "")
        if is_pdf_bytes(content, content_type, response.status):
            return content, {
                "final_pdf_url": response.url,
                "pdf_size": len(content),
                "download_via": "playwright_context_request",
                "content_type": content_type,
            }
        errors.append(
            {
                "mode": "playwright_context_request",
                "status": response.status,
                "content_type": content_type,
                "bytes": len(content),
            }
        )
    except Exception as exc:
        errors.append({"mode": "playwright_context_request", "error": str(exc)})

    result = await fetch_with_browser(page, url)
    if result.get("ok"):
        content = result.get("content") or b""
        content_type = result.get("content_type", "")
        status = result.get("status", 0)
        if is_pdf_bytes(content, content_type, status):
            return content, {
                "final_pdf_url": result.get("final_url", url),
                "pdf_size": len(content),
                "download_via": "browser_page_fetch",
                "content_type": content_type,
            }
        errors.append(
            {
                "mode": "browser_page_fetch",
                "status": status,
                "content_type": content_type,
                "bytes": len(content),
            }
        )
    else:
        errors.append({"mode": "browser_page_fetch", "error": result.get("error")})

    return None, {"browser_fetch_errors": errors}


async def download_pdf_by_click(page, session: requests.Session, article_url: str, save_path: Path) -> Tuple[bool, Dict[str, Any]]:
    click_page = await page.context.new_page()
    try:
        print(f"    Open article page for PDF button: {article_url}")
        await click_page.goto(article_url, wait_until="domcontentloaded", timeout=90000)
        await click_page.wait_for_timeout(1500)
        blocked = await is_security_verification(click_page)
        if blocked:
            return False, {"click_error": "OUP security verification page", "article_url": article_url}

        pdf_link = click_page.locator("a.article-pdfLink, a.al-link.pdf, a:has-text('PDF')").first
        if await pdf_link.count() == 0:
            return False, {"click_error": "PDF button not found", "article_url": article_url}

        href = await pdf_link.get_attribute("href")
        print(f"    Click PDF button: {href or 'no href'}")

        if href:
            pdf_url = urljoin(click_page.url, href)
            token_page = await page.context.new_page()
            try:
                response = await token_page.goto(pdf_url, wait_until="commit", timeout=90000)
                await token_page.wait_for_timeout(1500)
                token_url = token_page.url
                if ".pdf" in token_url.lower() and "silverchair.com" in urlparse(token_url).netloc.lower():
                    content, browser_info = await fetch_pdf_with_browser_context(page.context, token_page, token_url)
                    if content:
                        save_path.write_bytes(content)
                        if save_path.stat().st_size > 5000:
                            return True, {
                                "pdf_source": "oup_pdf_button_href_browser_token",
                                "pdf_url": pdf_url,
                                "final_pdf_url": browser_info.get("final_pdf_url", token_url),
                                "pdf_size": save_path.stat().st_size,
                                "download_via": browser_info.get("download_via", "browser_context"),
                            }
                    token_response, download_mode = get_pdf_with_fallback(session, token_url)
                    content_type = token_response.headers.get("content-type", "")
                    if is_pdf_bytes(token_response.content, content_type, token_response.status_code):
                        save_path.write_bytes(token_response.content)
                        if save_path.stat().st_size > 5000:
                            return True, {
                                "pdf_source": "oup_pdf_button_href_browser_token",
                                "pdf_url": pdf_url,
                                "final_pdf_url": token_response.url,
                                "pdf_size": save_path.stat().st_size,
                                "download_via": f"browser_generated_silverchair_token_requests_{download_mode}",
                            }
                if response:
                    try:
                        body = await response.body()
                    except Exception:
                        body = b""
                    content_type = response.headers.get("content-type", "")
                    if is_pdf_bytes(body, content_type, response.status):
                        save_path.write_bytes(body)
                        if save_path.stat().st_size > 5000:
                            return True, {
                                "pdf_source": "oup_pdf_button_href_browser_response",
                                "pdf_url": pdf_url,
                                "final_pdf_url": response.url,
                                "pdf_size": save_path.stat().st_size,
                                "download_via": "browser_navigation_response_body",
                            }
            finally:
                await token_page.close()

        await pdf_link.click(timeout=30000)

        token_url = ""
        for _ in range(30):
            current = click_page.url
            parsed = urlparse(current)
            if ".pdf" in current.lower() and "silverchair.com" in parsed.netloc.lower():
                token_url = current
                break
            await click_page.wait_for_timeout(1000)

        if not token_url:
            if ".pdf" in click_page.url.lower():
                content, browser_info = await fetch_pdf_with_browser_context(page.context, click_page, click_page.url)
                if content:
                    save_path.write_bytes(content)
                    return True, {
                        "pdf_source": "oup_pdf_button_click",
                        "pdf_url": href or article_url,
                        "final_pdf_url": browser_info.get("final_pdf_url", click_page.url),
                        "pdf_size": save_path.stat().st_size,
                        "download_via": browser_info.get("download_via", "browser_context"),
                    }
            return False, {
                "click_error": "PDF click did not reach a Silverchair token URL",
                "article_url": article_url,
                "after_click_url": click_page.url,
            }

        content, browser_info = await fetch_pdf_with_browser_context(page.context, click_page, token_url)
        if content:
            save_path.write_bytes(content)
            if save_path.stat().st_size > 5000:
                return True, {
                    "pdf_source": "oup_pdf_button_click",
                    "pdf_url": href or article_url,
                    "final_pdf_url": browser_info.get("final_pdf_url", token_url),
                    "pdf_size": save_path.stat().st_size,
                    "download_via": browser_info.get("download_via", "browser_context"),
                }
        response, download_mode = get_pdf_with_fallback(session, token_url)
        content_type = response.headers.get("content-type", "")
        if is_pdf_bytes(response.content, content_type, response.status_code):
            save_path.write_bytes(response.content)
            if save_path.stat().st_size > 5000:
                return True, {
                    "pdf_source": "oup_pdf_button_click",
                    "pdf_url": href or article_url,
                    "final_pdf_url": response.url,
                    "pdf_size": save_path.stat().st_size,
                    "download_via": f"click_pdf_button_silverchair_token_{download_mode}",
                }
        return False, {
            "click_error": "Silverchair token URL did not return PDF bytes",
            "article_url": article_url,
            "token_url": token_url,
            "status_code": response.status_code,
            "content_type": content_type,
            "bytes": len(response.content),
        }
    except Exception as exc:
        return False, {"click_error": str(exc), "article_url": article_url}
    finally:
        await click_page.close()


async def is_security_verification(page) -> bool:
    try:
        title = await page.title()
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return False
    body_lower = body.lower()
    title_lower = title.lower()
    combined = f"{title}\n{body}"
    return (
        "just a moment" in title_lower
        or "security verification" in body_lower
        or "\u8bf7\u7a0d\u5019" in combined
        or "\u6b63\u5728\u8fdb\u884c\u5b89\u5168\u9a8c\u8bc1" in combined
        or "\u672c\u7f51\u7ad9\u4f7f\u7528\u5b89\u5168\u670d\u52a1" in combined
    )


async def collect_search_articles(page, search_url: str, max_pages: int, access_mode: str = "both") -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    seen = set()
    all_variants = [
        ("open_access", add_query(search_url, access_openaccess="true", access_free=None, page="1")),
        ("free", add_query(search_url, access_openaccess=None, access_free="true", page="1")),
    ]
    variants = [item for item in all_variants if access_mode in ("both", item[0])]
    for access_label, first_url in variants:
        next_url = first_url
        for page_number in range(1, max_pages + 1):
            print(f"Search page {page_number} [{access_label}]: {next_url}")
            try:
                await page.goto(next_url, wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                if articles:
                    print(f"  Search navigation failed after collecting articles; processing partial results: {exc}")
                    break
                raise
            try:
                await page.wait_for_selector(".sr-list, .al-article-box, text=正在进行安全验证, text=请稍候", timeout=15000)
            except Exception:
                await page.wait_for_timeout(2500)
            if await is_security_verification(page):
                if articles:
                    print("  OUP security verification reached during pagination; processing articles collected so far.")
                    break
                raise RuntimeError("OUP is showing security verification; complete it in Chrome and rerun.")
            page_articles = await page.evaluate(
                r"""(accessLabel) => {
                    const out = [];
                    const localSeen = new Set();
                    const blocks = [...document.querySelectorAll('.sr-list.al-article-box, .sr-list')];
                    for (const block of blocks) {
                        const textBlock = block.innerText || '';
                        if (!/Journal Article|Corrected Proof|Advance Article/i.test(textBlock)) continue;
                        const titleLinks = [...block.querySelectorAll('a[href]')].filter(a => {
                            const href = a.href || '';
                            const text = (a.innerText || a.textContent || '').trim();
                            if (!text) return false;
                            if (!href.includes('/article') && !href.includes('/advance-article')) return false;
                            if (href.includes('#')) return false;
                            if (/article-pdf|article-abstract|search-results|issue|supplementary-data|advance-articles$/i.test(href)) return false;
                            if (/\bPDF\b|Abstract|Supplementary data/i.test(text)) return false;
                            if (a.className && String(a.className).includes('sri-figure-title')) return false;
                            return true;
                        });
                        if (!titleLinks.length) continue;
                        const preferred = titleLinks.find(a => String(a.className || '').includes('article-link')) || titleLinks[0];
                        const key = preferred.href.split('?')[0].split('#')[0];
                        if (localSeen.has(key)) continue;
                        localSeen.add(key);
                        const pdf = block.querySelector('a.sri-attachment.pdf, a.solr-pdfaccess, a[href*=".pdf"]');
                        const flags = [...block.querySelectorAll('input[type="hidden"]')]
                            .map(input => input.value || '')
                            .filter(Boolean);
                        out.push({
                            title: (preferred.innerText || preferred.textContent || '').replace(/\s*Get access\s*$/i, '').replace(/\s+/g, ' ').trim(),
                            url: key,
                            search_pdf_url: pdf ? pdf.href : '',
                            access_label: accessLabel,
                            access_flags: flags,
                        });
                    }
                    return out;
                }""",
                access_label,
            )
            fresh = []
            for item in page_articles:
                key = item["url"]
                if key in seen:
                    continue
                seen.add(key)
                articles.append(item)
                fresh.append(item)
            print(f"  found {len(page_articles)}, new {len(fresh)}")
            next_data_url = await page.evaluate(
                r"""() => {
                    const next = document.querySelector('a.al-nav-next[data-url], a[aria-label="Next"][data-url]');
                    return next ? next.getAttribute('data-url') : '';
                }"""
            )
            if not page_articles or not next_data_url:
                break
            next_url = search_page_url(page.url, next_data_url)
    return articles


async def page_metadata(
    page,
    article_url: str,
    fallback_title: str,
    keyword: str,
    journal: str,
    source_search_url: str,
) -> Dict[str, Any]:
    meta = await page.evaluate(
        r"""async (url) => {
            const response = await fetch(url, {credentials: 'include', redirect: 'follow'});
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, 'text/html');
            const pick = (selector, attr='content') => {
                const node = doc.querySelector(selector);
                return node ? (node.getAttribute(attr) || '').trim() : '';
            };
            const all = (selector, attr='content') => [...doc.querySelectorAll(selector)]
                .map(n => (n.getAttribute(attr) || '').trim()).filter(Boolean);
            const abstractNode = doc.querySelector('.abstract, section.abstract, #abstract, .abstractSection');
            return {
                final_url: response.url,
                title: pick('meta[name="citation_title"]') || pick('meta[property="og:title"]') || doc.querySelector('h1')?.textContent?.trim() || '',
                doi: pick('meta[name="citation_doi"]'),
                abstract: pick('meta[name="description"]') || abstractNode?.textContent?.trim() || '',
                authors: all('meta[name="citation_author"]'),
                journal: pick('meta[name="citation_journal_title"]'),
                publication_date: pick('meta[name="citation_publication_date"]'),
                volume: pick('meta[name="citation_volume"]'),
                issue: pick('meta[name="citation_issue"]'),
                first_page: pick('meta[name="citation_firstpage"]'),
                pdf_url: pick('meta[name="citation_pdf_url"]'),
            };
        }""",
        article_url,
    )
    doi = strip_doi(meta.get("doi") or "")
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "title": normalize_html_text(meta.get("title") or fallback_title),
        "abstract": normalize_html_text(meta.get("abstract") or ""),
        "authors": [{"name": name} for name in meta.get("authors") or []],
        "journal": meta.get("journal") or journal,
        "publisher": "Oxford University Press",
        "volume": meta.get("volume") or "",
        "issue": meta.get("issue") or "",
        "page": meta.get("first_page") or "",
        "publication_date": meta.get("publication_date") or "",
        "search_keyword": keyword,
        "source_search_url": source_search_url,
        "article_url": meta.get("final_url") or article_url,
        "citation_pdf_url": meta.get("pdf_url") or "",
        "pdf_downloaded": True,
        "pdf_file": "",
    }


def load_jobs(args: argparse.Namespace) -> Tuple[str, Path, List[Dict[str, str]]]:
    if args.config:
        config_path = Path(args.config)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        journal = config["journal"]
        output_root = Path(config.get("output_root") or args.output_root or ".")
        jobs = []
        for item in config.get("keywords") or []:
            jobs.append({"keyword": item["keyword"], "search_url": item["search_url"]})
        if not jobs:
            raise ValueError("Config must contain at least one keyword job.")
        return journal, output_root, jobs

    if not (args.journal and args.keyword and args.search_url):
        raise ValueError("--journal, --keyword, and --search-url are required without --config.")
    return args.journal, Path(args.output_root or "."), [{"keyword": args.keyword, "search_url": args.search_url}]


def validate_keyword_dir(keyword_dir: Path) -> Dict[str, Any]:
    pdfs = sorted(keyword_dir.glob("*.pdf"))
    jsons = sorted(keyword_dir.glob("*_metadata.json"))
    bad_pdf = []
    missing_json = []
    metadata_issues = []
    doi_files: Dict[str, List[str]] = {}
    for pdf in pdfs:
        try:
            if pdf.read_bytes()[:4] != b"%PDF":
                bad_pdf.append(pdf.name)
        except Exception as exc:
            bad_pdf.append(f"{pdf.name}: {exc}")
        json_path = keyword_dir / f"{pdf.stem}_metadata.json"
        if not json_path.exists():
            missing_json.append(pdf.name)
            continue
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            metadata_issues.append({"file": json_path.name, "error": str(exc)})
            continue
        for field in ["title", "doi", "abstract", "pdf_file"]:
            if not metadata.get(field):
                metadata_issues.append({"file": json_path.name, "missing": field})
        doi = strip_doi(metadata.get("doi", "")).lower()
        if doi:
            doi_files.setdefault(doi, []).append(json_path.name)
    duplicate_dois = [{"doi": doi, "files": files} for doi, files in doi_files.items() if len(files) > 1]
    return {
        "pdf_count": len(pdfs),
        "metadata_count": len(jsons),
        "bad_pdf_count": len(bad_pdf),
        "missing_json_count": len(missing_json),
        "metadata_issue_count": len(metadata_issues),
        "duplicate_doi_count": len(duplicate_dois),
        "bad_pdf": bad_pdf,
        "missing_json": missing_json,
        "metadata_issues": metadata_issues[:50],
        "duplicate_dois": duplicate_dois[:50],
        "first_pdf": pdfs[0].name if pdfs else "",
        "last_pdf": pdfs[-1].name if pdfs else "",
    }


async def run(args: argparse.Namespace) -> None:
    import urllib3
    from playwright.async_api import async_playwright

    urllib3.disable_warnings()
    journal, output_root, jobs = load_jobs(args)
    session = make_session(None if args.no_proxy else args.proxy)
    journal_dir = output_root / journal
    journal_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = journal_dir / "oup_crawl_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    else:
        manifest = {}
    manifest.update({
        "journal": journal,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "oup_search_pages_pdf_button",
        "keywords": manifest.get("keywords", {}),
    })

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(
            f"http://127.0.0.1:{args.cdp_port}",
            timeout=args.cdp_timeout_ms,
        )
        if not browser.contexts:
            raise RuntimeError("No Chrome CDP contexts found. Start Chrome with remote debugging first.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            user_agent = await page.evaluate("navigator.userAgent")
            if user_agent:
                session.headers["User-Agent"] = user_agent
                print(f"Using Chrome UA from CDP: {user_agent}")
        except Exception:
            pass
        for job in jobs:
            keyword = job["keyword"]
            search_url = job["search_url"]
            print(f"\n=== {journal}: {keyword} ===")
            keyword_dir = journal_dir / keyword
            keyword_dir.mkdir(parents=True, exist_ok=True)
            existing_dois, index = successful_existing(keyword_dir)
            written = []
            failed = []
            try:
                articles = await collect_search_articles(page, search_url, args.max_pages, args.access_mode)
            except RuntimeError as exc:
                failed.append({"error": str(exc), "stage": "collect_search_articles"})
                manifest["keywords"][keyword] = {
                    "keyword": keyword,
                    "source_search_url": search_url,
                    "search_pages": args.max_pages,
                    "articles_found": 0,
                    "written_this_run": written,
                    "failed_this_run": failed,
                    "partial": True,
                }
                manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                write_json(manifest_path, manifest)
                continue

            def write_progress(partial: bool = True) -> None:
                manifest["keywords"][keyword] = {
                    "keyword": keyword,
                    "source_search_url": search_url,
                    "search_pages": args.max_pages,
                    "articles_found": len(articles),
                    "written_this_run": written,
                    "failed_this_run": failed,
                    "partial": partial,
                }
                manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                write_json(manifest_path, manifest)

            write_progress()
            for article in articles:
                try:
                    metadata = await asyncio.wait_for(
                        page_metadata(page, article["url"], article["title"], keyword, journal, search_url),
                        timeout=args.metadata_timeout,
                    )
                except Exception as exc:
                    failed.append(
                        {
                            "title": article.get("title", ""),
                            "article_url": article.get("url", ""),
                            "error": str(exc),
                            "stage": "page_metadata",
                        }
                    )
                    write_progress()
                    continue
                metadata["search_access_label"] = article.get("access_label", "")
                metadata["search_access_flags"] = article.get("access_flags", [])
                metadata["search_pdf_url"] = article.get("search_pdf_url", "")
                doi = strip_doi(metadata.get("doi", ""))
                if doi and doi.lower() in existing_dois:
                    print(f"  Skip existing DOI: {metadata['title']}")
                    continue

                reused = find_downloaded_by_doi(journal_dir, doi, keyword_dir) if doi else None
                if reused:
                    pdf_path, json_path = copy_existing_download(
                        reused,
                        keyword_dir,
                        index,
                        metadata,
                        keyword,
                        search_url,
                    )
                    written.append(
                        {
                            "index": f"{index:02d}",
                            "doi": doi,
                            "title": metadata["title"],
                            "pdf_path": str(pdf_path),
                            "json_path": str(json_path),
                            "pdf_size": pdf_path.stat().st_size,
                            "reused_existing_download": True,
                        }
                    )
                    existing_dois.add(doi.lower())
                    print(f"  Reused existing DOI: {metadata['title']}")
                    index += 1
                    write_progress()
                    await page.wait_for_timeout(int(args.delay * 1000))
                    continue

                prefix = f"{index:02d}_{sanitize_filename(metadata['title'])}"
                pdf_path = keyword_dir / f"{prefix}.pdf"
                json_path = keyword_dir / f"{prefix}_metadata.json"
                print(f"  Candidate: {metadata['title']}")
                try:
                    ok, pdf_info = await asyncio.wait_for(
                        download_pdf_by_click(page, session, metadata["article_url"], pdf_path),
                        timeout=args.article_timeout,
                    )
                except asyncio.TimeoutError:
                    ok = False
                    pdf_info = {
                        "click_error": f"article download timed out after {args.article_timeout} seconds",
                        "article_url": metadata["article_url"],
                    }
                if not ok:
                    if pdf_path.exists() and pdf_path.stat().st_size <= 5000:
                        pdf_path.unlink()
                    failed.append({"title": metadata["title"], "doi": doi, **pdf_info})
                    details = []
                    if pdf_info.get("click_error"):
                        details.append(str(pdf_info.get("click_error")))
                    if pdf_info.get("token_url"):
                        details.append(f"token={pdf_info.get('token_url')}")
                    for err in pdf_info.get("browser_fetch_errors") or []:
                        mode = err.get("mode", "fetch")
                        status = err.get("status")
                        content_type = err.get("content_type")
                        size = err.get("bytes")
                        error = err.get("error")
                        parts = [mode]
                        if status is not None:
                            parts.append(f"status={status}")
                        if content_type:
                            parts.append(f"type={content_type}")
                        if size is not None:
                            parts.append(f"bytes={size}")
                        if error:
                            parts.append(f"error={error}")
                        details.append(" ".join(parts))
                    print("    PDF failed" + (": " + " | ".join(details[:4]) if details else ""))
                    write_progress()
                    await page.wait_for_timeout(int(args.failure_delay * 1000))
                    continue

                metadata.update(pdf_info)
                metadata["pdf_file"] = pdf_path.name
                if doi and (not metadata.get("abstract") or not metadata.get("authors")):
                    crossref = get_crossref(session, doi)
                    metadata["abstract"] = metadata["abstract"] or normalize_html_text(crossref.get("abstract") or "")
                    metadata["authors"] = metadata["authors"] or authors_from_crossref(crossref)
                    metadata["publication_date"] = metadata["publication_date"] or date_from_parts(
                        (crossref.get("issued") or {}).get("date-parts") or []
                    )

                write_json(json_path, metadata)
                written.append(
                    {
                        "index": f"{index:02d}",
                        "doi": doi,
                        "title": metadata["title"],
                        "pdf_path": str(pdf_path),
                        "json_path": str(json_path),
                        "pdf_size": pdf_info.get("pdf_size"),
                    }
                )
                if doi:
                    existing_dois.add(doi.lower())
                print(f"    Saved {pdf_path.name} ({pdf_info.get('pdf_size')} bytes)")
                index += 1
                write_progress()
                await page.wait_for_timeout(int(args.delay * 1000))

            write_progress(partial=False)
        await browser.close()

    summary = {
        "journal": journal,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "OUP dynamic search pages + verified Chrome PDF button; OA and Free filters",
        "keywords": {job["keyword"]: validate_keyword_dir(journal_dir / job["keyword"]) for job in jobs},
    }
    summary_path = journal_dir / "oup_crawl_summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl Oxford Academic OA/Free article PDFs and metadata.")
    parser.add_argument("--config", help="JSON config containing journal, output_root, and keywords.")
    parser.add_argument("--journal", help="Journal name; used as first-level output directory.")
    parser.add_argument("--keyword", help="Search keyword; used as second-level output directory.")
    parser.add_argument("--search-url", help="Oxford Academic search-results URL.")
    parser.add_argument("--output-root", default=".", help="Root directory for journal output.")
    parser.add_argument("--cdp-port", type=int, default=9338, help="Chrome DevTools Protocol port.")
    parser.add_argument("--cdp-timeout-ms", type=int, default=120000, help="Timeout for connecting to Chrome CDP.")
    parser.add_argument("--max-pages", type=int, default=6, help="Maximum pages per availability filter.")
    parser.add_argument("--access-mode", choices=["both", "open_access", "free"], default="both", help="Availability filter to crawl.")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds to pause after a successful save or reuse.")
    parser.add_argument("--failure-delay", type=float, default=8.0, help="Seconds to pause after a failed PDF attempt.")
    parser.add_argument("--metadata-timeout", type=float, default=90.0, help="Seconds before skipping a stuck metadata request.")
    parser.add_argument("--article-timeout", type=float, default=180.0, help="Seconds before skipping a stuck article PDF attempt.")
    parser.add_argument("--proxy", default="http://127.0.0.1:7892", help="Optional HTTP/HTTPS proxy.")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy.")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
