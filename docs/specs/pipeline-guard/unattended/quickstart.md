# Quickstart: pipeline-guard — Execução Não Assistida

**Spec**: [spec.md](./spec.md)

Roteiro mínimo para quem implementa ou revisa esta feature: simular, num container limpo, a chamada que um host faz quando um pipeline falha. Não depende de Databricks, Bitbucket Pipelines nem Airflow; esses hosts têm guias próprios em `./docs/unattended/`.

## 1. Preparar um ambiente isolado

Use um container Linux descartável, com um usuário sem privilégios, sem sessão nem credencial pessoal (FR-012):

- `uv` e `git` no `PATH`; Claude Code 2.1.295 ou superior somente quando o backend selecionado for `anthropic`;
- o marketplace clonado numa ref fixa, com `.`;
- credenciais de escrita do provedor suportado pelo publicador privado do Pipeline Guard (Bitbucket ou GitHub);
- um repositório de sandbox com um projeto dbt e a branch `dev`;
- um diretório persistente do host, montado fora do repositório e do workspace, para o estado de
  consumo das autorizações. Reutilize o mesmo diretório nas novas tentativas.

## 2. Habilitar a política no projeto consumidor

Acrescente a seção `unattended` em `config/pipeline-guard.yaml` ([schema](./contracts/project_config_unattended.schema.json)). Comece com a correção desabilitada para validar o caminho de "somente relatório":

```yaml
unattended:
  remediation_enabled: false
  service_identity: "sp:pipeline-guard-sandbox"
```

Depois habilite a correção, sempre com referências e nunca com valores:

```yaml
unattended:
  remediation_enabled: true
  service_identity: "sp:pipeline-guard-sandbox"
  authorization_secret_ref: "env:PIPELINE_GUARD_POLICY_SECRET"
  max_seconds: 3600
  max_model_tokens: 200000
```

## 3. Montar o contexto da falha

Rode `dbt build` com um teste quebrado de propósito (ex.: teste singular com a condição invertida), mantenha `target/run_results.json` e `target/manifest.json` e escreva um `incident_input_v1` 1.1 ([schema](./contracts/incident_input_v1.schema.json)):

```json
{
  "schema_version": "1.1",
  "incident_id": "INC-SANDBOX-001",
  "source": "ci",
  "environment": "sandbox",
  "pipeline_id": "delivery-dashboard-sandbox",
  "run_id": "run-001",
  "failed_step": "dbt_build",
  "occurred_at": "2026-11-10T02:00:00Z",
  "error_summary": "Singular test failed in pipeline_guard_smoke.",
  "artifacts": [
    { "type": "dbt_run_results", "uri": "artifact://dbt/run_results.json", "hash": "sha256:<digest>" },
    { "type": "dbt_manifest", "uri": "artifact://dbt/manifest.json", "hash": "sha256:<digest>" }
  ],
  "project_profile": "dbt",
  "code_version": { "branch": "dev", "commit": "<sha-de-40-caracteres-executado>" },
  "host_run": { "host": "other", "run_reference": "local-simulation-001" }
}
```

## 4. Executar como o host executaria

Exporte as variáveis a partir do seu cofre de segredos, sem colá-las no terminal com eco ([contrato da CLI](./contracts/unattended-cli.md)), e rode:

```bash
uv run ./scripts/unattended_cli.py \
  --mode unattended \
  --config "<sandbox>/config/pipeline-guard.yaml" \
  --project-root "<sandbox>" \
  --output-dir ./out \
  --authorization-state-dir "<durable-host-state-dir>" \
  ./failure-context.json
```

## 5. Conferir o resultado

| Verificação | Esperado |
|---|---|
| Código de saída | `3` com a correção desabilitada; `0` depois de habilitada |
| `out/result.json` | Válido contra `unattended_run_result_v1`, com `decisions[]` preenchido |
| Saída padrão | Resumo sem segredos; com a correção habilitada, inclui o link da draft PR |
| Repositório de sandbox | Uma draft PR para `dev`, nunca mergeada |
| Checkout do sandbox | Sem alteração; a correção foi feita no workspace isolado |
| Diretório de estado | Marcador da autorização consumida persistido fora do sandbox e não exposto ao agente de autoria |
| Segunda execução com o mesmo contexto | Código `0` com `draft_pr.reused: true`, mesma branch determinística e nenhuma chamada de modelo |
| Contexto sem `code_version.commit` | Código `3` com `reason_code: MISSING_INPUT`, sem branch nem PR |

## 6. Próximos passos

Cada integração de host deve preparar o ambiente, fornecer as identidades de serviço, montar o contexto do incidente e chamar o entrypoint somente quando o pipeline falhar; este quickstart descreve o contrato comum com o host.
