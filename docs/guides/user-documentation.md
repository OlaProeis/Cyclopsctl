# User documentation layout

Scenario-first docs for operators and contributors.

| Document | Audience | Purpose |
|----------|----------|---------|
| `README.md` | End users | Install → init → launch quick start; advanced CLI reference |
| `docs/guides/testing-guide.md` | Developers / operators | Manual validation on fresh and existing repos; pytest pointers |
| `docs/guides/technical-brief.md` | AI agents | Module map, adoption flow, testing approach |

## README structure

1. **Quick start** — install, new project, existing project
2. **What the cyclopsctl does (and does not do)**
3. **Prerequisites**
4. **Advanced CLI reference** — includes `cyclopsctl tasks list` (table), filters, and CRUD
5. **How it works** — cycle diagram, task selection, handover verification
6. **Development and testing** — link to `docs/guides/testing-guide.md`

## Content checks

`tests/test_readme_docs.py` asserts README scenario sections, testing-guide link, advanced CLI coverage, and technical-brief module map completeness.
