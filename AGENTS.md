# AGENTS.md — Repository Operating Guide

Tento dokument je záväzný pre celý repozitár (`/workspace/fergusson`).

## 1) Cieľ projektu (stručne)
Fergusson je modulárny AI asistent s event-driven architektúrou:
- **kanály** (CLI, Discord, budúce vstupy) prijímajú správy,
- **broker** (Redis) ich distribuuje,
- **core agent** rozhoduje medzi priamym vybavením a delegovaním na skill,
- **pamäť** je per-chat v SQLite.

## 2) Architektúra podľa adresárov
- `src/agent/` — jadro agenta, orchestrácia, pamäť, načítanie skillov, archiver.
- `src/broker/` — message bus a schémy správ medzi kanálmi a runtime.
- `src/channels/` — integračné vstupy/výstupy (napr. Discord, CLI adaptery).
- `src/tools/` — nástroje volané agentom (bash, filesystem, web).
- `src/db/` — DB modely a session vrstva pre perzistenciu stavu.
- `src/prompt/` — Jinja šablóny systémových promptov.
- `workspace/skills/` — dynamické skills podľa `SKILL.md` štandardu.
- `docs/` — dlhšia technická dokumentácia architektúry a rozhodnutí.
- `tests/` — automatizované testy.

## 3) Pravidlá pre implementáciu zmien
Pri každej netriviálnej zmene **musí agent aktualizovať relevantnú časť tohto súboru (`AGENTS.md`)**.

Za „relevantnú časť“ sa považuje najmä:
1. zmena zodpovedností modulov,
2. pridanie/odstránenie adresára alebo významného komponentu,
3. zmena toku správ medzi kanálmi, brokerom a agentom,
4. zmena spôsobu perzistencie pamäte,
5. zmena kontraktov nástrojov alebo registrácie skillov,
6. nové prevádzkové pravidlá, ktoré majú poznať ďalší agenti.

Ak sa mení architektúra detailnejšie, synchronizuj aj `docs/ARCHITECTURE.md`.

## 4) Definition of Done pre dokumentáciu
Pred odovzdaním implementácie skontroluj:
- je popísané **čo** sa zmenilo,
- je popísané **kde** sa zmena nachádza (priečinok/modul),
- je popísané **ako** sa zmenil tok dát alebo zodpovednosti,
- AGENTS.md ostáva stručný, ale aktuálny.

## 5) Praktické zásady
- Nerob broad refactor dokumentácie bez potreby; uprav iba dotknuté sekcie.
- Pri väčšej zmene doplň krátku poznámku „Migration note“ (ak sa mení správanie).
- Ak pridávaš nový subsystém, pridaj ho do sekcie „Architektúra podľa adresárov“.
