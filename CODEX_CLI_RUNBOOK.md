# Codex CLI runbook — dokładna kolejność

## 1. Środowisko na Windows

Zalecany zestaw: WSL2 + Ubuntu + Docker Desktop z integracją WSL. W PowerShell uruchomionym jako administrator:

```powershell
wsl --install -d Ubuntu
```

Po restarcie uruchom Ubuntu/WSL i sprawdź:

```bash
git --version
python3 --version
docker --version
docker compose version
node --version
npm --version
```

Docelowo używaj Python 3.12. Node jest potrzebny dopiero przy frontendzie.

## 2. Instalacja Codex CLI i uv

W WSL:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex --version
codex login
```

Instalacja `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
exec "$SHELL"
uv --version
```

Jeżeli komenda `codex` lub `uv` nie jest widoczna, otwórz nową sesję WSL i ponów sprawdzenie. Diagnostyka Codexa:

```bash
codex doctor
```

## 3. Utworzenie repozytorium

```bash
mkdir -p ~/projects/ai-sre-platform
cd ~/projects/ai-sre-platform
git init -b main
```

Rozpakuj dostarczony pakiet i skopiuj do repozytorium: `AGENTS.md`, `CODEX_CLI_RUNBOOK.md`, `docs/`, `prompts/` oraz opcjonalnie `README_START_HERE.md`.

Sprawdź:

```bash
find . -maxdepth 3 -type f | sort
git status --short
```

Utwórz pierwszy checkpoint:

```bash
git add AGENTS.md CODEX_CLI_RUNBOOK.md README_START_HERE.md docs prompts
git commit -m "docs: add AI SRE platform specification and Codex workflow"
```

## 4. Zalecane uruchomienie interaktywne

Uruchamiaj Codex z katalogu repozytorium:

```bash
codex -C . -m gpt-5.6-sol -s workspace-write -a on-request
```

W interfejsie sprawdź:

```text
/status
/permissions
/model
```

Model ustaw na najmocniejszy model coding/reasoning dostępny na Twoim koncie. W przykładach użyty jest `gpt-5.6-sol`; jeśli CLI go nie udostępnia, wybierz dostępny model przez `/model` zamiast zgadywać nazwę.

Nie używaj `--yolo` ani `--dangerously-bypass-approvals-and-sandbox`. `workspace-write` wystarcza do pracy w repozytorium, a `on-request` pozwala zatwierdzić np. pobranie zależności.

## 5. Wykonywanie etapów

Zacznij nową sesję i wklej całą treść:

```text

--------------------------------------------------

Perform a deep read-only audit of this entire repository using parallel specialist subagents.

Do not modify anything.

Understand the complete architecture and current implementation state.

Read all important source code, documentation, git history, tests, infrastructure and configuration.

Compare the documented architecture with what is actually implemented.

At the end give me:
- architecture overview
- current implementation status
- completed functionality
- partially implemented functionality
- missing functionality
- bugs
- inconsistencies
- technical debt
- security concerns
- test gaps
- documentation drift
- recommended next milestone

Treat source code as the source of truth.

----------------------------------------------------------

