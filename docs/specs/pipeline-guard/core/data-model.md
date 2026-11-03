# Fase 1 — Modelo de Dados: pipeline-guard

**Spec**: [spec.md](./spec.md)

Entidades extraídas da seção "Entidades-Chave" do `spec.md` e dos contratos canônicos do PRD (`docs/specs/pipeline-guard/core/PRD-pipeline-guard.md`, seção 10). Os cinco contratos versionados (`incident_input_v1`, `pipeline_diagnosis_v1`, `eligibility_decision_v1`, `approval_input_v1`, `remediation_result_v1`) têm seus schemas em [contracts/](./contracts/).

## Configuração do Projeto

- **Campos**: `schema_version`, `project.id`, `pipeline.adapter`, `pipeline.root`, `repository.provider`, `repository.url`, `repository.default_branch`, `evidence_providers[]`, `validation.commands[]`, `safety.editable_paths[]`, `safety.limits`, `safety.sensitive_paths[]` (paths sempre bloqueados para remediação — migrations, seeds, dados, configuração de produção — controle verificável, não uma garantia genérica de ausência de mudança de dado/regra de negócio), `runtime.artifact_store`.
- **Relações**: referencia exatamente um `Adapter de Pipeline` (por nome, resolvido pelo Adapter Registry) e zero ou mais `EvidenceProvider`/`GitProvider`/`ArtifactStore`.
- **Regras de validação**: `additionalProperties=false`; adapter deve existir no registry allowlisted; credenciais somente como nome de variável/segredo, nunca valor literal; ausência de configuração para um projeto nunca é tratada como permissão ampla por padrão (FR-006). `validation.commands[]` contém comandos pertencentes ao projeto, executados como declarados; o sistema controla declaração, timeout e registro do resultado, mas não infere efeitos colaterais. Comandos capazes de escrever em sistemas externos exigem alvo isolado e não produtivo e credenciais de privilégio mínimo, e não podem aprovar ou fazer merge de PR, implantar em produção nem reexecutar o pipeline do host que falhou (FR-022).
- **Transições de estado**: nenhuma — é um documento estático versionado por projeto, revalidado a cada execução.

## Incidente

- **Campos**: `schema_version`, `incident_id`, `source`, `environment`, `pipeline_id`, `run_id`, `failed_step`, `occurred_at`, `error_summary`, `artifacts[]` (tipo, URI autorizada, hash), `project_profile`, `correlation` (tentativa, assinatura, incidentes relacionados).
- **Relações**: é a entrada de uma execução de `diagnose`; pode ter 1:N `Evidência` associadas; referencia `project_profile` que resolve a `Configuração do Projeto`.
- **Regras de validação**: campos obrigatórios ou explicitamente marcados como indisponíveis; validado antes de qualquer acesso a evidência (FR-008); repetições reaproveitam o mesmo `incident_id` quando já correlacionadas pelo host (FR-026).
- **Transições de estado**: `recebido` → `validado` → (`evidência coletada` | `recusado por contrato inválido`).

## Evidência

- **Campos**: `evidence_id`, `origem` (provider), `tipo` (log, artefato, metadado, consulta, link), `hash`, `política_de_retenção`.
- **Relações**: pertence a exatamente um `Incidente`; é citada por 0..N `evidence_citations` dentro de um `Diagnóstico`.
- **Regras de validação**: toda evidência tem `evidence_id`, origem, hash e retenção (FR-011); consultas são somente leitura e limitadas por wrappers allowlisted (FR-009).
- **Transições de estado**: `coletada` → `citada` (referenciada por um diagnóstico) ou `descartada` (retenção expirada).

## Diagnóstico (`pipeline_diagnosis_v1`)

- **Campos**: `failure_family` (`CODE | DATA | BUSINESS_RULE | CONFIGURATION | DEPENDENCY | PLATFORM_INFRA | ACCESS_PERMISSION | EXTERNAL_SYSTEM | UNKNOWN`), `confidence` (`HIGH | MEDIUM | LOW`), `cause_status` (`CONFIRMED | HYPOTHESIS | UNKNOWN`), `cause`, `impact_summary`, `affected_resources[]`, `evidence_citations[]` (`evidence_id` + afirmação), `recommended_action`, `limitations[]`, `adapter_details` (objeto versionado por adapter).
- **Relações**: produzido a partir de 1..N `Evidência`; consumido por exatamente uma `Decisão de Elegibilidade`.
- **Regras de validação**: `additionalProperties=false`; toda afirmação relevante cita um `evidence_id` existente (FR-012); campos como `approved`, `eligible`, `next_step`, `merge`, `deploy` nunca são produzidos pelo modelo (reservados ao Gate/Git Executor); falha de schema, citação ausente ou orçamento esgotado força `cause_status=UNKNOWN` e parada segura (FR-013).
- **Transições de estado**: `HYPOTHESIS` → `CONFIRMED` (mais evidência) ou → `UNKNOWN` (evidência insuficiente/orçamento esgotado) — nunca o inverso dentro da mesma execução.

