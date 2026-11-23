# Price Monitor Bot

Um bot de Telegram que acompanha preços em lojas como Amazon, Mercado Livre e Magalu. Cole um link no chat ou busque pelo nome do produto para escolher o que acompanhar e receber avisos quando o preço baixar.

## O que dá para fazer

- **Acompanhar um produto.** Cole o link para receber avisos de novas quedas ou use `/add_alvo 29,90 LINK` para definir quanto quer pagar. Dá para acompanhar anúncios de várias lojas para o mesmo produto.
- **Encontrar ofertas.** Use `/buscar sabão líquido OMO 3L` para comparar resultados e escolher qual anúncio acompanhar. Para ar-condicionado, `/vigiar LG | dual inverter | 9000` repete a busca por marca, linha e capacidade e avisa quando encontra um novo menor preço na Amazon ou no catálogo do Mercado Livre.
- **Entender a variação de preço.** Consulte o histórico com gráfico no Telegram ou exporte os dados em CSV.

## Como funciona

O bot roda localmente em **Python**, guarda o histórico no **SQLite** e usa o **Telegram** como interface. Cada loja tem uma integração própria. Depois que você escolhe uma oferta, o monitor consulta diretamente o anúncio salvo, o que mantém as verificações periódicas leves.

A busca verifica as páginas antes de ordenar as ofertas e considera frete conhecido e descontos confirmados. O bot sinaliza quando falta o frete ou uma loja falha, e usa o total confirmado para disparar o alerta de preço-alvo. Testes com respostas simuladas cobrem a busca, o cálculo de preços e os alertas.

## Rode no seu computador

Com Python 3.12 e um bot criado pelo [@BotFather](https://t.me/BotFather), abra o PowerShell na pasta do projeto:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_CHAT_ID` no `.env`. Inicie o bot com o comando abaixo e envie `/start` no Telegram.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-monitor.ps1
```

O monitoramento funciona enquanto o computador estiver ligado e conectado. Os detalhes de instalação e funcionamento estão no [guia de operação](docs/OPERACAO.md) e na [especificação técnica](docs/SPEC.md).
=======
Um bot pessoal do Telegram que acompanha preços na Amazon, no Mercado Livre e na Magalu e avisa quando vale a pena comprar. Ele roda no seu próprio computador: enquanto o PC estiver ligado e com internet, o monitoramento continua.

## Tecnologias

Feito em **Python 3.11+**, com banco **SQLite** e o **Telegram** como interface. Não usa inteligência artificial nem serviços pagos.

## O que ele faz

### Cadastrar produtos

- **Colar um link no chat:** o jeito mais rápido. O bot salva o preço atual e avisa sempre que aparecer um preço menor.
- **Definir um preço-alvo:** `/add_alvo 29,90 LINK` avisa quando o total, já com o frete, chegar a esse valor. Se o frete for desconhecido, o bot não dispara esse alerta.
- **Buscar pelo nome:** `/buscar sabão líquido OMO 3L` procura nas lojas e mostra uma lista única, do mais barato para o mais caro. Depois é só escolher um resultado pelo botão.
- **Comparar lojas:** `/anexar ID LINK` acrescenta outra oferta ao mesmo produto, e o bot passa a acompanhar todas juntas.

### Acompanhar preços

- **Verificação automática:** cada produto é consultado a cada 30 minutos, ou a cada 10 quando o preço está perto do alvo. Com `/intervalo ID MINUTOS` você define outra frequência, e `/check` verifica tudo na hora.
- **Histórico e gráfico:** `/historico ID` mostra os preços atual, mínimo e máximo, com um gráfico.
- **Planilha:** `/exportar` envia o histórico em CSV, pronto para abrir no Excel.

### Receber alertas

- **Alerta principal:** chega na hora em que o preço atinge o alvo ou cai. Cada aviso tem botões para abrir a oferta, ver o gráfico, pausar ou silenciar o produto por 7 dias.
- **Outros avisos:** produto de volta ao estoque e outra loja mais barata. Produtos com preço-alvo também avisam do menor preço em 90 dias e de queda de 15% ou mais em 7 dias.
- **Resumo diário:** às 9h, esses outros avisos chegam juntos em uma só mensagem. `/resumo` envia o resumo na hora.
- **Horário de silêncio:** das 22h às 7h, as mensagens chegam sem som.
- **Loja com problema:** se uma loja parar de responder, o bot avisa uma vez e avisa de novo quando ela voltar.

### Vigiar ofertas

`/vigiar LG | dual inverter | 9000` procura ar-condicionado dessa marca, linha e potência a cada 6 horas, no Mercado Livre (precisa das credenciais da API oficial) e na Amazon. A primeira busca define o preço de referência, e o bot avisa a cada novo menor preço. Quedas de R$ 500 ou mais tocam mesmo no horário de silêncio. `/vigias` lista as buscas salvas e `/parar_vigia ID` encerra uma delas.

### Organizar a lista

- `/lista` mostra os produtos com botões para pausar, mudar o alvo, ver o gráfico e remover. `/lista todos` inclui os pausados.
- `/pausar ID` e `/remover ID` param o monitoramento sem apagar o histórico. `/reativar ID` desfaz.
- `/alvo ID PREÇO` muda o preço-alvo.
- `/status` mostra como está cada oferta, e `/help` lista todos os comandos.

### O que ele não faz

O bot nunca inventa preço ou frete: se uma loja bloquear o acesso ou mudar a página, ele registra a falha e segue com as outras. Também não contorna CAPTCHA nem calcula frete por CEP. O "menor preço" é o menor entre as ofertas que ele conseguiu confirmar, não o menor de toda a internet.

## Como instalar

Você precisa de Python 3.11+ e de um bot criado no Telegram com o [@BotFather](https://t.me/BotFather).

1. Instale o projeto (no PowerShell, dentro da pasta dele):

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   Copy-Item .env.example .env
   ```

2. No arquivo `.env`, preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_CHAT_ID`, que é o número do seu chat. Os demais ajustes são opcionais e estão no mesmo arquivo. Nunca compartilhe o `.env`.

3. Inicie o bot:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-monitor.ps1
   ```

Para o bot iniciar sozinho sempre que você entrar no Windows, rode uma vez:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -StartNow
```

Mais detalhes de operação e diagnóstico estão em [docs/OPERACAO.md](docs/OPERACAO.md).
