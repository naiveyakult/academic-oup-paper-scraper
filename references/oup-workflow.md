# Oxford Academic Workflow Notes

## Browser and verification

- Use a Chrome instance the user can see and verify manually. Connect with Playwright over CDP, usually `http://127.0.0.1:9338`.
- Do not hard-code the Chrome version or user agent. A mismatched UA can make OUP cookies appear invalid and trigger repeated verification.
- If a page title is "Just a moment", "请稍候", or the body mentions security verification, stop and ask the user to complete verification in the visible Chrome window.

## Search pages

- OUP search-result pagination often uses `href="javascript:;"` and stores the real next-page query in `data-url`; always read `a.al-nav-next[data-url]` or `a[aria-label="Next"][data-url]`.
- Crawl both availability filters unless the user says otherwise:
  - `access_openaccess=true`
  - `access_free=true`
- Search result pages can include figures and other resource hits whose links point to the same article with a `#fragment`. Collect article titles from result blocks and ignore fragment links, figure links, supplementary data, issue links, and PDF links during article discovery.

## PDF download

- Direct `/article-pdf/...pdf` links may redirect to HTML or a validation page. The reliable route is:
  1. Open the article page in the verified browser context.
  2. Click the visible PDF button (`a.article-pdfLink`, `a.al-link.pdf`, or text `PDF`).
  3. Wait for a Silverchair token URL whose host contains `silverchair.com` and whose URL contains `.pdf`.
  4. Download that token URL with the current requests session.
  5. Save only if bytes begin with `%PDF` or the content type is `application/pdf`.
- Do not save PDFs linked from references, figures, supplementary material, or unrelated hosts unless the user explicitly asks for those assets.

## Output and deduplication

- Save each keyword independently. If the same DOI appears under two keywords, each keyword directory should contain its own PDF and JSON.
- Within one keyword directory, skip existing successful downloads by DOI when available.
- Continue numbering from the highest existing `NN_` prefix in the keyword directory.
- Validate the final directory by checking PDF magic bytes, matching metadata files, missing core metadata fields, and duplicate DOI values within a keyword.
