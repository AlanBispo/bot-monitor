# Price Monitor Bot

Bot pessoal e local para descobrir e acompanhar preços, com SQLite, Telegram e verificações periódicas. A busca é determinística, consulta adapters de Amazon, Mercado Livre e Magalu, pode receber URLs de um provedor web configurável e valida cada página antes de ranquear.

Veja [operação, recuperação de alertas e descoberta SearXNG](docs/OPERACAO.md) para o fluxo atualizado de cadastro, diagnóstico e as limitações verificadas nas lojas.

## Requisitos

- Windows 10/11 (Python nativo ou WSL2) ou Linux;
- Python 3.11+;
- acesso à internet;
- um bot criado no Telegram via `@BotFather`.

## Instalação no WSL

No terminal do WSL:

```bash
cd /mnt/c/dev/price-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

No PowerShell, o equivalente para um checkout nativo é:

```powershell
cd C:\Desenvolvimento\bot-monitor
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

## Configuração do Telegram

1. Abra `@BotFather`, use `/newbot` e copie o token para `TELEGRAM_BOT_TOKEN` no `.env`.
2. Para descobrir seu `chat_id`, envie uma mensagem ao bot e consulte a API do Telegram (`https://api.telegram.org/bot<TOKEN>/getUpdates`) ou use um bot auxiliar confiável.
3. Preencha `TELEGRAM_ALLOWED_CHAT_ID` com o número inteiro do seu chat privado.
4. Ajuste `CHECK_INTERVAL_MINUTES` se necessário. O mínimo aplicado pelo programa é 5 minutos.

Nunca compartilhe o `.env` nem cole o token em código ou logs.

## Banco e execução

O banco é criado automaticamente em `data/prices.db`, com histórico append-only e WAL:

```bash
python main.py
```

O mesmo processo mantém o polling do Telegram e o scheduler. Ao iniciar, verifica imediatamente os anúncios confirmados e ativos salvos no banco e repete a cada `CHECK_INTERVAL_MINUTES` (30 minutos por padrão). Não é necessário cadastrar os produtos novamente a cada reinicialização. Na primeira instalação, o banco estará vazio até cadastrar produtos pelo Telegram.

### Windows: execução manual e início automático

Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_CHAT_ID` no `.env` local. Depois, execute no PowerShell:

```powershell
cd C:\Desenvolvimento\bot-monitor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-monitor.ps1
```

Use Ctrl+C para encerrar a execução manual. Para registrar a tarefa `PriceMonitorBot` e iniciá-la em segundo plano:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -StartNow
```

A tarefa inicia 30 segundos após entrar na sua conta do Windows, sem janela, usa o diretório correto do projeto e não exige senha nem execução como administrador. Ela permanece ativa sem limite de duração, pode executar na bateria e tenta reiniciar após falhas (a cada minuto, até 999 tentativas). A tarefa ignora partidas duplicadas; o script também impede duas execuções na mesma sessão. Use sempre o script para iniciar manualmente. Se o token ou ID ainda estiverem vazios, o script termina sem iniciar o bot; após preencher o `.env`, inicie a tarefa novamente.

Comandos de operação:

```powershell
Get-ScheduledTask -TaskName PriceMonitorBot
Start-ScheduledTask -TaskName PriceMonitorBot
Stop-ScheduledTask -TaskName PriceMonitorBot
Get-Content .\logs\monitor-*.log -Tail 40
# Para remover o inicio automatico, pare a tarefa e execute:
Unregister-ScheduledTask -TaskName PriceMonitorBot -Confirm:$false
```

Os logs da execução em segundo plano ficam em `logs/monitor-AAAA-MM-DD.log` (data de início do processo). O computador precisa estar ligado, acordado e conectado; ao sair da conta ou desligar, o monitor para. O início antes do login não é configurado por estes scripts. Para WSL, use uma tarefa separada apontando para sua distribuição e ambiente Linux.

`ENABLED_STORES` aceita uma lista separada por vírgulas entre `amazon`, `mercadolivre` e `magalu`. Os limites HTTP e de busca podem ser ajustados globalmente com `HTTP_TIMEOUT_SECONDS` e `SOURCE_TIMEOUT_SECONDS`, ou por loja com as variáveis `*_HTTP_TIMEOUT_SECONDS` e `*_SOURCE_TIMEOUT_SECONDS`. A descoberta web tem `WEB_DISCOVERY_TIMEOUT_SECONDS` e `WEB_DISCOVERY_SOURCE_TIMEOUT_SECONDS`. Os limites são aplicados por requisição/fonte; concorrência, bytes de resposta e número de candidatos também são limitados.

As opções `AMAZON_BROWSER_AUTOMATION_ENABLED`, `MERCADOLIVRE_BROWSER_AUTOMATION_ENABLED` e `MAGALU_BROWSER_AUTOMATION_ENABLED` devem permanecer `false`: esta versão usa HTTP público e rejeita explicitamente `true`, pois não inclui automação de navegador. O monitor executa no máximo quatro verificações de listing em paralelo, sem sobrepor ciclos periódicos e manuais.

Comandos:

```text
/buscar sabão líquido OMO 3L
/selecionar ID_DA_BUSCA R1 29,90
/add https://www.amazon.com.br/...
/add_alvo 29,90 https://www.amazon.com.br/...
/anexar ID_DO_PRODUTO https://www.amazon.com.br/...
/alvo ID_DO_PRODUTO 25,90
/reativar ID_DO_PRODUTO
/lista
/lista todos
/check
/historico ID_DO_PRODUTO
/remover ID_DO_PRODUTO
/status
/help
```

