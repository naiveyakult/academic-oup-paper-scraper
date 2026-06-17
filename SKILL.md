---
name: academic-oup-paper-scraper
description: Crawl Oxford Academic / academic.oup.com journal search-result pages for Open Access and Free article PDFs and metadata. Use when the user provides OUP search-result URLs, journal names, and keywords and wants dynamic browser-driven PDF downloads saved as journal/keyword/00_title.pdf plus matching JSON metadata.
---

# Academic OUP Paper Scraper

Use this skill for Oxford Academic (`academic.oup.com`) paper collection tasks where the user wants article PDFs and metadata from journal search-result pages.

## Core Workflow

1. Prefer a user-verified Chrome instance exposed through Chrome DevTools Protocol (CDP). Do not launch a fresh automated profile when Cloudflare verification or institutional cookies matter.
2. If needed, ask the user to open or verify Chrome with remote debugging, then pass the port with `--cdp-port`; default to `9338`.
3. Use `scripts/oup_crawl.py` with either single-job arguments or a JSON config file.
4. Crawl both OUP availability filters by default:
   - `access_openaccess=true`
   - `access_free=true`
5. Follow OUP pagination through the page's real `data-url` values. Do not guess that every OUP journal uses simple `page=N` URLs.
6. Download only article-body PDFs by opening each article page and clicking the visible PDF button; do not save figures, supplementary files, references, or unrelated PDF links.
7. Save outputs as `journal_name/search_keyword/00_title.pdf` and `journal_name/search_keyword/00_title_metadata.json`. Numbering restarts per keyword directory.
8. Validate results at the end: PDF files must start with `%PDF`, metadata JSON must exist, and DOI duplicates should be absent inside each keyword directory.

## Common Commands

Single keyword:

```powershell
python C:\Users\viruser.v-desktop\.codex\skills\academic-oup-paper-scraper\scripts\oup_crawl.py `
  --journal "Review of Corporate Finance Studies" `
  --keyword investment `
  --search-url "https://academic.oup.com/rcfs/search-results?page=1&q=investment&fl_SiteID=5507&SearchSourceType=1&allJournals=1" `
  --output-root "C:\Users\viruser.v-desktop\Desktop\金融数据爬取" `
  --cdp-port 9338 `
  --max-pages 8
```

Multiple keywords:

```powershell
python C:\Users\viruser.v-desktop\.codex\skills\academic-oup-paper-scraper\scripts\oup_crawl.py `
  --config rcfs_oup_jobs.json `
  --cdp-port 9338 `
  --max-pages 8
```

Config format:

```json
{
  "journal": "Review of Corporate Finance Studies",
  "output_root": "C:\\Users\\viruser.v-desktop\\Desktop\\金融数据爬取",
  "keywords": [
    {
      "keyword": "investment",
      "search_url": "https://academic.oup.com/rcfs/search-results?page=1&q=investment&fl_SiteID=5507&SearchSourceType=1&allJournals=1"
    }
  ]
}
```

## Output Contract

- PDF: `<output_root>/<journal>/<keyword>/<NN>_<sanitized title>.pdf`
- Metadata: `<output_root>/<journal>/<keyword>/<NN>_<sanitized title>_metadata.json`
- Run manifest: `<output_root>/<journal>/oup_crawl_manifest.json`
- Final summary: `<output_root>/<journal>/oup_crawl_summary.json`

Metadata includes DOI, title, abstract, authors, journal, publisher, volume, issue, page, publication date, source search URL, article URL, search access label, search PDF URL, final PDF URL, PDF size, and download method when available.

## Notes

Read `references/oup-workflow.md` before modifying the scraper or troubleshooting OUP-specific failures.

Install runtime dependencies in the active Python environment if missing:

```powershell
pip install playwright requests
```