## Decisão de Elegibilidade (`eligibility_decision_v1`)

- **Campos**: `decision` (`ELIGIBLE_FOR_REMEDIATION | REPORT_ONLY | ESCALATE`), `policy_version`, `rule_id`, sinais independentes utilizados, motivo, limitações, hash do diagnóstico avaliado.
- **Relações**: consome exatamente um `Diagnóstico`; determina se uma `Aprovação` pode legitimamente iniciar remediação para o mesmo incidente.
- **Regras de validação**: decidida deterministicamente (sem julgamento do modelo, FR-015); `failure_family` fora de `CODE` nunca resulta em `ELIGIBLE_FOR_REMEDIATION` (FR-016).
- **Transições de estado**: terminal por execução — uma nova falha do mesmo pipeline gera um novo `incident_id` e uma nova decisão.

## Aprovação (`approval_input_v1`)

- **Campos**: `incident_id`, `diagnosis_hash`, `decision` (`APPROVED | REJECTED`), `actor`, `decided_at`, `channel_type`, referência externa, assinatura/prova de identidade (modo orquestrado), `base_commit` aprovado.
- **Relações**: vinculada a um `Diagnóstico` (via `diagnosis_hash`) e a uma `Decisão de Elegibilidade` (`ELIGIBLE_FOR_REMEDIATION`); habilita um `Resultado de Remediação`.
- **Regras de validação**: vínculo obrigatório a `incident_id` + `diagnosis_hash` + `base_commit`; expiração ou divergência bloqueia a execução (FR-019).
- **Transições de estado**: `recebida` → `validada` → `consumida por uma remediação` (uma aprovação não é reaproveitável entre execuções).

## Resultado de Remediação (`remediation_result_v1`)

- **Campos**: status terminal e motivo, `base_commit`, branch, commit produzido, arquivos alterados e estatísticas, patch hash, testes executados e resultados, draft PR URL/ID, `pr_validation_status` (`VALIDATED` | `PUBLISHED_UNVALIDATED` — distingue uma PR publicada com todos os testes declarados em `PASSED` de uma PR publicada mesmo com testes ausentes/falhos, por política explícita), limitações, avisos, referências de log.
- **Relações**: produzido a partir de uma `Aprovação` válida; referencia o workspace isolado e a `Configuração do Projeto` que definiu paths/limites/comandos.
- **Regras de validação**: PR sempre criada em modo draft (FR-024); nenhuma operação de merge/deploy; idempotente por `incident_id`/`run_id` (FR-026); quando `status=DRAFT_PR_PUBLISHED`, `branch`/`commit`/`patch_hash`/`tests` (não vazio)/`draft_pr`/`pr_validation_status` são obrigatórios (condicional no schema, não sempre-opcionais).
- **Transições de estado**: `workspace preparado` → `patch aplicado` → `testado` → (`draft PR publicada` | `bloqueado por falha de teste/limite`).

## Solicitação de PR (interna, não um dos 5 contratos canônicos)

- **Campos**: título, descrição (incidente, diagnóstico, evidências, arquivos alterados, testes, limitações), branch, base, `draft=true`.
- **Relações**: produzida por `scripts/pr_request_builder.py` a partir do `Resultado de Remediação` intermediário (branch/commit/patch/testes), aprovada pelo Gate de política de PR (`scripts/pr_policy_gate.py`) e entregue a `scripts/pr_publication.py`, que revalida branch/testes antes de acionar o publicador privado de repair drafts.
- **Regras de validação**: nenhuma chamada de rede neste componente; `draft=true` sempre presente; não decide política de "PR não validada" (isso é o Gate de política de PR, um componente separado).
- **Transições de estado**: `montada` → `aprovada pelo gate de política` → `branch/testes revalidados` → `publicada ou reutilizada por publish-draft-pr`.

## Artifact Store

- **Campos**: `run_id`, referência de artefato, conteúdo/ponteiro, modo (`local` | `referência de host`).
- **Relações**: usado por `scripts/incident_validator.py` (idempotência por `incident_id`) e por `scripts/run_manifest.py` (artefatos por etapa); resolve `runtime.artifact_store` da `Configuração do Projeto`.
- **Regras de validação**: quando uma referência de host é fornecida, o armazenamento local não é usado; ausência de configuração não implica fallback silencioso para um modo diferente do declarado.
- **Transições de estado**: N/A — é uma interface de armazenamento, não um registro de execução com ciclo de vida próprio.

## Adapter de Pipeline

- **Campos**: nome (chave do registry), versão, capacidades declaradas (quais artefatos interpreta), `adapter_details` schema próprio.
- **Relações**: referenciado por `Configuração do Projeto`; produz `adapter_details` dentro de um `Diagnóstico`; pode propor comandos lógicos de validação consumidos por `Resultado de Remediação`.
- **Regras de validação**: só resolve por nome se estiver instalado e allowlisted no registry (FR-003, mitigação do risco "adapter malicioso ou desconhecido").
- **Transições de estado**: N/A — é uma extensão versionada, não um registro de execução.
