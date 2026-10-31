# Especificação de Feature: pipeline-guard — Diagnóstico e Correção Assistida de Pipelines


**Criado em**: 2026-09-25
**Status**: Rascunho


## Cenários de Usuário & Testes *(obrigatório)*

### User Story 1 - Instalação e configuração do plugin por projeto (Prioridade: P1)

Como mantenedor de um pipeline, quero instalar o `pipeline-guard` pelo marketplace e materializar sua configuração a partir de um preset (genérico ou dbt), para usar o plugin em meu projeto sem editar o código do plugin — seguindo o mesmo padrão de instalação e manifesto já usado pelos demais plugins deste repositório (ex.: plugin de auditoria, plugin de gerenciamento de projetos).

**Por que esta prioridade**: sem instalação e configuração validada, nenhuma outra capacidade do plugin (diagnóstico ou remediação) pode ser exercitada com segurança em um projeto real. É a base sobre a qual as demais histórias dependem.

**Teste Independente**: validado quando um projeto novo materializa configuração a partir de um preset, `validate-config` aprova ou recusa com mensagem acionável, e nenhuma credencial literal aparece no arquivo versionado.

**Cenários de Aceite**:

1. **Dado** um projeto sem configuração prévia, **Quando** o setup é executado escolhendo um preset (genérico ou dbt), **Então** uma configuração com `schema_version` é criada no projeto consumidor, pronta para versionamento sem segredos.
2. **Dado** uma configuração com um campo obrigatório ausente ou uma chave desconhecida, **Quando** `validate-config` é executado, **Então** o sistema aponta arquivo, campo e a correção esperada, sem aplicar nenhuma ação.
3. **Dado** um projeto que não possui configuração instalada, **Quando** o plugin é invocado para esse projeto, **Então** o sistema recusa a execução em vez de aplicar um conjunto de permissões padrão.
4. **Dado** uma configuração que referencia um adapter não instalado ou não presente no registry permitido, **Quando** a configuração é validada, **Então** o sistema recusa com mensagem acionável.

---

### User Story 2 - Diagnóstico automatizado de falha de pipeline (Prioridade: P2)

Como mantenedor de um pipeline, quando um pipeline (iniciando pelo adapter dbt) falha, quero que o plugin colete apenas as evidências configuradas e produza um diagnóstico rastreável até a evidência, para entender a causa sem reconstruir manualmente o contexto (logs, artefatos, código, linhagem).

**Por que esta prioridade**: é o valor central do V1 e o motivo de existir do produto — transformar uma falha em uma explicação confiável e citável.

**Teste Independente**: validado submetendo um incidente normalizado com evidências fixas de um projeto dbt de teste e conferindo que o diagnóstico resultante cita `evidence_id`s existentes e classifica a causa corretamente (ou retorna `UNKNOWN` quando a evidência é insuficiente).

**Cenários de Aceite**:

1. **Dado** um incidente reportado por qualquer origem (orquestrador, CI ou execução local), **Quando** o plugin recebe a entrada, **Então** ele valida a entrada normalizada antes de acessar qualquer evidência, independentemente de a origem ser Airflow, Databricks ou outra.
2. **Dado** um projeto com o adapter dbt configurado, **Quando** uma falha ocorre, **Então** o plugin interpreta os artefatos disponíveis (resultados de execução, manifesto, modelos, testes, linhagem) sem exigir todas as fontes de evidência possíveis.
3. **Dado** uma evidência coletada, **Quando** o diagnóstico é produzido, **Então** cada afirmação relevante cita uma evidência existente e identificável, e hipótese é distinguida de causa confirmada.
4. **Dado** evidência insuficiente, ambígua, ou o orçamento de investigação esgotado, **Quando** o diagnóstico é gerado, **Então** o sistema retorna causa desconhecida e interrompe de forma segura, em vez de arriscar um palpite.
5. **Dado** um diagnóstico produzido, **Quando** ele é inspecionado, **Então** ele não contém nenhuma escrita em arquivos do projeto, no Git remoto ou no warehouse — o diagnóstico é somente leitura.

---

### User Story 3 - Decisão de elegibilidade e relatório neutro de canal (Prioridade: P3)

Como orquestrador ou aprovador, quero uma decisão determinística sobre se uma falha diagnosticada pode ser considerada para correção automática de código, acompanhada de um relatório neutro de canal, para decidir o próximo passo sem que o modelo tome essa decisão por mim.

**Por que esta prioridade**: fecha o ciclo do V1 (diagnosticar → decidir → relatar) e é o gate de segurança que impede que causas de dado, regra de negócio, acesso ou infraestrutura sigam para remediação de código.

