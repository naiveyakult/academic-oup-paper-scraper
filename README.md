# Academic OUP Paper Scraper

Codex skill for crawling Oxford Academic (`academic.oup.com`) journal search-result pages and saving Open Access / Free article PDFs with matching metadata.

## Contents

- `SKILL.md` - Codex skill instructions and usage contract.
- `scripts/oup_crawl.py` - CDP-backed OUP crawler.
- `references/oup-workflow.md` - OUP-specific workflow notes.
- `agents/openai.yaml` - Agent metadata.

## Requirements

```powershell
pip install playwright requests
```

Use a user-verified Chrome instance exposed through Chrome DevTools Protocol when Cloudflare verification or institutional cookies matter.

## Example

```powershell
python scripts/oup_crawl.py `
  --journal "Review of Corporate Finance Studies" `
  --keyword investment `
  --search-url "https://academic.oup.com/rcfs/search-results?page=1&q=investment&fl_SiteID=5507&SearchSourceType=1&allJournals=1" `
  --output-root "." `
  --cdp-port 9338 `
  --max-pages 8
```

