# Revisão de simplicidade e regressões — 26/10/2026

## 1. Status e resumo

**CHANGES_REQUIRED**.

Escopo: alterações locais, inclusive arquivos novos, em relação a `3057ad0`, com foco em segurança, regressões, duplicação e complexidade para um monitor pessoal. Foram lidos SPEC, CODER e REVIEWER. Esta revisão substitui o relatório anterior e altera somente este arquivo; não corrige a aplicação.

Há correções necessárias que devem ser preservadas: descompressão HTTP, tratamento de erros de transporte, limites de coleta, disponibilidade/moeda explícitas, proteção contra variantes incompatíveis, condições comerciais e recuperação de notificações interrompidas. A arquitetura continua sendo um monólito com SQLite; não houve adição de dependências ou infraestrutura distribuída.

O excesso está principalmente no acúmulo de funcionalidades e estados operacionais, não em uma arquitetura distribuída: diagnóstico persistido, suspensão/rearme de alertas, dois modos de monitoramento e caminhos adicionais de interação. Isso merece redução, mas não justifica reverter o pacote inteiro.

No início da revisão havia 1.178 linhas adicionadas de aplicação/entry point, 859 de testes, 403 de documentação e 75 de scripts, além de 3 no exemplo de ambiente. São adições brutas; não equivalem ao crescimento líquido. `git diff --stat` sozinho não inclui os arquivos novos não rastreados. Parte do volume são testes úteis, não complexidade de produção.

Foram reproduzidos quatro problemas abaixo, apesar dos 210 testes aprovados.

## 2. Validação executada

Ambiente: Windows, `.venv`, Python 3.12.10. Comandos executados na raiz do projeto.

