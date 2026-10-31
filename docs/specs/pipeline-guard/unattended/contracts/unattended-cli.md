# Contrato da CLI: entrypoint não interativo (`unattended_cli.py`)

**Spec**: [../spec.md](../spec.md)

Única chamada que um host faz para executar o Pipeline Guard sem pessoa presente (FR-001). Segue o padrão do `diagnose_cli.py`: script standalone com metadados PEP 723, executado com `uv run`, sem instalação manual.

## Invocação

```bash
uv run "<pipeline-guard-root>/scripts/unattended_cli.py" \
  --mode unattended \
  --config "<consumer-root>/config/pipeline-guard.yaml" \
  --project-root "<consumer-root>" \
  --output-dir "<host-retention-dir>" \
  [--authorization-state-dir "<durable-host-state-dir>"] \
  [--workspace-parent "<writable-dir>"] \
  [--model "<model-id>"] \
  "<failure-context.json>"
```

| Argumento | Obrigatório | Descrição |
|---|---|---|
| `--mode unattended` | Sim | Declara o modo explicitamente (FR-002). Sem ele, ou com outro valor, a CLI sai com código 2 antes de qualquer leitura de evidência. |
| `--config` | Sim | Configuração validada do projeto, com a seção `unattended` ([schema](./project_config_unattended.schema.json)). |
| `--project-root` | Sim | Raiz do projeto consumidor, onde o host colocou os artefatos declarados no contexto (ex.: `target/` do dbt). |
| `--output-dir` | Sim | Diretório que o host retém depois do descarte do ambiente (FR-018). |
| `--authorization-state-dir` | Quando `unattended.remediation_enabled: true` | Diretório persistente do host, compartilhado entre novas tentativas isoladas, usado somente para marcadores create-once de autorizações consumidas. Deve ficar fora do repositório consumidor e do workspace, preservar criação atômica e não é repassado ao agente de autoria. |
| `--workspace-parent` | Não | Onde criar o workspace isolado. Padrão: diretório temporário do sistema. |
| `--model` | Não | Sobrescreve `ANTHROPIC_MODEL`. |
| arquivo de contexto | Sim | `incident_input_v1` 1.1 ([schema](./incident_input_v1.schema.json)). `-` lê da entrada padrão. |

## Variáveis de ambiente

Todas são fornecidas pelo host a partir do seu cofre de segredos. Nenhuma aparece em argumento, log, resumo ou PR.

| Finalidade | Variável | Origem |
|---|---|---|
| Endpoint do modelo | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (ou `ANTHROPIC_API_KEY`), `ANTHROPIC_MODEL` | Identidade de uso do modelo |
| Armazenamento local de artefatos comuns do plugin | `CLAUDE_PLUGIN_DATA` | Diretório gravável do ambiente efêmero; não armazena o estado de consumo da autorização |
| Push da branch de correção | a referenciada em `repository.push_credential_ref` | Identidade de escrita restrita |
| Publicação da draft PR | as do publicador privado do Pipeline Guard para o provedor configurado (ex.: `BITBUCKET_API_TOKEN` ou `GITHUB_API_TOKEN`) | Identidade de escrita restrita |
| Assinatura da autorização | a referenciada em `unattended.authorization_secret_ref` | Segredo do host, com pelo menos 32 bytes |

O entrypoint repassa ao agente de autoria somente o ambiente mínimo de autoria (`ClonedWorkspace.authorship_environment()` mais as variáveis do endpoint do modelo). Credenciais de escrita e o segredo de autorização nunca são herdados por processos controlados pelo modelo (FR-014).

## Ordem de execução

1. Pré-requisitos (FR-019): configuração válida; `uv`, `git` e `claude` (≥ 2.1.295) disponíveis; provedor Bitbucket ou GitHub aceito pelo publicador privado do Pipeline Guard; variáveis referenciadas presentes (só a presença é verificada, nunca o valor); diretórios graváveis; e, quando a remediação está habilitada, `--authorization-state-dir` fora do repositório e do workspace, com suporte ao `put` create-once.
2. Validação do contexto da falha no modo não interativo, incluindo a proteção contra recursão (FR-017).
3. Cálculo da assinatura e deduplicação (FR-021).
4. Diagnóstico e decisão de elegibilidade, pelo mesmo fluxo do `diagnose_cli.py`.
5. Autorização por política (FR-009), apenas com `ELIGIBLE_FOR_REMEDIATION` e `remediation_enabled: true`, seguida do consumo atômico no store resolvido a partir de `--authorization-state-dir`.
6. Somente depois do consumo, workspace isolado no commit executado, autoria restrita, validação do patch, testes declarados, push da branch de incidente e publicação da draft PR pelo gate de política (componentes da spec principal).
7. Escrita do resultado, do resumo e dos artefatos em `--output-dir`.

Os limites de tempo e de tokens (`unattended.max_seconds`, `unattended.max_model_tokens`) valem para a execução inteira. Ao estourar, nenhuma nova operação externa é iniciada; se o push já ocorreu, a execução informa `pushed_branch` e encerra sem tentar publicar a PR (FR-011).

## Saídas

| Canal | Conteúdo |
|---|---|
| Saída padrão | Resumo sanitizado em Markdown: desfecho, diagnóstico resumido, limitações e link da draft PR quando houver. É o que aparece no log do host. |
| Erro padrão | Mensagens operacionais sanitizadas. |
| `<output-dir>/result.json` | `unattended_run_result_v1` ([schema](./unattended_run_result_v1.schema.json)). |
| `<output-dir>/summary.md` | O mesmo resumo da saída padrão. |
| `<output-dir>/artifacts/` | Diagnóstico, elegibilidade, relatórios JSON e Markdown, manifesto, autorização assinada e `remediation_result_v1`, cada um com `sha256` listado em `result.json`. |

## Códigos de saída

| Código | Desfecho | Significado para o host |
|---|---|---|
| 0 | `DRAFT_PR_PUBLISHED` | Draft PR criada ou reaproveitada; o link está no resumo. |
| 3 | `REPORT_ONLY` | Diagnóstico entregue sem correção (não elegível, causa desconhecida, correção desabilitada ou commit ausente). |
| 4 | `BLOCKED` | Um controle impediu a publicação; `reason_code` diz qual. |
| 5 | `LIMIT_REACHED` | Tempo ou tokens esgotados; nenhuma nova operação é iniciada e uma branch já enviada é informada em `pushed_branch`. |
| 1 | `ERROR` | Falha operacional (modelo indisponível, credencial expirada etc.). |
| 2 | `ERROR` | Entrada, configuração ou pré-requisito inválido, antes de qualquer alteração. |

Nenhum código transforma a execução do pipeline em sucesso: os guias por host mantêm a execução original registrada como falha, qualquer que seja o desfecho do reparo (FR-016).
