# AI SRE Platform — pakiet startowy dla Codex CLI

Ten katalog rozpoczął się jako pakiet instrukcji potrzebnych do zbudowania portfolio-grade
platformy AI SRE. Repozytorium zawiera już fundament Stage 00 oraz działający przepływ checkout z
Milestone 1A / Stage 01. Aktualne komendy i status implementacji są w `README.md`; poniższa treść
pozostaje przewodnikiem po etapowym workflow.

## Najważniejsza zasada

Nie zlecaj Codexowi całej aplikacji jednym promptem. Każdy etap ma osobny zakres, testy, kryteria odbioru i checkpoint Git. Następny etap zaczynaj dopiero po ręcznym sprawdzeniu poprzedniego.

## Zawartość

- `AGENTS.md` — trwałe reguły pracy Codexa w repozytorium.
- `CODEX_CLI_RUNBOOK.md` — instalacja, komendy i dokładna kolejność pracy.
- `docs/PRODUCT_REQUIREMENTS.md` — zakres produktu i wymagania funkcjonalne.
- `docs/ARCHITECTURE.md` — architektura i granice komponentów.
- `docs/DOMAIN_AND_API.md` — model domenowy, API i kontrakty danych.
- `docs/IMPLEMENTATION_PLAN.md` — roadmapa i zależności między etapami.
- `docs/QUALITY_GATES.md` — testy i bramki jakości.
- `docs/SECURITY.md` — model bezpieczeństwa i human-in-the-loop.
- `docs/EVALS.md` — scenariusze oraz metryki jakości AI.
- `prompts/*.md` — gotowe prompty implementacyjne.

## Minimalny workflow

1. Utwórz puste repozytorium i skopiuj do niego zawartość tego pakietu.
2. Wykonaj komendy z `CODEX_CLI_RUNBOOK.md`.
3. Uruchom Codex CLI w katalogu głównym repozytorium.
4. Najpierw wykonaj `prompts/00_REPOSITORY_BOOTSTRAP.md`.
5. Potem realizuj prompty numerycznie, po jednym.
6. Po każdym etapie uruchom `/review`, testy i utwórz commit.
7. Nie podawaj prawdziwego `OPENAI_API_KEY` w promptach ani nie commituj `.env`.

## Docelowa historia demonstracyjna

System ma prezentować pełną ścieżkę:

`detect → investigate → correlate → hypothesize → verify → recommend → approve → remediate → verify recovery`

Finalne demo powinno uruchamiać się lokalnie jedną komendą, generować kontrolowaną awarię, utworzyć incydent, zebrać dowody, wygenerować ustrukturyzowane RCA i zatrzymać remediation do czasu decyzji człowieka.