| Verificação | Resultado |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest` | **210 passed**, 7,45 s. |
| `.venv\Scripts\python.exe -m ruff check .` | PASS. |
| `.venv\Scripts\python.exe -m bandit -q -r app main.py` | PASS, saída 0, sem achados. |
| `.venv\Scripts\python.exe -m pip check` | PASS, sem dependências quebradas. |
| `.venv\Scripts\python.exe -m pip_audit --progress-spinner off` | Sem vulnerabilidades conhecidas nas dependências auditadas; o pacote local `price-monitor` não existe no PyPI e foi ignorado pelo auditor. |
| Parser de PowerShell nos dois scripts de `scripts/` | Zero erros de sintaxe; scripts não executados nem instalados. |
| Busca de assinatura de token Telegram em 51 arquivos de código, testes, documentação e configuração de exemplo | Nenhuma ocorrência; pesquisa limitada a essa assinatura, sem imprimir segredos. |
| Probes Python por stdin, com URLs fictícias e SQLite temporário | Reproduziram R01–R04 e a suspensão de alertas descrita em C01. |
| Migração a partir da implementação de banco de `HEAD` | Reproduziu R01 usando o esquema anterior real, sem alterar o banco pessoal. |

Verificação de tipos não está configurada e não foi executada. Nenhuma mensagem foi enviada ao Telegram e nenhuma loja foi acessada nesta revisão.

## 3. Achados bloqueantes confirmados

### R01 — HIGH — Migração pode eliminar o único monitoramento ativo de uma oferta

**Local:** `app/database.py:197–215`; recuperação em `app/database.py:389–403`.

**Problema:** a migração elege a primeira identidade encontrada por ID, independentemente de o produto/listing estar ativo. Uma oferta antiga desativada pode ficar com a chave única; a oferta ativa equivalente vira `active = 0` e `validation_status = 'duplicate'`.

**Reprodução:** usando a classe Database do commit base, cadastrar um ASIN com `?tag=old`, desativá-lo e recadastrar o mesmo ASIN com `?tag=new`. Antes da atualização há um listing ativo. Após `Database.initialize()` atual, há zero listings ativos, embora o segundo produto continue ativo. `/reativar` desse produto também não recupera a oferta, pois só reativa listings `confirmed`.

**Impacto:** a atualização interrompe um monitoramento existente sem ação do usuário. O histórico é preservado, mas novas verificações e alertas deixam de ocorrer.

**Correção mínima:** ao resolver colisões legadas, preservar um representante ativo cujo produto também esteja ativo. Se houver múltiplos representantes ativos com alvos/produtos diferentes, tratar o conflito explicitamente, sem escolher silenciosamente o menor ID. Manter snapshots e IDs históricos. Testar migração e recuperação.

### R02 — HIGH — SKU de uma loja vira restrição global do produto

**Local:** `app/matching.py:160–172`; atributos coletados em `app/stores/structured.py:208–216`; uso em `app/bot.py:441–444,562–569`.

**Problema:** `match_product()` exige igualdade de todos os atributos persistidos, incluindo `sku`. O SKU identifica o catálogo do vendedor/loja e não precisa coincidir entre duas ofertas equivalentes.

**Reprodução:** registrar `Sabao OMO 3L` com atributos `brand=OMO, sku=shop-123`. A oferta de outra loja com o mesmo título e marca, mas `sku=other-456`, retorna `rejected / hard_mismatch_sku`. Se a segunda loja não informar SKU, retorna `ambiguous / saved_attribute_unknown_sku`; os handlers também recusam a anexação.

**Impacto:** o fluxo de acompanhar o mesmo produto em várias lojas pode ser bloqueado por metadados específicos de uma oferta. A nova validação também pode parar o monitoramento quando a loja deixa de publicar seu SKU, sem mudança do produto.

**Correção mínima:** restringir a identidade compartilhada aos atributos comparáveis entre lojas: modelo, capacidade, tamanho, quantidade, condição e outros comprovadamente globais. SKU deve continuar associado ao listing/loja. Não remover as verificações de variante que corrigem defeitos reais.

### R03 — MEDIUM — Números descritivos adicionais são tratados como troca de modelo

**Local:** `app/identity.py:120–123`; `app/matching.py:114–124`.

**Problema:** todos os números fora das unidades reconhecidas entram em uma única string `model_numbers`, comparada por igualdade. Informações adicionais sobre tela, resolução ou rendimento acabam misturadas com o modelo.

**Reprodução:** `Apple iPhone 15 128GB` contra `Apple iPhone 15 128GB Tela 6,1 polegadas` retorna `rejected / hard_mismatch_model`. O conjunto de números do segundo título inclui as dimensões da tela, embora o modelo solicitado continue explicitamente sendo iPhone 15.

**Impacto:** ofertas válidas desaparecem da classificação e mudanças meramente descritivas de título podem interromper a coleta de um produto salvo.

**Correção mínima:** usar evidência de modelo identificável, sem exigir igualdade de todos os números incidentais do título. Quando não for possível identificar a função do número, manter a incerteza em vez de afirmar incompatibilidade. Preservar a rejeição de iPhone 14 versus 15 e os testes de variantes realmente distintas; não criar um catálogo genérico de exceções.

### R04 — MEDIUM — Regex incompleta permite fallback após JSON-LD de produto malformado

**Local:** `app/stores/structured.py:83–96`; fallback em `app/stores/magalu.py:38` e `app/stores/mercadolivre.py:45`.

**Problema:** após falha de JSON, a presença de Product é detectada por regex que só reconhece a representação simples do tipo. O código diferencia formas equivalentes de declarar um produto e pode tratar dados inválidos como ausência de dados estruturados.

**Reprodução:** junto de HTML de fallback com título, preço BRL e disponibilidade explícita, o fragmento malformado `{"@type":"Product","offers":` é recusado. Os fragmentos igualmente malformados `{"@type":["Product"],"offers":` e `{"@type":"https://schema.org/Product","offers":` são aceitos pelo fallback Magalu e retornam R$ 99,90. Não houve acesso externo.

**Impacto:** a garantia de não contornar evidência estruturada inválida depende da grafia de `@type`, permitindo aceitar uma oferta que deveria falhar de maneira conservadora.

**Correção mínima:** adotar uma política única e conservadora para JSON-LD inválido. O caminho mais simples é recusar dados estruturados malformados antes de considerar fallback, com testes das representações em lista e URL. Não tentar implementar um parser de JSON com regex.

## 4. Achados não bloqueantes e cortes recomendados

### C01 — LOW — Entrega de alertas ganhou um protocolo operacional maior que o necessário

**Local:** `app/alerts.py:40–119`; `app/database.py:795–870`; `app/bot.py:699–716,796`.

A pendência persistida é justificada pelos defeitos anteriores de cancelamento/reentrada. Já os estados `failed/cancelled`, contagem de tentativas, suspensão na terceira falha, rearme global por `/check` e métodos SQL separados ampliam bastante a manutenção. O comportamento é intencional e documentado, não uma falha desconhecida: três erros de envio seguidos de recuperação deixam zero alertas entregues e um registro `failed` até intervenção ou nova condição elegível.

**Recomendação:** manter confirmação de envio e pendência recuperável, mas simplificar a política. Uma tentativa por ciclo, sem laço interno de retry, reaproveita o intervalo já existente; a observação atual decide se a pendência ainda faz sentido. Unificar as atualizações de preço/referência/tentativa em uma operação transacional. Avaliar o custo da suspensão manual antes de mantê-la; não voltar a registrar alertas como enviados antes da confirmação.

### C02 — LOW — Estado de diagnóstico duplica informação e aparenta controlar o scheduler

**Local:** `app/database.py:179–187,479–517`; `app/models.py:73–78`; `app/bot.py:699–758`.

`next_check_at` é gravado após cada coleta, mas não controla quando o scheduler executa. Um `/check` entre dois ciclos muda a estimativa persistida sem mudar o próximo disparo real. Contadores cumulativos, proporção de frete conhecido, aviso de degradação/recuperação e detalhes extensos de `/status` são funcionalidades operacionais adicionais, não correções de segurança.

**Recomendação:** manter última tentativa/sucesso/erro e um resumo curto. Se exibir a próxima execução, usar o scheduler como fonte de verdade, sem persistir uma segunda agenda. Contadores cumulativos e avisos de saúde são candidatos a corte se não forem usados; manter `last_error` e a proteção que impede apresentar preço antigo como atual. Na limpeza, não apagar colunas de um banco pessoal só para diminuir o esquema.

### C03 — INFO — Segundo modo de monitoramento é uma decisão de produto, não hardening

**Local:** `app/alerts.py:91–145`; `app/models.py:54–55`; `app/bot.py:317–371`; `app/database.py:248–322`.

O modo `price_drop` adiciona baseline próprio por listing, ramificações de alerta, mensagens, estado no banco e uma segunda semântica para `target_price`. A SPEC permite alertas adicionais, mas só exige o de preço-alvo. `docs/IMPLEMENTACAO.md` informa que o cadastro sem alvo e a inicialização vieram de trabalho paralelo; não é possível atribuir todo esse escopo a um único agente.

**Recomendação:** se queda sem alvo não fizer parte do uso real, conservar apenas o fluxo de alvo. Se fizer, preservar o recurso e evitar usar uma coluna chamada `target_price` para representar uma referência de outro significado. A revisão não remove esse comportamento nem presume que nunca foi solicitado.

### C04 — LOW — Código sem consumidor e adaptação de handlers podem ser enxugados

**Local:** `app/database.py:871`; `app/bot.py:79,532–535`; `app/matching.py:23–24`.

`delete_alert()` ficou sem chamadas após a nova política. `format_search_results()` só é chamado por teste, sem caminho de execução da aplicação. `_title_attributes()` apenas repassa a chamada a `explicit_attributes()`. Os dois últimos já existiam na base; são sobras a limpar, não prova de excesso novo. A seleção guiada fabrica um `SimpleNamespace` para chamar outro handler e repete o parsing monetário.

**Recomendação:** remover código comprovadamente sem consumidor e o wrapper sem função. Compartilhar a operação de selecionar/registrar entre os dois handlers por parâmetros explícitos, sem criar contexto Telegram artificial. Ajustar os testes para validar os caminhos reais, sem removê-los apenas para reduzir contagem.

### C05 — INFO — Consolidar documentação e preservar comentários que explicam riscos reais

**Local:** `README.md`, `docs/OPERACAO.md`, `docs/IMPLEMENTACAO.md`; comentários em `app/network.py`, `app/stores/http.py` e `app/alerts.py`.

O registro de implementação repete detalhes e resultados já presentes nos documentos de operação/revisão. Pode ser arquivado ou eliminado após consolidar instruções úteis em README/OPERACAO. A operação detalhada tem uso concreto e não precisa ser convertida em comentários de código.

**Recomendação:** remover comentários narrativos ou redundantes durante uma implementação de limpeza. Preservar explicações sobre descompressão, DNS pinado, frete desconhecido, limites de fallback e cancelamento de envio: essas restrições não são óbvias e evitam regressões. Há ainda compatibilidade com doubles de teste preexistente, como o `except TypeError` de markup; revisar os doubles em vez de expandir exceções de produção para acomodá-los.

## 5. Requisitos da SPEC ainda não satisfeitos

- **§§7, 9 e 17:** anexar ofertas equivalentes de várias lojas não é confiável quando seus SKUs diferem ou estão ausentes, conforme R02.
- **§§6.2 e 23:** atualização não preserva a continuidade do monitoramento no cenário legado de R01.
- **§8.3:** a identificação de modelo confunde números incidentais com conflito explícito, conforme R03.
- **§33.1:** a extração/fallback não falha consistentemente diante de Product malformado, conforme R04.
- **§31:** a entrega permanece sem aprovação por causa das regressões confirmadas. Esta revisão não homologa operação real de Amazon, Mercado Livre e Magalu.

## 6. Testes ausentes ou insuficientes

Faltam regressões para: duplicata antiga inativa versus atual ativa em esquema anterior real; anexação do mesmo produto com SKUs diferentes/ausentes; números incidentais no título sem mudança de modelo; e Product malformado com tipos em lista/URL antes de fallback.

Os testes novos de HTTP, autorização, condições comerciais, timeout e recuperação de alertas exercitam requisitos úteis. Não há motivo para removê-los em massa. O teste de três tentativas comprova a política atual; não comprova recuperação automática após a terceira falha.

## 7. Avaliação de segurança

Manter autorização de chat privado, proteção de URLs/DNS/redirects, timeouts, limite de resposta decodificada, Decimal, queries parametrizadas, isolamento das lojas e distinção de frete/desconto desconhecido. Essas proteções têm requisitos concretos na SPEC e não são overengineering para este bot.

Não foram identificadas novas dependências, exposição de token na pesquisa limitada ou bypass de autorização. R04 é um defeito confirmado de validação conservadora, não evidência de SSRF, execução remota ou vazamento de segredo. Ferramentas estáticas aprovadas não substituem esses testes de lógica.

## 8. Ordem sugerida para uma implementação enxuta

1. Corrigir R01–R04 com mudanças locais e testes dos comportamentos reproduzidos.
2. Remover sobras sem consumidor e consolidações triviais de C04/C05.
3. Simplificar entrega de alertas e diagnóstico de C01/C02, preservando recuperação e dados válidos.
4. Tratar a permanência do modo sem alvo de C03 como escolha funcional; não apagá-lo incidentalmente em um hardening.

Não recomendo criar camadas, serviços, framework de comandos, barramento de eventos ou novas dependências para fazer essa limpeza.

## 9. Limitações

Revisão do working tree atual, sem commits separados que permitam atribuir cada mudança ao agente de revisão ou ao trabalho paralelo. Probes não foram incorporados à suíte porque a tarefa é de revisão; a aplicação não foi editada. Banco pessoal, `.env` real, Telegram, lojas ao vivo, instalação de tarefa Windows, piloto prolongado e Python 3.11 não foram usados/validados. A análise de segredos foi limitada à assinatura pesquisada. O relatório anterior descrevia outra etapa e foi substituído, não tratado como evidência de que seus achados antigos continuavam presentes.