**Teste Independente**: validado alimentando o gate com diagnósticos de diferentes `failure_family` e confirmando que apenas causas de código elegíveis recebem a decisão de elegibilidade, com relatório em JSON e Markdown gerado em ambos os casos.

**Cenários de Aceite**:

1. **Dado** um diagnóstico validado com causa em código, **Quando** o gate avalia o caso, **Então** a decisão registra a regra aplicada, a versão da política e o motivo.
2. **Dado** um diagnóstico cuja causa é dado, regra de negócio, acesso, infraestrutura ou desconhecida, **Quando** o gate avalia o caso, **Então** a decisão nunca é elegibilidade para remediação de código — apenas relatório ou escalonamento.
3. **Dado** qualquer decisão do gate, **Quando** o relatório é produzido, **Então** ele é entregue em JSON e Markdown sem formatação específica de um canal (ex.: Slack), deixando a publicação a cargo do host.
4. **Dado** a execução completa do V1, **Quando** o resultado é inspecionado, **Então** nenhum diff, branch, commit ou PR foi criado.

---

### User Story 4 - Correção assistida aprovada com testes e draft PR (Prioridade: P4)

Como aprovador técnico, depois de confirmar explicitamente em uma sessão interativa ou aprovar por um canal externo autenticado uma falha diagnosticada e elegível, quero que o plugin prepare um workspace isolado, aplique uma correção restrita aos caminhos permitidos, execute os comandos de validação declarados e abra uma draft PR, para revisar a correção pelo fluxo Git normal sem que o plugin faça merge ou deploy.

**Por que esta prioridade**: é o valor incremental do V2 — só faz sentido depois que o V1 (diagnóstico e gate) estiver confiável, e depende de uma aprovação verificável, registrada localmente após confirmação explícita no modo interativo ou autenticada pelo adapter do host no modo orquestrado.

**Teste Independente**: validado aprovando um diagnóstico elegível de um caso de teste e confirmando que o plugin produz uma branch, um commit, um patch dentro dos caminhos permitidos, resultados de teste e uma draft PR — nunca um merge.

**Cenários de Aceite**:

1. **Dado** uma aprovação recebida, **Quando** o plugin inicia a remediação, **Então** ele verifica que a aprovação está vinculada ao mesmo incidente, ao hash do diagnóstico avaliado e ao commit-base registrado, recusando em caso de divergência ou expiração.
2. **Dado** uma aprovação válida, **Quando** o workspace é preparado, **Então** um clone ou worktree isolado é criado em uma branch descartável, sem afetar o checkout principal do mantenedor.
3. **Dado** um workspace preparado, **Quando** a correção é aplicada, **Então** apenas caminhos, extensões e volume de alteração permitidos pela configuração são tocados, e nenhuma alteração de dado ou regra de negócio é realizada.
4. **Dado** uma correção aplicada, **Quando** os comandos de validação declarados na configuração são executados, **Então** o resultado (status, duração, referência de log) é registrado, e uma falha ou ausência de teste impede a publicação automática, salvo política explícita em contrário.
5. **Dado** uma correção validada, **Quando** a PR é publicada, **Então** ela é sempre criada como draft, com descrição contendo incidente, diagnóstico, evidências, arquivos alterados e testes — e o plugin nunca aprova, mergeia ou faz deploy.

---

### Casos de Borda

- O que acontece quando um incidente referencia um projeto ou perfil sem configuração correspondente? O plugin recusa a execução com uma mensagem acionável, sem aplicar um conjunto de permissões padrão.
- Como o sistema trata a indisponibilidade de uma fonte de evidência configurada (ex.: timeout de consulta)? O diagnóstico registra a limitação e reduz a confiança (ou retorna causa desconhecida) em vez de inferir sem base.
- O que acontece quando a mesma falha é reportada mais de uma vez (nova tentativa do host)? O plugin trata a repetição de forma idempotente pelo mesmo identificador de incidente/execução, sem duplicar diagnósticos, branches ou PRs.
- Como o sistema reage quando a aprovação chega depois que o diagnóstico ou o commit-base mudou? A execução de remediação é bloqueada até uma nova aprovação vinculada ao estado atual.
- O que acontece quando a configuração declara um adapter, provedor de evidência ou provedor Git que não está instalado ou permitido? A validação de configuração falha de forma explícita, sem resolver silenciosamente para outro adapter.
- Como o sistema trata segredos ou dados sensíveis presentes em logs ou artefatos coletados? Eles são mascarados antes de qualquer envio ao modelo e antes de qualquer persistência.

## Requisitos *(obrigatório)*

### Requisitos Funcionais

