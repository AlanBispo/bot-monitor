# Diagnóstico de acesso a anúncios do Mercado Livre

Testes ao vivo em 09/11/2026, executados no Windows do projeto, com o cliente HTTP
do próprio bot e identificação `price-monitor/0.1 (personal use)`.

## Objetivo e limites

Obter preço atual do anúncio `MLB4749859345`, associado pelo usuário ao produto
de vendedor `MLBU4056230273`, sem cookies de navegador, login automatizado,
camuflagem, CAPTCHA ou evasão de proteções. A associação entre esses dois IDs
foi fornecida pelo usuário; as respostas acessíveis não permitiram confirmá-la.

## Resultados com client_credentials

A emissão de token funcionou. A consulta do próprio aplicativo retornou 200:
aplicativo ativo, com escopos de leitura de publicação/sincronização e sem
escopos `urn:mp:` na resposta. Isso não comprova autorização para cada recurso.

| Consulta | Resultado observado |
| --- | --- |
| `/user-products/MLBU4056230273` | 403, `caller is not allowed to access this user product` |
| `/items/MLB4749859345` | 403, `access_denied` |
| `/items/MLB4749859345/sale_price?context=channel_marketplace` | 403 |
| `/items/MLB4749859345/prices` | 403 |
| `/items/MLB4749859345/description` | 200, descrição de LG Dual Inverter AI Voice Frio 127V, sem preço |
| `/items?ids=MLB4749859345` | HTTP externo 200, mas resultado individual 403 |
| `/items/bulk?ids=MLB4749859345` | HTTP externo 200, mas `status_code: 403` para o anúncio |
| Página pública `/up/MLBU4056230273` | 302 para `/gz/account-verification`, sem JSON-LD na resposta |
| Página pública `produto.mercadolivre.com.br/MLB-4749859345-_JM` | 302 para `/gz/account-verification`, sem JSON-LD na resposta |

Os redirecionamentos para verificação não foram seguidos. Nenhum preço foi
obtido para o anúncio específico.

## Catálogo

As buscas por LG Dual Inverter retornaram 200, mas também trouxeram variantes
e capacidades diferentes das solicitadas. Busca exata por `S3NQ09AA31Q`
retornou zero resultados nesta execução.

O produto `MLB79477691` retornou 200 e contém:

- modelo `S3NQ09AA31Q.EB1GAM1`;
- tensão `127V` e capacidade `9000 BTU`;
- estado `active`, `children_ids: []` e `buy_box_winner: null`;
- pai `MLB79477685`, que está `inactive` e tem apenas esse filho.

Sua rota `/products/MLB79477691/items` retornou 404 com `No winners found`.
Portanto, nesse caso, consultar um produto pai em vez de um filho não explica
a ausência de preço. Não foi possível estabelecer uma associação oficial
entre esse catálogo e o anúncio de vendedor testado.

Outros 12 produtos consultados também retornaram detalhes com 200,
`buy_box_winner: null` e ofertas com 404 / `No winners found`:
`MLB77059312`, `MLB35276616`, `MLB21013991`, `MLB15489387`, `MLB15344465`,
`MLB51879525`, `MLB16656827`, `MLB39991002`, `MLB40338562`, `MLB29583351`,
`MLB19043825` e `MLB38501955`. Essa lista inclui controles de outras capacidades;
não representa somente aparelhos equivalentes aos procurados.

O resultado `MLB62550982` apresenta uma inconsistência: título com 127V,
mas atributo `VOLTAGE` com 220V. Não deve ser assumido como equivalente 127V.

## Autorização da conta: concluída

O `.env` inicialmente continha apenas as credenciais do aplicativo. Após o
usuário adicionar `ML_AUTH_CODE`, a tentativa de troca retornou
`400 invalid_grant`. Nenhum token de conta foi obtido ou salvo nessa primeira
tentativa.

Depois, foi gerado um novo fluxo com o endereço de retorno cadastrado no
aplicativo e `state` aleatório. O usuário autorizou o aplicativo e salvou a URL
completa de retorno no `.env`. A origem, o caminho e o `state` foram validados
antes da troca do código, que retornou 200. O access token, o refresh token e
a expiração foram salvos no `.env` ignorado pelo Git, sem exibição de seus
valores. O prazo retornado foi de 21.600 segundos (seis horas). O código e os
dados temporários de autorização foram removidos após a gravação dos tokens.

Com o token novo da conta:

| Consulta | Resultado observado |
| --- | --- |
| `/users/me` | 200, confirmando autenticação da conta |
| `/user-products/MLBU4056230273` | 403, `caller is not allowed to access this user product` |
| `/items/MLB4749859345` | 403, `access_denied` |
| `/items/MLB4749859345/sale_price?context=channel_marketplace` | 403 |
| `/items/MLB4749859345/prices` | 403 |
| `/items/bulk?ids=MLB4749859345` | HTTP externo 200, resultado individual 403 |
| `/products/MLB79477691` | 200, produto ativo, `buy_box_winner: null` |
| `/products/MLB79477691/items` | 404, `No winners found` |

A documentação de preços descreve consulta por token de outro vendedor sem
os metadados da promoção. Isso justifica testar a rota com autorização de
conta válida, mas o teste confirmou que ela também foi negada para este
aplicativo e anúncio. O erro não identifica sozinho a política específica
responsável pela negativa.

## Teste de navegador com login manual

Após autorização explícita do usuário para um perfil separado com sessão
autenticada, foi instalado Playwright 1.64.1 no ambiente virtual local para
um teste isolado. O teste usa o Chrome instalado, em janela visível, com
sandbox habilitada, sem camuflagem e sem copiar o perfil habitual do usuário.
O perfil fica em `%LOCALAPPDATA%/bot-monitor/browser-test/profile`, fora do Git.
O login e as verificações ficam sob controle manual do usuário.

Após o primeiro login manual, o Playwright leu a página
`https://www.mercadolivre.com.br/up/MLBU4056230273` e extraiu do DOM:

- título: `Ar-condicionado Hw LG Dual Inverter +ai Voice 12.000 Fr 127v`;
- bloco de preço: `R$3.1497% OFF`;
- disponibilidade: `(+50 disponíveis)`.

Isso demonstra acesso à página e leitura do preço exibido de R$ 3.149,
mas ainda não valida frete, vendedor, condições ou a integração do parser.
O anúncio lido é de 12.000 BTU; não deve ser associado automaticamente ao
catálogo de 9.000 BTU usado nos testes anteriores.

O navegador foi fechado normalmente e reaberto com o mesmo perfil para testar
a persistência. A nova navegação retornou HTTP 200, porém na rota
`/captcha/wall/web-risk-verification/logged`, com título
`Segurança — Mercado Livre`. Foi solicitada a resolução manual ao usuário.
Portanto, a primeira leitura bem-sucedida ainda não comprova monitoramento
automático sem intervenção.

Na retomada seguinte, o processo anterior já não estava disponível e a pasta
`%LOCALAPPDATA%/bot-monitor/browser-test` não foi encontrada. A causa da
ausência da pasta não foi determinada. O auxiliar de teste foi reexecutado,
criando um novo perfil; confirmou-se a criação de `profile/Local State`.
A navegação voltou para `/gz/account-verification` e o usuário confirmou
que a janela solicitou login. Como se trata de um perfil novo, essa solicitação
não demonstra perda ou expiração de uma sessão previamente preservada.
O teste de persistência permanece pendente de login manual nesse novo perfil
e posterior fechamento e reabertura com a pasta mantida.

Em seguida, o usuário relatou a mensagem de limite de tentativas de login,
apesar de ter feito apenas uma tentativa manual nessa abertura. Não foi
possível determinar o critério ou a duração do limite. O teste foi interrompido
sem repetir login, trocar IP ou recriar perfis para contornar a restrição.
Esse resultado impede considerar o navegador autenticado uma solução
confiável de monitoramento desassistido nas condições testadas.

## Conclusão limitada aos testes executados

Não foi encontrada uma rota oficial que entregue o preço do anúncio com as credenciais
válidas disponíveis, incluindo a autorização nova da conta do comprador.
A descrição acessível não basta para monitoramento de preço.
`No winners found` não comprova ausência de ofertas fora do catálogo.

Nenhum código do bot, configuração de permissões do aplicativo, banco de dados
ou alerta do Telegram foi alterado por esta investigação. O `.env` foi
atualizado somente para o fluxo OAuth e suas credenciais. O adaptador continua
usando client_credentials; salvar o token da conta não altera sua integração.
Os arquivos de código que já estavam modificados no início foram preservados.
O teste de navegador é separado do monitor e não gravou histórico nem enviou
alertas. Playwright foi instalado apenas no ambiente virtual para a experiência;
não foi adicionado como dependência obrigatória do aplicativo.

## Referências oficiais

- [Preços de produtos](https://developers.mercadolivre.com.br/devcenter/api-de-precos)
- [Busca de itens e consulta em lote](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas)
- [User Products](https://developers.mercadolivre.com.br/en_us/user-products)
- [Gerencie seu aplicativo](https://developers.mercadolivre.com.br/devcenter/gerencie-seu-aplicativo)
- [Buscador de produtos](https://developers.mercadolivre.com.br/buscador-de-produtos)