`/buscar` é uma busca ampla de cadastro. A resposta usa identificadores curtos (`R1`, `R2`...) e informa cobertura, falhas parciais, variantes ambíguas e frete desconhecido. Use `/selecionar ID_DA_BUSCA R1 PREÇO_ALVO` ou o botão da resposta para confirmar uma oferta. O bot valida a página novamente antes de registrar. `/anexar ID_DO_PRODUTO URL` associa outra oferta ao mesmo produto.

Para cadastrar diretamente pelo link, escolha um dos dois modos:

- `/add URL`: consulta e salva o preço atual como primeira observação, sem pedir preço-alvo. Avisa quando o preço do produto ficar abaixo do preço inicial e dos valores já avisados. Por exemplo: cadastrado a R$ 100, avisa a R$ 90; repetir R$ 90 ou subir e voltar a R$ 95 não gera outro aviso; cair a R$ 80 gera um novo aviso. A referência e os avisos sobrevivem à reinicialização. A comparação considera o preço do produto, sem frete e sem cupons condicionais; o aviso informa o frete separadamente, inclusive quando desconhecido. `/historico ID` mostra o preço inicial, atual, mínimo e máximo por anúncio nesse modo.
- `/add_alvo PREÇO_ALVO URL`: salva o preço atual e o alvo escolhido. Avisa quando o preço efetivo confiável (com frete conhecido e descontos confirmados) ficar menor ou igual ao alvo. Frete desconhecido impede esse alerta. O formato antigo `/add PREÇO_ALVO URL` continua funcionando como cadastro com alvo, e `/alvo ID PREÇO` permite definir um alvo posteriormente.

Os cadastros anteriores mantêm seu modo de preço-alvo. A migração preserva os produtos, anúncios e histórico existentes. Falhas de consulta e produtos indisponíveis não geram alertas de queda.

Preços exibidos por mecanismos de busca são apenas descoberta e nunca entram no histórico, nos alertas ou no ranking. A menor oferta significa a menor oferta confiável validada naquela busca, não uma garantia sobre toda a internet. Frete desconhecido fica fora do menor preço garantido.

A busca preserva todos os termos informados, inclusive modelo, capacidade e frases entre aspas. O matching elimina conflitos explícitos de modelo, capacidade, volume, tamanho, quantidade, condição e cores reconhecidas. Depois dessas restrições, a pontuação lexical conta palavras relevantes presentes no título, ignorando filtros, unidades e palavras de comando; exige uma palavra em consultas de um termo relevante e duas em consultas maiores. Correspondências sem atributo obrigatório ou com pouca evidência ficam ambíguas e não entram no menor preço garantido. `/confirmar` registra uma escolha ambígua feita conscientemente pelo usuário; conflitos explícitos continuam bloqueados. Ao selecionar, o bot consulta e compara novamente a página. As sessões expiram após `SEARCH_SESSION_MAX_AGE_HOURS` mesmo sem uma nova busca.

## Descoberta web genérica

O caminho sem serviço pago é deixar `GENERIC_DISCOVERY_ENDPOINT` vazio e usar as buscas nativas das três lojas. Para acrescentar descoberta web, configure um endpoint HTTP(S) autorizado que retorne JSON no formato `{"results":[{"url":"https://loja.example/produto","title":"..."}]}`. O endpoint deve obedecer seus próprios termos e limites; o bot não raspa mecanismos de busca nem contorna CAPTCHA. O endpoint precisa ter DNS público; endereços locais e redes privadas são bloqueados mesmo se o serviço for autohospedado. URLs descobertas passam pelas mesmas validações de esquema, redirecionamento, DNS público e limite de resposta. Páginas de lojas adicionais só entram no ranking quando contêm Product/Offer JSON-LD consistente, moeda ISO `BRL`, preço completo e disponibilidade afirmativa; caso contrário ficam excluídas.

Os corpos HTTP são lidos em fluxo e interrompidos ao ultrapassar `MAX_RESPONSE_BYTES`, antes de acumular a resposta inteira na memória. O endpoint configurado também é limitado e não segue redirecionamentos; URLs de produto podem seguir até três redirecionamentos, revalidando cada destino.

## Qualidade

```bash
pytest
ruff check .
bandit -r app
pip-audit
```

## Limitações conhecidas

- Uma loja pode bloquear ou alterar HTML; o bot registra a falha e continua com as demais fontes, sem inventar preço, frete ou alerta.
- Frete desconhecido impede o cálculo do preço efetivo e, portanto, o alerta de preço-alvo.
- O MVP não tenta contornar CAPTCHA ou proteções anti-bot.
- Descontos condicionais são mantidos como preço possível e não como preço garantido.
- O cálculo de frete por CEP não está implementado; sem valor público inequívoco, o frete permanece desconhecido.
- Automação de navegador não está habilitada; páginas que exigirem login, CAPTCHA ou interação dinâmica são tratadas como indisponíveis.
- O HTML das lojas é externo e pode mudar; fixtures locais cobrem os formatos suportados, mas a configuração real deve ser validada sem depender dos testes na internet.

## Backup e troubleshooting

Pare o processo antes de copiar `data/prices.db` para um backup. O histórico é append-only; não edite as tabelas manualmente. Em caso de erro de uma fonte, consulte os logs e execute `/check` depois de confirmar que a URL pública está acessível. Se o banco estiver em caminho inválido, corrija `DATABASE_PATH`; se o Telegram não responder, confirme token, chat autorizado e conectividade. Os limites de concorrência, resposta, candidatos e sessão ficam no `.env.example`.