- **FR-001**: O sistema DEVE ser instalável como um plugin independente pelo marketplace, seguindo o mesmo padrão de manifesto e estrutura (agente(s) especialista(s) e skills de domínio) usado pelos demais plugins deste repositório.
- **FR-002**: O sistema DEVE permitir que um projeto consumidor materialize sua configuração a partir de um preset (genérico e dbt) sem exigir alteração do código do plugin.
- **FR-003**: O sistema DEVE validar toda configuração de projeto contra um esquema versionado, recusando chaves desconhecidas e exigindo descrições acionáveis para cada campo obrigatório.
- **FR-004**: O sistema DEVE aceitar apenas referências a credenciais (nomes de variável ou segredo), nunca valores literais, na configuração versionável.
- **FR-005**: O sistema DEVE informar arquivo, campo e correção esperada quando a validação de configuração falhar.
- **FR-006**: O sistema NÃO DEVE aplicar um conjunto de permissões padrão a um projeto sem configuração validada.
- **FR-007**: O sistema DEVE aceitar um contrato de entrada de incidente normalizado, independente da origem (orquestrador, CI, execução local), contendo identificação do pipeline, execução, etapa falha, horário e resumo do erro.
- **FR-008**: O sistema DEVE validar o contrato de entrada do incidente antes de acessar qualquer fonte de evidência.
- **FR-009**: O sistema DEVE coletar evidências apenas das fontes explicitamente configuradas para o projeto (logs, metadados, consulta somente leitura, artefatos ou links), sem tornar nenhuma fonte obrigatória por padrão.
- **FR-010**: O sistema DEVE interpretar, para o adapter dbt, os artefatos de execução disponíveis (resultados, manifesto, SQL compilado, modelos, testes e linhagem) quando presentes.
- **FR-011**: O sistema DEVE atribuir a cada evidência coletada um identificador, origem, hash e política de retenção.
- **FR-012**: O sistema DEVE produzir um diagnóstico que distingue causa confirmada de hipótese e cita evidência existente para cada afirmação relevante.
- **FR-013**: O sistema DEVE retornar causa desconhecida e interromper com segurança quando a evidência for insuficiente, a citação falhar, ou o orçamento de investigação se esgotar.
- **FR-014**: O sistema NÃO DEVE realizar nenhuma escrita em arquivos do projeto, Git remoto ou warehouse durante o diagnóstico.
- **FR-015**: O sistema DEVE decidir de forma determinística (sem depender do julgamento do modelo) se um diagnóstico é elegível para remediação de código, report-only ou escalonamento, registrando a regra, a versão da política e o motivo aplicados.
- **FR-016**: O sistema DEVE tratar causas de dado, regra de negócio, configuração, dependência, infraestrutura, acesso, sistema externo ou desconhecida como não elegíveis à remediação automática de código.
- **FR-017**: O sistema DEVE produzir um relatório em JSON e em Markdown, neutro quanto ao canal de publicação, deixando a decisão de onde publicar a cargo do host.
- **FR-018**: O sistema NÃO DEVE manter processo, sessão ou computação viva aguardando uma decisão humana; cada chamada valida a entrada, executa sua responsabilidade e termina.
- **FR-019**: O sistema DEVE aceitar uma aprovação verificável vinculada ao incidente, ao hash do diagnóstico avaliado e ao commit-base, recusando a execução quando o vínculo estiver ausente, expirado ou divergente. No modo interativo, a confirmação explícita da pessoa é registrada como aprovação local; no modo orquestrado, o adapter do host valida assinatura ou identidade antes de encaminhar a aprovação.
- **FR-020**: O sistema DEVE preparar um workspace isolado e descartável (clone ou worktree) antes de qualquer edição, sem afetar o checkout principal do mantenedor.
- **FR-021**: O sistema DEVE restringir a edição de código, durante a remediação, aos caminhos, extensões e limites de volume declarados na configuração do projeto, sem permitir alteração de dado ou regra de negócio.
- **FR-022**: O sistema DEVE executar somente os comandos de validação declarados na configuração do projeto, registrando status, duração e referência de log de cada execução.
- **FR-023**: O sistema DEVE impedir a publicação automática de uma correção quando a validação falhar, expirar ou estiver ausente, exceto quando uma política explícita do projeto permitir PR não validada.
- **FR-024**: O sistema DEVE publicar toda correção aprovada como uma pull request em modo rascunho (draft), contendo incidente, diagnóstico, evidências, arquivos alterados, testes e limitações — nunca realizando merge ou deploy.
- **FR-025**: O sistema DEVE mascarar segredos e dados sensíveis antes de qualquer envio a um modelo e antes de qualquer persistência de log ou artefato.
- **FR-026**: O sistema DEVE tratar cada chamada (diagnóstico ou remediação) de forma idempotente em relação ao identificador do incidente/execução, evitando diagnósticos, branches ou PRs duplicados em novas tentativas.

