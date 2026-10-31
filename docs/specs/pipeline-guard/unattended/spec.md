# Especificação de Feature: pipeline-guard — Execução Não Assistida em Ambientes Isolados (Databricks, Bitbucket Pipelines e Airflow)


**Criado em**: 2026-10-26  
**Status**: Rascunho  

**Spec base**: [especificação principal](../core/spec.md) — esta spec reutiliza os fluxos de diagnóstico (V1) e de correção com draft PR (V2) sem alterar a especificação principal (ver [Relação com a especificação principal](#relação-com-a-especificação-principal)).  
**Referências**: [especificação principal](../core/spec.md) e [ADR de autorização](../../../adrs/pipeline-guard-policy-authorization.md).

## Clarifications

### Session 2026-10-26

- Q: Como a correção não assistida é autorizada sem uma pessoa presente? → A: Por política versionada do projeto, habilitada explicitamente. Depois que a decisão de elegibilidade aprova o diagnóstico, a execução registra uma autorização verificável em nome da identidade de serviço, autenticada pelo host; o portão humano passa a ser a revisão e o merge da draft PR.
- Q: Até onde Bitbucket Pipelines e Airflow são validados nesta entrega? → A: Databricks ponta a ponta, com o pipeline de referência; Bitbucket Pipelines e Airflow com uma execução de fumaça (falha, acionamento, execução não interativa e desfecho) antes da publicação dos guias.

## Cenários de Usuário & Testes *(obrigatório)*

### User Story 1 - Execução ponta a ponta sem interação humana (Prioridade: P1)

Como engenheiro(a) de plataforma, quero invocar o Pipeline Guard em modo não interativo, entregando apenas o contexto da falha e a configuração versionada do projeto, para que ele execute o fluxo completo — diagnóstico, decisão de elegibilidade, correção, validação e abertura da draft PR — sem fazer nenhuma pergunta e sem depender da conta, da máquina ou da sessão de uma pessoa.

Hoje o fluxo só funciona localmente e de forma interativa: a pessoa conduz uma entrevista (informa, por exemplo, em qual PR ou commit a falha ocorreu e confirma cada etapa) usando as próprias credenciais e permissões. Dentro de um pipeline não há ninguém para responder.

**Por que esta prioridade**: é a base das demais histórias. Sem um modo que conclua sozinho, nenhum host (Databricks, Bitbucket Pipelines ou Airflow) consegue acionar o Pipeline Guard. Parte dos fluxos V1 e V2 já definidos na especificação principal.

**Teste Independente**: validada quando, em um ambiente limpo e isolado (sem sessão, histórico ou credencial pessoal), uma única invocação com um incidente de referência de causa em código produz uma draft PR com a correção — ou um resultado estruturado de parada segura — sem nenhum ponto de espera por resposta humana.

**Cenários de Aceite**:

1. **Dado** um incidente com causa em código elegível e um projeto que habilita a correção não assistida, **Quando** o Pipeline Guard é invocado em modo não interativo, **Então** ele conclui diagnóstico, correção, validação e publicação da draft PR sem solicitar nenhuma entrada humana.
2. **Dado** que uma informação crítica que o modo interativo perguntaria à pessoa (ex.: commit em que a falha ocorreu, repositório ou branch de destino) não está na invocação nem na configuração, **Quando** a execução precisa dela, **Então** a execução para com segurança, sem alterar nada, e informa exatamente qual informação faltou — nunca adivinha o valor.
3. **Dado** uma decisão de baixo risco para a qual o modo interativo oferece uma opção recomendada, **Quando** ela ocorre no modo não interativo, **Então** a opção recomendada documentada é aplicada e registrada no resultado.
4. **Dado** um diagnóstico com causa desconhecida ou não elegível para correção de código (dado, acesso, infraestrutura, regra de negócio), **Quando** a execução não assistida avalia o caso, **Então** ela entrega somente o relatório, sem criar branch, commit ou PR.
5. **Dado** um projeto que não habilita a correção não assistida, **Quando** a execução não assistida diagnostica uma causa elegível, **Então** ela entrega diagnóstico, decisão de elegibilidade e relatório, sem criar branch, commit ou PR.
6. **Dado** o mesmo incidente executado nos modos interativo e não interativo, **Quando** os resultados são comparados, **Então** diagnóstico, decisão de elegibilidade, controles de segurança e conteúdo da PR seguem os mesmos contratos e regras.
7. **Dado** qualquer execução não assistida, **Quando** ela termina, **Então** o host recebe um resultado legível por máquina, um resumo legível por pessoas e um sinal de término que distingue "draft PR publicada", "somente relatório", "bloqueado", "limite atingido" e "erro".
8. **Dado** uma aprovação local (confirmação de uma pessoa em sessão interativa) apresentada a uma execução não interativa, **Quando** a execução a avalia, **Então** ela é recusada antes de qualquer alteração.
9. **Dado** um diagnóstico elegível em um projeto que habilita a correção não assistida, **Quando** a correção começa, **Então** já existe uma autorização por política registrada e verificável, vinculada ao incidente, ao diagnóstico avaliado e ao commit-base; sem ela, nada é alterado.

---

### User Story 2 - Acionamento automático na falha de um pipeline Databricks, com identidade de serviço (Prioridade: P2)

Como mantenedor(a) de um pipeline de dados, quero que, quando uma etapa falhar fora do horário de trabalho, o Pipeline Guard seja acionado automaticamente no próprio ambiente do pipeline, com uma identidade de serviço configurada para isso — e não com a minha conta —, para que, ao começar o dia, eu já encontre o diagnóstico e uma draft PR com a correção proposta, restando apenas revisar e fazer o merge.

Fluxo esperado: a etapa 1 conclui; a etapa 2 falha; as etapas dependentes não executam; o Pipeline Guard é acionado; uma draft PR é aberta; a pessoa revisa e faz o merge; a próxima execução do pipeline já roda com a correção.

**Por que esta prioridade**: permite validar a execução não interativa em um cenário de pipeline de dados com necessidade de recuperação rápida. Databricks é o primeiro host desta entrega. Depende da User Story 1.

**Teste Independente**: validada pelo pipeline de referência no sandbox (Databricks + repositório Bitbucket, com dados sintéticos): uma falha de código introduzida de propósito (ex.: teste dbt com a condição invertida) aciona o Pipeline Guard sem intervenção, uma draft PR é aberta com a correção e, após o merge, a execução seguinte do pipeline conclui com sucesso.

**Cenários de Aceite**:

1. **Dado** um pipeline com o Pipeline Guard configurado, **Quando** todas as etapas concluem com sucesso, **Então** o Pipeline Guard não é executado e não consome chamadas de modelo.
2. **Dado** um pipeline com o Pipeline Guard configurado, **Quando** uma etapa falha, **Então** as etapas dependentes não executam, o Pipeline Guard é acionado com o contexto da execução que falhou e a execução do pipeline continua registrada como falha.
3. **Dado** a execução acionada pela falha, **Quando** o Pipeline Guard lê a execução, os artefatos e o repositório, **Então** ele usa exclusivamente as identidades de serviço configuradas, com as permissões mínimas documentadas, sem nenhuma credencial pessoal.
4. **Dado** uma draft PR aberta pelo reparo, **Quando** a pessoa responsável abre o registro da execução que falhou, **Então** encontra o diagnóstico resumido, o link direto para a PR e os resultados completos da execução retidos pelo host.
5. **Dado** que a correção foi revisada e incorporada, **Quando** o pipeline executa novamente, **Então** a execução usa a versão corrigida; se falhar de novo, o Pipeline Guard é acionado como um novo incidente.
6. **Dado** uma falha do próprio Pipeline Guard (ex.: modelo indisponível, configuração inválida, credencial ausente), **Quando** ela ocorre, **Então** o resultado registra a causa de forma clara, não inicia novos efeitos após a falha e informa qualquer branch que já tenha sido publicada, enquanto a falha original do pipeline continua visível.
7. **Dado** uma execução que falhou com o código de uma branch específica, **Quando** a draft PR é aberta, **Então** ela tem como destino essa branch, salvo quando a configuração do projeto definir outra.

---

### User Story 3 - Guias de configuração por host, no padrão do PR Reviewer (Prioridade: P3)

Como engenheiro(a) de plataforma de qualquer time, quero um guia passo a passo para configurar a execução não assistida do Pipeline Guard no host em que meu pipeline roda — Databricks, Bitbucket Pipelines ou Airflow —, no mesmo modelo do PR Reviewer configurado em `bitbucket-pipelines.yml`, para adotar o recurso sem depender do time que construiu o plugin.

Como no PR Reviewer, a preparação do ambiente, as credenciais, o acionamento e o prompt pertencem a quem configura; o Pipeline Guard em si é a chamada final. Os guias precisam deixar essa fronteira explícita e oferecer um ponto de partida pronto.

**Por que esta prioridade**: amplia as opções de integração e documentação para diferentes hosts. Depende das histórias 1 e 2, que definem o comportamento e o exemplo de referência.

**Teste Independente**: validada quando uma pessoa que não participou do desenvolvimento, seguindo apenas o guia de um host, configura o acionamento automático em um pipeline de teste e obtém o desfecho esperado (draft PR ou somente relatório) na primeira falha.

**Cenários de Aceite**:

1. **Dado** o guia de um host suportado, **Quando** uma pessoa o segue do início ao fim, **Então** ela prepara o ambiente, configura identidades e segredos, liga o acionamento em caso de falha e obtém o primeiro resultado sem suporte do time do plugin.
2. **Dado** qualquer guia, **Quando** consultado, **Então** ele apresenta um prompt de referência com passos numerados e ordenados e indica o que é responsabilidade do host e o que é do plugin.
3. **Dado** qualquer guia, **Quando** consultado, **Então** ele lista as permissões mínimas de cada identidade de serviço e explica por que o ambiente deve ser isolado e descartável.
4. **Dado** uma execução que terminou bloqueada ou com erro, **Quando** a pessoa consulta a seção de diagnóstico de problemas, **Então** encontra o significado de cada desfecho e a ação corretiva.
5. **Dado** um pipeline de tecnologia sem adapter dedicado, **Quando** a pessoa consulta a documentação, **Então** entende as limitações aplicáveis e como usar o perfil genérico.

---

### Casos de Borda

- **Falha recorrente com PR ainda aberta**: a mesma etapa do mesmo pipeline falha de novo na execução agendada seguinte, sobre o mesmo commit, enquanto a draft PR anterior não foi incorporada. O sistema não abre uma segunda PR; o resultado aponta a PR existente.
- **Várias etapas falham na mesma execução**: o reparo é acionado uma única vez para aquela execução, trata como incidente principal a falha mais a montante e registra as demais no relatório; no máximo uma draft PR é aberta por execução que falhou.
- **Pipelines diferentes falham ao mesmo tempo**: cada execução recebe seu próprio reparo, sem que uma interfira no workspace, na branch ou na PR da outra.
- **Credencial expira durante a execução** (ex.: token de curta duração): a execução para com segurança e não inicia novos efeitos. Se a branch já tiver sido publicada, o resultado registra `pushed_branch`; uma nova execução retoma de forma idempotente, sem duplicar branch ou PR.
- **Limite de tempo ou custo atingido**: a execução encerra com o desfecho "limite atingido", sem publicar alterações, e registra até onde chegou.
- **Correção não passa na validação**: nenhuma PR é publicada (salvo política explícita do projeto que permita PR não validada); o desfecho é "bloqueado", com os detalhes dos testes.
- **Destino da PR recusado pelo repositório**: se o host não aceitar o destino configurado, o resultado informa a recusa e a branch já publicada para revisão manual ou ajuste da configuração.
- **Commit da falha indisponível**: o commit executado pela falha não existe mais no repositório remoto. A execução para com segurança antes de qualquer alteração.
- **Conteúdo malicioso nos dados da falha**: logs, mensagens de erro, artefatos ou código contêm texto que tenta instruir o agente. Esse conteúdo é tratado como dado não confiável e não muda o escopo da execução nem dá acesso a credenciais.
- **Segredos nos registros do host**: além do mascaramento antes do modelo e da persistência, segredos não aparecem em logs do host, resumos, PRs ou artefatos retidos.
- **Pré-requisito ausente no ambiente** (ferramenta de runtime, identidade, credencial de provedor ou segredo referenciado): a execução falha logo no início, antes de qualquer alteração, com mensagem acionável.
- **Falha na própria etapa de reparo ou em execuções da branch de correção**: não aciona o Pipeline Guard novamente (sem recursão).

## Requisitos *(obrigatório)*

### Requisitos Funcionais

**Modo não interativo**

- **FR-001**: O sistema DEVE oferecer um modo não interativo que execute, a partir de uma única invocação, o fluxo completo: validação da configuração e do incidente, diagnóstico, decisão de elegibilidade, preparação de workspace isolado, correção, validação, publicação da draft PR e resultado final.
- **FR-002**: O modo de execução (interativo ou não interativo) DEVE ser declarado explicitamente na invocação. Aprovações locais — a confirmação de uma pessoa em sessão interativa — NÃO DEVEM ser aceitas no modo não interativo.
- **FR-003**: O modo não interativo NÃO DEVE solicitar, aguardar ou depender de resposta humana; toda execução DEVE terminar por conta própria com um desfecho definido.
- **FR-004**: Toda informação que o fluxo interativo obtém perguntando à pessoa DEVE, no modo não interativo, vir da invocação (contexto da falha), da configuração versionada do projeto ou de um padrão documentado.
- **FR-005**: Decisões de baixo risco DEVEM usar a opção recomendada documentada e registrá-la no resultado. Decisões que afetam segurança, escopo da alteração ou destino da publicação NÃO DEVEM ser resolvidas por padrão implícito: sem valor explícito, a execução DEVE parar com segurança e informar o que faltou. A documentação DEVE listar cada ponto de decisão do fluxo interativo e como ele é resolvido no modo não interativo.
- **FR-006**: O modo não interativo DEVE aplicar os mesmos contratos, controles de segurança, política de elegibilidade, limites de alteração e regras de publicação do modo interativo. Nenhum controle pode ser desativado pela ausência de uma pessoa ou pelo modo de permissões configurado no host.
- **FR-007**: As etapas de controle — verificação de autorização, validação do patch, execução dos testes, push e decisão de publicação — DEVEM ser executadas de forma determinística, fora do julgamento do modelo.
- **FR-008**: O sistema DEVE executar correção não assistida somente quando a configuração versionada do projeto a habilitar explicitamente. Sem essa habilitação, a execução não assistida DEVE limitar-se a diagnóstico, decisão de elegibilidade e relatório.
- **FR-009**: Toda correção não assistida DEVE ser autorizada pela política versionada do projeto (FR-008), e somente depois que a decisão determinística de elegibilidade aprovar o diagnóstico. A execução DEVE registrar essa autorização de forma verificável, vinculada ao incidente, ao diagnóstico avaliado e ao commit-base, identificando a identidade de serviço e a execução do host que a originaram; a autorização DEVE ser autenticada como originada do host configurado antes de qualquer alteração. Nesse modo, o portão humano é a revisão e o merge da draft PR.
- **FR-010**: Ao final de toda execução não assistida, o sistema DEVE entregar ao host: (a) um resultado legível por máquina, incluindo a identidade de serviço usada, a referência da execução do host e as decisões automáticas aplicadas; (b) um resumo legível por pessoas, com desfecho, diagnóstico resumido, limitações e, quando houver, o link da draft PR; (c) um sinal de término documentado e distinto para cada desfecho — draft PR publicada, somente relatório, bloqueado, limite atingido e erro operacional.
- **FR-011**: O sistema DEVE respeitar um limite máximo configurável de duração e de consumo de modelo por execução. Ao atingir o limite, DEVE interromper novas operações; quando isso ocorrer depois do push, o resultado DEVE informar a branch já publicada e não tentar criar a PR.

**Ambiente, identidades e acionamento**

- **FR-012**: O sistema DEVE executar em um ambiente isolado e descartável, preparado pelo host a cada execução, sem depender de estado, sessão ou credencial de uma pessoa.
- **FR-013**: O sistema DEVE operar exclusivamente com identidades de serviço (não pessoais) fornecidas pelo host para: ler a execução e os artefatos do pipeline; publicar a branch de correção e a draft PR no repositório; e acessar o endpoint do modelo.
- **FR-014**: As credenciais de escrita (push e publicação de PR) DEVEM ser usadas apenas pelas etapas determinísticas que precisam delas e NÃO DEVEM ficar acessíveis ao agente de correção nem a ferramentas controladas pelo modelo. O conteúdo do pipeline (logs, mensagens de erro, artefatos e código) DEVE ser tratado como dado não confiável.
- **FR-015**: O host DEVE poder entregar o contexto da falha no contrato de incidente normalizado — evoluído de forma retrocompatível quando necessário —, sem campos específicos de um host. O contexto DEVE incluir a identificação do pipeline, da execução e da etapa que falhou, o horário, o resumo do erro, as referências de artefatos e a versão do código (commit) efetivamente executada.
- **FR-016**: A integração documentada de cada host DEVE garantir que: (a) sem falha, o reparo não execute; (b) com falha, as etapas dependentes não executem; (c) a execução do pipeline continue registrada como falha, mesmo quando o reparo publicar uma PR.
- **FR-017**: Uma falha na própria etapa de reparo, ou em uma execução que valida uma branch de correção criada pelo Pipeline Guard, NÃO DEVE acionar o Pipeline Guard novamente.
- **FR-018**: O sistema DEVE permitir que o host indique onde gravar os resultados e artefatos da execução, para retê-los após o descarte do ambiente.
- **FR-019**: Antes de qualquer alteração, o sistema DEVE verificar os pré-requisitos da execução não assistida — configuração válida, identidades e segredos referenciados, credenciais exigidas pelo publisher privado e ferramentas de runtime — e falhar rapidamente com mensagem acionável quando algum faltar. Quando a remediação estiver habilitada, a invocação também DEVE fornecer um diretório de estado persistente do host, fora do repositório e do workspace, capaz de consumir a autorização com criação atômica e uso único entre execuções isoladas.
- **FR-020**: A draft PR DEVE ter como destino a branch cujo código falhou, salvo quando a configuração do projeto definir outra branch de destino.
- **FR-021**: O sistema NÃO DEVE abrir uma nova draft PR quando a mesma etapa do mesmo pipeline falhar novamente sobre o mesmo commit para o qual já exista uma draft PR aberta pelo Pipeline Guard; nesse caso, o resultado DEVE apontar a PR existente.
- **FR-022**: O sistema DEVE abrir no máximo uma draft PR por execução que falhou. Quando várias etapas falham na mesma execução, a falha mais a montante DEVE ser tratada como incidente principal e as demais DEVEM constar no relatório.
- **FR-023**: O sistema NÃO DEVE aprovar ou fazer merge de PR, implantar, reexecutar o pipeline ou alterar dados. A nova execução após o merge segue o agendamento ou o processo normal do pipeline.
- **FR-024**: O sistema DEVE mascarar segredos e dados sensíveis em tudo o que o host registra ou publica: logs da execução, resumo, PR e artefatos retidos.

**Documentação e validação**

- **FR-025**: DEVE existir um guia de configuração para cada host — Databricks, Bitbucket Pipelines e Airflow — cobrindo: pré-requisitos e instalação do plugin e de suas dependências; identidades de serviço e permissões mínimas; gestão de segredos no host; preparação do ambiente isolado; configuração do projeto, incluindo a habilitação da correção não assistida; acionamento somente em caso de falha; montagem do contexto da falha a partir da execução; prompt de referência; retenção de resultados; limites de tempo e custo; interpretação dos desfechos e diagnóstico de problemas; e considerações de segurança. Cada guia só DEVE ser publicado depois de validado no respectivo host: Databricks com o ciclo completo no pipeline de referência (FR-030); Bitbucket Pipelines e Airflow com ao menos uma execução de fumaça em ambiente de teste — falha, acionamento, execução não interativa e desfecho.
- **FR-026**: Cada guia DEVE incluir um exemplo completo e funcional no formato do PR Reviewer em `bitbucket-pipelines.yml`, separando a preparação do ambiente (responsabilidade do host) da chamada ao Pipeline Guard.
- **FR-027**: A documentação DEVE fornecer um prompt de referência para execução não interativa, com passos numerados e ordenados, e deixar explícito que adaptá-lo é responsabilidade de quem configura. As garantias de segurança do sistema NÃO DEVEM depender do conteúdo do prompt.
- **FR-028**: A documentação DEVE indicar, por tecnologia de pipeline, o que é suportado hoje (perfis dbt e genérico) e as limitações para outras tecnologias (ex.: Python, Terraform).
- **FR-029**: A documentação DEVE descrever o fluxo completo — diagnóstico e correção com draft PR — tanto na execução local quanto na não assistida.
- **FR-030**: DEVE existir um pipeline de referência reproduzível, em sandbox com dados sintéticos, que demonstre três caminhos: (a) falha de código → acionamento → draft PR → merge → nova execução bem-sucedida, com no mínimo 5 cenários distintos de falha de código; (b) execução sem falha → reparo não executa; (c) falha não elegível → somente relatório.

### Entidades-Chave

- **Execução não assistida**: uma invocação do Pipeline Guard feita por um host, sem pessoa presente. Tem identidade de serviço, contexto da falha, limites de duração e custo, desfecho e referências aos resultados retidos.
- **Host de execução**: plataforma onde o pipeline e o reparo rodam (Databricks, Bitbucket Pipelines, Airflow). Prepara o ambiente, fornece identidades e segredos, aciona o reparo na falha e define o prompt.
- **Contexto da falha**: o incidente normalizado da especificação principal, estendido de forma retrocompatível com a versão do código (commit) executada e a referência à execução no host.
- **Identidade de serviço**: identidade não pessoal (ex.: service principal, token de repositório) com permissões mínimas por finalidade: ler o pipeline, escrever de forma restrita no repositório e usar o modelo.
- **Política de execução não assistida**: parte da configuração versionada do projeto que habilita ou não a correção não assistida e define destino da PR e limites.
- **Autorização por política**: aprovação emitida no modo não assistido pela política versionada do projeto, em nome da identidade de serviço, vinculada ao incidente, ao diagnóstico avaliado e ao commit-base, com a execução do host como origem rastreável.
- **Desfecho da execução**: classificação final — draft PR publicada, somente relatório, bloqueado, limite atingido ou erro —, com resumo e link para a PR quando houver.
- **Guia de configuração por host**: documentação com pré-requisitos, exemplo funcional, prompt de referência e diagnóstico de problemas para um host.
- **Prompt de referência**: instruções numeradas e ordenadas para a execução não interativa, fornecidas pelo plugin e adaptáveis por quem configura.
- **Pipeline de referência**: pipeline de sandbox com dados sintéticos e falhas introduzidas de propósito, usado para validar o ciclo completo e servir de exemplo.

## Critérios de Sucesso *(obrigatório)*

### Resultados Mensuráveis

- **SC-001**: No pipeline de referência, uma falha de código elegível resulta em draft PR disponível para revisão em até 60 minutos após a falha, sem nenhuma interação humana.
- **SC-002**: 100% das execuções não assistidas terminam sozinhas dentro do limite configurado; nenhuma fica aguardando entrada humana.
- **SC-003**: Em pelo menos 4 de 5 cenários de falha de código do pipeline de referência, a correção proposta, depois de incorporada, faz a execução seguinte do pipeline concluir com sucesso.
- **SC-004**: 100% das falhas não elegíveis (dado, acesso, infraestrutura, regra de negócio ou causa desconhecida) em execuções não assistidas resultam apenas em relatório, sem branch, commit ou PR.
- **SC-005**: Em execuções do pipeline sem falha, o reparo não é executado: zero chamadas de modelo e nenhum custo adicional.
- **SC-006**: 100% das execuções não assistidas usam somente identidades de serviço; zero credenciais pessoais e zero segredos expostos em logs do host, resumos, PRs ou artefatos retidos.
- **SC-007**: Uma pessoa que não participou do desenvolvimento configura a execução não assistida em um novo pipeline seguindo apenas o guia do host, em até 4 horas de trabalho e sem suporte do time do plugin.
- **SC-008**: Falhas repetidas da mesma etapa sobre o mesmo commit geram no máximo uma draft PR aberta.
- **SC-009**: 100% das PRs abertas em modo não assistido são drafts; zero aprovações, merges, deploys ou reexecuções feitos pelo Pipeline Guard.
- **SC-010**: Para falhas de código elegíveis, o trabalho humano entre a descoberta da falha e uma correção pronta para merge cai de horas de investigação manual para o tempo de revisão da PR (meta: menos de 30 minutos).
- **SC-011**: 100% dos guias publicados têm validação registrada no respectivo host: ciclo completo no Databricks e execução de fumaça bem-sucedida em Bitbucket Pipelines e Airflow.
- **SC-012**: 100% das correções não assistidas têm autorização por política registrada e verificável, vinculada ao incidente, ao diagnóstico avaliado e ao commit-base; nenhuma ocorre em projeto que não habilitou o modo.

## Suposições

- Esta spec parte dos fluxos V1 e V2 da especificação principal. As regras de diagnóstico, elegibilidade, limites de alteração e publicação não mudam; muda apenas como o fluxo é acionado, autorizado e operado sem pessoa presente.
- A autorização por política é um tipo de aprovação exclusivo do modo não assistido e não substitui as aprovações da especificação principal (local no fluxo interativo; orquestrada, autenticada pelo host). Nesse modo, o portão humano passa da aprovação antes da alteração para a revisão antes do merge.
- Como no PR Reviewer, a preparação do ambiente (instalação de ferramentas, geração de credenciais de curta duração, variáveis de ambiente), o acionamento e o prompt são responsabilidade de quem configura o host. O plugin fornece contratos, guias, exemplos e prompt de referência.
- Quem configura o host monta o contexto da falha e disponibiliza os artefatos (por exemplo, artefatos dbt), seguindo o guia e o prompt de referência. A coleta nativa de evidências de Databricks ou Airflow não faz parte do plugin.
- O ambiente de execução é isolado, efêmero e dedicado a uma execução. Por isso o modo amplo de permissões da ferramenta, usado no PR Reviewer, é aceitável — desde que os controles do plugin (FR-006, FR-014) continuem ativos.
- Databricks é o primeiro host coberto pela especificação. Bitbucket Pipelines e Airflow são extensões previstas; a coleta nativa de evidências desses hosts fica fora do escopo.
- "Seguir a opção recomendada" vale só para decisões de baixo risco (FR-005). Destino, escopo e autorização da alteração nunca são inferidos.
- O endpoint de modelo é qualquer endpoint compatível com a API da Anthropic — por exemplo, Databricks Model Serving acessado por identidade de serviço, como no PR Reviewer.
- A publicação continua no boundary privado do Pipeline Guard, limitado a localizar, criar, reutilizar ou fechar com segurança as draft PRs do próprio fluxo em Bitbucket e GitHub. O ambiente não precisa instalar outro plugin.
- Bitbucket é o provedor Git do pipeline de referência; o contrato continua neutro a provedor.
- O limite padrão por execução não assistida é de 60 minutos (3.600 segundos, como na demonstração), ajustável pela configuração do projeto.
- O reparo roda no mesmo host do pipeline que falhou.
- O primeiro uso operacional mira pipelines de sandbox e desenvolvimento; habilitar a correção não assistida em pipelines de produção é uma decisão de cada projeto, orientada pelo guia.

## Dependências

- Fluxos de diagnóstico e remediação da especificação principal concluídos e estáveis, incluindo os respectivos exit gates e a publicação standalone de draft PR.
- Sandbox Databricks integrado a um repositório Bitbucket de teste, com as identidades de serviço criadas.
- Publicação idempotente de draft PR no boundary privado do Pipeline Guard (já disponível para Bitbucket e GitHub).
- Endpoint de modelo acessível pelo host com identidade de serviço.
- A autorização por política no modo não assistido é definida no [ADR de autorização](../../../adrs/pipeline-guard-policy-authorization.md); aprovações orquestradas são autenticadas pelo host, e aprovações locais continuam exclusivas do fluxo interativo.

## Fora de Escopo

- SDK ou camada universal de integração com orquestradores (fora do escopo desta fase).
- Integração nativa de coleta de evidências de Databricks ou Airflow dentro do plugin (fora do escopo).
- Novos adapters de tecnologia de pipeline (ex.: Python, Terraform).
- Notificação ou aprovação por canais externos (Slack, Teams, e-mail).
- Merge, deploy ou reexecução automática do pipeline após a correção.
- Execução não assistida do setup do plugin: a materialização da configuração continua interativa e é pré-requisito.
- Acionamento entre hosts diferentes (ex.: falha em um job Databricks acionando uma execução no Bitbucket Pipelines); cada guia cobre o reparo executado no mesmo host do pipeline.
- Pipeline de referência com ciclo completo em Bitbucket Pipelines e Airflow (nesta entrega, apenas validação de fumaça).
- Alterações no `bitbucket-pipelines.yml` deste repositório: os exemplos ficam na documentação do plugin e a validação ocorre em repositórios e ambientes de sandbox.

## Relação com a especificação principal

- Esta especificação detalha a execução não assistida do Pipeline Guard e complementa a especificação principal.

- A especificação principal continua sendo a referência de contratos e guardrails, reutilizados aqui sem alteração. A única exceção é a autorização por política no modo não assistido (FR-009), que deve ser registrada como decisão de arquitetura.
