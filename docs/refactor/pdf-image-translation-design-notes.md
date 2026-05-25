# PDF Image Translation Design Notes

This note records design discussion that is not glossary material and is not yet
an implementation plan.

## Accepted First-Version Decisions

- Image generation uses a dedicated image-generation model role and dedicated
  image-generation capability. It is not treated as a text translation model.
- Users can manually enter image model names. Fetching a model list is available
  only when the current provider/capability supports it; otherwise the UI should
  guide the user to manual entry.
- Image connectivity testing uses a minimal real image-generation call, retries
  up to three times, and records model availability state on success or failure.
- Generated page quality checks run in this order: image can be decoded, page
  aspect ratio is within the accepted tolerance, minimum readable resolution is
  satisfied, then the image is saved.
- Failure placeholder pages use a clear white-background warning layout with a
  prominent title, PDF page number, failure ordinal such as `1/3`, error
  summary, source page image path, and placeholder page path.
- Final PDF assembly preserves each source PDF page's original page size rather
  than forcing every page to A4.
- The output directory has one global Markdown report and one global JSON
  manifest, grouped by source file inside each artifact.
- Custom output still creates a timestamped child output directory by default.
  App-managed existing output directories can be recognized for revision
  handling.
- Scanning skips application-generated output directories, `_pdf_pages/`,
  hidden temporary files, and application-generated artifacts.
- The first version does not support breakpoint resume, but the manifest should
  retain enough page and artifact information for future resume, page
  regeneration, and PDF reassembly workflows.
- Large PDFs use a bounded pipeline rather than rendering all pages into memory
  or processing everything strictly serially. Rendering may prepare only a small
  number of pages ahead, such as page-generation concurrency plus two pages.
  Rendered source page images are written immediately to `_pdf_pages/source_pages/`.
  Image-generation requests follow the PDF page generation concurrency setting,
  translated page images are quality-checked and saved immediately, and the app
  keeps only a small number of pending or in-flight page artifacts in memory.
  Even when users set high PDF page concurrency, the implementation should keep
  an internal safety cap so ordinary user machines do not run out of memory.
- PDF settings include page retry attempts with default `3`, a blankable PDF page
  generation concurrency override, and separate image model availability state.
- The PDF page UI should reuse the Excel/Word visual language: scanning area,
  parameter card, execution monitor, result view, and compact KPI styling.
- Runtime logs should record file-level and page-level events, including page
  rendering, page submission, retry, rate-limit concurrency reduction, failure
  placeholder generation, and final PDF assembly.
- The first version does not show a PDF route selector. Internally the route is
  named PDF image-layout translation so a future Markdown/text route can be
  added later.
- PyMuPDF is the preferred first-choice dependency for PDF rendering, page-size
  reading, and final PDF assembly. Pillow is the preferred helper for placeholder
  pages, ratio normalization, and PNG handling.
- UI completion state maps to existing result language: use success only when
  there are no failure placeholder pages and no emergency ratio-normalized pages;
  otherwise show generated-with-review language.

## Deferred: Protected PDF Handling

Current status: deferred.

The PDF image translation first version will not implement protected-PDF
handling beyond normal files that the rendering library can open. The discussion
covered password entry, skipping protected PDFs, authorized printing/exporting
copies, and attempts to handle protected PDFs through cracking or preview/screen
capture-style workarounds. This topic is intentionally skipped for the first
implementation pass and should not be included in the next code-writing scope.

When this topic is revisited, the product boundary needs to be decided before
implementation starts:

- Whether protected PDFs should be skipped automatically.
- Whether users can provide a password for the current task.
- Whether an authorized print/export copy is allowed when the PDF can already be
  opened and printed by the user.
- What UI should appear for password retry, skip, and cancel.
- Which approaches are explicitly out of scope.