### Entidades-Chave

- **Configuração do Projeto**: dados validados por esquema que descrevem adapter de pipeline, fontes de evidência, comandos de validação, caminhos editáveis, limites de segurança e armazenamento de execução de um projeto consumidor específico.
- **Incidente**: representação normalizada de uma falha de pipeline reportada ao plugin, com origem, ambiente, identificação da execução falha e artefatos referenciados.
- **Evidência**: unidade de informação coletada por um provedor configurado (log, artefato, metadado, consulta ou link), com identificador, origem, hash e retenção próprios.
- **Diagnóstico**: explicação estruturada da causa de uma falha, com nível de confiança, status da causa, citações de evidência, ações recomendadas e limitações.
- **Decisão de Elegibilidade**: veredito determinístico sobre se um diagnóstico pode avançar para remediação de código, report-only ou escalonamento, com a regra e a política aplicadas.
- **Aprovação**: registro que autoriza a remediação de um incidente específico, vinculado ao diagnóstico avaliado e ao commit-base; nasce de confirmação explícita local no modo interativo ou de identidade/assinatura validada pelo host no modo orquestrado.
- **Resultado de Remediação**: registro final de uma execução de correção, incluindo branch, commit, arquivos alterados, testes executados e referência da draft PR.
- **Adapter de Pipeline**: extensão que reconhece a estrutura e os artefatos de uma tecnologia de pipeline específica (o dbt no V1) e fornece detalhes de domínio ao diagnóstico.

## Critérios de Sucesso *(obrigatório)*

### Resultados Mensuráveis

- **SC-001**: O tempo entre o recebimento de uma falha normalizada e a entrega de um diagnóstico acionável (relatório) é reduzido para minutos, eliminando a reconstrução manual de contexto entre ferramentas.
- **SC-002**: Um segundo projeto consumidor pode ser configurado e passa a receber diagnósticos sem qualquer alteração no código do plugin.
- **SC-003**: Ao menos 10 de 12 casos de teste de referência do adapter dbt são classificados corretamente, com toda afirmação relevante sustentada por evidência citável.
- **SC-004**: 100% dos casos cuja causa é dado, regra de negócio, acesso ou infraestrutura são roteados com segurança para relatório ou escalonamento, nunca para remediação automática de código.
- **SC-005**: 100% das correções publicadas chegam como pull request em modo rascunho, revisável por uma pessoa, sem nenhum merge ou deploy automático.
- **SC-006**: Zero ocorrências de segredo literal exposto em configuração versionada, logs persistidos ou conteúdo enviado ao modelo.
- **SC-007**: Toda remediação executada possui uma aprovação verificável vinculada ao diagnóstico e ao commit-base correspondentes, sem exceção; aprovação local só é aceita no fluxo interativo, e aprovação orquestrada só é aceita depois da autenticação pelo adapter do host.
- **SC-008**: Percentual de configurações de projeto validadas com sucesso já na primeira tentativa aumenta de forma mensurável em relação ao processo manual anterior.

## Suposições

- O `pipeline-guard` é distribuído como um plugin independente do marketplace, seguindo o mesmo padrão de manifesto (`plugin.json`) e estrutura (`agents/`, `skills/`) já usado por plugins como plugin de auditoria e plugin de gerenciamento de projetos deste repositório, respeitando a regra de plugins domain-scoped da constituição do repositório.
- dbt é assumido como o primeiro `PipelineAdapter` e o Delivery Dashboard como o primeiro perfil validado; nenhum dos dois limita adapters ou projetos futuros — novos adapters seguem o mesmo contrato e a mesma suíte de validação.
- Um esquema de configuração validado por preset (nos moldes do padrão de configuração já usado no plugin de Project Management) é suficiente para instalar o plugin em um novo projeto sem tocar no núcleo.
- GitHub é assumido como o primeiro provedor Git operacional para a publicação de draft PRs, mas o contrato permanece neutro a provedor, reaproveitando o padrão já usado pelos plugins de gestão de PR deste repositório.
- Slack, Teams, e-mail e demais canais externos de notificação/aprovação permanecem fora do plugin; sua integração, autenticação e espera humana são responsabilidade do host orquestrador. O plugin pode registrar diretamente apenas a confirmação local explícita coletada em uma sessão interativa.
- O plugin não mantém processo ou computação viva aguardando decisão humana; cada chamada é autocontida, valida sua entrada, executa uma responsabilidade delimitada e termina.