```

```text
prompts/00_REPOSITORY_BOOTSTRAP.md
```

Po zakończeniu poproś w tej samej sesji:

```text
Re-read the active stage acceptance criteria. Inspect the complete diff, run every required quality check that is currently applicable, fix failures within this stage, and give me a concise evidence-based completion report. Do not start the next stage.
```

Następnie uruchom:

```text
/review
```

Po uwagach przekaż:

```text
Address every valid review finding within the current stage. If you disagree with a finding, explain concretely why. Re-run the relevant tests and stop with a final diff/test summary. Do not start another milestone.
```

Ręcznie sprawdź:

```bash
git status --short
git diff --check
git diff --stat
```

Jeżeli wynik jest dobry:

```bash
git add -A
git commit -m "chore: bootstrap AI SRE platform repository"
```

Potem uruchom kolejny prompt. Dla kolejnych etapów użyj sensownych commitów:

```text
1
feat(demo): implement checkout microservice flow
2
feat(observability): add correlated metrics logs and traces
3
feat(alerting): add controlled fault and alert pipeline
4
feat(incidents): add durable alert ingestion and queue
5
feat(evidence): add bounded telemetry adapters
6
feat(investigator): add evidence-grounded LangGraph workflow
7
feat(rag): add knowledge ingestion and retrieval
8
feat(ui): add incident dashboard and approvals
9
test(evals): add reproducible incident evaluation suite
10
feat(remediation): add approved rollback and recovery verification
11
feat(platform): add Kubernetes Helm Terraform and CI
12
docs: harden final demo and portfolio documentation
```

## 6. Kolejność promptów

Wykonuj ściśle w tej kolejności:

1. `00_REPOSITORY_BOOTSTRAP.md`
2. `01_DEMO_SERVICES.md`
3. `02_OBSERVABILITY.md`
4. `03_FAULT_AND_ALERTING.md`
5. `04_INCIDENT_API_AND_QUEUE.md`
6. `05_EVIDENCE_ADAPTERS.md`
7. `06_LANGGRAPH_INVESTIGATOR.md`
8. `07_RAG.md`
9. `08_FRONTEND_AND_APPROVAL.md`
10. `09_SCENARIOS_AND_EVALS.md`
11. `10_REMEDIATION_AND_RECOVERY.md`
12. `11_PLATFORM_AND_CI.md`
13. `12_FINAL_HARDENING.md`

Nie wykonuj punktów 4–13, dopóki trzy pierwsze etapy funkcjonalne nie spełniają całego Milestone 1 z `docs/QUALITY_GATES.md`.

## 7. Uruchamianie promptu jako osobnego zadania

Interaktywna sesja jest zalecana podczas implementacji. Do powtarzalnych, nieinteraktywnych zadań możesz przekazać prompt przez stdin:

```bash
codex exec -C . -m gpt-5.6-sol -s workspace-write -a never - < prompts/00_REPOSITORY_BOOTSTRAP.md
```

Kontynuacja ostatniej nieinteraktywnej sesji w bieżącym repo:

```bash
printf '%s\n' 'Run the applicable checks, fix failures within the current stage, and report the final status.' | codex exec resume --last -
```

Tryb nieinteraktywny z `-a never` nie będzie prosił o zgodę. Jeśli zadanie wymaga niedozwolonej operacji lub sieci, zatrzyma się — dlatego instalację zależności i złożone etapy wygodniej wykonywać w TUI z `on-request`.

## 8. Dobre prompty naprawcze

Gdy testy nie przechodzą:

```text
Diagnose the failing checks from their actual output. Identify the root cause before editing. Implement the smallest correct fix within the current milestone, add or update a regression test, re-run the narrow test and then the full applicable gate. Do not suppress, skip, or weaken the test.
```

Gdy Docker Compose nie startuje:

```text
Inspect `docker compose config`, container status, health checks, and relevant logs. Determine the first causal failure rather than patching downstream symptoms. Fix only the current milestone, validate from a clean Compose start, and document any changed command or environment variable.
```

Gdy Codex poszerza zakres:

```text
Stop. Re-read the active prompt and `docs/IMPLEMENTATION_PLAN.md`. Revert only your out-of-scope edits while preserving unrelated user changes. Finish the current acceptance criteria and explicitly list deferred work.
```

Gdy wynik AI halucynuje:

```text
Treat this as an evidence-validation bug, not a prompt-style issue. Reproduce it with a deterministic fixture, identify which invariant allowed an unsupported claim, enforce the invariant in deterministic validation, add a regression test, and then adjust the prompt only if still necessary.
```

## 9. Zasady checkpointów

- Jeden etap = co najmniej jeden czytelny commit.
- Nigdy nie commituj przy czerwonych obowiązkowych testach.
- Przed ryzykowną zmianą utwórz commit, ale nie używaj `git reset --hard` jako standardowego workflow.
- Po każdym etapie zachowaj raport testów i aktualizuj README.
- Nie wkładaj klucza API do historii Git. Jeśli tak się stanie, samo usunięcie pliku nie wystarcza — klucz należy unieważnić.

## 10. Konfiguracja aplikacji

Do repo commituj wyłącznie `.env.example`, np.:

```env
ENVIRONMENT=development
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=aisre
POSTGRES_USER=aisre
POSTGRES_PASSWORD=change-me
REDIS_URL=redis://redis:6379/0
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100
TEMPO_URL=http://tempo:3200
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OPENAI_API_KEY=
LLM_INVESTIGATION_MODEL=
LLM_FINAL_RCA_MODEL=
LLM_REASONING_EFFORT=medium
EMBEDDING_MODEL=text-embedding-3-small
```

Lokalnie:

```bash
cp .env.example .env
chmod 600 .env
```

Wpisz wartości ręcznie w `.env`. Nie wklejaj sekretów do rozmowy z Codexem.

## 11. Warunek przejścia do kolejnego etapu

Przejdź dalej tylko wtedy, gdy:

- kryteria aktywnego promptu są spełnione;
- wymagane testy przechodzą;
- `/review` nie wykrywa nierozwiązanych błędów high/critical;
- `git status` pokazuje wyłącznie oczekiwane zmiany;
- dokumentacja opisuje faktyczne polecenia i zachowanie;
- etap kończy się działającym, demonstracyjnym przyrostem.

## Oficjalna dokumentacja Codex

- [Codex CLI — instalacja i quickstart](https://learn.chatgpt.com/docs/codex/cli)
- [AGENTS.md — zasady wykrywania i priorytety instrukcji](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Komendy i flagi Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
