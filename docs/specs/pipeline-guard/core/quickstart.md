# Quickstart: pipeline-guard

**Spec**: [spec.md](./spec.md)

Guia mínimo do modo interativo local, para um mantenedor de pipeline validar o V1 (diagnóstico) em seu próprio projeto — sem depender de Airflow, CI ou Slack.

## 1. Instalar o plugin

Instale o Pipeline Guard pelo marketplace configurado no seu ambiente.

## 2. Materializar a configuração do projeto

Rode o setup do plugin no projeto consumidor e escolha um preset:

- `generic` — quando o adapter de pipeline ainda não é dbt.
- `dbt` — para um projeto com `dbt_project.yml` (ex.: o piloto Delivery Dashboard).

O setup materializa `pipeline-guard.config.yaml` (nome ilustrativo) com `schema_version`, o adapter escolhido, o caminho do projeto dbt (`pipeline.root`), o repositório (`repository.provider`/`url`/`default_branch`), os evidence providers habilitados e os limites de segurança — sem nenhuma credencial literal.

## 3. Validar a configuração

Rode `validate-config`. Erros apontam arquivo, campo e a correção esperada — nenhuma execução prossegue com configuração inválida ou com um adapter fora do registry allowlisted.

## 4. Rodar `diagnose` localmente com um incidente de exemplo

Monte um `incident_input_v1` mínimo (ver [contracts/incident_input_v1.schema.json](./contracts/incident_input_v1.schema.json)) apontando para uma execução dbt que falhou (ex.: `run_results.json` de um `dbt build` com teste quebrado) e invoque o modo `diagnose`.

Saída esperada:

- Um `pipeline_diagnosis_v1` (ver schema) com `failure_family`, `cause_status` e ao menos uma citação em `evidence_citations` apontando para um `evidence_id` real.
- Um `eligibility_decision_v1` com a decisão, a regra e a versão da política aplicadas.
- `report.md` e o JSON correspondente, sem qualquer formatação de canal (nenhuma menção a Slack).

## 5. Confirmar que nada além de diagnóstico aconteceu

Verifique que o repositório do projeto não tem diff, branch nova, commit ou PR — o V1 é somente leitura por definição (FR-014, FR-018).

## 6. (Opcional) Repetir com um segundo projeto

Repita os passos 2–5 em um segundo projeto (ainda que fictício) sem alterar nenhum arquivo do plugin — esse é justamente o critério de sucesso SC-002 do `spec.md` e parte do exit gate do V1.

## Próximos passos

O modo `remediate` (V2) exige `approval_input_v1` válido e vinculado a um diagnóstico elegível; ele não é coberto por este quickstart. Consulte o [schema de aprovação](../../../../contracts/schema/approval_input_v1.schema.json) e a [skill remediate](../../../../skills/remediate/SKILL.md).
