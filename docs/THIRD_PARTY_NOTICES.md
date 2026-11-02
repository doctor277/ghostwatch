# Third-Party Notices

GhostWatch-authored source and documentation are released under the MIT License in [`../LICENSE`](../LICENSE). That license does not replace or override the licenses of third-party software.

## Distribution model

The public source archive does not vendor Python packages, npm packages, browser binaries, font files, or Ollama models. `requirements.txt`, `package.json`, and `package-lock.json` identify packages that users may install separately. Those packages remain subject to their own licenses and notices.

## Direct Python dependencies

The exact versions declared in `requirements.txt` and the license metadata inspected in the accepted Windows environment are:

| Package | Version | License |
|---|---:|---|
| FastAPI | 0.157.0 | MIT |
| Starlette | 1.9.0 | BSD-3-Clause |
| Uvicorn | 0.37.0 | BSD-3-Clause |
| psutil | 7.1.0 | BSD-3-Clause |
| Pydantic | 2.12.0 | MIT |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| HTTPX | 0.28.1 | BSD-3-Clause |
| Ruff | 0.14.1 | MIT |

Resolved transitive packages in that environment reported MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF-2.0, or compatible dual-license metadata, except Certifi, which reported MPL-2.0. These packages are not copied into the GhostWatch source archive. Anyone redistributing a bundled application or environment must review the licenses and notice files for the exact dependency versions they redistribute.

## Browser-test dependencies

`@playwright/test`, `playwright`, and `playwright-core` 1.64.1 report Apache-2.0 in `package-lock.json`. Playwright browser executables are not part of the public source archive and may have separate component terms.

## Optional local AI

GhostWatch does not bundle Ollama or an AI model. Enabling local AI requires the user to install and configure those components separately under their applicable licenses and model terms.

## Assets and system components

- The included V3 Demo Mode screenshot is a GhostWatch interface capture using synthetic data. Its owner confirmed the right to distribute it with the project.
- Two OpenAI-generated concept images remain in the private project but are explicitly excluded from public archives because their generating-account provenance could not be established from the files alone.
- The CSS references Windows system font family names as fallbacks; no Microsoft or other third-party font files are distributed.

This inventory is an engineering record, not legal advice. Downstream distributors remain responsible for preserving every applicable third-party license and notice.

