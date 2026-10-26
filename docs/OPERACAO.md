# Operação do monitor

## Cadastro e manutenção

1. Execute a aplicação com o ambiente configurado no README e abra a conversa privada do bot. `/start` ou `/help` mostram os comandos disponíveis.
2. Use `/buscar nome do produto` com modelo, armazenamento, tamanho, condição e quantidade quando forem relevantes. RAM e armazenamento são campos separados. Nomes como AirPods Max são aceitos.
3. Toque no botão de uma oferta e envie somente o preço-alvo, como `99,90`. `/cancelar` encerra essa seleção. Sessões expiradas exigem nova busca.
4. O comando equivalente é `/selecionar BUSCA R1 ALVO`. Para adicionar a um produto existente, use `/selecionar BUSCA R1 ALVO ID_PRODUTO`; o alvo já existente continua sendo o do produto. Use `/alvo ID PREÇO` para alterá-lo explicitamente.
5. `/confirmar BUSCA R1 ALVO` confirma uma oferta ambígua conscientemente. Conflitos explícitos continuam bloqueados. A confirmação vale para a identidade da oferta escolhida; não autoriza futuras trocas de variante.

Para URL direta, `/add URL` salva a referência atual e acompanha novos menores preços do produto, conforme o modo de queda disponível no projeto. `/add_alvo PREÇO URL` acompanha o preço efetivo com frete conhecido; o formato antigo `/add PREÇO URL` continua funcionando. `/anexar ID URL` inclui outra oferta equivalente no mesmo produto.

Use `/lista` para os ativos e `/lista todos` para localizar também os inativos. `/remover ID` desativa o acompanhamento, `/reativar ID` retoma e `/alvo ID PREÇO` define o modo de preço-alvo. O histórico permanece preservado.

URLs da mesma oferta são deduplicadas, descartando rastreamento e preservando parâmetros de SKU, variante e vendedor. A migração prioriza cadastros ativos e preserva todos os IDs e históricos. Se a mesma oferta já estiver ativa em produtos diferentes, ambos são mantidos e o conflito aparece nos logs e em `/status`; use `/lista todos` e desative o cadastro que não quiser acompanhar. Faça backup antes de atualizar, conforme o README.

## Preços, identidade e incerteza

Ofertas com frete desconhecido mostram o preço do produto e a ausência de total calculável. Elas não ocupam o menor total garantido nem disparam alertas de preço-alvo. No modo de queda do produto, o aviso informa explicitamente que compara o preço do item, sem frete e sem cupons condicionais.

Condições de clube, compra mínima, preço por período/unidade e entrega indisponível não são promovidas a total garantido. Condições estruturadas do item são confrontadas com o título; um conflito entre novo e recondicionado impede classificação confiável. Pacotes com quantidade não solicitada e variantes não especificadas ficam ambíguos.

O monitor usa os atributos da oferta confirmada, além da busca original. Se uma URL mudar de 3 L para 5 L, a coleta falha sem gravar o preço da outra variante. SKUs de lojas diferentes não são comparados como identidade global do produto. Números são confrontados com seu contexto: iPhone 15 não conflita com a informação adicional de tela de 6,1 polegadas.

## Diagnóstico e recuperação

`/check` informa coletas válidas, falhas, frete desconhecido, alertas enviados e falhas de envio. `/status` mostra a última tentativa, o último sucesso e o erro de cada oferta. Um preço anterior não é tratado como atual quando a consulta mais recente falhou.

Alertas de preço têm estado pendente distinto de enviado. Uma falha no Telegram ou cancelamento mantém o evento para o próximo ciclo, inclusive após reiniciar. A repetição exige uma observação nova que ainda satisfaça a condição. Oferta indisponível, frete desconhecido no modo alvo, desativação ou preço fora do alvo cancelam pendências incompatíveis.

Há uma tentativa de envio por observação elegível, sem repetir em laço. Após uma falha, o próximo ciclo consulta novamente o preço antes de tentar entregar; não é necessário rearmar alertas manualmente. `/check` antecipa uma verificação. Uma falha após o Telegram aceitar a mensagem, mas antes de confirmar no banco, ainda pode resultar em repetição, pois Telegram e SQLite não compartilham uma transação.

`FETCH_TOTAL_TIMEOUT_SECONDS=30` limita toda a consulta de uma oferta, inclusive redirects e envio lento do corpo, tanto no cadastro quanto no monitor. `HTTP_TIMEOUT_SECONDS` continua limitando operações HTTP individuais. A busca aplica também seus limites por fonte.

## Descoberta web com SearXNG

Além do contrato JSON customizado existente, há integração com a [API oficial do SearXNG](https://docs.searxng.org/dev/search_api.html). Ela envia `q`, `format=json`, `language=pt-BR` e `categories=general` para o endpoint `/search`. Os resultados apenas descobrem URLs; cada oferta continua exigindo validação independente.

Use uma instância cujo operador autorize sua utilização e tenha habilitado saída JSON. Muitas instâncias públicas desabilitam JSON e retornam 403; o bot reporta essa falha e não tenta contorná-la. Exemplo de configuração, substituindo o domínio pelo endpoint autorizado:

```dotenv
GENERIC_DISCOVERY_FORMAT=searxng
GENERIC_DISCOVERY_ENDPOINT=https://seu-dominio.example/search
GENERIC_DISCOVERY_API_KEY=
```

No `settings.yml` de uma instância sob sua administração, a saída JSON precisa estar habilitada:

```yaml
search:
  formats:
    - html
    - json
```

O endpoint precisa ser público e resolver somente para IPs públicos; localhost, rede privada e metadados continuam bloqueados. Esta entrega não instala nem hospeda uma instância. SearXNG é opcional: com endpoint vazio, a aplicação usa somente as fontes nativas, sem dependência de serviço pago. `GENERIC_DISCOVERY_FORMAT=json` mantém compatibilidade com endpoints customizados de formato `{"results":[{"url":"https://loja.example/produto"}]}`.

## Limitações verificadas e piloto

Na validação pública de 26/10/2026, a Amazon retornou dez candidatos e uma página de produto forneceu título, disponibilidade e preço; o frete ficou desconhecido. Mercado Livre entregou uma página pedindo login, e Magalu retornou 403. Esses estados são falhas de cobertura, e não resultados vazios válidos. Não foram usados login, checkout, CAPTCHA bypass ou Telegram real nessa validação. Não se afirma compatibilidade completa e permanente das três lojas.
